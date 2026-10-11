"""探测 deepseek-flash 的**上下文窗口**与输出上限（两个不同的量）。

区分清楚：
  · max_tokens      = 单次**输出**上限（我们客户端封顶 65536，服务端 393216）
  · context window  = 输入 + 输出 **合计**上限 ← 本次要测的

探测方法（省 token）：发一个极小的 prompt，把 max_tokens 设成候选值。
  · 若 prompt + max_tokens 超过上下文窗口 → 服务端拒绝
  · 被接受 → 说明该值不超过上下文窗口
每次只花几十 token。

⚠️ 实测踩过的环境坑：`no_proxy` 里若含**带方括号**的 IPv6 字面量（如 `[::1]`，
某些工具链会自动生成），httpx2 解析会抛 `InvalidURL: Invalid port: ':1]'`，
**客户端连构造都失败**。所以这里先做一次净化。裸写法 `::1` 没问题。
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


def sanitize_no_proxy() -> str:
    """去掉 `no_proxy` 里带方括号的 IPv6 字面量。

    返回净化后的值；`no_proxy` 与 `NO_PROXY` 都设置，避免大小写不一致。
    """
    raw = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    keep = [p for p in parts if not (p.startswith("[") and p.endswith("]"))]
    dropped = [p for p in parts if p not in keep]
    value = ",".join(keep)
    if value:
        os.environ["no_proxy"] = value
        os.environ["NO_PROXY"] = value
    else:
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
    if dropped:
        print(f"⚠️  已从 no_proxy 移除无法解析的条目：{dropped}")
    return value


sanitize_no_proxy()

from openai import OpenAI  # noqa: E402

cli = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
    timeout=90,
)
model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
print(f"模型：{model}\n")


def probe(mt: int) -> tuple[bool, str]:
    try:
        cli.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=mt,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:400]


print("=== 逐级试探上下文窗口 ===")
print("（max_tokens 被接受 → prompt + max_tokens 未超过上下文窗口）\n")
accepted = []
for mt in (393_216, 500_000, 700_000, 1_000_000, 1_048_576, 2_000_000):
    ok, msg = probe(mt)
    print(f"  max_tokens={mt:>10,}  {'✅ 接受' if ok else '❌ 拒绝'}")
    if ok:
        accepted.append(mt)
    else:
        print(f"      {msg}")
        break

if accepted:
    print(f"\n  可接受的最大值：{max(accepted):,}")
    print(f"  → 上下文窗口 ≥ {max(accepted):,} tok（prompt 另占几 token）")
