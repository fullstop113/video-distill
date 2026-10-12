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

import contextlib
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
from .pipeline import AsrNotConfigured, QuotaExceeded, run_pipeline
from .summarize import DeepSeekClient, SummarizeError
from .transcript import NoTranscriptError

log = logging.getLogger(__name__)

# 心跳间隔（秒）。比 `requeue_stale` 的阈值小得多，才能可靠地区分
# "进程死了"与"任务还在跑"。
HEARTBEAT_INTERVAL = 15.0


@contextlib.contextmanager
def _heartbeat(task_id: str, interval: float = HEARTBEAT_INTERVAL):
    """任务运行期间定期刷新心跳；退出时停掉。

    ## 为什么需要它（实测的僵尸任务）

    服务进程被强杀时，任务会永远停在 `RUNNING` —— 前端看起来像卡住了。
    为此有 `tasks.requeue_stale()` 在启动时回收，但实测**它回收不了**：

      · 判定条件是 `heartbeat='' OR heartbeat < now-3600s`
      · 而 `heartbeat` 原先**只在任务开始和结束时各写一次**（假心跳）
      · 所以刚被杀的僵尸任务心跳是"几分钟前"，
        **既不满足"空"，也不满足"早于 1 小时前"** → 一条都收不回来

    实测原文：

        f30a8c9c0978  running  heartbeat='2026-10-12T19:46:39'
        cutoff = 18:48:42      会被回收的条数 = 0

    有了真心跳后，把 `requeue_stale` 的阈值调到明显大于心跳间隔
    （见 `worker.start()`），判断就可靠了：
      · 进程活着 → 心跳一直在刷新 → 不会被误回收
      · 进程死了 → 心跳停在死亡时刻 → 超过阈值即回收

    心跳线程是 daemon，且异常只记日志 —— 心跳失败不该影响任务本身。
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                # touch=True 即"只刷新心跳"（tasks.update 已有该语义）
                tasks.update(task_id, touch=True)
            except Exception:  # noqa: BLE001
                log.debug("心跳更新失败（不影响任务）", exc_info=True)

    t = threading.Thread(target=beat, name=f"vd-heartbeat-{task_id[:6]}",
                         daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=2.0)


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
        # 回收僵尸任务。阈值必须**明显大于心跳间隔**：
        #   · 进程活着 → 心跳每 15 秒刷新 → 不会被误回收
        #   · 进程死了 → 心跳停在死亡时刻 → 90 秒后即被回收
        #
        # ⚠️ 这个值原先是默认的 3600 秒，配合"假心跳"（只在任务开始/结束
        # 各写一次）导致**僵尸任务一条都收不回来**，实测卡了任务。
        revived = tasks.requeue_stale(timeout_seconds=90)
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
                with _heartbeat(task.id):
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
        # 云 ASR 的应扣额度。由 pipeline 在**转写成功**时回填 ——
        # 一旦这里非零，就说明钱已经花给阿里云了。
        charges = {"uid": creds.uid, "amount": 0.0, "bvid": task.bvid,
                   "seconds": 0}
        try:
            result = run_pipeline(
                client, task.bvid,
                uid=creds.uid,
                out_dir=self.out_dir,
                llm=None,  # 由 run_pipeline 内部按环境变量构造
                # 允许"无字幕 → 云 ASR"这条路。它**要花钱**，
                # 所以 pipeline 会先查余额再下载音频（见其模块说明）。
                allow_asr=True,
                on_stage=on_stage,
                charges=charges,
            )
        except QuotaExceeded as exc:
            tasks.fail(task.id, f"额度不足：{exc}")
        except AsrNotConfigured as exc:
            # 云 ASR 没配好。这**不是用户能解决的问题**（是部署配置），
            # 所以要说清"找管理员"以及该配什么。
            tasks.fail(
                task.id,
                f"该视频没有站内字幕，需要云语音识别，但服务未配置完整。\n\n{exc}",
            )
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
                "strategy": result.summary.strategy.value,
                "cost_cny": round(result.cost_cny, 4),
                "asr_cost_cny": round(result.asr_cost_cny, 4),
                "transcript_file": result.transcript_path.name,
                "summary_file": result.summary_path.name,
                "tl_dr": result.summary.tl_dr,
                "overall": result.summary.overall,
                # up 主主要观点（核心论点）
                "main_viewpoints": list(result.summary.main_viewpoints),
                # 关键节点只保留文字（已去掉时间戳与跳转）
                "highlights": [
                    {"text": h.text}
                    for h in result.summary.highlights
                ],
                "key_points": [
                    {
                        "topic": p.display_topic,
                        "text": p.text,
                        "insight": p.insight,
                        "category": p.category,
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
        finally:
            # ⚠️ 扣费必须在 finally 里，**独立于任务成败**。
            #
            # 理由：云 ASR 的钱在"转写成功"那一刻就付给阿里云了。
            # 如果之后总结失败／落盘失败，那笔钱**照样花掉了**。
            # 挂在 `else`（成功分支）上会导致"花了钱却不扣账"，
            # 账面成本低于真实成本 —— 而这个账本是用来控制成本的，
            # 少记就等于失控。
            #
            # pipeline 只在**转写真的成功**时才把金额写进 charges，
            # 所以"转写失败"不会被误扣。
            self._commit_charge(charges)

        # 给使用者一点间隔，避免连续任务把风控惹毛
        self._stop.wait(0.5)

    def _commit_charge(self, charges: dict) -> None:
        """把云 ASR 的应扣额度真正写入余额。

        失败只记日志、**不让任务变成失败** —— 产物已经生成了，用户要的是产物。
        但必须留下明显日志：这是账目问题，静默失败等于账本失真。
        """
        amount = float(charges.get("amount") or 0.0)
        if amount <= 0:
            return
        from . import billing

        uid = charges.get("uid") or ""
        bvid = charges.get("bvid") or ""
        seconds = int(charges.get("seconds") or 0)
        try:
            left = billing.charge(uid, amount, bvid=bvid,
                                  note="云语音识别（无字幕视频）",
                                  seconds=seconds)
            log.info("已扣云 ASR 费用 ¥%.4f（%d 秒，uid=%s，余额剩 ¥%.2f）",
                     amount, seconds, uid, left)
        except Exception:  # noqa: BLE001
            log.exception("扣云 ASR 费用失败（uid=%s 金额=%.4f）—— 需人工核对",
                          uid, amount)


def make_llm() -> DeepSeekClient:
    """构造 LLM 客户端（供 Web 层在启动时快速校验配置）。"""
    return DeepSeekClient()
