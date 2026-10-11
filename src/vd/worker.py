"""工作线程：串行消费任务队列。

## 为什么只有一个线程

方案 §5.2①：*"FunASR 占满显存，多人同时提交不能并发转写。"*
所以这里**刻意**只跑一个工作线程 —— 并发度恒为 1 不是简化，而是对 GPU 的正确建模。

## 凭证缺失时的行为

工作线程可能在**操作号还没登录**时就被启动（比如服务先起来、稍后再扫码）。
这时 `load_operator_credentials()` 会抛 `CredentialPolicyError`。
处理方式是：**把任务标记为失败并给出可读的原因**，而不是让它无限排队——
用户看到"请先扫码登录"比看到"排队中"停在那里有用得多。

## 崩溃恢复

进程被强杀时任务会停在 RUNNING。启动时调 `tasks.requeue_stale()` 把它们放回队列。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path

from . import tasks
from .bilibili import (
    BiliClient,
    NotLoggedInError,
    RestrictedVideoError,
)
from .credentials import CredentialPolicyError, load_operator_credentials
from .pipeline import QuotaExceeded, run_pipeline
from .summarize import DeepSeekClient, SummarizeError
from .transcript import NoTranscriptError

log = logging.getLogger(__name__)


class Worker:
    """单线程任务消费者。"""

    def __init__(
        self,
        out_dir: Path,
        *,
        poll_interval: float = 2.0,
    ):
        self.out_dir = Path(out_dir)
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: str = ""

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        revived = tasks.requeue_stale()
        if revived:
            log.warning("恢复了 %d 个上次中断的任务", revived)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vd-worker", daemon=True)
        self._thread.start()
        log.info("工作线程已启动（串行消费，out_dir=%s）", self.out_dir)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def current_task(self) -> str:
        return self._current

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            task = None
            try:
                task = tasks.claim_next()
            except Exception:  # noqa: BLE001
                log.exception("取任务失败")
            if not task:
                self._stop.wait(self.poll_interval)
                continue
            self._current = task.id
            try:
                self._run(task)
            finally:
                self._current = ""

    # ---------- 单任务 ----------

    def _run(self, task: tasks.Task) -> None:
        log.info("开始处理任务 %s（%s）", task.id, task.bvid)

        def on_stage(stage: str, message: str) -> None:
            tasks.update(task.id, stage=stage, message=message)

        try:
            creds = load_operator_credentials()
        except CredentialPolicyError:
            # 明确失败，不要让任务永远排队
            tasks.fail(task.id, "服务凭据不可用，请联系管理员。")
            return

        client = BiliClient(creds)
        try:
            result = run_pipeline(
                client, task.bvid,
                uid=creds.uid,
                out_dir=self.out_dir,
                llm=None,  # 由 run_pipeline 内部按环境变量构造
                on_stage=on_stage,
            )
        except QuotaExceeded as exc:
            tasks.fail(task.id, f"配额不足：{exc}")
        except RestrictedVideoError as exc:
            # 权限受限（充电专属/付费预览）：这是**用户能理解并可能自行解决**的情况，
            # 不该和"接口报错"混在一起。给出可操作的说明。
            tasks.fail(
                task.id,
                f"该视频受权限限制，无法取到完整内容。\n\n{exc}\n\n"
                "可能原因：\n"
                "  · 充电专属视频（需要给 UP 主充电后才能看完整版）\n"
                "  · 付费/大会员专享内容\n"
                "  · 仅提供试看预览\n\n"
                "当前账号无权访问完整媒体流，因此无法生成完整逐字稿。\n"
                "换一个有权限的账号，或换一个视频再试。",
            )
        except NotLoggedInError as exc:
            # R1：这不是"没有字幕"，必须显式失败
            tasks.fail(task.id, f"未登录或无权访问：{exc}")
        except NoTranscriptError as exc:
            tasks.fail(task.id, str(exc))
        except SummarizeError as exc:
            tasks.fail(task.id, f"总结失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 未预期失败", task.id)
            tasks.fail(task.id, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}")
        else:
            payload = {
                "title": result.transcript.title,
                "owner_name": result.transcript.info.owner_name,
                "duration": result.transcript.info.duration,
                "transcript_source": result.transcript.source.label,
                "uses_gpu": result.transcript.source.uses_gpu,
                "chars": result.transcript.char_count,
                "segments": len(result.transcript.segments),
                "points": len(result.summary.key_points),
                "anchored_points": sum(1 for p in result.summary.key_points if p.anchors),
                "strategy": result.summary.strategy.value,
                "cost_cny": round(result.cost_cny, 4),
                "transcript_file": result.transcript_path.name,
                "summary_file": result.summary_path.name,
                "tl_dr": result.summary.tl_dr,
                "overall": result.summary.overall,
                "highlights": [
                    {
                        "at": h.label,
                        "sec": int(h.at),
                        "text": h.text,
                        # 前端靠它把气泡链接到下方对应的要点段落
                        "point_index": h.point_index,
                        # 气泡里可以顺带显示对应要点的陈述（悬停即读到内容）
                        "detail": (
                            result.summary.key_points[h.point_index].text
                            if 0 <= h.point_index < len(result.summary.key_points)
                            else ""
                        ),
                    }
                    for h in result.summary.highlights
                ],
                "key_points": [
                    {
                        "topic": p.display_topic,
                        "text": p.text,
                        "insight": p.insight,
                        "category": p.category,
                        "anchors": p.anchor_labels(),
                    }
                    for p in result.summary.key_points
                ],
            }
            tasks.finish(task.id, result=payload)
            # 冗余记录标题与产物清单：
            # 列表页要显示标题，"我的产物"页要列出大小与类型。
            # 都从 result 里再解析一遍是浪费，而且产物清单还需要"task → 文件"的关联，
            # 光扫 output 目录给不了这个关系。
            tasks.set_title(task.id, result.transcript.title)
            tasks.set_up_name(task.id, result.transcript.info.owner_name)
            tasks.set_artifacts(task.id, [
                {"name": result.summary_path.name, "kind": "summary",
                 "size": result.summary_path.stat().st_size},
                {"name": result.transcript_path.name, "kind": "transcript",
                 "size": result.transcript_path.stat().st_size},
            ])
            tasks.set_lineage(
                task.id,
                # 分片总结时调用次数 = 分片数 + 1 次合并；单次全文就是 1
                n_chunks=max(result.summary.usage.calls - 1, 0),
            )
            log.info("任务 %s 完成（¥%.4f）", task.id, result.cost_cny)

        # 给使用者一点间隔，避免连续任务把风控惹毛
        self._stop.wait(0.5)


def make_llm() -> DeepSeekClient:
    """构造 LLM 客户端（供 Web 层在启动时快速校验配置）。"""
    return DeepSeekClient()
