"""部署自检：确认 VD_PUBLIC_BASE_URL 配对、中转路由可从公网访问。

## 为什么需要这个脚本

云 ASR 依赖"阿里云能访问到我们托管的音频"。配错的表现是
一个含糊的 `FILE_403_FORBIDDEN`，很难定位到底是：
  · 没配 VD_PUBLIC_BASE_URL
  · 配了内网 IP / 127.0.0.1
  · 服务没监听 0.0.0.0
  · 安全组没放行

这个脚本把四件事分别验掉，**问题出在哪一步一眼可见**。

用法：
    python scripts/check_deploy.py                 # 只查配置与本地
    python scripts/check_deploy.py --remote        # 额外从公网角度探测
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

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

ok = True


def check(name: str, passed: bool, detail: str = "", hint: str = "") -> None:
    global ok
    ok = ok and passed
    print(("  ✅ " if passed else "  ❌ ") + name + (f"  {detail}" if detail else ""))
    if not passed and hint:
        print(f"      → {hint}")


def is_private_host(host: str) -> tuple[bool, str]:
    """判断主机名/IP 是否是**公网不可达**的地址。"""
    if host in ("localhost",):
        return True, "localhost"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False, "域名"          # 域名交给 DNS，这里不判
    if ip.is_loopback:
        return True, "回环地址"
    if ip.is_private:
        return True, "内网地址（10./172.16-31./192.168.）"
    if ip.is_link_local:
        return True, "链路本地地址"
    return False, "公网 IP"


print("=" * 66)
print("一、配置检查")
print("=" * 66)

base = os.getenv("VD_PUBLIC_BASE_URL", "").strip().rstrip("/")
check("VD_PUBLIC_BASE_URL 已配置", bool(base),
      f"= {base}" if base else "（空）",
      "在 .env 里填服务器公网地址，例如 http://123.45.67.89:8788")

if base:
    parsed = urlparse(base)
    check("地址含协议头（http:// 或 https://）",
          parsed.scheme in ("http", "https"),
          f"scheme={parsed.scheme or '（无）'}",
          "要写成 http://1.2.3.4:8788 这种完整形式")
    check("地址不含路径", parsed.path in ("", "/"),
          f"path={parsed.path!r}",
          "只填到根，不要带 /relay 之类的路径")
    if parsed.hostname:
        private, kind = is_private_host(parsed.hostname)
        check(f"主机 {parsed.hostname} 是公网可达地址", not private,
              f"（{kind}）",
              "阿里云访问不到内网/回环地址，必须用公网 IP 或域名")
    if parsed.scheme == "http":
        print("      ⚠️  用的是 http：中转 URL 会以明文经过网络。")
        print("         个人自用可接受；对外开放建议上 https。")

print()
print("=" * 66)
print("二、本地服务检查")
print("=" * 66)

port = 8788
if base:
    p = urlparse(base)
    if p.port:
        port = p.port

# 服务是否在跑、监听在哪个地址
listening_any = False
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        listening_any = s.connect_ex(("127.0.0.1", port)) == 0
except OSError:
    pass
check(f"本地 {port} 端口有服务在听", listening_any, "",
      "先启动：python scripts/serve.py --host 0.0.0.0 --port 8788")

# 关键：必须监听 0.0.0.0 才能被外网访问
host = "127.0.0.1"
if base:
    host = urlparse(base).hostname or "127.0.0.1"

if listening_any:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                    timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
        check("/healthz 正常", r.status == 200, body[:90])
    except Exception as exc:  # noqa: BLE001
        check("/healthz 可访问", False, f"{type(exc).__name__}: {exc}")

    # 假 token 必须 404 —— 同时验"路由存在"和"不泄露"
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/relay/deadbeefdeadbeef", timeout=5)
        check("假 token 返回 404", False, "竟然返回了 200",
              "中转路由可能在做危险的事，检查 relay_audio 的实现")
    except urllib.error.HTTPError as exc:
        check("假 token 返回 404", exc.code == 404, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        check("假 token 请求可达", False, f"{type(exc).__name__}: {exc}")

print()
print("=" * 66)
print("三、从公网角度探测（-remote 才做）")
print("=" * 66)
if not base:
    print("  跳过（未配置 VD_PUBLIC_BASE_URL）")
elif not listening_any:
    print("  跳过（本地服务没起来）")
else:
    for path, label in (("/healthz", "健康检查"),
                        ("/relay/deadbeefdeadbeef", "中转路由")):
        url = f"{base}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception as exc:  # noqa: BLE001
            check(f"公网访问 {label}", False,
                  f"{type(exc).__name__}: {str(exc)[:80]}",
                  "99% 是安全组没放行；其次是服务没监听 0.0.0.0")
            continue
        if path == "/healthz":
            check(f"公网访问 {label}", code == 200, f"HTTP {code}",
                  "服务没监听 0.0.0.0，或安全组没放行")
        else:
            check(f"公网访问 {label}（假 token 应 404）", code == 404,
                  f"HTTP {code}")

print()
print("=" * 66)
print(("全部通过 ✅  云 ASR 可以工作了" if ok else "存在问题 ❌  见上面的 → 提示"))
print("=" * 66)
if not ok:
    print("\n常见原因速查：")
    print("  · 没配 VD_PUBLIC_BASE_URL        → 填公网地址")
    print("  · 填了内网 IP / 127.0.0.1        → 换成公网 IP")
    print("  · 服务只听 127.0.0.1             → 加 --host 0.0.0.0")
    print("  · 安全组没放行端口               → 云控制台加入站规则")
    print("\n详见 docs/deploy.md")
sys.exit(0 if ok else 1)
