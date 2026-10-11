"""验证关键时间轴：每个时间点是否真的落在它声称的事件上。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BVID = sys.argv[1] if len(sys.argv) > 1 else "BV1dFem67EYt"
summary = (ROOT / "output" / f"{BVID}.summary.md").read_text(encoding="utf-8")
transcript = (ROOT / "output" / f"{BVID}.md").read_text(encoding="utf-8")

seg = {}
for m in re.finditer(r"\*\*\[(\d{2}):(\d{2}):(\d{2})\]\*\*\s*(.*)", transcript):
    h, mi, s = map(int, m.groups()[:3])
    seg[h * 3600 + mi * 60 + s] = m.group(4).strip()


def sec(t):
    a, b, c = (int(x) for x in t.split(":"))
    return a * 3600 + b * 60 + c


block = summary.split("## 关键时间轴", 1)[1].split("## 要点", 1)[0]
items = re.findall(r"- `(\d{2}:\d{2}:\d{2})` (.+?)\s*　", block)

print(f"=== 关键时间轴逐条核对（{len(items)} 条）===\n")
for label, text in items:
    t = sec(label)
    # 取该时间点前后 20 秒的原文，人工判断是否支持这个短语
    window = [(k, v) for k, v in sorted(seg.items()) if t - 3 <= k <= t + 22]
    print(f"[{label}] {text}")
    if not window:
        print("   ⚠️ 该时间点附近没有逐字稿内容")
    else:
        for k, v in window[:4]:
            mark = "→" if abs(k - t) <= 3 else " "
            print(f"   {mark} [{k // 3600:02d}:{k % 3600 // 60:02d}:{k % 60:02d}] {v[:62]}")
    print()
