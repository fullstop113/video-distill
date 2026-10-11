"""P0 关键探针：实测站内字幕命中率。

方案 §6 明确写了这个数字决定 P1 的重心：
  >70% → 字幕为主、ASR 为辅；<30% → ASR 才是主干。

同时这个脚本是 R1「静默失败」陷阱的正面验收：它必须把
「未登录 / 无权限」与「该视频没有字幕」当成两种结果分别计数，
并把「拿到字幕后正文为空」也算失败，绝不让空产物混进命中率里。

用法：
    python scripts/hit_rate.py --n 20                      # 默认策略自动选片
    python scripts/hit_rate.py --n 20 --source history     # 用主人账号的观看历史
    python scripts/hit_rate.py --bvid BV1xx --bvid BV1yy   # 手工指定
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass
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
    BiliError,
    NoSubtitleError,
    NotLoggedInError,
    RiskControlError,
    VideoInfo,
)
from vd.credentials import CredentialPolicyError, load_operator_credentials  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("hit_rate")

RESULT_PATH = ROOT / "data" / "hit_rate_result.json"


@dataclass
class ProbeResult:
    bvid: str
    title: str = ""
    duration: int = 0
    owner: str = ""
    outcome: str = ""          # ok / no_subtitle / not_logged_in / risk / error
    lan: str = ""
    lan_doc: str = ""
    is_ai: bool = False
    cue_count: int = 0
    char_count: int = 0
    note: str = ""


def discover(source: str, n: int, creds) -> list[str]:
    """选片。

    ⚠️ 大号/小号分离带来的直接后果：`history` 取的是**当前凭据账号**的观看历史。
    既然取数只能用操作号（小号），那这里拿到的是**小号的**观看历史，
    而不是你大号的——大号的观看记录不会、也不该被本服务读取。
    想用大号的观看历史选片，只能把 bvid 手工列出来传 `--bvid`。

    因此默认策略是 `ranking`（热门榜），它对"字幕覆盖率"这个问题的代表性也不差。
    """
    client = BiliClient(creds)
    bvids: list[str] = []

    if source == "history":
        try:
            data = client._get_json(
                "https://api.bilibili.com/x/web-interface/history/cursor",
                {"ps": n, "max": 0, "view_at": 0, "business": ""},
            )["data"]
            for item in data.get("list") or []:
                history = item.get("history") or {}
                bvid = history.get("bvid") or item.get("bvid")
                if bvid and history.get("oid"):
                    bvids.append(bvid)
            print(f"[选片] 操作号（小号）的观看历史取得 {len(bvids)} 个视频")
        except (BiliError, NotLoggedInError, RiskControlError) as exc:
            print(f"[选片] 观看历史不可用（{type(exc).__name__}: {exc}）")

    if len(bvids) < n and source in ("auto", "ranking"):
        need = n - len(bvids)
        data = client._get_json(
            "https://api.bilibili.com/x/web-interface/ranking/v2",
            {"rid": 0, "type": "all"},
        )["data"]
        for item in (data.get("list") or [])[:need]:
            bvid = item.get("bvid")
            if bvid and bvid not in bvids:
                bvids.append(bvid)
        print(f"[选片] 热门榜补足到 {len(bvids)} 个视频")

    return bvids[:n]


def probe_one(client: BiliClient, bvid: str) -> ProbeResult:
    res = ProbeResult(bvid=bvid)
    try:
        info: VideoInfo = client.video_info(bvid)
    except Exception as exc:  # noqa: BLE001
        res.outcome = "error"
        res.note = f"元信息失败：{type(exc).__name__}: {exc}"
        return res

    res.title = info.title
    res.duration = info.duration
    res.owner = info.owner_name

    try:
        tracks = client.subtitle_tracks(info)
    except NoSubtitleError:
        res.outcome = "no_subtitle"
        res.note = "确认无站内字幕"
        return res
    except NotLoggedInError as exc:
        res.outcome = "not_logged_in"
        res.note = f"未登录/无权限，无法判定：{exc}"
        return res
    except RiskControlError as exc:
        res.outcome = "risk"
        res.note = str(exc)
        return res

    # 优先人工字幕（lan 不以 ai- 开头），否则取第一条
    manual = [t for t in tracks if not t.lan.startswith("ai-")]
    track = manual[0] if manual else tracks[0]
    res.lan = track.lan
    res.lan_doc = track.lan_doc
    res.is_ai = track.lan.startswith("ai-")

    try:
        cues = client.fetch_cues(track)
    except Exception as exc:  # noqa: BLE001
        res.outcome = "error"
        res.note = f"字幕下载失败：{type(exc).__name__}: {exc}"
        return res

    res.cue_count = len(cues)
    res.char_count = sum(len(c.get("content", "")) for c in cues)

    # 关键校验：拿到轨道但正文为空 = 不算命中，明确报错（方案 §5.2③）
    if res.cue_count == 0 or res.char_count < 20:
        res.outcome = "empty_subtitle"
        res.note = f"字幕轨道存在但正文为空/过短（{res.char_count} 字）"
        return res

    res.outcome = "ok"
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="实测 B 站站内字幕命中率")
    ap.add_argument("--n", type=int, default=20, help="取片数量，默认 20")
    ap.add_argument(
        "--source",
        choices=["auto", "history", "ranking"],
        default="auto",
        help="选片来源。history 指**操作号（小号）**的观看历史，不是大号的",
    )
    ap.add_argument("--bvid", action="append", default=[], help="手工指定 bvid，可重复")
    args = ap.parse_args()

    try:
        creds = load_operator_credentials()
    except CredentialPolicyError as exc:
        print(f"❌ 凭据策略拒绝：{exc}")
        return 2
    print(f"[凭证] 操作号 UID={creds.uid} uname={creds.uname} "
          f"（管理员={'是' if creds.is_owner else '否'}；大号凭据不参与取数）")

    bvids = args.bvid or discover(args.source, args.n, creds)
    if not bvids:
        print("没有取到任何视频，无法测量。")
        return 1

    client = BiliClient(creds)
    results: list[ProbeResult] = []
    print(f"\n开始探测 {len(bvids)} 个视频（限速 {client.min_interval}s/请求）…\n")
    for i, bvid in enumerate(bvids, 1):
        res = probe_one(client, bvid)
        results.append(res)
        flag = {"ok": "✅", "no_subtitle": "➖", "not_logged_in": "🔒"}.get(res.outcome, "❌")
        print(f"{i:2d}. {flag} {bvid} [{res.duration // 60}:{res.duration % 60:02d}] "
              f"{(res.title or res.note)[:40]}")

    counts = Counter(r.outcome for r in results)
    total = len(results)
    ok = counts.get("ok", 0)
    rate = ok / total * 100 if total else 0.0
    ai_count = sum(1 for r in results if r.outcome == "ok" and r.is_ai)
    durations = [r.duration for r in results if r.duration]

    print("\n" + "=" * 62)
    print(f"样本数            : {total}")
    print(f"命中站内字幕      : {ok}  →  命中率 {rate:.1f}%")
    print(f"  其中 AI 字幕    : {ai_count}")
    print(f"  其中人工字幕    : {ok - ai_count}")
    print(f"确认无字幕        : {counts.get('no_subtitle', 0)}")
    print(f"无法判定(未登录)  : {counts.get('not_logged_in', 0)}  ← 必须为 0，否则结果不可信")
    print(f"空字幕(正文为空)  : {counts.get('empty_subtitle', 0)}")
    print(f"风控拦截          : {counts.get('risk', 0)}")
    print(f"其它错误          : {counts.get('error', 0)}")
    if durations:
        print(f"样本时长          : 中位 {statistics.median(durations) / 60:.1f} 分钟，"
              f"合计 {sum(durations) / 60:.1f} 分钟")

    print("\n结论判据（方案 §6）：")
    if rate >= 70:
        print("  命中率 >= 70% → **字幕为主干线，ASR 为辅**。P1 重心放在字幕解析与时间戳锚点。")
    elif rate >= 30:
        print("  30% <= 命中率 < 70% → **混合策略**。ASR 必须是一等公民，两条路都要打磨。")
    else:
        print("  命中率 < 30% → **ASR 才是主干**。P1 重心转向切句、标点、热词调优。")

    if counts.get("not_logged_in", 0) > 0:
        print("\n⚠️ 存在 not_logged_in 样本 —— 命中率**不可信**，请先解决登录态再重测（R1）。")

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(
            {
                "total": total,
                "ok": ok,
                "hit_rate": round(rate, 2),
                "counts": dict(counts),
                "durations_total_seconds": sum(durations),
                "results": [asdict(r) for r in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n明细已写入 {RESULT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
