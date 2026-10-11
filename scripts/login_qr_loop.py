"""持续刷新二维码，直到登录成功。

解决的问题：B 站二维码**实测只有约 182 秒**有效期（见 docs/p0-findings.md）。
如果先把二维码交出去、再等人扫，很容易错过窗口，而且每次失败都要重来一轮。

本脚本的做法是**不等**——它在一个循环里不断生成新二维码，每次都写入
**唯一文件名**（带序号与时间戳），随时打开最新的那个都是活的。
使用者不必"赶上"某一轮。

用法：
    python scripts/login_qr.py --loop              # 无限刷新，直到登录成功
    python scripts/login_qr.py --loop --rounds 5   # 最多刷新 5 轮
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
# 实测：B 站二维码约 182 秒失效，留 15 秒余量
WINDOW = 168


def render(qr, path: Path) -> None:
    import qrcode

    path.parent.mkdir(parents=True, exist_ok=True)
    qrcode.make(qr.url).save(path)


def one_round(index: int, out_dir: Path) -> tuple[str, object | None]:
    """跑一轮二维码轮询。

    Returns:
        ("ok", creds) / ("expired", None) / ("timeout", None)
    """
    qr = start_qr_login()
    stamp = dt.datetime.now().strftime("%H%M%S")
    png = out_dir / f"login-qr-{index:02d}-{stamp}.png"
    render(qr, png)

    # 让"最新一张"永远有个固定名字，方便反复打开
    latest = out_dir / "login-qr-latest.png"
    render(qr, latest)

    expires_at = dt.datetime.now() + dt.timedelta(seconds=WINDOW)
    print(f"ROUND {index} QR_IMAGE {png}", flush=True)
    print(f"ROUND {index} LATEST {latest}", flush=True)
    print(f"ROUND {index} VALID_UNTIL {expires_at.strftime('%H:%M:%S')}", flush=True)

    deadline = time.monotonic() + WINDOW
    last = None
    while time.monotonic() < deadline:
        state, message = poll_once(qr)
        if state != last:
            print(f"ROUND {index} STATE {state.value} {message}", flush=True)
            last = state
        if state is QrState.SUCCESS:
            if not is_logged_in(qr):
                print(f"ROUND {index} FAIL 无 SESSDATA", flush=True)
                return "timeout", None
            creds = persist(qr)
            client = BiliClient(creds)
            nav = client.nav()
            if not nav.get("isLogin"):
                print(f"ROUND {index} FAIL nav 判定未登录", flush=True)
                return "timeout", None
            uname = nav.get("uname", "")
            creds = persist(qr, uname=uname)
            print(f"ROUND {index} LOGGED_IN uid={creds.uid} uname={uname}", flush=True)
            return "ok", creds
        if state is QrState.EXPIRED:
            print(f"ROUND {index} EXPIRED", flush=True)
            return "expired", None
        time.sleep(2)
    print(f"ROUND {index} TIMEOUT", flush=True)
    return "timeout", None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="持续刷新直到成功")
    ap.add_argument("--rounds", type=int, default=0, help="最多轮数，0 = 不限")
    ap.add_argument("--timeout", type=int, default=WINDOW)
    ap.add_argument("--png", default="", help="指定单次输出路径（非 loop 模式）")
    args = ap.parse_args()

    out_dir = DEFAULT_PNG_DIR
    if not args.loop:
        # 单次模式：保留原有行为
        qr = start_qr_login()
        png = Path(args.png) if args.png else out_dir / (
            f"login-qr-{dt.datetime.now().strftime('%H%M%S')}.png"
        )
        render(qr, png)
        expires_at = dt.datetime.now() + dt.timedelta(seconds=args.timeout)
        print(f"QR_IMAGE {png}")
        print(f"VALID_UNTIL {expires_at.strftime('%H:%M:%S')}")
        deadline = time.monotonic() + args.timeout
        last = None
        while time.monotonic() < deadline:
            state, message = poll_once(qr)
            if state != last:
                print(f"STATE {state.value} {message}", flush=True)
                last = state
            if state is QrState.SUCCESS:
                if not is_logged_in(qr):
                    return 3
                creds = persist(qr)
                nav = BiliClient(creds).nav()
                creds = persist(qr, uname=nav.get("uname", ""))
                print(f"LOGGED_IN uid={creds.uid} uname={creds.uname}", flush=True)
                return 0
            if state is QrState.EXPIRED:
                print("EXPIRED", flush=True)
                return 2
            time.sleep(2)
        print("TIMEOUT", flush=True)
        return 1

    # 循环模式
    index = 0
    while True:
        index += 1
        if args.rounds and index > args.rounds:
            print("GIVE_UP 已达最大轮数", flush=True)
            return 1
        result, creds = one_round(index, out_dir)
        if result == "ok":
            print(f"DONE uid={creds.uid} uname={creds.uname}", flush=True)
            return 0
        # 立刻开始下一轮，不等待 —— 让"最新一张"始终有效


if __name__ == "__main__":
    raise SystemExit(main())
