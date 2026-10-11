"""对照实验：同一个真实长视频，旧上限 8192 vs 新预算，看哪个能完成。

这是对"调大 max_tokens 就能保证任务完成"的**直接验证**，
而不是只看代码推断。用真实逐字稿、真实 API、真实 prompt。

用法：
    python scripts/verify_budget_fix.py            # 用最长的那份逐字稿
    python scripts/verify_budget_fix.py BV1xxxx    # 指定
"""

from __future__ import annotations

import json
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

from vd.summarize import (  # noqa: E402
    SYSTEM_SINGLE,
    DeepSeekClient,
    SummarizeError,
    _salvage_truncated_json,
    output_budget,
)


def extract_body(p: Path) -> str:
    txt = p.read_text(encoding="utf-8")
    parts = re.split(r"^##+ .*$", txt, flags=re.M)
    return (max(parts, key=len) if parts else txt).strip()


cands = []
for p in (ROOT / "output").glob("*.md"):
    if ".summary." in p.name:
        continue
    body = extract_body(p)
    if len(body) > 3000:
        cands.append((len(body), p, body))
if not cands:
    raise SystemExit("找不到足够长的逐字稿")

if len(sys.argv) > 1:
    want = sys.argv[1]
    cands = [c for c in cands if want in c[1].name] or cands
n_chars, src, body = max(cands)

print(f"样本：{src.name}")
print(f"正文 {n_chars:,} 字符 ≈ {n_chars / 403:.0f} 分钟视频")
print(f"旧上限 8,192  → 新预算 {output_budget(n_chars):,}\n")

user = f"标题：对照实验\n\n带时间戳的逐字稿：\n{body}"
cli = DeepSeekClient()


def run(label: str, mt: int) -> tuple[bool, int, int]:
    """返回 (是否拿到可用 JSON, 输出 token, 输出字符数)。"""
    try:
        data, u = cli.json_call(SYSTEM_SINGLE, user, max_tokens=mt, retries=1)
    except SummarizeError as exc:
        msg = str(exc)
        m = re.search(r"已输出 (\d+) 字符", msg) or re.search(r"全文 (\d+) 字符", msg)
        out_chars = int(m.group(1)) if m else 0
        print(f"  {label}（max_tokens={mt:,}）→ ❌ 失败")
        print(f"      {msg[:220]}")
        return False, 0, out_chars
    pts = data.get("key_points") or []
    print(f"  {label}（max_tokens={mt:,}）→ ✅ 成功：{len(pts)} 条要点，"
          f"输出 {u.completion_tokens:,} tok"
          f"{'（曾截断后抢救）' if u.truncated else ''}")
    return True, u.completion_tokens, 0


print("=== 对照实验 ===")
ok_old, tok_old, chars_old = run("旧上限", 8192)
print()
ok_new, tok_new, _ = run("新预算", output_budget(n_chars))

print("\n=== 结论 ===")
if ok_new and not ok_old:
    print(f"  ✅ 修复有效：旧上限失败，新预算成功（用了 {tok_new:,} tok）")
    print(f"     旧上限在 {chars_old:,} 字符处被截断")
elif ok_new and ok_old:
    print(f"  两者都成功（旧 {tok_old:,} tok / 新 {tok_new:,} tok）——"
          f"这份样本还没到旧上限的边界")
else:
    print("  ⚠️ 新预算也失败，需要进一步排查")
