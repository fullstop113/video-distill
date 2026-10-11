"""登录路径探测：找出哪条路在本机真的可用。

背景：Web 端扫码登录连续失败（App 提示"二维码已过期"，而服务端日志显示
从未收到扫码事件）。于是逐条实测替代路径，不靠猜。

探测项：
  A. 扫码登录（已知可用，作为基线）
  B. Web 短信登录 —— 需要极验人机验证
  C. TV 端二维码登录 —— 手机输入 6 位码，不用 App 扫码
  D. 浏览器里是否已有 B 站登录态（读 Cookie）
"""

from __future__ import annotations

import json
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

import requests  # noqa: E402

from vd.bilibili import REFERER, UA  # noqa: E402

H = {"User-Agent": UA, "Referer": REFERER}


def show(title: str, resp) -> dict:
    print(f"\n--- {title} ---")
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        print(f"  HTTP {resp.status_code}  非 JSON：{resp.text[:180]}")
        return {}
    print(f"  HTTP {resp.status_code}  code={data.get('code')}  message={data.get('message')!r}")
    keys = list((data.get('data') or {}).keys()) if isinstance(data.get('data'), dict) else data.get('data')
    print(f"  data 字段：{keys}")
    return data


def main() -> int:
    s = requests.Session()
    s.headers.update(H)
    results: dict[str, str] = {}

    # 基线：Web 扫码登录（已知可用）
    try:
        d = show(
            "A. Web 扫码登录 生成",
            s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate", timeout=15),
        )
        ok = d.get("code") == 0 and (d.get("data") or {}).get("qrcode_key")
        results["A. Web 扫码登录"] = "✅ 可用" if ok else "❌ 不可用"
    except Exception as exc:  # noqa: BLE001
        results["A. Web 扫码登录"] = f"❌ {type(exc).__name__}"
        print(f"  异常：{exc}")

    # B. Web 短信登录：先看 captcha 接口要不要极验
    try:
        d = show(
            "B. Web 短信登录 申请 captcha",
            s.get(
                "https://passport.bilibili.com/x/passport-login/captcha",
                params={"source": "main_web"},
                timeout=15,
            ),
        )
        dd = d.get("data") or {}
        gt = (dd.get("geetest") or {}).get("gt")
        results["B. Web 短信登录"] = (
            f"⚠️ 可用但需极验人机验证（gt={gt}）" if gt else "❌ 结构不符"
        )
    except Exception as exc:  # noqa: BLE001
        results["B. Web 短信登录"] = f"❌ {type(exc).__name__}"
        print(f"  异常：{exc}")

    # C. TV 端登录：探测三个候选端点
    tv_endpoints = [
        ("TV auth_code",
         "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"),
        ("TV qrcode generate",
         "https://passport.bilibili.com/x/passport-tv-login/qrcode/generate"),
        ("TV login (app)",
         "https://passport.bilibili.com/x/passport-tv-login/login"),
    ]
    tv_ok = False
    for name, url in tv_endpoints:
        try:
            r = s.post(url, data={"appkey": "4409e2ce8ffd12b8",
                                  "appsec": "59b43e04ad6965f34319062b478f83dd",
                                  "local_id": "0", "ts": "0"},
                       timeout=15)
            d = r.json()
            print(f"\n--- C. {name} ---")
            print(f"  POST HTTP {r.status_code}  code={d.get('code')}  message={d.get('message')!r}")
            if d.get("code") == 0:
                print(f"  data 字段：{list((d.get('data') or {}).keys())}")
                tv_ok = True
            # 也试 GET
            r2 = s.get(url, timeout=15)
            d2 = r2.json()
            print(f"  GET  HTTP {r2.status_code}  code={d2.get('code')}  message={d2.get('message')!r}")
            if d2.get("code") == 0:
                print(f"  data 字段：{list((d2.get('data') or {}).keys())}")
                tv_ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- C. {name} ---\n  异常：{type(exc).__name__}: {exc}")
    results["C. TV 端登录"] = "✅ 接口可用" if tv_ok else "⚠️ 未确认（见上）"

    # D. 浏览器里是否已有 B 站登录态
    print("\n--- D. 浏览器 Cookie 探测 ---")
    found = []
    for browser, paths in {
        "Edge": [Path.home() / "AppData/Local/Microsoft/Edge/User Data"],
        "Chrome": [Path.home() / "AppData/Local/Google/Chrome/User Data"],
        "Firefox": [Path.home() / "AppData/Roaming/Mozilla/Firefox/Profiles"],
    }.items():
        for p in paths:
            if p.exists():
                cookies = list(p.rglob("Cookies")) + list(p.rglob("cookies.sqlite"))
                found.append(f"{browser}: {len(cookies)} 个 cookie 库 @ {p}")
    if found:
        for f in found:
            print(f"  ✅ {f}")
        results["D. 浏览器 Cookie"] = "✅ 检测到浏览器配置（可尝试读取）"
    else:
        print("  未检测到受支持的浏览器配置")
        results["D. 浏览器 Cookie"] = "❌ 未检测到"

    print("\n" + "=" * 66)
    print("登录路径实测结论")
    print("=" * 66)
    for k, v in results.items():
        print(f"  {k:22s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
