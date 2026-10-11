"""P2 前置探针：DeepSeek 凭证与模型名实测。

存在的理由：方案文档里写的模型名是 `deepseek-chat`，但官方价目页当前只列出
`deepseek-flash` / `deepseek-v4-pro`。**模型名写错的话 P2 会直接 400**，
所以这件事必须在写总结层之前用真实请求钉死，而不是照抄文档。
"""

from __future__ import annotations

import os
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

BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
KEY = os.getenv("DEEPSEEK_API_KEY", "")
CONFIGURED = os.getenv("DEEPSEEK_MODEL", "")

CANDIDATES = ["deepseek-flash", "deepseek-chat", "deepseek-v4-pro", "deepseek-reasoner"]


def main() -> int:
    if not KEY:
        print("❌ 未配置 DEEPSEEK_API_KEY")
        return 2
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

    print(f"BASE={BASE}")
    print(f".env 里配置的模型名 = {CONFIGURED!r}\n")

    print("【1】余额查询（同时验证 key 有效）")
    try:
        r = requests.get(f"{BASE}/user/balance", headers=headers, timeout=20)
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            print(f"  is_available = {d.get('is_available')}")
            for b in d.get("balance_infos") or []:
                print(f"  余额 {b.get('currency')} {b.get('total_balance')} "
                      f"(赠送 {b.get('granted_balance')} / 充值 {b.get('topped_up_balance')})")
        else:
            print(f"  {r.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  请求失败：{type(exc).__name__}: {exc}")

    print("\n【2】逐个模型名实测（发一次最小 chat 请求）")
    ok_models = []
    for model in CANDIDATES:
        try:
            r = requests.post(
                f"{BASE}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "只回复两个字：可用"}],
                    "max_tokens": 8,
                    "stream": False,
                },
                timeout=60,
            )
            if r.status_code == 200:
                d = r.json()
                text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
                served = d.get("model", "")
                usage = d.get("usage") or {}
                print(f"  ✅ {model:20s} → 实际服务模型={served!r} 回复={text.strip()!r} "
                      f"tokens={usage.get('prompt_tokens')}+{usage.get('completion_tokens')}")
                ok_models.append(model)
            else:
                body = r.text.replace("\n", " ")[:160]
                print(f"  ❌ {model:20s} → HTTP {r.status_code}  {body}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {model:20s} → {type(exc).__name__}: {exc}")

    print("\n【3】结论")
    if not ok_models:
        print("  ❌ 没有任何模型名可用 —— 检查 key 与账户状态")
        return 1
    if CONFIGURED and CONFIGURED not in ok_models:
        print(f"  ⚠️ .env 里的 {CONFIGURED!r} **不可用**，必须改成：{ok_models[0]!r}")
        return 3
    print(f"  可用模型：{ok_models}")
    print(f"  .env 配置 {CONFIGURED!r} 可用" if CONFIGURED in ok_models else "  建议改用上表可用模型")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
