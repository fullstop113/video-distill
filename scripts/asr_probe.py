"""P0 验收：ASR 路线端到端跑通，并实测**实时率（RTF）**。

这一步回答方案 §3.3 留下的问题：装了 CUDA torch 之后，本机到底转得多快。
RTF < 1 表示比实时快；0.3 表示 1 小时音频要转 3.3 小时（CPU 版的量级）。

流程全部按真实链路走，不含捷径：
    B 站 playurl → DASH 音频流(GET + Referer/UA) → imageio-ffmpeg 解码 16k 单声道
    → FunASR Paraformer(VAD + ASR + 标点) → 逐字稿

用法：
    python scripts/asr_probe.py                 # 自动挑一个短视频
    python scripts/asr_probe.py BV1xxxxxxxxx --max-seconds 120
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from vd.bilibili import BiliClient  # noqa: E402
from vd.credentials import CredentialPolicyError, load_operator_credentials  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

AUDIO_DIR = ROOT / "data" / "audio"
MODEL_DIR = ROOT / "data" / "models"


def ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def download_audio(client: BiliClient, url: str, out: Path, max_bytes: int) -> int:
    """流式下载音频。

    实测坑（方案 §4.3）：必须带 Referer: https://www.bilibili.com 和浏览器 UA，
    否则 403；且 HEAD 在 P2P 节点返回 404，只有 GET 正常 —— 所以这里只用 GET。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": client.session.headers["User-Agent"],
        "Referer": "https://www.bilibili.com",
        "Range": f"bytes=0-{max_bytes}",
    }
    total = 0
    with client.session.get(url, headers=headers, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        with out.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
    return total


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def to_wav16k(src: Path, dst: Path, max_seconds: float) -> None:
    cmd = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-i", str(src),
        "-t", str(max_seconds),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="FunASR 端到端与 RTF 实测")
    ap.add_argument("bvid", nargs="?", default="")
    ap.add_argument("--max-seconds", type=float, default=120.0, help="最多转写多少秒音频")
    ap.add_argument("--model", default="paraformer-zh", help="FunASR 模型名")
    ap.add_argument("--no-gpu", action="store_true", help="强制用 CPU（用于对比 RTF）")
    args = ap.parse_args()

    try:
        creds = load_operator_credentials()
    except CredentialPolicyError as exc:
        print(f"❌ 凭据策略拒绝：{exc}")
        return 2
    client = BiliClient(creds)

    bvid = args.bvid
    if not bvid:
        data = client._get_json(
            "https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
        )["data"]
        # 挑时长最短的，省时间
        items = sorted(
            (i for i in (data.get("list") or []) if i.get("duration")),
            key=lambda i: i["duration"],
        )
        bvid = items[0]["bvid"]
        print(f"【选片】热门榜中时长最短：{bvid}（{items[0]['duration']}s）")

    info = client.video_info(bvid)
    print(f"【视频】{info.title}")
    print(f"       UP={info.owner_name} 时长={ts(info.duration)}")

    audio_url = client.audio_url(info)
    print(f"【音频】已取得 DASH 流地址（{audio_url.split('?')[0][:60]}…）")

    raw = AUDIO_DIR / f"{info.bvid}.m4s"
    wav = AUDIO_DIR / f"{info.bvid}.16k.wav"
    # 16kHz 单声道 16bit ≈ 32KB/s，留够 max_seconds 的量
    max_bytes = int(args.max_seconds * 32000 * 1.5)
    t0 = time.time()
    got = download_audio(client, audio_url, raw, max_bytes)
    t_dl = time.time() - t0
    print(f"【下载】{got / 1024 / 1024:.2f} MB，耗时 {t_dl:.1f}s "
          f"（{got / 1024 / t_dl:.0f} KB/s）")

    t0 = time.time()
    to_wav16k(raw, wav, args.max_seconds)
    t_ff = time.time() - t0
    wav_seconds = wav.stat().st_size / (16000 * 2)
    print(f"【解码】ffmpeg 耗时 {t_ff:.1f}s → {wav_seconds:.1f}s 单声道 16kHz")

    device = "cpu" if args.no_gpu else "cuda"
    print(f"\n【ASR】加载 {args.model}（device={device}）…首次会从 ModelScope 下载权重")
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    from funasr import AutoModel

    t0 = time.time()
    model = AutoModel(
        model=args.model,
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        device=device,
        disable_update=True,
        hub="ms",
        cache_dir=str(MODEL_DIR),
    )
    t_load = time.time() - t0
    print(f"【ASR】模型就绪，加载耗时 {t_load:.1f}s")

    t0 = time.time()
    result = model.generate(input=str(wav), batch_size_s=300)
    t_asr = time.time() - t0

    text = ""
    if result and isinstance(result, list):
        text = result[0].get("text", "") or ""
    chars = len(text)
    rtf = t_asr / wav_seconds if wav_seconds else 0.0

    print("\n" + "=" * 62)
    print(f"音频时长        : {wav_seconds:.1f}s")
    print(f"转写耗时        : {t_asr:.1f}s（不含模型加载）")
    print(f"实时率 RTF      : {rtf:.3f}   ← <1 即比实时快")
    if rtf > 0:
        print(f"折算 1 小时音频 : {rtf * 60:.1f} 分钟")
    print(f"转写字数        : {chars}")
    if chars:
        print(f"语速            : {chars / wav_seconds:.1f} 字/秒")
    print(f"\n逐字稿前 200 字 :\n{text[:200]}")

    ok = chars >= 20
    print("\n" + ("✅ ASR 路线跑通" if ok else "❌ 转写结果为空/过短 —— 不算通过"))
    if ok and rtf < 0.2:
        print("   结论：GPU 转写速度足以支撑多人 Web 服务（方案 §3.3 的顾虑解除）。")
    elif ok and rtf >= 0.5:
        print("   结论：RTF 偏高，需检查是否真的跑在 GPU 上。")

    (ROOT / "data" / "asr_probe_result.json").write_text(
        json.dumps(
            {
                "bvid": info.bvid,
                "title": info.title,
                "audio_seconds": round(wav_seconds, 2),
                "asr_seconds": round(t_asr, 2),
                "rtf": round(rtf, 4),
                "chars": chars,
                "device": device,
                "model": args.model,
                "load_seconds": round(t_load, 2),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
