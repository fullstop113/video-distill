"""管线编排：BV → 逐字稿 + 总结，一次跑完。

## 为什么要单独一层

在没有这一层之前，`scripts/distill.py` 和 `scripts/summarize.py` 各自写了一遍
"取数 → 总结 → 落盘"的编排。P3 的 Web 工作线程需要**第三遍**。
三份会漂移的编排逻辑是维护灾难，所以抽成这里，CLI 与 Worker 都调它。

## 配额在这里判定

方案 §5.3⑤ 的要求：配额必须在**下载音频之前**判定，避免先烧带宽和 GPU 再拒绝。
所以 `planned_seconds` 用视频真实时长，在取逐字稿之前就检查。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import cache
from .bilibili import BiliClient, VideoInfo
from .quota import check, commit
from .summarize import DeepSeekClient, SummarizeError, Summary, summarize
from .summarize import to_markdown as summary_to_markdown
from .transcript import NoTranscriptError, Transcript, fetch_transcript
from .transcript import to_markdown as transcript_to_markdown

log = logging.getLogger(__name__)

# 阶段名与 tasks.STAGES 对齐
STAGE_META = "meta"
STAGE_TRANSCRIPT = "transcript"
STAGE_SUMMARIZE = "summarize"
STAGE_RENDER = "render"


class QuotaExceeded(RuntimeError):
    """配额不足。单独一类，便于 Web 层返回 429 而不是 500。"""


def _info_to_dict(info: VideoInfo) -> dict:
    """缓存序列化。**必须包含权限标记** —— 否则读缓存时受限视频会被当成正常视频，
    于是又回到"下载 250 秒预览当完整逐字稿"那条老路。"""
    return {
        "bvid": info.bvid, "cid": info.cid, "title": info.title,
        "duration": info.duration, "owner_name": info.owner_name,
        "owner_mid": info.owner_mid, "desc": info.desc,
        "is_upower_exclusive": info.is_upower_exclusive,
        "is_ugc_pay_preview": info.is_ugc_pay_preview,
        "is_upower_play": info.is_upower_play,
        "preview_toast": info.preview_toast,
        "need_login": info.need_login,
    }


@dataclass
class PipelineResult:
    bvid: str
    transcript: Transcript
    summary: Summary
    transcript_path: Path
    summary_path: Path
    cost_cny: float
    notes: list[str] = field(default_factory=list)


def run_pipeline(
    client: BiliClient,
    bvid: str,
    *,
    uid: str,
    out_dir: Path,
    llm: DeepSeekClient | None = None,
    use_cache: bool = True,
    strategy: str = "auto",
    chunk_chars: int = 9000,
    single_pass_chars: int = 60000,
    enforce_quota: bool = True,
    on_stage=None,
) -> PipelineResult:
    """完整跑一条视频。`on_stage(stage, message)` 用于上报进度。"""

    def stage(name: str, msg: str = "") -> None:
        log.info("[%s] %s", name, msg)
        if on_stage:
            on_stage(name, msg)

    notes: list[str] = []

    # ---------- 1. 元信息 + 配额（配额务必早于任何下载） ----------
    stage(STAGE_META, "获取视频信息")
    info: VideoInfo | None = None
    cached = cache.get("video_info", bvid) if use_cache else None
    if cached:
        # 只取 VideoInfo 认识的字段：缓存结构可能比当前代码旧（缺少权限标记），
        # 直接 **cached 会在字段增删后炸掉
        known = {f for f in VideoInfo.__dataclass_fields__}
        info = VideoInfo(**{k: v for k, v in cached.items() if k in known})
        # 旧缓存可能没有权限标记，补一次（受限视频必须被识别出来）
        if not info.is_upower_exclusive and not info.is_ugc_pay_preview:
            client._fill_restriction(info)
            cache.put("video_info", bvid, _info_to_dict(info))
    else:
        info = client.video_info(bvid)
        if use_cache:
            cache.put("video_info", bvid, _info_to_dict(info))

    if enforce_quota:
        st = check(uid, planned_seconds=info.duration)
        stage(STAGE_META, f"{info.title}（{info.duration // 60} 分钟）· {st.describe()}")
        if not st.allowed:
            raise QuotaExceeded(st.reason)
    else:
        stage(STAGE_META, f"{info.title}（{info.duration // 60} 分钟）")

    # ---------- 2. 逐字稿 ----------
    stage(STAGE_TRANSCRIPT, "取逐字稿（仅站内字幕，含 AI 字幕）")
    transcript = fetch_transcript(
        client, bvid,
        use_cache=use_cache,
        progress=lambda m: stage(STAGE_TRANSCRIPT, m),
    )
    notes.extend(transcript.notes)
    stage(
        STAGE_TRANSCRIPT,
        f"{transcript.source.label} · {transcript.char_count} 字 / {len(transcript.segments)} 段",
    )

    # ---------- 3. 总结 ----------
    stage(STAGE_SUMMARIZE, "生成总结与要点")
    summary = summarize(
        transcript, client=llm, strategy=strategy,
        chunk_chars=chunk_chars, single_pass_chars=single_pass_chars,
        progress=lambda m: stage(STAGE_SUMMARIZE, m),
    )
    notes.extend(summary.notes)
    anchored = sum(1 for p in summary.key_points if p.anchors)
    stage(
        STAGE_SUMMARIZE,
        f"{len(summary.key_points)} 条要点（{anchored} 条带锚点）· ¥{summary.cost_cny:.4f}",
    )

    # ---------- 4. 落盘 ----------
    stage(STAGE_RENDER, "写入产物")
    out_dir.mkdir(parents=True, exist_ok=True)
    tpath = out_dir / f"{bvid}.md"
    spath = out_dir / f"{bvid}.summary.md"
    tpath.write_text(transcript_to_markdown(transcript), encoding="utf-8")
    spath.write_text(summary_to_markdown(summary), encoding="utf-8")

    # 记账放在最后：只有在真的产出产物之后才计入配额，
    # 否则失败的任务也会白占用户的每日额度。
    if enforce_quota:
        commit(uid, bvid, info.duration)

    return PipelineResult(
        bvid=bvid,
        transcript=transcript,
        summary=summary,
        transcript_path=tpath,
        summary_path=spath,
        cost_cny=summary.cost_cny,
        notes=notes,
    )
