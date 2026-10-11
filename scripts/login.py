"""P0 探针：扫码登录 + 取数路线判定。

⚠️ 本项目规定：**取数只能用操作号（小号）的凭据**，大号凭据不得用于取数。
   所以正常情况下你应该用**小号**扫码，并把它的 UID 填进 `.env` 的
   `VD_OPERATOR_BILI_UID`。大号只需要 UID（填 `VD_OWNER_BILI_UID`/`VD_ADMIN_UIDS`）
   用于识别管理员身份，**不需要、也不应该登录到这台机器上**。

   若你误用大号登录，凭据会落盘但不会被用于取数（白名单会拒绝），
   此时脚本会提示你用 `--logout` 清理。

用法：
    python scripts/login.py                # 终端显示二维码，扫码后用加密凭证落盘
    python scripts/login.py --status       # 只查看已有凭证与登录态
    python scripts/login.py --logout       # 吊销凭证（一键登出）

失败时**明确报错**，不静默降级为匿名（R1）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台默认 GBK，输不出 emoji / 二维码半块字符 —— 强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from vd.bilibili import BiliClient  # noqa: E402
from vd.login import (  # noqa: E402
    is_logged_in,
    persist,
    poll_until_done,
    qr_ascii,
    start_qr_login,
)
from vd.credentials import (  # noqa: E402
    admin_uids,
    operator_uid,
    owner_uid,
)
from vd.storage import delete_credentials, list_uids, load_credentials  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("login")


def show_status() -> int:
    op = operator_uid()
    print(f"管理员集合 = {sorted(admin_uids()) or '（未配置）'}")
    print(f"操作号（唯一允许取数） = {op or '（未配置）'}")
    print(f"大号 UID（仅用于配额） = {owner_uid() or '（未配置）'}\n")

    uids = list_uids()
    if not uids:
        print("尚无任何已保存的凭证。运行 `python scripts/login.py` 扫码登录。")
        return 1
    for uid in uids:
        creds = load_credentials(uid)
        client = BiliClient(creds)
        role = "操作号（允许取数）" if uid == op else "⚠️ 非操作号（取数时会被拒绝）"
        try:
            nav = client.nav()
            logged = nav.get("isLogin")
            uname = nav.get("uname", "")
            vip = (nav.get("vipStatus") or 0)
            print(
                f"UID={uid} uname={uname or creds.uname} isLogin={logged} "
                f"vip={vip} 登录于={creds.logged_in_at} "
                f"管理员={'是' if uid in admin_uids() else '否'} → {role}"
            )
            if not logged:
                print("  ⚠️ 凭证存在但服务端判定未登录，说明 SESSDATA 已失效，需重新扫码。")
        except Exception as exc:  # noqa: BLE001
            print(f"UID={uid} 校验失败：{type(exc).__name__}: {exc}")
    return 0


def do_login() -> int:
    qr = start_qr_login()
    print("\n请用 B 站手机客户端扫描下面的二维码：\n")
    try:
        print(qr_ascii(qr.url))
    except ImportError:
        print(f"（未安装 qrcode，改为手工打开）\n{qr.url}")
    print()

    def on_state(state, message):
        if state.value == "86090":
            print("已扫码，请在手机上点击确认…")

    if not poll_until_done(qr, on_state=on_state):
        print("❌ 二维码已失效或超时，请重新运行。")
        return 2
    if not is_logged_in(qr):
        print("❌ 轮询返回成功，但会话中没有 SESSDATA —— 拒绝保存不完整的凭证。")
        return 3

    creds = persist(qr)
    # 用 nav 补全昵称，并确认这套 Cookie 真的能通过服务端校验
    client = BiliClient(creds)
    nav = client.nav()
    if not nav.get("isLogin"):
        print("❌ 凭证已保存，但 nav 判定未登录，请重试。")
        return 4
    creds.uname = nav.get("uname", "")
    saved = persist(qr, uname=creds.uname)
    uid = saved.uid

    print(f"\n✅ 登录成功：{nav.get('uname')} (UID={uid})")
    print(f"   凭证已加密保存于 data/credentials/{uid}.enc")

    op = operator_uid()
    admins = admin_uids()
    if uid in admins:
        print(f"   身份：管理员（{sorted(admins)}）→ 该账号配额不限。")
    else:
        print("   身份：非管理员 → 按访客计（每日 3 次 / 累计 ≤3 小时）。")

    if not op:
        print(
            f"\n   ⚠️ 尚未配置操作号。取数需要它 —— 请把 UID={uid} 填进 .env：\n"
            f"      VD_OPERATOR_BILI_UID={uid}\n"
            "   （前提是**这个账号就是你打算用来承担抓取风险的小号**）"
        )
    elif uid == op:
        print("   ✅ 与 VD_OPERATOR_BILI_UID 一致 → 这是允许取数的操作号。")
    else:
        print(
            f"\n   ⚠️ 注意：本机已保存 UID={uid} 的凭据，但它**不是**操作号"
            f"（操作号={op}）。\n"
            "      取数时白名单会拒绝它，因此它不会被用于自动抓取。\n"
            "      若这就是你的大号，建议立刻清理，避免大号凭据留在服务器上：\n"
            f"        python scripts/login.py --logout   # 然后重新用小号登录"
        )
    return 0


def do_logout() -> int:
    uids = list_uids()
    if not uids:
        print("没有可吊销的凭证。")
        return 1
    for uid in uids:
        delete_credentials(uid)
        print(f"已吊销 UID={uid} 的凭证。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="B 站扫码登录（P0 探针）")
    ap.add_argument("--status", action="store_true", help="查看已有凭证与登录态")
    ap.add_argument("--logout", action="store_true", help="吊销所有本地凭证")
    args = ap.parse_args()
    if args.status:
        return show_status()
    if args.logout:
        return do_logout()
    return do_login()


if __name__ == "__main__":
    raise SystemExit(main())
