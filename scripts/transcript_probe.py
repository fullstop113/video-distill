"""P1 取数层的最小闭环探针：BV → 逐字稿（含全部校验）。

这一步是方案 §5.2③ 的验收点：**绝不产出空文件**。
它把三种结果严格区分开，且每种都有明确的用户可读结论：
    ok            → 拿到了带时间戳的逐字稿
    no_subtitle   → 该视频确实没站内字幕 → 需要 ASR 兜底
    not_logged_in → 无法判定（未登录/无权限）→ 必须报错，不许静默走过

用法：
    python scripts/transcript_probe.py BV1xxxxxxxxx
    python scripts/transcript_probe.py BV1xxxxxxxxx --out output/xxx.md
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台默认 GBK，输不出 emoji —— 强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from vd.bilibili import (  # noqa: E402
    BiliClient,
    NoSubtitleError,
    NotLoggedInError,
)
from vd.credentials import CredentialPolicyError, load_operator_credentials  # noqa: E402
from vd.disclaimer import artifact_header  # noqa: E402
from vd.quota import check, commit  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("transcript")

MIN_TRANSCRIPT_CHARS = 80  # 逐字稿长度下限（方案 §5.2③）


def ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description="BV → 逐字稿探针")
    ap.add_argument("bvid", help="视频 BV 号")
    ap.add_argument("--out", default="", help="输出 Markdown 路径（默认 output/<bvid>.md）")
    ap.add_argument("--no-quota", action="store_true", help="跳过配额检查（仅本机调试用）")
    args = ap.parse_args()

    try:
        creds = load_operator_credentials()
    except CredentialPolicyError as exc:
        print(f"❌ 凭据策略拒绝：{exc}")
        return 2
    client = BiliClient(creds)

    print(f"[凭证] 操作号 UID={creds.uid} uname={creds.uname} "
          f"（管理员={'是' if creds.is_owner else '否'}；大号凭据不参与取数）")

    info = client.video_info(args.bvid)
    print(f"[视频] {info.title}")
    print(f"       UP={info.owner_name} 时长={ts(info.duration)} "
          f"({info.duration // 60} 分钟)")

    if not args.no_quota:
        status = check(creds.uid, planned_seconds=info.duration)
        print(f"[配额] {status.describe()}")
        if not status.allowed:
            print(f"❌ 拒绝受理：{status.reason}")
            return 3

    try:
        tracks = client.subtitle_tracks(info)
    except NotLoggedInError as exc:
        print(f"❌ 无法判定字幕情况：{exc}")
        print("   → 按 R1，这**不是**「无字幕」，不允许当作无字幕继续处理。")
        return 4
    except NoSubtitleError as exc:
        print(f"➖ 该视频确实没有站内字幕：{exc}")
        print("   → 应转入 ASR 兜底（P1 的 FunASR 路径），此处探针到此为止。")
        return 5

    manual = [t for t in tracks if not t.lan.startswith("ai-")]
    track = manual[0] if manual else tracks[0]
    print(f"[字幕] {track.lan_doc or track.lan}（{'AI 生成' if track.is_ai else '人工/UP 提交'}）"
          f"，共 {len(tracks)} 条轨道")

    cues = client.fetch_cues(track)
    text = "".join(c.get("content", "") for c in cues)
    if len(text) < MIN_TRANSCRIPT_CHARS:
        print(f"❌ 字幕正文过短（{len(text)} 字 < 下限 {MIN_TRANSCRIPT_CHARS} 字）→ 判为失败，"
              "不产出文件（方案 §5.2③：宁可报错，不要给用户一个空文件）。")
        return 6

    print(f"[校验] 通过：{len(cues)} 条字幕，{len(text)} 字，"
          f"覆盖 {ts(cues[-1].get('to', 0))}")

    out = Path(args.out) if args.out else ROOT / "output" / f"{args.bvid}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    source_desc = (
        f"B 站站内字幕（{track.lan_doc or track.lan}，"
        f"{'AI 生成' if track.is_ai else 'UP/人工'}）"
    )
    lines = [
        f"# {info.title}",
        "",
        # 免责声明必须在产物里，而不是只写在文档里（单一事实来源见 vd.disclaimer）
        artifact_header(source_desc, info.bvid),
        "",
        f"- BV：{info.bvid}   UP：{info.owner_name}   时长：{ts(info.duration)}",
        f"- 来源：{source_desc}",
        f"- 链接：https://www.bilibili.com/video/{info.bvid}",
        "",
        "## 逐字稿",
        "",
    ]
    for cue in cues:
        lines.append(f"**[{ts(cue.get('from', 0))}]** {cue.get('content', '').strip()}")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[产物] 已写入 {out.relative_to(ROOT)}")

    if not args.no_quota:
        commit(creds.uid, info.bvid, info.duration)
        print(f"[配额] 已记账：{check(creds.uid).describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
