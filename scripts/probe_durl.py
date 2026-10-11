"""诊断：durl 降级路线是否可用，以及它拿到的是完整音频还是预览片段。

背景：某些视频（充电专属 / 付费预览 / 老投稿）playurl 不返回 `dash`，
只返回 `durl`。`audio_url()` 之前只认 `dash.audio`，于是把这类视频一律报成
"接口没返回音频流" —— 但 B 站其实**给了东西**，只是格式不同。

本脚本回答三个问题：
  1. durl 指向的媒体流实际有多大、有多长？
  2. 它是完整音频，还是被截断的预览片段？
  3. 用 ffmpeg 能不能从里面解出音轨（durl 可能是纯视频容器）？

用法：
    python scripts/probe_durl.py BV1xxxxxxxxx
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
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

from vd.asr import ffmpeg_exe  # noqa: E402
from vd.bilibili import BiliClient  # noqa: E402
from vd.credentials import load_operator_credentials  # noqa: E402

TMP = ROOT / "data" / "audio"


def probe_stream(url: str, headers: dict, tag: str) -> dict:
    """用 ffmpeg 读流信息。

    ⚠️ 必须把 Referer / UA 传给 ffmpeg（`-headers`），否则 CDN 返回 403，
    ffmpeg 只会打印一行 "Server returned 403 Forbidden"，很容易被误读成
    "这个流没有音频轨" —— 这两个结论的处置方式完全不同。
    """
    out = {"tag": tag, "ok": False}
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-headers", hdr, "-i", url,
         "-f", "null", "-t", "3", "-"],
        capture_output=True, text=True, timeout=240,
    )
    err = proc.stderr or ""
    out["stderr_tail"] = err[-800:]
    out["forbidden"] = "403" in err or "Forbidden" in err
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", err)
    if m:
        h, mi, s, cs = (int(x) for x in m.groups())
        out["duration_s"] = h * 3600 + mi * 60 + s + cs / 100
    mm = re.search(r"Stream #\d+:\d+.*?: Audio: ([^\n,]+)", err)
    if mm:
        out["audio"] = mm.group(1).strip()
    mv = re.search(r"Stream #\d+:\d+.*?: Video: ([^\n,]+)", err)
    if mv:
        out["video"] = mv.group(1).strip()
    out["ok"] = "duration_s" in out
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid")
    args = ap.parse_args()

    creds = load_operator_credentials()
    client = BiliClient(creds)
    info = client.video_info(args.bvid)
    print(f"【视频】{info.title}")
    print(f"        时长 {info.duration}s（{info.duration // 60} 分 {info.duration % 60} 秒）")

    headers = {
        "User-Agent": client.session.headers["User-Agent"],
        "Referer": "https://www.bilibili.com",
    }

    # 不带 fnval=16，拿默认格式（durl 路线）
    raw = client._signed(
        "https://api.bilibili.com/x/player/wbi/playurl",
        {"bvid": info.bvid, "cid": info.cid, "fnver": 0, "fourk": 1},
    )
    data = raw.get("data") or {}
    durl = data.get("durl") or []
    print(f"\n【1】默认格式响应：durl {len(durl)} 段，declare timelength={data.get('timelength')}")
    for i, seg in enumerate(durl, 1):
        print(f"  第 {i} 段：length={seg.get('length')}ms  size={seg.get('size')}字节  "
              f"order={seg.get('order')}")

    if not durl:
        print("  没有 durl，无法走降级路线")
        return 1

    TMP.mkdir(parents=True, exist_ok=True)
    url = durl[0].get("url") or ""
    print(f"\n【2】用 ffmpeg 读流信息（带 Referer/UA，只解码前 3 秒）")
    r = probe_stream(url, headers, "durl")
    print(f"  可读: {r['ok']}   被 403 拒绝: {r.get('forbidden')}")
    print(f"  容器时长: {r.get('duration_s')}s")
    if r.get("video"):
        print(f"  视频轨: {r['video'][:70]}")
    if r.get("audio"):
        print(f"  音频轨: {r['audio'][:70]}")
    if not r.get("audio") and not r.get("forbidden"):
        print("  ⚠️ 没有音频轨 —— durl 可能是纯视频容器，无法用于 ASR")
    if not r["ok"]:
        print("  ffmpeg stderr 末尾：")
        for line in (r.get("stderr_tail") or "").strip().splitlines()[-4:]:
            print(f"    {line[:96]}")

    print(f"\n【3】对照：视频真实时长 {info.duration}s")
    dur = r.get("duration_s")
    if dur:
        ratio = dur / info.duration
        if ratio < 0.2:
            print(f"  → 只有 {dur:.0f}s（{ratio:.0%}）→ **是预览片段**，不能当完整逐字稿")
        elif ratio > 0.8:
            print(f"  → {dur:.0f}s（{ratio:.0%}）→ **接近完整**，可作为降级来源")
        else:
            print(f"  → {dur:.0f}s（{ratio:.0%}）→ 部分内容，需谨慎")
    else:
        print("  → 读不出时长，无法判定")

    print("\n【4】durl 的 URL 与 dash 音频 URL 是否同一类资源")
    print(f"  durl[0].url 前缀: {url.split('?')[0][:78]}")
    print(f"  声明 size: {durl[0].get('size')} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
