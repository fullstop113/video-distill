"""实测 ASR 在 **CPU** 上的实时率（RTF），用于评估纯 CPU 服务器部署的可行性。

RTF = 转写耗时 / 音频时长。
  · RTF < 1   → 比实时快，能用于在线任务
  · RTF = 0.5 → 104 分钟视频约 52 分钟转写完
  · RTF > 1   → 慢于实时，批量任务会排长队

先用合成语音信号做基准（不需要下载音频，可重复），
再对比 GPU —— 两者比例在同一模型上基本稳定，
所以即使合成信号与真实语音的绝对耗时不同，**CPU/GPU 的比值**仍然有参考价值。

用法：
    python scripts/bench_asr_cpu.py            # CPU 与 GPU 都测
    python scripts/bench_asr_cpu.py --device cpu
"""

from __future__ import annotations

import argparse
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

import math  # noqa: E402

from vd.asr import load_model, pick_device  # noqa: E402


def make_wav(path: Path, seconds: float, sr: int = 16000) -> None:
    """造一段"像语音"的信号：基频随时间起伏 + 共振峰 + 包络。

    纯静音或纯正弦会让 VAD 直接跳过，测不出真实计算量。
    这里让幅度和频率持续变化，使 VAD 判定为"有人说话"。
    """
    frames = []
    n = int(seconds * sr)
    for i in range(n):
        t = i / sr
        # 基频在 90~220 Hz 之间缓慢滑动（模拟语调）
        f0 = 120 + 70 * math.sin(2 * math.pi * 0.7 * t)
        v = 0.0
        for k, amp in ((1, 0.6), (2, 0.3), (3, 0.15), (5, 0.08)):
            v += amp * math.sin(2 * math.pi * f0 * k * t)
        # 音节包络（约 4 Hz 的起伏），让能量有断续，更像说话
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t)
        v *= env * 0.25
        frames.append(int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<" + "h" * len(frames), *frames))


def bench(device: str, seconds: float) -> tuple[float, float]:
    """返回 (加载耗时, 转写耗时)。

    ⚠️ 临时文件必须放在**工作区内**：系统 temp 目录在受限沙箱下不可写，
    实测会抛 `PermissionError: [WinError 5]`。funasr 内部也会写缓存目录，
    所以顺便把 TMP/TEMP 也重定向进来。
    """
    work_tmp = ROOT / "data" / "tmp"
    work_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = str(work_tmp)

    t0 = time.time()
    model = load_model(device=device)
    load_s = time.time() - t0

    wav = work_tmp / f"bench-{device}.wav"
    try:
        make_wav(wav, seconds)
        t1 = time.time()
        model.generate(
            input=str(wav),
            batch_size_s=300,
            sentence_timestamp=True,
        )
        asr_s = time.time() - t1
    finally:
        wav.unlink(missing_ok=True)
    return load_s, asr_s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="")
    ap.add_argument("--seconds", type=float, default=60.0)
    args = ap.parse_args()

    print(f"自动选择设备：{pick_device()}")
    devices = [args.device] if args.device else ["cpu"]
    try:
        import torch

        if torch.cuda.is_available():
            devices.append("cuda")
    except ImportError:
        pass

    print(f"音频长度：{args.seconds:.0f} 秒\n")
    print(f"{'device':>8s} {'加载':>10s} {'转写':>10s} {'RTF':>8s} "
          f"{'104分钟视频预计':>16s}")
    print("-" * 60)
    results = {}
    for dev in devices:
        try:
            load_s, asr_s = bench(dev, args.seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"{dev:>8s} ❌ {type(exc).__name__}: {str(exc)[:70]}")
            continue
        rtf = asr_s / args.seconds
        est = rtf * 6260
        results[dev] = rtf
        print(f"{dev:>8s} {load_s:>9.1f}s {asr_s:>9.2f}s {rtf:>8.4f} "
              f"{est / 60:>14.1f} 分")

    if len(results) == 2:
        print(f"\nCPU/GPU 倍率：{results['cpu'] / results['cuda']:.1f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
