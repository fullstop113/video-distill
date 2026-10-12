"""确认：经隧道访问时，"本机免登录"逻辑**不会**被误触发。

## 为什么必须验

web.py 里有一条便利逻辑：来自 `127.0.0.1` / `::1` / `localhost` 的请求
**免登录**（`is_local()`）——因为凭据就在本机磁盘上，登录只是仪式感。

但隧道会让请求看起来来自别处。如果这条判定被绕过（比如误信了某个
可伪造的头），**任何人都能通过隧道直接拿到管理员权限**。

这是安全边界，必须在开隧道**之前**验掉，而不是之后。

本测试直接构造不同来源的请求，看 `current_session()` 是否按预期拒绝。
"""

from __future__ import annotations

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

from fastapi.testclient import TestClient  # noqa: E402

from vd import web  # noqa: E402

print("=" * 66)
print("is_local() 判定范围")
print("=" * 66)
print(f"  LOCAL_HOSTS = {sorted(web.LOCAL_HOSTS)}")
print(f"  trust_lan() = {web.trust_lan()}  "
      f"（VD_WEB_TRUST_LAN={web.os.getenv('VD_WEB_TRUST_LAN', '（未设）')}）")

print()
print("=" * 66)
print("不同来源的会话判定（关键：非本机必须要求登录）")
print("=" * 66)


class FakeReq:
    """只带 client 信息的最小请求替身。"""

    def __init__(self, host: str, headers: dict | None = None,
                 cookies: dict | None = None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}
        self.cookies = cookies or {}


cases = [
    ("127.0.0.1", "本机回环", True),
    ("::1", "本机 IPv6 回环", True),
    ("localhost", "本机 localhost", True),
    ("203.0.113.7", "公网 IP（模拟隧道来源）", False),
    ("10.0.0.5", "内网其它机器", False),
    ("172.16.3.9", "内网其它机器", False),
]

ok = True
for host, label, expect_local in cases:
    req = FakeReq(host)
    local = web.is_local(req)
    sess = web.current_session(req)
    allowed = sess is not None
    flag = "✅" if allowed == expect_local else "❌"
    if allowed != expect_local:
        ok = False
    print(f"  {flag} {host:16s} {label:22s} is_local={local!s:5s} "
          f"拿到会话={allowed}")

print()
print("=" * 66)
print("伪造 Host 头能否骗过判定")
print("=" * 66)
# Host 头是客户端可控的，**绝不能**用来判断来源
for fake_host in ("127.0.0.1", "localhost", "127.0.0.1:8788"):
    req = FakeReq("203.0.113.7", headers={"host": fake_host})
    sess = web.current_session(req)
    got = sess is not None
    flag = "✅" if not got else "❌ 被伪造成本机了！"
    if got:
        ok = False
    print(f"  {flag} 来源=203.0.113.7  Host 头伪造为 {fake_host!r:18s} → "
          f"拿到会话={got}")

print()
print("=" * 66)
print("⚠️  一个便利逻辑的副作用（我要提醒你）")
print("=" * 66)
print("  is_local() 只认**对端地址**，不认 Host 头 —— 这是对的。")
print()
print("  但注意：如果你用 `--host 0.0.0.0` 启动，并且**从本机浏览器**访问")
print("  公网地址或隧道地址，那么……")
print("    · 数据包出去再回来，对端地址仍是 127.0.0.1（本机）")
print("    · 于是**仍会免登录**")
print("  这是设计上的取舍（本人本机使用免登录），但在对外开放时值得知道。")

print()
print("=" * 66)
print(("安全边界正常 ✅  可以开隧道" if ok else "存在问题 ❌  先别开隧道"))
print("=" * 66)
sys.exit(0 if ok else 1)
