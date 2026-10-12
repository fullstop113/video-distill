"""探测 content 里 `file` 类型的具体限制（可能是文档理解，不是音频）。

服务端报错列出了允许的变体：text / image_url / file。
前两个用途明确，`file` 需要问清楚：支持什么格式？大小上限？
""" 

from __future__ import annotations

import base64
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
    timeout=120,
)
model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()


def try_content(label: str, part: dict) -> None:
    try:
        r = cli.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": [{"type": "text", "text": "这是什么？"}, part]}],
            max_tokens=48,
            extra_body={"thinking": {"type": "disabled"}},
        )
        out = (r.choices[0].message.content or "").strip()[:150]
        print(f"  {label:34s} ⚠️ 未报错 → {out!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:34s} ❌ {type(exc).__name__}: {str(exc)[:260]}")


print("=== `file` 类型的各种写法 ===")

# 1) 字符串 URL
try_content("file: 字符串 URL", {"type": "file",
                                "file": "https://example.com/a.pdf"})
# 2) 对象 + file_data（OpenAI 文档格式）
try_content("file: {file_data: base64}", {
    "type": "file",
    "file": {"file_data": "data:application/pdf;base64,JVBERi0xLjQK"},
})
# 3) 对象 + filename + file_data
try_content("file: {filename, file_data}", {
    "type": "file",
    "file": {"filename": "a.txt",
             "file_data": "data:text/plain;base64," + base64.b64encode(b"hi").decode()},
})
# 4) 直接给 base64 字符串
try_content("file: 裸 base64 字符串", {
    "type": "file", "file": base64.b64encode(b"hello").decode()})
# 5) 尝试音频 MIME —— 看是否按 MIME 判断
try_content("file: audio/mpeg MIME", {
    "type": "file",
    "file": {"filename": "a.mp3", "file_data": "data:audio/mpeg;base64,SUQz"},
})
