"""P1 取数层：BV → 逐字稿（仅站内字幕，含 AI 字幕）。

## 这是 P1 的主干

把 P0 探针里验证过的逻辑收拢成一个可复用的模块，三件事在此统一：

1. **站内字幕优先**：人工字幕 → AI 字幕，零 GPU 成本。
2. **共用热词修正**：短板相同，修正表只有一份（vd.hotwords）。
3. **绝不产出空文件**：任何环节拿到空内容都抛异常，不返回"成功但空"的结果。

方案 §5.2③ 的硬要求在这里落地：**宁可报错，不要给用户一个空文件。**

## 为什么没有 ASR 兜底

历史版本里"无字幕 → 本地 FunASR"的兜底已被移除：B 站对绝大多数视频都提供了
AI 字幕，能覆盖绝大部分场景；剩余没有任何字幕的视频直接报错更安全——
而不是烧几小时 GPU 产出一份质量不可控的语音识别结果。

## 与探针的关系

`scripts/transcript_probe.py` 保留为**最小可运行示例**（便于排查"到底是哪一层坏了"），
但它不再是唯一实现；正式流程一律走本模块，避免两处逻辑漂移。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import cache, hotwords
from .bilibili import (
    BiliClient,
    NoSubtitleError,
    NotLoggedInError,
    SubtitleTrack,
    VideoInfo,
)

log = logging.getLogger(__name__)

# 逐字稿长度下限（方案 §5.2③）。低于此值视为失败，而不是"短一点的成功"。
MIN_TRANSCRIPT_CHARS = 80


class NoTranscriptError(RuntimeError):
    """取不到任何可用逐字稿。

    本项目**不再做 ASR 兜底**，所以这通常意味着：
      该视频既没有人工字幕、也没有 B 站 AI 字幕。
    """


class TranscriptSource(str, Enum):
    SUBTITLE = "subtitle"          # B 站站内字幕（含 AI 字幕）
    SUBTITLE_CACHE = "subtitle_cache"

    @property
    def uses_gpu(self) -> bool:
        return False

    @property
    def label(self) -> str:
        return {
            TranscriptSource.SUBTITLE: "B 站站内字幕",
            TranscriptSource.SUBTITLE_CACHE: "B 站站内字幕（缓存）",
        }[self]


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    bvid: str
    title: str
    source: TranscriptSource
    segments: list[Segment]
    info: VideoInfo
    subtitle_lan: str = ""
    subtitle_lan_doc: str = ""
    corrections: list[hotwords.CorrectionHit] = field(default_factory=list)
    timing: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def duration_covered(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def plain_text(self, joiner: str = "") -> str:
        return joiner.join(s.text for s in self.segments)


def _pick_track(tracks: list[SubtitleTrack]) -> SubtitleTrack:
    """优先人工字幕，其次中文，最后任意一条。"""
    manual = [t for t in tracks if not t.lan.startswith("ai-")]
    if manual:
        zh = [t for t in manual if t.lan.startswith("zh")]
        return zh[0] if zh else manual[0]
    zh = [t for t in tracks if t.lan.startswith("ai-zh") or t.lan.startswith("zh")]
    return zh[0] if zh else tracks[0]


def _cues_to_segments(cues: list[dict]) -> list[Segment]:
    segs: list[Segment] = []
    for c in cues:
        text = (c.get("content") or "").strip()
        if not text:
            continue
        segs.append(
            Segment(
                start=float(c.get("from", 0) or 0),
                end=float(c.get("to", 0) or 0),
                text=text,
            )
        )
    return segs


def _dedupe_adjacent(segments: list[Segment], window: float = 10.0) -> list[Segment]:
    """去掉短时间窗口内的重复行。

    B 站 AI 字幕与 ASR 都会重复吐同一句。实测遇到的两种形态：

    1. **紧邻重复**（完全相同、时间相接）—— 滚动字幕常见；
    2. **近邻重复**（相同文本，中间隔了 1-2 行/几秒）—— 例如同一句在
       `[00:07:30]` 出现两次，中间夹着别的行。

    第一版只处理了形态 1，于是形态 2 会原样进到产物里，读起来像结巴。
    这里改成在 `window` 秒内查重（默认 10 秒），并把时间跨度合并到较早那条。
    """
    out: list[Segment] = []
    for seg in segments:
        hit = None
        # 只回看窗口内的若干条，避免 O(n^2) 在大逐字稿上变慢
        for prev in reversed(out[-12:]):
            if seg.start - prev.end > window:
                break
            if prev.text == seg.text:
                hit = prev
                break
        if hit is not None:
            merged = Segment(hit.start, max(hit.end, seg.end), hit.text)
            out[out.index(hit)] = merged
            continue
        out.append(seg)
    return out


def _validate(text: str, where: str, min_chars: int = MIN_TRANSCRIPT_CHARS) -> None:
    """空产物校验。方案 §5.2③：绝不静默放过。"""
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < min_chars:
        raise NoTranscriptError(
            f"{where} 产出的逐字稿过短（有效字符 {len(stripped)} < 下限 {min_chars}）→ "
            "判为失败。宁可报错，不产出空文件。"
        )


def fetch_transcript(
    client: BiliClient,
    bvid: str,
    *,
    use_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Transcript:
    """取逐字稿：仅站内字幕（人工字幕优先，其次 AI 字幕）。

    Raises:
        NotLoggedInError: 未登录/无权限 → **不是**"没有字幕"，必须让上层报错。
        NoTranscriptError: 该视频没有任何可用字幕（人工 + AI 都没有），
            或字幕正文过短。本项目不做 ASR 兜底。
    """
    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    timing: dict[str, float] = {}
    notes: list[str] = []
    t_all = time.time()

    # ---------- 元信息（带缓存） ----------
    t0 = time.time()
    info: VideoInfo | None = None
    if use_cache:
        cached = cache.get("video_info", bvid)
        if cached:
            info = VideoInfo(**cached)
            notes.append("元信息来自缓存")
    if info is None:
        info = client.video_info(bvid)
        if use_cache:
            cache.put(
                "video_info",
                bvid,
                {
                    "bvid": info.bvid, "cid": info.cid, "title": info.title,
                    "duration": info.duration, "owner_name": info.owner_name,
                    "owner_mid": info.owner_mid, "desc": info.desc,
                },
            )
    timing["meta"] = time.time() - t0
    say(f"视频：{info.title}（{info.duration // 60} 分钟）")

    # ---------- 站内字幕（人工优先，AI 兜底） ----------
    t0 = time.time()
    tracks: list[SubtitleTrack] = []
    no_subtitle = False
    short_text = ""
    try:
        tracks = client.subtitle_tracks(info)
    except NoSubtitleError as exc:
        no_subtitle = True
        notes.append(f"确认无站内字幕：{exc}")
        say("站内无字幕（含 AI 字幕）")
    except NotLoggedInError:
        # 原样抛出：这不是"没有字幕"，不能悄悄把未登录掩盖成"无字幕"
        raise

    if tracks:
        track = _pick_track(tracks)
        ck = f"{bvid}:{track.lan}"
        cues = cache.get("subtitle", ck) if use_cache else None
        from_cache = cues is not None
        if cues is None:
            cues = client.fetch_cues(track)
            if use_cache and cues:
                cache.put("subtitle", ck, cues)
        segments = _dedupe_adjacent(_cues_to_segments(cues))
        text = "".join(s.text for s in segments)
        if len(re.sub(r"\s+", "", text)) >= MIN_TRANSCRIPT_CHARS:
            timing["subtitle"] = time.time() - t0
            fixed = hotwords.apply_corrections(text)
            if fixed.total:
                segments = _apply_to_segments(segments, fixed)
                notes.append(f"热词修正 {fixed.total} 处")
            timing["total"] = time.time() - t_all
            return Transcript(
                bvid=bvid, title=info.title,
                source=TranscriptSource.SUBTITLE_CACHE if from_cache else TranscriptSource.SUBTITLE,
                segments=segments, info=info,
                subtitle_lan=track.lan, subtitle_lan_doc=track.lan_doc,
                corrections=fixed.hits, timing=timing, notes=notes,
            )
        # 字幕轨道存在但正文过短：记录下来，作为报错信息的一部分
        short_text = f"（字幕正文仅 {len(text)} 字，低于下限 {MIN_TRANSCRIPT_CHARS}）"
        notes.append(f"字幕轨道存在但正文过短（{len(text)} 字）")
        say("字幕内容过短")

    # ---------- 没有可用字幕：直接报错，不做 ASR 兜底 ----------
    raise NoTranscriptError(
        f"该视频没有可用的字幕（人工字幕与 B 站 AI 字幕均不存在）{short_text}。\n\n"
        "本服务依赖 B 站提供的字幕生成逐字稿，不再对无字幕视频做语音识别兜底。\n"
        "可以换一个带字幕的视频再试。"
    )


def _apply_to_segments(
    segments: list[Segment], fixed: hotwords.CorrectionResult
) -> list[Segment]:
    """把整篇的修正结果逐段套用（逐段替换，保持时间戳不变）。"""
    out: list[Segment] = []
    for seg in segments:
        one = hotwords.apply_corrections(seg.text)
        out.append(Segment(seg.start, seg.end, one.text))
    return out


def ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def to_markdown(t: Transcript) -> str:
    """渲染成 Markdown 产物。

    放成共享函数而不是写在 CLI 里：Web 层与 P2 的总结/要点都要用同一套头部
    （尤其是免责声明与来源标注），不能各写一份。
    """
    from .disclaimer import artifact_header

    source = t.source.label
    if t.subtitle_lan_doc:
        source += f"（{t.subtitle_lan_doc}{'，AI 生成' if t.subtitle_lan.startswith('ai-') else ''}）"

    lines = [
        f"# {t.title}",
        "",
        artifact_header(source, t.bvid),
        "",
        f"- BV：{t.bvid}   UP：{t.info.owner_name}   时长：{ts(t.info.duration)}",
        f"- 取数路线：**{source}**",
        f"- 逐字稿：{t.char_count} 字，{len(t.segments)} 段，"
        f"覆盖至 {ts(t.duration_covered)}",
        f"- 链接：https://www.bilibili.com/video/{t.bvid}",
    ]
    if t.corrections:
        fixes = "、".join(f"{h.wrong}→{h.right}（{h.count}）" for h in t.corrections)
        lines.append(f"- 热词修正：{fixes}")
    if t.timing:
        cost = "，".join(f"{k} {v:.2f}s" for k, v in t.timing.items())
        lines.append(f"- 耗时：{cost}")
    lines += ["", "## 逐字稿", ""]
    for seg in t.segments:
        lines.append(f"**[{ts(seg.start)}]** {seg.text}")
        lines.append("")
    if t.notes:
        lines += ["---", "", "### 处理记录", ""]
        lines += [f"- {n}" for n in t.notes]
        lines.append("")
    return "\n".join(lines)
