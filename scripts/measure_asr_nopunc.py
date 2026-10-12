"""决定性问题：**去掉标点模型**能否把峰值内存压到 4 GB 档位？

动机：标点模型单独占 1.6 GB（峰值 5.3 → 3.6 GB）。
如果服务器上不加载它，改用 DeepSeek 给逐字稿加标点，
就可能从 4核8G（450元/年）降到 4核4G（38元/年），省 412 元/年。

但 3.6 GB 离 4 GB 只差 0.4 GB —— 系统本身还要占 0.5~1 GB，
所以**必须精确量**，不能估算。

本脚本用真实语音、稳态转写，量两种配置的峰值内存与 RTF。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = r'''
import ctypes, ctypes.wintypes as wt, json, os, sys, time
from pathlib import Path
ROOT = Path(r"{root}")
sys.path.insert(0, str(ROOT / "src"))
WITH_PUNC = {with_punc}
CLIP = r"{clip}"

raw = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
keep = [p.strip() for p in raw.split(",")
        if p.strip() and not (p.strip().startswith("[") and p.strip().endswith("]"))]
if keep:
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = ",".join(keep)
else:
    os.environ.pop("no_proxy", None); os.environ.pop("NO_PROXY", None)

class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]
def rss():
    k32 = ctypes.windll.kernel32; ps = ctypes.windll.psapi
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    ps.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), wt.DWORD]
    ps.GetProcessMemoryInfo.restype = wt.BOOL
    c = PMC(); c.cb = ctypes.sizeof(c)
    ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 1048576, c.PeakWorkingSetSize / 1048576

from funasr import AutoModel
kw = dict(model="paraformer-zh", vad_model="fsmn-vad", device="cpu",
          disable_update=True, hub="ms", cache_dir=str(ROOT / "data" / "models"))
if WITH_PUNC:
    kw["punc_model"] = "ct-punc"
t0 = time.time()
m = AutoModel(**kw)
load_s = time.time() - t0
rss_after_load = rss()[0]

# 连续两次，取第二次为稳态
times, segs = [], 0
for i in range(2):
    t1 = time.time()
    r = m.generate(input=CLIP, batch_size_s=300, sentence_timestamp=True)
    dt = time.time() - t1
    item = (r[0] if r else {{}}) or {{}}
    segs = len(item.get("sentence_info") or [])
    if i == 1:
        times.append(dt)
out = dict(with_punc=WITH_PUNC, load_s=round(load_s, 1),
           rss_after_load_mb=round(rss_after_load),
           peak_mb=round(rss()[1]), steady_s=round(times[0], 2),
           rtf=round(times[0] / 60.0, 4), segments=segs)
print("RESULT" + json.dumps(out))
'''


def main() -> int:
    clip = ROOT / "data" / "tmp" / "clip0.wav"
    if not clip.exists():
        raise SystemExit(f"缺少 {clip}（先跑 bench_asr_steady.py 生成）")

    print("真实语音 60 秒 · 稳态转写 · 独立子进程测峰值内存\n")
    print(f"{'配置':26s} {'加载后MB':>9s} {'峰值MB':>8s} {'稳态s':>7s} "
          f"{'RTF':>7s} {'段落':>5s}")
    print("-" * 70)
    results = {}
    for with_punc, label in ((True, "带标点模型（当前）"),
                             (False, "去标点模型")):
        proc = subprocess.run(
            [sys.executable, "-c",
             CHILD.format(root=str(ROOT), with_punc=with_punc, clip=str(clip))],
            capture_output=True, text=True, timeout=3600,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        line = next((x for x in (proc.stdout or "").splitlines()
                     if x.startswith("RESULT")), "")
        if not line:
            tail = ((proc.stderr or "").strip().splitlines() or [""])[-1]
            print(f"{label:26s} ❌ {tail[:40]}")
            continue
        d = json.loads(line[6:])
        results[with_punc] = d
        print(f"{label:26s} {d['rss_after_load_mb']:>9,} {d['peak_mb']:>8,} "
              f"{d['steady_s']:>7.2f} {d['rtf']:>7.4f} {d['segments']:>5}")

    if len(results) == 2:
        a, b = results[True], results[False]
        print(f"\n去掉标点模型：峰值内存 {a['peak_mb']:,} → {b['peak_mb']:,} MB "
              f"（省 {a['peak_mb'] - b['peak_mb']:,} MB）")
        print(f"              RTF {a['rtf']:.4f} → {b['rtf']:.4f}")
        print("\n=== 落到哪个腾讯云档位（系统预留 800 MB）===")
        for cfg, gb in (("4核4G（38元/年）", 4), ("4核8G（450元/年）", 8)):
            avail = gb * 1024 - 800
            for tag, d in (("带标点", a), ("去标点", b)):
                ok = d["peak_mb"] < avail
                print(f"  {cfg:18s} {tag:6s} 峰值 {d['peak_mb']:>6,} MB / "
                      f"可用 {avail:>5,} MB  {'✅ 可行' if ok else '❌ 不够'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
