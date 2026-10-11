"""P2 CLI：逐字稿 → 总结 + 要点（带时间戳锚点）。

用法：
    python scripts/summarize.py BV1xxxxxxxxx                    # 取数 + 总结（一条龙）
    python scripts/summarize.py --from-output BV1xxxxxxxxx      # 只用已有产物，不重新取数
    python scripts/summarize.py BV1xxx --strategy single        # 强制单次
    python scripts/summarize.py BV1xxx BV1yyy --quiet           # 批量
"""

from __future__ import annotations

import argparse
import logging
import re
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

from vd.bilibili import BiliClient, NotLoggedInError, VideoInfo  # noqa: E402
from vd.credentials import CredentialPolicyError, load_operator_credentials  # noqa: E402
from vd.summarize import (  # noqa: E402
    DeepSeekClient,
    SummarizeError,
    summarize,
    to_markdown,
)
from vd.transcript import (  # noqa: E402
    NoTranscriptError,
    Segment,
    Transcript,
    TranscriptSource,
    fetch_transcript,
    to_markdown as transcript_to_markdown,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("summarize")

TS_RE = re.compile(r"^\*\*\[(\d{2}:\d{2}:\d{2})\]\*\*\s*(.*)$")


def load_transcript_from_output(path: Path, bvid: str) -> Transcript:
    """从已生成的逐字稿 Markdown 还原 Transcript（避免重复取数）。

    只解析我们自己生成的格式（`**[HH:MM:SS]** 正文`），
    解析不出内容就明确报错，不返回空的半成品。
    """
    segments: list[Segment] = []
    title = bvid
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and title == bvid:
            title = line[2:].strip()
        m = TS_RE.match(line.strip())
        if m:
            label, text = m.group(1), m.group(2).strip()
            if not text:
                continue
            h, mi, s = (int(x) for x in label.split(":"))
            start = h * 3600 + mi * 60 + s
            segments.append(Segment(start=float(start), end=float(start), text=text))
    if len(segments) < 3:
        raise SummarizeError(
            f"{path.name} 里只解析出 {len(segments)} 段逐字稿，不像是本工具生成的产物。"
        )
    # 补齐 end：用下一段的 start，最后一段加 5 秒
    for i, seg in enumerate(segments):
        seg.end = segments[i + 1].start if i + 1 < len(segments) else seg.start + 5.0
    return Transcript(
        bvid=bvid, title=title, source=TranscriptSource.SUBTITLE_CACHE,
        segments=segments,
        info=VideoInfo(bvid=bvid, cid=0, title=title, duration=int(segments[-1].end)),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="逐字稿 → 总结与要点")
    ap.add_argument("bvid", nargs="+")
    ap.add_argument("--out-dir", default=str(ROOT / "output"))
    ap.add_argument("--from-output", action="store_true",
                    help="复用已有的 output/<bvid>.md，不重新取数（省时间与配额）")
    ap.add_argument("--strategy", choices=["auto", "single", "map_reduce"], default="auto")
    ap.add_argument("--chunk-chars", type=int, default=9000)
    ap.add_argument("--single-pass-chars", type=int, default=60000)
    ap.add_argument("--model", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        client_llm = DeepSeekClient(model=args.model)
    except SummarizeError as exc:
        print(f"❌ {exc}")
        return 2
    if not args.quiet:
        print(f"[模型] {client_llm.model} @ {client_llm.base_url}（思考模式已关闭）")

    client = None
    creds = None
    if not args.from_output:
        try:
            creds = load_operator_credentials()
        except CredentialPolicyError as exc:
            print(f"❌ 凭据策略拒绝：{exc}")
            return 2
        client = BiliClient(creds)

    ok = 0
    failures: list[tuple[str, str]] = []
    total_cost = 0.0

    for bvid in args.bvid:
        print(f"\n===== {bvid} =====")
        try:
            # 1) 逐字稿
            t0 = time.time()
            if args.from_output:
                src = out_dir / f"{bvid}.md"
                if not src.exists():
                    raise SummarizeError(f"找不到 {src}，请先运行 distill.py")
                transcript = load_transcript_from_output(src, bvid)
                print(f"[逐字稿] 复用产物 {src.name}（{len(transcript.segments)} 段）")
            else:
                transcript = fetch_transcript(
                    client, bvid,
                    progress=None if args.quiet else (lambda m: print(f"  · {m}")),
                )
                print(f"[逐字稿] {transcript.source.label}　{transcript.char_count} 字 / "
                      f"{len(transcript.segments)} 段　{time.time() - t0:.1f}s")

            # 2) 总结
            t1 = time.time()
            summary = summarize(
                transcript, client=client_llm, strategy=args.strategy,
                chunk_chars=args.chunk_chars, single_pass_chars=args.single_pass_chars,
                progress=None if args.quiet else (lambda m: print(f"  · {m}")),
            )
            total_cost += summary.cost_cny

            # 3) 落盘：总结与逐字稿各自一个文件，互不覆盖
            out = out_dir / f"{bvid}.summary.md"
            out.write_text(to_markdown(summary), encoding="utf-8")

            anchored = sum(1 for p in summary.key_points if p.anchors)
            print(f"[策略] {'单次全文' if summary.strategy.value == 'single' else '分片 map-reduce'}"
                  f"　{summary.usage.calls} 次调用　{time.time() - t1:.1f}s")
            print(f"[要点] {len(summary.key_points)} 条，其中 {anchored} 条带时间戳锚点")
            print(f"[成本] ¥{summary.cost_cny:.4f}"
                  f"（输入 {summary.usage.prompt_tokens} / 输出 {summary.usage.completion_tokens}）")
            print(f"[产物] {out.relative_to(ROOT)}")
            for note in summary.notes:
                print(f"  · {note}")
            ok += 1

        except NotLoggedInError as exc:
            failures.append((bvid, f"未登录/无权限：{exc}"))
            print(f"❌ 未登录/无权限：{exc}")
        except (NoTranscriptError, SummarizeError) as exc:
            failures.append((bvid, str(exc)))
            print(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((bvid, f"{type(exc).__name__}: {exc}"))
            print(f"❌ 未预期错误 {type(exc).__name__}: {exc}")

    print(f"\n{'=' * 60}")
    print(f"成功 {ok} / {len(args.bvid)}　累计成本 ¥{total_cost:.4f}")
    if failures:
        print("失败明细：")
        for bvid, why in failures:
            print(f"  - {bvid}: {why}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
