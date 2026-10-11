"""诊断：从"使用者真正会看到的那张 PNG"反解出二维码内容，核对是否与我生成的一致。

存在的理由：连续几次"扫码提示已过期"，而服务端日志一切正常。
在"我生成的码"与"对方扫到的码"之间存在展示层，必须**从展示物本身验证**，
否则只能靠猜。本脚本把 PNG 解码回 URL，并与生成时的 URL 逐字比对。

用法：
    python scripts/qr_verify.py            # 生成 → 存 PNG → 解码回读 → 比对
"""

from __future__ import annotations

import datetime as dt
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

import cv2  # noqa: E402
import qrcode  # noqa: E402

from vd.login import QrState, poll_once, start_qr_login  # noqa: E402


def decode_png(path: Path) -> str:
    img = cv2.imread(str(path))
    if img is None:
        raise RuntimeError(f"无法读取图片：{path}")
    data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return data or ""


def main() -> int:
    out_dir = ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    qr = start_qr_login()
    stamp = dt.datetime.now().strftime("%H%M%S")
    png = out_dir / f"verify-{stamp}.png"
    qrcode.make(qr.url).save(png)
    t_gen = time.monotonic()

    print(f"生成时间   : {dt.datetime.now().strftime('%H:%M:%S')}")
    print(f"图片路径   : {png.name}")
    print(f"图片大小   : {png.stat().st_size} 字节")
    print(f"生成时 URL : {qr.url[:78]}…")

    decoded = decode_png(png)
    print(f"解码回读   : {decoded[:78] if decoded else '（解码失败）'}…")

    print("\n--- 内容一致性 ---")
    if decoded == qr.url:
        print("  ✅ 图片内容与生成时完全一致（展示层没有做手脚）")
    else:
        print("  ❌ 不一致！图片里的码不是刚生成的那个")
        print(f"     生成 qrcode_key = {qr.url.split('qrcode_key=')[-1].split('&')[0]}")
        if decoded:
            print(f"     图片 qrcode_key = {decoded.split('qrcode_key=')[-1].split('&')[0]}")
        return 1

    print("\n--- 立即验证这个码在服务端是否可用 ---")
    print("（如果这里就报失效，说明码一生成就是死的，与展示层无关）")
    last = None
    for i in range(4):
        state, message = poll_once(qr)
        el = time.monotonic() - t_gen
        print(f"  {el:5.1f}s  状态={state.value}  {message!r}")
        last = state
        if state in (QrState.SUCCESS, QrState.EXPIRED):
            break
        time.sleep(1)

    print("\n--- 结论 ---")
    if last is QrState.EXPIRED:
        print("  ❌ 码在生成后几秒内就被服务端判定失效 —— 不是展示层问题，需要查请求本身")
        return 2
    if last is QrState.PENDING:
        print("  ✅ 码是活的（86101 未扫码），展示层内容也正确")
        print("     如果使用者此时扫码仍报过期，那问题在**扫码客户端**（App 缓存旧会话）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
