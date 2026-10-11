"""探测**上下文窗口**（输入+输出合计上限）—— 用大输入去撞。

与 `probe_context.py` 的区别：
  · 那个用 max_tokens 试探，只能证明"输出上限 ≥ X"；
  · 这个用**大 prompt** 试探，才能真正撞到上下文窗口。
    超窗时服务端在**处理输入阶段**就拒绝，不会产生输出费用 —— 所以相对便宜。

⚠️ 同样要先净化 `no_proxy`（详见 probe_context.py 的说明）。
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


def sanitize_no_proxy() -> None:
    raw = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    keep = [p.strip() for p in raw.split(",")
            if p.strip() and not (p.strip().startswith("[") and p.strip().endswith("]"))]
    if keep:
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = ",".join(keep)
    else:
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)


sanitize_no_proxy()

from openai import OpenAI  # noqa: E402

cli = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
    timeout=180,
)
model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()

# 中文填充：1 字 ≈ 0.55 tok，所以 1 tok ≈ 1.82 字
CHARS_PER_TOK = 1 / 0.55
FILLER = "这是一段用于探测上下文窗口的填充文本，内容本身没有意义。"


def probe(target_tokens: int) -> tuple[str, int, str]:
    """返回 (状态, 实际 prompt_tokens, 说明)。"""
    n = int(target_tokens * CHARS_PER_TOK)
    text = (FILLER * (n // len(FILLER) + 1))[:n]
    try:
        r = cli.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": text + "\n\n只回复：好"}],
            max_tokens=8,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:  # noqa: BLE001
        return "拒绝", 0, str(exc)[:320]
    return "接受", r.usage.prompt_tokens, ""


print(f"模型：{model}")
print("每级用大 prompt 去撞窗口；超窗会在输入阶段被拒绝，不产生输出费用。\n")
print(f"{'目标输入tok':>12s} {'实际prompt_tok':>15s} {'结果':>6s}")
print("-" * 40)
ceiling = None
for target in (50_000, 200_000, 400_000, 600_000, 900_000, 1_100_000):
    st, actual, msg = probe(target)
    shown = f"{actual:,}" if actual else "—"
    print(f"{target:>12,} {shown:>15s} {st:>6s}")
    if st == "拒绝":
        ceiling = target
        print(f"    {msg}")
        break

if ceiling:
    print(f"\n上下文窗口落在 {ceiling:,} 以下（上一级仍被接受）")
else:
    print("\n所有档位都被接受，窗口比探测上限还大")
