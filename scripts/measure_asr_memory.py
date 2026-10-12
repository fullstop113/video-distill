"""实测 ASR 在 CPU 上的**峰值内存占用** —— 决定腾讯云最低配能否跑得动。

2核2G / 4核4G 这类档位，内存通常先于 CPU 成为瓶颈。
FunASR 的 paraformer-large + VAD + 标点 三个模型都要常驻内存。

用法：
    python scripts/measure_asr_memory.py            # 测 CPU
    python scripts/measure_asr_memory.py --device cuda
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from vd.asr import load_model  # noqa: E402


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> tuple[float, float]:
    """返回 (当前工作集 MB, 峰值工作集 MB)。

    ⚠️ 必须显式声明 restype/argtypes：ctypes 默认把返回值当 `c_int`（32 位），
    而 `GetCurrentProcess()` 返回的是 64 位伪句柄，会被截断成无效值，
    于是 `GetProcessMemoryInfo` 失败、读数全是 0（踩过，白跑一轮）。
    """
    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL

    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    ok = psapi.GetProcessMemoryInfo(
        k32.GetCurrentProcess(), ctypes.byref(c), c.cb
    )
    if not ok:
        raise OSError(f"GetProcessMemoryInfo 失败，err={ctypes.get_last_error()}")
    return c.WorkingSetSize / 1024 / 1024, c.PeakWorkingSetSize / 1024 / 1024


def make_wav(path: Path, seconds: float, sr: int = 16000) -> None:
    frames = []
    n = int(seconds * sr)
    for i in range(n):
        t = i / sr
        f0 = 120 + 70 * math.sin(2 * math.pi * 0.7 * t)
        v = sum(a * math.sin(2 * math.pi * f0 * k * t)
                for k, a in ((1, 0.6), (2, 0.3), (3, 0.15), (5, 0.08)))
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t)
        frames.append(int(max(-1.0, min(1.0, v * env * 0.25)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<" + "h" * len(frames), *frames))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--seconds", type=float, default=60.0)
    args = ap.parse_args()

    work_tmp = ROOT / "data" / "tmp"
    work_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = str(work_tmp)

    base_cur, base_peak = rss_mb()
    print(f"设备：{args.device}")
    print(f"加载模型前：当前 {base_cur:.0f} MB，峰值 {base_peak:.0f} MB\n")

    t0 = time.time()
    model = load_model(device=args.device)
    load_s = time.time() - t0
    after_load_cur, after_load_peak = rss_mb()
    print(f"模型加载完成（{load_s:.1f}s）")
    print(f"  当前 {after_load_cur:.0f} MB，峰值 {after_load_peak:.0f} MB")
    print(f"  → 模型常驻约 {after_load_cur - base_cur:.0f} MB\n")

    wav = work_tmp / "mem-test.wav"
    try:
        make_wav(wav, args.seconds)
        t1 = time.time()
        model.generate(input=str(wav), batch_size_s=300, sentence_timestamp=True)
        asr_s = time.time() - t1
        cur, peak = rss_mb()
        print(f"转写 {args.seconds:.0f} 秒音频用时 {asr_s:.2f}s")
        print(f"  当前 {cur:.0f} MB，**峰值 {peak:.0f} MB**")
        print(f"\n=== 结论 ===")
        print(f"  进程峰值内存 ≈ **{peak / 1024:.1f} GB**")
        for cfg, gb in (("2核2G", 2), ("2核4G", 4), ("4核4G", 4), ("4核8G", 8)):
            ok = peak / 1024 < gb * 0.75
            print(f"    {cfg:8s} {'✅ 够（留了 25% 余量）' if ok else '❌ 不够 / 太紧'}")
    finally:
        wav.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
