"""P1 CLI：一个视频 → 逐字稿产物。

这是取数层的正式入口（探针脚本仍保留，用于分层排查）。

用法：
    python scripts/distill.py BV1xxxxxxxxx                 # 仅站内字幕（含 AI 字幕）
    python scripts/distill.py BV1xxxxxxxxx --fresh         # 忽略缓存
    python scripts/distill.py BV1xxx BV1yyy --quiet        # 批量
"""

from __future__ import annotations

import argparse
import logging
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

from vd import cache  # noqa: E402
from vd.bilibili import BiliClient, NotLoggedInError  # noqa: E402
from vd.credentials import CredentialPolicyError, load_operator_credentials  # noqa: E402
from vd.quota import check, commit  # noqa: E402
from vd.transcript import (  # noqa: E402
    NoTranscriptError,
    fetch_transcript,
    to_markdown,
    ts,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("distill")


def main() -> int:
    ap = argparse.ArgumentParser(description="BV → 逐字稿")
    ap.add_argument("bvid", nargs="+", help="一个或多个 BV 号")
    ap.add_argument("--out-dir", default=str(ROOT / "output"))
    ap.add_argument("--fresh", action="store_true", help="忽略缓存")
    ap.add_argument("--no-quota", action="store_true", help="跳过配额检查（仅调试）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        creds = load_operator_credentials()
    except CredentialPolicyError as exc:
        print(f"❌ 凭据策略拒绝：{exc}")
        return 2
    client = BiliClient(creds)
    if not args.quiet:
        print(f"[凭证] 操作号 UID={creds.uid} uname={creds.uname}"
              f"（管理员={'是' if creds.is_owner else '否'}；大号凭据不参与取数）")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok_count = 0
    failures: list[tuple[str, str]] = []

    for bvid in args.bvid:
        print(f"\n===== {bvid} =====")
        try:
            # 配额：在**取数之前**判定，避免先烧带宽/GPU 再拒绝
            if not args.no_quota:
                info_pre = client.video_info(bvid)
                st = check(creds.uid, planned_seconds=info_pre.duration)
                print(f"[配额] {st.describe()}")
                if not st.allowed:
                    failures.append((bvid, st.reason))
                    print(f"❌ 拒绝受理：{st.reason}")
                    continue

            t0 = time.time()
            transcript = fetch_transcript(
                client,
                bvid,
                use_cache=not args.fresh,
                progress=None if args.quiet else (lambda m: print(f"  · {m}")),
            )
            elapsed = time.time() - t0

            out = out_dir / f"{bvid}.md"
            out.write_text(to_markdown(transcript), encoding="utf-8")

            print(f"[路线] {transcript.source.label}")
            print(f"[产物] {out.relative_to(ROOT)}　"
                  f"{transcript.char_count} 字 / {len(transcript.segments)} 段 / "
                  f"覆盖 {ts(transcript.duration_covered)}")
            print(f"[耗时] {elapsed:.1f}s")
            for note in transcript.notes:
                print(f"  · {note}")

            if not args.no_quota:
                commit(creds.uid, bvid, transcript.info.duration)
            ok_count += 1

        except NotLoggedInError as exc:
            # R1：这不是"没有字幕"，必须显式失败
            msg = f"未登录/无权限，无法判定字幕情况：{exc}"
            failures.append((bvid, msg))
            print(f"❌ {msg}")
        except NoTranscriptError as exc:
            failures.append((bvid, str(exc)))
            print(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((bvid, f"{type(exc).__name__}: {exc}"))
            print(f"❌ 未预期错误 {type(exc).__name__}: {exc}")

    print(f"\n{'=' * 60}")
    print(f"成功 {ok_count} / {len(args.bvid)}")
    if failures:
        print("失败明细：")
        for bvid, why in failures:
            print(f"  - {bvid}: {why}")
    used = cache.stats()
    if used:
        print(f"缓存条目：{used}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
