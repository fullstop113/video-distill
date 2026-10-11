"""ASR 质量与 RTF 的正式实测（取**完整音频**，不截断）。

与 asr_probe.py 的区别：那个用 Range 截断来省时间，会把 m4s 切成不完整的文件，
测出的时长偏短；本脚本取完整音频再按 --limit 截断**解码后**的时长，
这样时长口径与实际一致，逐字稿也更有参考价值。
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


def ts(s: float) -> str:
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=float, default=180.0, help="最多解码多少秒")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--repeat", type=int, default=1, help="重复转写次数，用于稳定测 RTF")
    args = ap.parse_args()

    try:
        creds = load_operator_credentials()
    except CredentialPolicyError as exc:
        print(f"❌ 凭据策略拒绝：{exc}")
        return 2
    client = BiliClient(creds)

    # 明确挑一个"说话为主"的视频：用一个常看榜里的科技/知识类，而不是唱歌剪辑。
    # 这里用热门榜里标题最长的一个作为启发式（长标题多为讲解类），并排除音乐区。
    data = client._get_json(
        "https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
    )["data"]
    cands = [i for i in (data.get("list") or []) if i.get("bvid")]
    cands.sort(key=lambda i: -(len(i.get("title", ""))))
    info = client.video_info(cands[0]["bvid"])
    print(f"【视频】{info.title}")
    print(f"       UP={info.owner_name} 全片 {ts(info.duration)}")

    url = client.audio_url(info)
    raw = AUDIO_DIR / f"{info.bvid}.full.m4s"

    t0 = time.time()
    headers = {
        "User-Agent": client.session.headers["User-Agent"],
        "Referer": "https://www.bilibili.com",
    }
    total = 0
    with client.session.get(url, headers=headers, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with raw.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    t_dl = time.time() - t0
    print(f"【下载】完整音频 {total / 1024 / 1024:.2f} MB，耗时 {t_dl:.1f}s "
          f"（{total / 1024 / t_dl:.0f} KB/s）")

    import imageio_ffmpeg

    wav = AUDIO_DIR / f"{info.bvid}.full.16k.wav"
    t0 = time.time()
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(raw),
         "-t", str(args.limit), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(wav)],
        check=True, capture_output=True,
    )
    t_ff = time.time() - t0
    secs = wav.stat().st_size / (16000 * 2)
    print(f"【解码】{t_ff:.1f}s → {secs:.1f}s（{ts(secs)}）单声道 16kHz WAV")

    from funasr import AutoModel

    t0 = time.time()
    model = AutoModel(
        model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc",
        device=args.device, disable_update=True, hub="ms", cache_dir=str(MODEL_DIR),
    )
    t_load = time.time() - t0
    print(f"【ASR】模型加载 {t_load:.1f}s，device={args.device}")

    best = None
    for i in range(args.repeat):
        t0 = time.time()
        res = model.generate(input=str(wav), batch_size_s=300)
        dt = time.time() - t0
        text = res[0].get("text", "") if res else ""
        rtf = dt / secs if secs else 0
        print(f"  第 {i + 1} 次：{dt:.2f}s  RTF={rtf:.4f}  字数={len(text)}")
        if best is None or dt < best[0]:
            best = (dt, text, rtf)

    dt, text, rtf = best
    print("\n" + "=" * 66)
    print(f"音频时长          : {secs:.1f}s（{ts(secs)}）")
    print(f"最快转写耗时      : {dt:.2f}s")
    print(f"实时率 RTF        : {rtf:.4f}")
    print(f"折算 1 小时音频   : {rtf * 60:.1f} 分钟")
    print(f"折算 30 分钟视频  : {rtf * 30:.1f} 分钟")
    print(f"字数              : {len(text)}   语速 {len(text) / secs:.1f} 字/秒")

    print(f"\n--- 逐字稿（前 600 字，用于人眼判断质量） ---\n{text[:600]}")

    (ROOT / "output" / f"asr-quality-{info.bvid}.txt").write_text(text, encoding="utf-8")
    (ROOT / "data" / "asr_quality_result.json").write_text(
        json.dumps(
            {
                "bvid": info.bvid, "title": info.title, "audio_seconds": round(secs, 2),
                "asr_seconds": round(dt, 2), "rtf": round(rtf, 4),
                "chars": len(text), "device": args.device,
                "download_seconds": round(t_dl, 2), "decode_seconds": round(t_ff, 2),
                "model_load_seconds": round(t_load, 2), "audio_mb": round(total / 1024 / 1024, 2),
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n产物：output/asr-quality-{info.bvid}.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
