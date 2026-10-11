"""ASR 兜底：DASH 音频 → 16kHz 单声道 → FunASR Paraformer → 带时间戳的逐字稿。

## 与探针的关键差别

`scripts/asr_quality.py` 每次调用都 `AutoModel(...)` 一次——在本机测一次没问题，
但在服务里等于**每个视频都重新加载一遍模型**（数秒到数十秒，还反复吃显存）。
这里改成**进程内单例**，并按 device 分别缓存。

## 实测数据（P0，docs/p0-findings.md §6）

| 项 | 值 |
| --- | --- |
| RTF（RTX 4070 Ti） | 0.0108 |
| 1 小时音频 | 约 0.7 分钟 |
| 音频下载 | 必须带 `Referer: https://www.bilibili.com` + 浏览器 UA，否则 403 |
| HEAD 请求 | 在 P2P 节点返回 404，**只有 GET 正常** |

## 为什么要有时长上限

GPU 是串行资源。一个 4 小时的视频会把队列堵死很久，
而配额规则里"他人账号累计 ≤3 小时"也是基于同一考虑。
所以这里显式设 `max_seconds`，超长视频明确报错而不是默默占用 GPU 几十分钟。
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .bilibili import (
    AudioUnavailableError,
    BiliClient,
    RestrictedVideoError,
    VideoInfo,
    ts_hms,
)

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
AUDIO_DIR = ROOT / "data" / "audio"
MODEL_DIR = ROOT / "data" / "models"

# 单例缓存：key = (model, device)
_MODELS: dict[tuple[str, str], object] = {}
_MODEL_LOCK = Lock()


class AsrError(RuntimeError):
    """ASR 失败（可重试与否由调用方判断，这里只区分类型）。"""


class AsrUnavailableError(AsrError):
    """环境不具备 ASR 能力（缺依赖 / 无 GPU / 模型加载失败）。"""


@dataclass
class AsrSegment:
    start: float
    end: float
    text: str


@dataclass
class AsrResult:
    text: str
    segments: list[AsrSegment]
    audio_seconds: float
    asr_seconds: float
    device: str
    model: str

    @property
    def rtf(self) -> float:
        return self.asr_seconds / self.audio_seconds if self.audio_seconds else 0.0


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise AsrUnavailableError(
            "未安装 imageio-ffmpeg（GitHub 不通，ffmpeg 必须走这个包）"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def pick_device(prefer: str = "") -> str:
    if prefer:
        return prefer
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def load_model(model: str = "paraformer-zh", device: str = "cuda"):
    """进程内单例加载。同一 device 只加载一次。"""
    key = (model, device)
    if key in _MODELS:
        return _MODELS[key]
    with _MODEL_LOCK:
        if key in _MODELS:  # 双检，避免并发下重复加载
            return _MODELS[key]
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise AsrUnavailableError(f"未安装 funasr：{exc}") from exc
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        log.info("加载 ASR 模型 %s（device=%s）…", model, device)
        t0 = time.time()
        try:
            m = AutoModel(
                model=model,
                vad_model="fsmn-vad",
                punc_model="ct-punc",
                device=device,
                disable_update=True,
                hub="ms",
                cache_dir=str(MODEL_DIR),
            )
        except Exception as exc:  # noqa: BLE001
            raise AsrUnavailableError(f"模型加载失败（{model}@{device}）：{exc}") from exc
        log.info("模型就绪，耗时 %.1fs", time.time() - t0)
        _MODELS[key] = m
        return m


def download_audio(
    client: BiliClient, url: str, out: Path, max_bytes: int = 0
) -> int:
    """流式下载 DASH 音频。只用 GET（HEAD 在 P2P 节点会 404）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": client.session.headers["User-Agent"],
        "Referer": "https://www.bilibili.com",
    }
    if max_bytes:
        headers["Range"] = f"bytes=0-{max_bytes}"
    total = 0
    with client.session.get(url, headers=headers, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with out.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                if max_bytes and total >= max_bytes:
                    break
    return total


def to_wav16k(src: Path, dst: Path, max_seconds: float = 0) -> float:
    """转 16kHz 单声道 WAV，返回实际音频时长（秒）。"""
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src)]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AsrError(f"ffmpeg 解码失败：{proc.stderr.strip()[:300]}")
    return dst.stat().st_size / (16000 * 2)


def transcribe_wav(
    wav: Path,
    audio_seconds: float,
    *,
    model: str = "paraformer-zh",
    device: str = "cuda",
) -> AsrResult:
    """对已解码的 WAV 做转写。模型走单例。

    ⚠️ 必须传 `sentence_timestamp=True`。踩过的坑：不传时 FunASR 只返回
    `key/text/timestamp` 三个字段，**没有 `sentence_info`**，于是逐字稿只能拿到
    一个覆盖全片的时间戳。方案 §5.2② 要求"每条结论都能点回原视频位置"，
    一个锚点显然不合格。开了这个参数（配合 punc_model）才拿到按标点切分的
    句级 `sentence_info`（start/end 为毫秒）。
    """
    m = load_model(model, device)
    t0 = time.time()
    try:
        out = m.generate(input=str(wav), batch_size_s=300, sentence_timestamp=True)
    except TypeError:
        # 老版本 FunASR 不认这个参数：降级重试，并明确记下来
        log.warning("当前 FunASR 版本不支持 sentence_timestamp=True，降级为整段单一时间戳")
        out = m.generate(input=str(wav), batch_size_s=300)
    except Exception as exc:  # noqa: BLE001
        raise AsrError(f"转写失败：{exc}") from exc
    dt = time.time() - t0

    text = ""
    segments: list[AsrSegment] = []
    if out and isinstance(out, list):
        item = out[0] or {}
        text = item.get("text", "") or ""
        for s in item.get("sentence_info") or []:
            seg_text = (s.get("text") or "").strip()
            if not seg_text:
                continue
            segments.append(
                AsrSegment(
                    start=float(s.get("start", 0)) / 1000.0,
                    end=float(s.get("end", 0)) / 1000.0,
                    text=seg_text,
                )
            )
    return AsrResult(
        text=text.strip(),
        segments=segments,
        audio_seconds=audio_seconds,
        asr_seconds=dt,
        device=device,
        model=model,
    )


def transcribe_video(
    client: BiliClient,
    info: VideoInfo,
    *,
    model: str = "paraformer-zh",
    device: str = "",
    max_seconds: int = 0,
    keep_audio: bool = False,
) -> AsrResult:
    """完整链路：取音频地址 → 下载 → 解码 → 转写。

    Args:
        max_seconds: 超过此长度直接拒绝（0 = 不限制）。用于防止超长视频堵住 GPU 队列。

    Raises:
        RestrictedVideoError: 视频受权限限制（充电专属/付费预览）—— 上层应提示用户。
        AudioUnavailableError: 接口拿不到音频。
        AsrError: 拿到音频但转写失败。
    """
    if max_seconds and info.duration > max_seconds:
        raise AsrError(
            f"视频时长 {info.duration // 60} 分钟超过 ASR 上限 "
            f"{max_seconds // 60} 分钟，拒绝转写（避免长时间占用 GPU 队列）。"
        )

    dev = pick_device(device)
    url, stream_seconds = client.audio_url(info)
    if not url:
        raise AudioUnavailableError("取到的音频地址为空", context="asr")

    # ⚠️ 关键校验：媒体流时长必须与视频时长相符。
    #
    # 实测（BV1f7HS68EZz，充电专属）：playurl 返回 code=0 + 一段**可正常播放**的
    # 250 秒预览，而视频真实时长 1749 秒。若不做这一步比对，就会生成一份 250 秒的
    # 逐字稿，使用者会以为它是完整视频的 —— 静默残废产物。
    #
    # 容差 5%：B 站报的 duration 与实际流长常有几秒出入，属正常。
    if info.duration and stream_seconds:
        ratio = stream_seconds / info.duration
        if ratio < 0.95:
            raise RestrictedVideoError(
                f"取到的媒体流只有 {ts_hms(stream_seconds)}，"
                f"而视频时长是 {ts_hms(info.duration)}（{ratio:.0%}）——"
                f"这是**预览片段**，不能当作完整逐字稿。"
                + (f" 平台提示：{info.preview_toast}" if info.preview_toast else "")
            )

    raw = AUDIO_DIR / f"{info.bvid}.m4s"
    wav = AUDIO_DIR / f"{info.bvid}.16k.wav"

    got = download_audio(client, url, raw)
    log.info("音频下载完成：%.2f MB（预期约 %.1f MB）",
             got / 1024 / 1024, stream_seconds * 32000 / 1024 / 1024)
    seconds = to_wav16k(raw, wav)
    log.info("解码完成：%.1fs 单声道 16kHz", seconds)

    # 二次校验：解码出来的实际音频长度也要对得上
    if info.duration and seconds and seconds / info.duration < 0.95:
        raise RestrictedVideoError(
            f"解码后音频只有 {ts_hms(seconds)}，视频时长 {ts_hms(info.duration)} ——"
            "实际拿到的仍是预览片段。"
        )

    try:
        result = transcribe_wav(wav, seconds, model=model, device=dev)
    finally:
        if not keep_audio:
            raw.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
    return result
