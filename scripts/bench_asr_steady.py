"""热启动基准：模型只加载一次，连续转写多段，得到**纯推理**吞吐。

## 为什么必须这样做

之前两次测量都不可信：
  · 合成正弦信号 → VAD 判无语音 → 推理被跳过，"RTF 0.0794"是无意义的空跑；
  · 用 `transcribe_wav` 单次调用 → 含 ~25-30s 模型加载，
    CPU 50.6s vs GPU 47.6s 几乎相同，说明测的是加载不是推理。

正确做法：加载一次，跑多段真实语音，用**稳态**耗时算 RTF。
这才是"一台服务器连续处理视频"的真实成本。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
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

from vd.asr import load_model  # noqa: E402

SRC = ROOT / "data" / "audio" / "BV1Yapc6SEZ5.full.16k.wav"
TMP = ROOT / "data" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)
os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = str(TMP)

# 从真实样本切出 3 段各 60 秒（首段/中段/尾段，覆盖不同内容）
from vd.asr import ffmpeg_exe  # noqa: E402

CLIPS: list[Path] = []
for i, start in enumerate((0, 120, 240)):
    out = TMP / f"clip{i}.wav"
    subprocess.run(
        [ffmpeg_exe(), "-y", "-ss", str(start), "-t", "60", "-i", str(SRC),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace",
    )
    if out.exists():
        CLIPS.append(out)
print(f"切出 {len(CLIPS)} 段真实语音（各 60 秒）\n")

device = sys.argv[1] if len(sys.argv) > 1 else "cpu"
print(f"=== 设备 {device} ===")
t0 = time.time()
model = load_model(device=device)
load_s = time.time() - t0
print(f"模型加载：{load_s:.1f}s（只发生一次，服务常驻可忽略）\n")

print(f"{'轮次':>4s} {'音频':>6s} {'耗时':>8s} {'RTF':>8s} {'段落':>5s}")
print("-" * 36)
steady: list[float] = []
for rnd in range(2):                      # 跑两轮，第一轮算预热
    for idx, clip in enumerate(CLIPS):
        t1 = time.time()
        r = model.generate(input=str(clip), batch_size_s=300,
                           sentence_timestamp=True)
        dt = time.time() - t1
        item = (r[0] if r else {}) or {}
        n_seg = len(item.get("sentence_info") or [])
        tag = f"{rnd + 1}-{idx + 1}"
        print(f"{tag:>4s} {60:>5.0f}s {dt:>7.2f}s {dt / 60:>8.4f} {n_seg:>5}")
        if rnd > 0:
            steady.append(dt)

if steady:
    avg = sum(steady) / len(steady)
    print(f"\n=== 稳态结果（第 2 轮，{len(steady)} 段） ===")
    print(f"  平均 RTF：**{avg / 60:.4f}**")
    print(f"  104 分钟视频预计：**{avg / 60 * 104.3:.0f} 分钟**")
    print(f"  3 小时视频预计：{avg / 60 * 180:.0f} 分钟")
