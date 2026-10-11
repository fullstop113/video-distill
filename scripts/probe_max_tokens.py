"""探测 DeepSeek API 的 max_tokens 真实上限。

做法：用一个极小的请求，把 max_tokens 设成明显超出范围的值，
让服务端**直接报错并告诉我合法范围** —— 这比逐级试探省 token。
如果服务端不报错，就退化为二分探测。

同时读回 usage，确认模型名与上下文能力。
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

from openai import OpenAI  # noqa: E402

key = os.getenv("DEEPSEEK_API_KEY", "").strip()
base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip()
model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
print(f"模型 {model!r}  base_url {base!r}  key {'已配置' if key else '未配置'}\n")

if not key:
    raise SystemExit("未配置 DEEPSEEK_API_KEY")

cli = OpenAI(api_key=key, base_url=base, timeout=60)


def try_call(mt: int, note: str = "") -> tuple[bool, str, int]:
    """返回 (是否成功, 说明, 实际输出 token 数)。"""
    try:
        r = cli.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "只回复两个字：好的"}],
            max_tokens=mt,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:300]}", 0
    u = r.usage
    return True, note, (u.completion_tokens if u else 0)


print("=== 1. 用一个明显超限的值让服务端报出合法范围 ===")
for probe in (100_000_000, 1_000_000, 200_000):
    ok, msg, _ = try_call(probe, f"max_tokens={probe}")
    print(f"  max_tokens={probe:>10,}  {'成功' if ok else '失败'}")
    print(f"      {msg[:280]}")
    if not ok:
        break

print("\n=== 2. 常规值对照 ===")
for probe in (8192, 16384, 32768, 65536):
    ok, msg, used = try_call(probe, "")
    print(f"  max_tokens={probe:>7,}  {'✅ 接受' if ok else '❌ 拒绝'}"
          f"{f'（实际输出 {used} tok）' if ok else ''}")
    if not ok:
        print(f"      {msg[:200]}")
