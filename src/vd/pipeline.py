"""管线编排：BV → 逐字稿 + 总结，一次跑完。

## 为什么要单独一层

在没有这一层之前，`scripts/distill.py` 和 `scripts/summarize.py` 各自写了一遍
"取数 → 总结 → 落盘"的编排。P3 的 Web 工作线程需要**第三遍**。
三份会漂移的编排逻辑是维护灾难，所以抽成这里，CLI 与 Worker 都调它。

## 配额在这里判定

方案 §5.3⑤ 的要求：配额必须在**下载音频之前**判定，避免先烧带宽和 GPU 再拒绝。
所以 `planned_seconds` 用视频真实时长，在取逐字稿之前就检查。

## 逐字稿的两条路（2026 新增第二条）

    ┌─ B 站站内字幕  → 免费，绝大多数视频走这条
    └─ 云语音识别    → **花钱**，仅在没有字幕时启用

第二条路要花钱，所以顺序是**先查余额、再下载音频**：

    查余额 → 下载 → 中转 → 转写 → 由调用方扣费

顺序反了就会"先花掉钱、再发现余额不够"—— 钱已经付给阿里云了，追不回来。

**这一层不扣费**：它只负责产出逐字稿并如实报告花了多少"应该扣"的额度
（`asr_seconds`）。真正的扣费在调用方（`worker`），因为钱是阿里云收的，
跟后面总结成不成功无关 —— 即使总结失败，ASR 的钱也已经花了。
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
    """配额不足（含**余额不足**）。

    单独一类，便于 Web 层返回 429 而不是 500。
    """


class AsrNotConfigured(RuntimeError):
    """需要云 ASR 但没配好（缺 DASHSCOPE_API_KEY 或 VD_PUBLIC_BASE_URL）。

    单独一类是为了让错误信息**可操作** —— 用户需要知道去配什么，
    而不是看到一个来自 httpx 的栈。
    """


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
    # 云 ASR 的**应扣额度**（元）。0 表示走了免费的字幕。
    #
    # 为什么叫"应扣"而不是"已扣"：本层不碰钱。真正扣费在 worker，
    # 因为钱是阿里云收的 —— 即使后面总结失败，这笔钱也已经花了，
    # 所以扣费不能挂在"任务成功"上。
    asr_cost_cny: float = 0.0
    # 云 ASR 返回的音频总时长（秒），用于对账
    asr_seconds: int = 0


def _charge(charges: dict | None, amount: float, seconds: int = 0) -> None:
    """把"应扣额度"写进调用方给的字典 —— **本层不碰数据库**。

    ## 为什么不在这里直接扣

    因为扣费时机必须由调用方控制，而这里有两种失败方式：

      · 转写失败 → 钱没花，**不该扣**
      · 转写成功、但后面总结失败 → **钱已经花了，必须扣**

    后一种情况在本函数返回之后才发生。所以这里只登记金额，
    由调用方在"无论成败都要执行"的位置（worker 的 finally）真正扣费。

    如果在这里就扣，那"转写成功但总结失败"时确实会扣对，
    但调用方再也无法知道该扣多少、也无法在转写失败时避免误扣 ——
    责任边界会糊掉。
    """
    if charges is None:
        return
    charges["amount"] = float(amount)
    charges["seconds"] = int(seconds)


def _transcribe_with_cloud_asr(
    client: BiliClient,
    info: VideoInfo,
    *,
    uid: str,
    out_dir: Path,
    say,
) -> tuple[Transcript, float]:
    """无字幕时的第二条路：下载音频 → 中转 → 阿里云 Paraformer。

    ## 顺序是安全要求，不是风格

        查余额 → 下载 → 转写

    先查余额是为了**不花冤枉钱**：一旦把音频交给阿里云，钱就已经产生了。
    如果先转写再发现余额不足，那笔钱追不回来（用户明确提过这个顾虑：
    "余额只有 0.1 元 总不能让它处理 2 小时无字幕视频吧"）。

    Returns:
        (逐字稿, 应扣金额)

    Raises:
        AsrNotConfigured: 没配 Key 或公网地址 —— **明确报错**，不静默降级
        QuotaExceeded:    余额不够
        AsrCloudError:    云端调用失败
    """
    import os

    from . import asr_cloud, billing

    # ---- 配置检查：缺什么就说什么，不要让用户看 httpx 的栈 ----
    if not os.getenv("DASHSCOPE_API_KEY", "").strip():
        raise AsrNotConfigured(
            "该视频没有站内字幕，需要云语音识别，但未配置 DASHSCOPE_API_KEY。\n"
            "请在 .env 里填入阿里云百炼的 API Key。"
        )
    if not asr_cloud.public_base_url():
        raise AsrNotConfigured(
            "该视频没有站内字幕，需要云语音识别，但未配置 VD_PUBLIC_BASE_URL。\n"
            "阿里云只接受**公网可访问**的音频 URL，而 B 站 CDN 校验 Referer，\n"
            "所以音频必须由本服务中转一次。请把 VD_PUBLIC_BASE_URL 设为\n"
            "本机对阿里云可达的地址（服务器公网地址 / 域名），然后重启服务。\n"
            "自检：python scripts/check_deploy.py"
        )

    # ---- 余额检查：**必须在下载与转写之前** ----
    aff = billing.can_afford_or_admin(uid, info.duration)
    if not aff.allowed:
        raise QuotaExceeded(f"{aff.reason}\n（该视频没有字幕，需要云语音识别）")
    say(f"余额检查通过（{aff.reason}），本次预计消耗 ¥{aff.estimated:.3f}")

    # ---- 真正干活 ----
    say("下载音频并转写（云 ASR，会产生费用）")
    res = asr_cloud.transcribe_video(
        client, info, title=info.title, out_dir=out_dir,
        progress=say,
    )

    # 应扣金额按**视频时长**算是刻意的：阿里云实际只对"语音内容"计费
    # （实测占 57%），所以预扣偏保守 —— 账面扣得比真实成本多，
    # 对账号主人而言更安全（用户明确要求这个口径）。
    cost = billing.estimate_cost(info.duration)
    say(f"云 ASR 完成：语音 {res.speech_seconds:.0f}s / "
        f"音频 {res.audio_seconds:.0f}s · 应扣 ¥{cost:.4f}")
    return res.transcript, cost


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
    allow_asr: bool = False,
    charges: dict | None = None,
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

    # ---------- 2. 逐字稿（字幕优先，无字幕时按配置走云 ASR）----------
    stage(STAGE_TRANSCRIPT, "取逐字稿（字幕优先）")
    asr_cost = 0.0
    asr_seconds = 0
    try:
        transcript = fetch_transcript(
            client, bvid,
            use_cache=use_cache,
            progress=lambda m: stage(STAGE_TRANSCRIPT, m),
        )
    except NoTranscriptError as exc:
        # 只有"**确认**没有字幕"才考虑花钱走 ASR。
        # 字幕存在但内容过短时不该走 —— 那种视频（试看预览/受限内容）
        # 下载音频多半也拿不到完整音轨，白花一笔钱。
        if not (allow_asr and getattr(exc, "no_subtitle", False)):
            raise
        stage(STAGE_TRANSCRIPT, "无站内字幕 → 改用云语音识别（会产生费用）")
        transcript, asr_cost = _transcribe_with_cloud_asr(
            client, info, uid=uid, out_dir=out_dir,
            say=lambda m: stage(STAGE_TRANSCRIPT, m),
        )
        asr_seconds = int(info.duration)
        # 登记应扣金额。**立刻**登记而不是等落盘成功 ——
        # 因为钱在转写成功那一刻就花出去了，后面总结失败也要扣。
        # 同时登记秒数：免费额度用量按月统计要用它。
        _charge(charges, asr_cost, asr_seconds)

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
        asr_cost_cny=asr_cost,
        asr_seconds=asr_seconds,
    )
