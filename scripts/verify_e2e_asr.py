"""端到端验证：真实跑通"无字幕视频 → 云端 ASR"整条链路。

## 这是唯一能证明架构可行的测试

`asr_cloud` 的单元测试全是离线的（假数据、假 HTTP），它们只证明**逻辑正确**，
不证明**真的能用**。三个关键事实只能靠真实调用验证：

  1. 阿里云能否从我们的 `/relay/<token>` 拉到音频
     （它只接受公网 URL，而 B 站链接因 Referer 校验拉不到）
  2. 真实返回的时间戳字段形态（毫秒？句子/词级都有？）
  3. 计费口径 `content_duration` 是否真的短于音频总长

## 前提

  · 服务在跑，且 `VD_PUBLIC_BASE_URL` 指向**公网可达**地址
    （本地开发用临时隧道；生产用服务器公网地址）
  · 已配 `DASHSCOPE_API_KEY`

## 用法

    python scripts/verify_e2e_asr.py                 # 默认用 8 分钟小视频
    python scripts/verify_e2e_asr.py BV1d8p56tEFC    # 指定（104 分钟，约 0.5 元）
    python scripts/verify_e2e_asr.py --dry-run       # 只验证中转可达，不调 ASR
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
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

OK = True


def check(name: str, passed: bool, detail: str = "", hint: str = "") -> None:
    global OK
    OK = OK and bool(passed)
    print(("  ✅ " if passed else "  ❌ ") + name + (f"  {detail}" if detail else ""))
    if not passed and hint:
        print(f"      → {hint}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid", nargs="?", default="BV1NXH661EpG",
                    help="默认用 8 分钟小视频（省成本）")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--dry-run", action="store_true",
                    help="只验证中转 URL 能被外部拉取，不调 ASR（不花钱）")
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    from vd import asr_cloud
    from vd.bilibili import BiliClient
    from vd.credentials import load_operator_credentials

    print("=" * 68)
    print("【0】环境检查")
    print("=" * 68)
    base = asr_cloud.public_base_url()
    check("VD_PUBLIC_BASE_URL 已配置", bool(base), base or "（空）",
          "必须是对阿里云公网可达的地址；本地开发用临时隧道")
    if not base:
        return 1
    check("DASHSCOPE_API_KEY 已配置",
          bool(os.getenv("DASHSCOPE_API_KEY", "").strip()))

    print()
    print("=" * 68)
    print("【1】取视频信息与音频地址")
    print("=" * 68)
    cli = BiliClient(load_operator_credentials())
    info = cli.video_info(args.bvid)
    print(f"  视频：{info.title[:44]}")
    print(f"  时长：{info.duration} 秒 = {info.duration / 60:.1f} 分钟")

    tracks = None
    try:
        tracks = cli.subtitle_tracks(info)
    except Exception as exc:  # noqa: BLE001
        print(f"  字幕：无（{type(exc).__name__}）← 这正是需要云 ASR 的场景")
    if tracks:
        print(f"  ⚠️  该视频**有** {len(tracks)} 条字幕 —— "
              f"生产流程会直接用字幕、不花钱；本脚本仍会走 ASR 以验证链路")

    url, dur = cli.audio_url(info)
    check("音频流可取", bool(url) and dur > 0,
          f"{dur:.0f} 秒",
          "受限视频（充电/付费预览）取不到完整音频")

    print()
    print("=" * 68)
    print("【2】下载音频并发布到中转")
    print("=" * 68)
    from vd.asr import download_audio

    relay_dir = ROOT / "data" / "relay"
    local = relay_dir / f"{args.bvid}.verify.m4s"
    local.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    n = download_audio(cli, url, local)
    dt = time.time() - t0
    check("音频下载完成", local.exists() and local.stat().st_size > 0,
          f"{local.stat().st_size / 1024 / 1024:.1f} MB / {dt:.1f}s")

    ticket = asr_cloud.publish(local, ttl=600)     # 验证期间给 10 分钟
    print(f"  中转 URL：{ticket.public_url}")

    try:
        print()
        print("=" * 68)
        print("【3】中转 URL 形态检查（可达性由第 4 步证明）")
        print("=" * 68)
        check("token 可本地解析", asr_cloud.resolve(ticket.token) is not None)
        # ⚠️ 不要用 HEAD 探测来判断"隧道是否可达"：实测 Cloudflare 快速隧道
        # 对 HEAD 返回 405，把这里判成失败会得出**假阴性**（我第一次跑就中招）。
        # 真正的证明只有一个：阿里云能不能拉到（第 4 步）。
        print("  （跳过 HEAD 探测：隧道对 HEAD 返回 405 属正常，不能当失败判据）")

        if args.dry_run:
            print("\n(--dry-run：跳过 ASR 调用)")
            return 0 if OK else 1

        print()
        print("=" * 68)
        print("【4】提交给阿里云 Paraformer（**真实调用，会产生费用**）")
        print("=" * 68)
        t1 = time.time()
        res = asr_cloud.transcribe_url(
            ticket.public_url, bvid=info.bvid, title=info.title, info=info,
            timeout=args.timeout,
        )
        dt1 = time.time() - t1

        print(f"  ⭐ **阿里云成功拉取并转写** —— 架构可行")
        print(f"     耗时 {dt1:.0f} 秒 · task {res.task_id}")

        print()
        print("=" * 68)
        print("【5】结果核对")
        print("=" * 68)
        segs = res.transcript.segments
        check("拿到逐字稿", len(segs) > 0, f"{len(segs)} 段")
        check("音频总长合理", abs(res.audio_seconds - dur) < max(60, dur * 0.1),
              f"返回 {res.audio_seconds:.0f}s / 视频 {dur:.0f}s")
        check("计费时长 ≤ 音频总长", res.speech_seconds <= res.audio_seconds + 1,
              f"语音 {res.speech_seconds:.0f}s ≤ 音频 {res.audio_seconds:.0f}s")

        price = float(os.getenv("VD_ASR_PRICE_PER_SECOND", "0.00008"))
        print(f"\n  计费口径（语音时长）：{res.speech_seconds:.0f} 秒")
        print(f"  音频总长            ：{res.audio_seconds:.0f} 秒")
        if res.audio_seconds:
            ratio = res.speech_seconds / res.audio_seconds
            print(f"  语音占比            ：{ratio * 100:.0f}%"
                  f"  ← 低于 100% 说明停顿不计费（符合官方说明）")
        print(f"  本次实际费用        ：¥{res.speech_seconds * price:.4f}")

        print("\n  前 3 段（含时间戳，验证毫秒→秒换算）：")
        for s in segs[:3]:
            print(f"    [{s.start:7.2f}s - {s.end:7.2f}s] {s.text[:48]}")
        if len(segs) > 3:
            print(f"    …共 {len(segs)} 段，末段到 {segs[-1].end:.1f}s")
        txt = res.transcript.text if hasattr(res.transcript, "text") else ""
        if txt:
            print(f"\n  转写开头：{txt[:80]}")

        check("时间戳单调递增",
              all(segs[i].start <= segs[i + 1].start for i in range(len(segs) - 1)),
              "否则要点锚点排序会乱")
    finally:
        # 无论成败都要清理：不给公网留文件
        ticket.release()
        asr_cloud.forget(ticket.token)
        print("\n  中转文件已清理")

    print()
    print("=" * 68)
    print(("端到端验证通过 ✅  架构可行" if OK else "存在问题 ❌  见上面的 → 提示"))
    print("=" * 68)
    return 0 if OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
