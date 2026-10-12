"""探测 deepseek-v4-flash 是否接受**音频输入**。

方法：
  1. 查 /models 列出可用模型；
  2. 分别用 OpenAI 的两种音频内容格式试：
     · input_audio（OpenAI 标准）
     · 直接把音频塞进 content 数组
  3. 看服务端是"明确拒绝"还是"接受但忽略"。

⚠️ 关键：**"不报错"不等于"能处理"**。所以不能只看有没有异常，
   还要看返回内容是否真的与音频有关 —— 否则会得到一个"看起来能用"
   的错误结论（这正是 R1 静默失败那个坑的形态）。
"""

from __future__ import annotations

import base64
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

print("=== 1. 可用模型列表 ===")
try:
    models = cli.models.list()
    for m in models.data:
        print(f"  · {m.id}")
except Exception as exc:  # noqa: BLE001
    print(f"  查询失败：{type(exc).__name__}: {str(exc)[:200]}")

# 造一小段静音 WAV 作为测试音频
import io as _io
import struct
import wave

buf = _io.BytesIO()
with wave.open(buf, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))
audio_b64 = base64.b64encode(buf.getvalue()).decode()
print(f"\n测试音频：1 秒静音 WAV，base64 {len(audio_b64):,} 字符")

print("\n=== 2. 试 input_audio 格式 ===")
for fmt in ("wav", "mp3"):
    try:
        r = cli.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "这段音频里说了什么？"},
                    {"type": "input_audio",
                     "input_audio": {"data": audio_b64, "format": fmt}},
                ],
            }],
            max_tokens=64,
            extra_body={"thinking": {"type": "disabled"}},
        )
        print(f"  format={fmt}: ⚠️ 未报错，返回："
              f"{(r.choices[0].message.content or '')[:120]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  format={fmt}: ❌ {type(exc).__name__}: {str(exc)[:220]}")

print("\n=== 3. 试把音频当普通文本字段塞（探测是否有隐藏支持） ===")
try:
    r = cli.chat.completions.create(
        model=model,
        messages=[{"role": "user",
                   "content": f"[audio]{audio_b64[:200]}[/audio] 转写这段音频"}],
        max_tokens=64,
        extra_body={"thinking": {"type": "disabled"}},
    )
    print(f"  返回：{(r.choices[0].message.content or '')[:160]!r}")
except Exception as exc:  # noqa: BLE001
    print(f"  ❌ {type(exc).__name__}: {str(exc)[:220]}")

print("\n=== 4. 结论判据 ===")
print("  · 若 input_audio 被明确拒绝（400 参数错）→ 不支持音频输入")
print("  · 若'接受'但回答与音频无关 → 也是不支持（只是被静默忽略）")
print("  · 判断依据不是有没有异常，而是**返回内容是否真的来自音频**")
