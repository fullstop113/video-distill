"""量化"去掉时间戳"到底省了多少 —— 用真实逐字稿向服务端要权威计数。

## 为什么值得量

实测（`_measure_prompt.py`）发现：**时间戳占了 prompt 的 62%**。
  · 逐字稿正文 12692 字 ≈ 6981 token
  · 渲染后（含 `[HH:MM:SS]`）≈ 18561 token
  · 差值 11580 token 全是时间戳

如果 prompt 里不再需要时间戳，输入 token 能直接砍掉一大半。
这个脚本把"带时间戳"与"不带时间戳"的实际 token 数对比出来，
而且**同时对比两种渲染的总长度**，用于确认结论。
"""

from __future__ import annotations

import os
import re
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

from vd.summarize import sanitize_no_proxy  # noqa: E402

sanitize_no_proxy()

from openai import OpenAI  # noqa: E402

from vd.summarize import SYSTEM_SINGLE, render_for_prompt  # noqa: E402
from vd.transcript import Segment  # noqa: E402

cli = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
    timeout=120,
)
model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()

# 用真实逐字稿
src = ROOT / "output" / "BV1Q3pe6GERF.md"
pat = re.compile(r"^\*\*\[(\d{2}):(\d{2}):(\d{2})\]\*\*\s*(.*)$")
segs: list[Segment] = []
for ln in src.read_text(encoding="utf-8").splitlines():
    m = pat.match(ln.strip())
    if m:
        st = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        segs.append(Segment(start=float(st), end=float(st) + 1.0, text=m.group(4)))

chars = sum(len(s.text) for s in segs)
print(f"样本：{src.name}")
print(f"  {len(segs):,} 段 / 正文 {chars:,} 字 / "
      f"{segs[-1].end / 60:.1f} 分钟\n")


def count(system: str, user: str) -> int:
    r = cli.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=8,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return r.usage.prompt_tokens


with_ts = render_for_prompt(segs, with_timestamps=True)
without_ts = render_for_prompt(segs, with_timestamps=False)

print("=== 渲染后的文本长度 ===")
print(f"  带时间戳：{len(with_ts):,} 字符")
print(f"  不带    ：{len(without_ts):,} 字符")
print(f"  差值    ：{len(with_ts) - len(without_ts):,} 字符"
      f"（占带时间戳版本的 {(len(with_ts) - len(without_ts)) / len(with_ts) * 100:.0f}%）\n")

print("=== 服务端权威 token 计数（同一条系统提示） ===")
tok_with = count(SYSTEM_SINGLE, f"标题：测试\n\n逐字稿：\n{with_ts}")
tok_without = count(SYSTEM_SINGLE, f"标题：测试\n\n逐字稿：\n{without_ts}")

print(f"  带时间戳：{tok_with:,} token")
print(f"  不带    ：{tok_without:,} token")
saved = tok_with - tok_without
print(f"  **省下  ：{saved:,} token（{saved / tok_with * 100:.0f}%）**\n")

print("=== 换算成实际成本（deepseek-flash） ===")
# 实测价目见 docs/p2-summarize-layer.md
print(f"  这次输入减少 {saved:,} token")
print(f"  约合每次总结省下 ¥{saved / 1_000_000 * 2:.4f}（按输入 ¥2/百万 token 估）")
print("  注：**输出侧不受影响** —— 要点本身不带时间戳，")
print("      所以去掉时间戳只省输入，不省输出。")
print("      而输出 token 才是成本大头（实测输出约占总成本 80%）。")
