"""扫码登录（面向 Web GUI 使用）：把二维码存成 PNG 并轮询到登录成功。

与 scripts/login.py 的区别：那个在终端画 ASCII 二维码，适合纯命令行；
本脚本额外输出 PNG，适合在聊天/网页界面里直接看。

用法：
    python scripts/login_qr.py            # 生成二维码并轮询（默认 4 分钟）
    python scripts/login_qr.py --png 路径.png
"""

from __future__ import annotations

import argparse
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

from vd.bilibili import BiliClient  # noqa: E402
from vd.login import QrState, is_logged_in, persist, poll_once, start_qr_login  # noqa: E402

DEFAULT_PNG_DIR = ROOT / "output"
# 实测：B 站二维码有效期约 182 秒，默认留 170 秒，别设更长
DEFAULT_TIMEOUT = 170


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", default="",
                    help="输出路径。留空则自动用带时间戳的唯一文件名")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"轮询窗口秒数，默认 {DEFAULT_TIMEOUT}（B 站二维码约 182s 失效）")
    args = ap.parse_args()

    qr = start_qr_login()

    # ⚠️ 必须用**唯一文件名**。踩过的坑：固定写成 bilibili-login-qr.png 时，
    # 浏览器/界面会缓存这张图，使用者扫到的其实是上一轮已经失效的旧二维码，
    # 却以为是新码过期，白等几轮。唯一文件名让缓存不可能命中。
    if args.png:
        png = Path(args.png)
    else:
        stamp = dt.datetime.now().strftime("%H%M%S")
        png = DEFAULT_PNG_DIR / f"login-qr-{stamp}.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    import qrcode

    img = qrcode.make(qr.url)
    img.save(png)

    # 实测（2026-10）：B 站二维码有效期约 182 秒。倒计时不是装饰 ——
    # 之前失败就是因为把二维码交给使用者时没说明剩余时间。
    started = time.monotonic()
    deadline = started + args.timeout
    expires_at = dt.datetime.now() + dt.timedelta(seconds=args.timeout)

    print(f"QR_IMAGE {png}")
    print(f"QR_URL {qr.url}")
    print(f"VALID_UNTIL {expires_at.strftime('%H:%M:%S')} （约 {args.timeout}s）")
    print("请用 B 站手机客户端扫码，并在手机上确认登录。", flush=True)

    last = None
    last_tick = -1
    while time.monotonic() < deadline:
        state, message = poll_once(qr)
        if state != last:
            print(f"STATE {state.value} {message}", flush=True)
            last = state
        if state is QrState.SUCCESS:
            if not is_logged_in(qr):
                print("FAIL 轮询成功但会话中没有 SESSDATA", flush=True)
                return 3
            creds = persist(qr)
            client = BiliClient(creds)
            nav = client.nav()
            if not nav.get("isLogin"):
                print("FAIL 保存后 nav 判定未登录", flush=True)
                return 4
            uname = nav.get("uname", "")
            creds = persist(qr, uname=uname)
            print(f"LOGGED_IN uid={creds.uid} uname={uname} owner={creds.is_owner}", flush=True)
            return 0
        if state is QrState.EXPIRED:
            remain = max(deadline - time.monotonic(), 0)
            print(f"EXPIRED 二维码已失效（服务端判定），剩余窗口 {remain:.0f}s 作废", flush=True)
            return 2
        remain = int(deadline - time.monotonic())
        # 每 30 秒报一次剩余时间，让使用者知道还剩多久
        if remain // 30 != last_tick:
            last_tick = remain // 30
            print(f"WAITING 剩余 {remain}s", flush=True)
        time.sleep(2)

    print("TIMEOUT 超时未完成扫码", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
