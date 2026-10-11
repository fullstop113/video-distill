"""从本机浏览器读取 B 站登录 Cookie（替代扫码登录）。

## 为什么需要它

Web 端扫码登录在本机反复失败：B 站 App 提示"二维码已过期"，
而服务端日志显示**从未收到扫码事件** —— 说明 App 在本地就拒绝了二维码，
请求根本没发出来。这不是本项目能修的。

而浏览器登录是用户平时就在做的事：在浏览器里登录一次，Cookie 就在本地。
本脚本把它读出来并加密存进项目凭证库，**完全不使用 App 扫码功能**。

## Windows 上 Cookie 加密的原理

Chrome / Edge 把 Cookie 存在 SQLite 里，值用 AES-256-GCM 加密：
  - 密钥在 `User Data/Local State` 的 `os_crypt.encrypted_key`（base64，前缀 DPAPI）
  - 该密钥本身由 **DPAPI**（用户级）保护，只有同一 Windows 用户能解
所以脚本必须在**当前登录用户**下运行，且不需要管理员权限。

⚠️ 已知限制：Chrome 127+ 引入了 App-Bound Encryption，部分新版本读不出来。
   本脚本会把失败原因明确打印，不静默返回空 Cookie。

## 安全说明

- 只读取 `.bilibili.com` 域的 Cookie，不碰其它站点；
- 读到的凭证用项目密钥加密后存入 `data/credentials/`（与扫码登录同一套）；
- 原浏览器 Cookie 不做任何修改或删除；
- 若你不想让脚本读浏览器数据，就不要运行它（这是纯粹的本地操作，无网络请求）。

用法：
    python scripts/login_from_browser.py --list        # 只列出可用浏览器与 profile
    python scripts/login_from_browser.py --browser edge
    python scripts/login_from_browser.py --browser chrome --profile "Profile 1"
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import shutil
import sqlite3
import sys
import tempfile
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

WANTED = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")

BROWSERS = {
    "edge": Path.home() / "AppData/Local/Microsoft/Edge/User Data",
    "chrome": Path.home() / "AppData/Local/Google/Chrome/User Data",
    "brave": Path.home() / "AppData/Local/BraveSoftware/Brave-Browser/User Data",
}


# ---------------- DPAPI ----------------


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    """调用 Windows DPAPI 解密（当前用户级）。"""
    blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"DPAPI 解密失败，GetLastError={ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def master_key(user_data: Path) -> bytes:
    """从 Local State 取出并解密 AES 主密钥。"""
    state = user_data / "Local State"
    if not state.exists():
        raise FileNotFoundError(f"找不到 {state}")
    js = json.loads(state.read_text(encoding="utf-8"))
    enc = js.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        raise KeyError("Local State 里没有 os_crypt.encrypted_key")
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        raise ValueError("encrypted_key 前缀不是 DPAPI，无法处理")
    return dpapi_unprotect(raw[5:])


def decrypt_value(blob: bytes, key: bytes) -> str:
    """解密单条 Cookie 值。"""
    # v10 / v11：AES-256-GCM，nonce 12 字节 + 密文 + tag 16 字节
    if blob[:3] in (b"v10", b"v11"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise RuntimeError("需要 cryptography 包来解密 v10/v11 Cookie") from exc
        nonce, payload = blob[3:15], blob[15:]
        return AESGCM(key).decrypt(nonce, payload, None).decode("utf-8", "replace")
    # 老格式：整段走 DPAPI
    return dpapi_unprotect(blob).decode("utf-8", "replace")


# ---------------- 读取 ----------------


def profiles(user_data: Path) -> list[Path]:
    out = []
    for p in user_data.iterdir():
        if p.is_dir() and ((p / "Network" / "Cookies").exists() or (p / "Cookies").exists()):
            out.append(p)
    return sorted(out)


def read_cookies(profile: Path, key: bytes) -> dict[str, str]:
    src = profile / "Network" / "Cookies"
    if not src.exists():
        src = profile / "Cookies"
    # 浏览器运行时该库被锁，复制一份再读
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    shutil.copy2(src, tmp)
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT name, value, encrypted_value, host_key FROM cookies "
            "WHERE host_key LIKE '%bilibili.com'"
        ).fetchall()
        conn.close()
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    out: dict[str, str] = {}
    for name, value, enc, host in rows:
        if name in WANTED and name not in out:
            if value:
                out[name] = value
            elif enc:
                try:
                    out[name] = decrypt_value(bytes(enc), key)
                except Exception as exc:  # noqa: BLE001
                    print(f"    ⚠️ 解密 {name} 失败（host={host}）：{type(exc).__name__}: {exc}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列出可用浏览器与 profile")
    ap.add_argument("--browser", default="", choices=["", *BROWSERS])
    ap.add_argument("--profile", default="", help="profile 目录名，如 'Default'")
    ap.add_argument("--dry-run", action="store_true", help="只读取并打印，不保存凭证")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("本脚本只支持 Windows（依赖 DPAPI）。")
        return 2

    # 列出可用目标
    avail: list[tuple[str, Path, list[Path]]] = []
    for name, ud in BROWSERS.items():
        if ud.exists():
            profs = profiles(ud)
            if profs:
                avail.append((name, ud, profs))

    print("=== 本机可读的浏览器 ===")
    if not avail:
        print("  未找到任何带 Cookie 库的浏览器 profile")
        return 1
    for name, ud, profs in avail:
        print(f"  {name}: {ud}")
        for p in profs:
            n = 0
            try:
                conn = sqlite3.connect(f"file:{p / 'Network' / 'Cookies'}?mode=ro", uri=True)
                n = conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%bilibili.com'"
                ).fetchone()[0]
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            print(f"      - {p.name:16s} bilibili 相关 Cookie: {n} 条")

    if args.list:
        return 0

    # 选目标
    if args.browser:
        cand = [a for a in avail if a[0] == args.browser]
        if not cand:
            print(f"\n❌ 未找到浏览器 {args.browser}")
            return 1
        name, ud, profs = cand[0]
        target = next((p for p in profs if p.name == args.profile), None) if args.profile else profs[0]
        if target is None:
            print(f"\n❌ 未找到 profile {args.profile}，可选：{[p.name for p in profs]}")
            return 1
    else:
        # 自动挑一个 bilibili Cookie 最多的
        best = None
        for name, ud, profs in avail:
            for p in profs:
                try:
                    db = p / "Network" / "Cookies"
                    if not db.exists():
                        db = p / "Cookies"
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                    n = conn.execute(
                        "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%bilibili.com'"
                    ).fetchone()[0]
                    conn.close()
                except Exception:  # noqa: BLE001
                    n = 0
                if n and (best is None or n > best[3]):
                    best = (name, ud, p, n)
        if best is None:
            print("\n❌ 没有任何浏览器里有 bilibili 的 Cookie。")
            print("   请先在浏览器里登录 B 站（用小号），再运行本脚本。")
            return 1
        name, ud, target, n = best
        print(f"\n自动选择：{name} / {target.name}（{n} 条 bilibili Cookie）")

    print(f"\n=== 解密 {name} / {target.name} ===")
    try:
        key = master_key(ud)
        print(f"  ✅ 取得主密钥（{len(key)} 字节）")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 取主密钥失败：{type(exc).__name__}: {exc}")
        return 1

    cookies = read_cookies(target, key)
    print(f"  读到的关键 Cookie：{sorted(cookies) or '（无）'}")
    missing = [k for k in ("SESSDATA", "DedeUserID") if k not in cookies]
    if missing:
        print(f"\n❌ 缺少必需项：{missing}")
        print("   请确认浏览器里已登录 B 站（域名 www.bilibili.com），且该浏览器已完全退出后重试。")
        print("   注：Chrome 127+ 的 App-Bound Encryption 可能导致读不出来，可改用 Edge。")
        return 1

    print("\n  ✅ 拿到完整登录凭据")
    if args.dry_run:
        print("  （--dry-run，不保存）")
        return 0

    from vd.storage import Credentials, save_credentials

    creds = Credentials(
        uid=cookies["DedeUserID"],
        uname="",
        cookies=cookies,
        logged_in_at=dt.datetime.now().isoformat(timespec="seconds"),
        extra={"source": f"browser:{name}/{target.name}"},
    )
    path = save_credentials(creds)
    print(f"  ✅ 已加密保存：{path.relative_to(ROOT)}")

    # 立刻用 nav 验证这套 Cookie 真的能通过服务端校验
    from vd.bilibili import BiliClient

    nav = BiliClient(creds).nav()
    print(f"  nav 校验：isLogin={nav.get('isLogin')} uname={nav.get('uname')!r}")
    if not nav.get("isLogin"):
        print("  ❌ 服务端判定未登录 —— Cookie 可能已失效，请重新在浏览器登录。")
        return 1
    uid = str(cookies["DedeUserID"])
    uname = nav.get("uname", "")
    save_credentials(Credentials(uid=uid, uname=uname, cookies=cookies,
                                 logged_in_at=creds.logged_in_at, extra=creds.extra))
    print(f"\n✅ 成功：UID={uid} uname={uname}")
    print(f"   如这是你的小号，请把 VD_OPERATOR_BILI_UID={uid} 写进 .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
