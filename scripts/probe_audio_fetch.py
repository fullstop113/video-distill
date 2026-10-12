"""确认：B 站音频链接的外部可拉取性 + 音频体积。

背景：阿里云 Paraformer 拉取该链接返回 FILE_403_FORBIDDEN。
需要区分两种原因：
  a) CDN 校验 Referer → 任何外部服务都拉不到（必须上传）
  b) 链接本身失效/需登录 → 换链接即可

同时量音频体积，用于评估上传方案的带宽代价。
"""

from __future__ import annotations

import os
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

from vd.summarize import sanitize_no_proxy  # noqa: E402

sanitize_no_proxy()

import httpx  # noqa: E402

from vd.bilibili import BiliClient  # noqa: E402
from vd.credentials import load_operator_credentials  # noqa: E402

BVID = sys.argv[1] if len(sys.argv) > 1 else "BV1d8p56tEFC"
cli = BiliClient(load_operator_credentials())
info = cli.video_info(BVID)
url, dur = cli.audio_url(info)
print(f"视频 {info.title[:36]}  {dur:.0f} 秒\n")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"

print("=== 用不同请求头试探链接的可拉取性 ===")
print("（阿里云拉不到 → 若带 Referer 能拉到，说明是 Referer 校验）\n")
cases = [
    ("裸请求（无任何头，模拟外部服务）", {}),
    ("仅 User-Agent", {"User-Agent": UA}),
    ("UA + Referer（模拟站内）", {"User-Agent": UA, "Referer": "https://www.bilibili.com"}),
    ("UA + Referer + Range(0-1023)", {
        "User-Agent": UA, "Referer": "https://www.bilibili.com",
        "Range": "bytes=0-1023"}),
]
for label, hdrs in cases:
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as c:
            r = c.get(url, headers=hdrs)
        size = r.headers.get("content-length") or r.headers.get("content-range") or "?"
        print(f"  {label:34s} HTTP {r.status_code}  len={size}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:34s} ❌ {type(exc).__name__}: {str(exc)[:70]}")

print("\n=== 音频体积（决定上传代价） ===")
hdrs = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
try:
    t0 = time.time()
    got = 0
    limit = 3 * 1024 * 1024
    with httpx.stream("GET", url, headers=hdrs, timeout=60) as r:
        total = int(r.headers.get("content-length") or 0)
        for chunk in r.iter_bytes(65536):
            got += len(chunk)
            if got >= limit:
                break
    dt = time.time() - t0
    print(f"  Content-Length: {total:,} 字节 = {total / 1024 / 1024:.1f} MB")
    print(f"  实测下载 {got / 1024 / 1024:.1f} MB 用时 {dt:.2f}s "
          f"→ {got / dt / 1024 / 1024:.1f} MB/s")
    print(f"\n  ⚠️ 上传 {total / 1024 / 1024:.0f} MB 到云端，按 5 Mbps 上行约需 "
          f"{total / 1024 / 1024 / (5 / 8) / 60:.1f} 分钟")
except Exception as exc:  # noqa: BLE001
    print(f"  ❌ {type(exc).__name__}: {exc}")

print("\n=== 结论 ===")
print("  若'裸请求'返回 403 而'带 Referer'返回 200 →")
print("  **B 站 CDN 校验 Referer，任何外部服务都无法直接拉取**。")
print("  那么 Paraformer 必须接收**我们上传的音频**，")
print("  即需要：下载音频 → 上传到某个阿里云可直接访问的位置 → 提交 URL")
