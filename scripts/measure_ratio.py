"""测量中文逐字稿的 token/字符比。

做法：把**真实逐字稿**发给 API，从 usage.prompt_tokens 反推比例。
这比查文档可靠 —— 不同 tokenizer 对中文的比例差异很大。

同时测量"总结产物的输出 token / 输出字符"比，用于估算 max_tokens 够不够。
"""

from __future__ import annotations

import json
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

from vd.transcript import Transcript  # noqa: E402

key = os.getenv("DEEPSEEK_API_KEY", "").strip()
base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
cli = OpenAI(api_key=key, base_url=base, timeout=120)

# 找一份真实逐字稿。
# ⚠️ 第一版按文件大小挑，结果挑中的文件正文只有 19 字符（只有声明头），
# 于是"2000 字符"样本实际是空串，比例算出来是 0.005 —— 完全错的。
# 改成：解析出**真实正文**，并按正文长度挑最大的那份。
def extract_body(p: Path) -> str:
    txt = p.read_text(encoding="utf-8")
    # 逐字稿用 "## 正文" 之类的标题分段；取最后一个二级标题之后的内容
    parts = re.split(r"^##+ .*$", txt, flags=re.M)
    body = max(parts, key=len) if parts else txt
    return body.strip()


import re  # noqa: E402

cands = sorted((ROOT / "output").glob("*.md"))
docs = [p for p in cands if ".summary." not in p.name]
pairs = [(len(extract_body(p)), p) for p in docs]
pairs = [x for x in pairs if x[0] > 500]
if not pairs:
    raise SystemExit(f"找不到有实质正文的逐字稿，候选：{[(n, p.name) for n, p in pairs]}")
n_chars, src = max(pairs)
body = extract_body(src)
print(f"样本：{src.name}  正文 {len(body)} 字符\n")

print("=== 1. 输入侧：中文逐字稿的 token/字符比 ===")
for n in (2000, 5000, len(body)):
    sample = body[:n]
    try:
        r = cli.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": sample + "\n\n只回复：好"}],
            max_tokens=8,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  {n:6d} 字符 → 调用失败：{type(exc).__name__}: {str(exc)[:120]}")
        continue
    pt = r.usage.prompt_tokens
    # 减去固定样板开销（"只回复：好" + 结构开销约 10 token）
    ratio = (pt - 10) / n
    print(f"  {n:6d} 字符 → prompt_tokens={pt:6d}  比例 {ratio:.3f} tok/字")

print("\n=== 2. 输出侧：总结产物的 token/字符比 ===")
# 用一份已有产物估算：总结 markdown 的字符数 vs 当时记录的输出 token
# （输出 token 没有落盘，这里用 API 对同样文本做一次测量作为近似）
sums = [p for p in cands if ".summary." in p.name and ".old." not in p.name]
if sums:
    s = max(sums, key=lambda p: p.stat().st_size)
    t = s.read_text(encoding="utf-8")
    print(f"  样本产物：{s.name}  {len(t)} 字符")
    r = cli.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": t + "\n\n只回复：好"}],
        max_tokens=8,
        extra_body={"thinking": {"type": "disabled"}},
    )
    pt = r.usage.prompt_tokens
    print(f"  → {pt} tok，输出侧比例约 {(pt - 10) / len(t):.3f} tok/字")
