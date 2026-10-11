"""实测 B 站官方「视频 AI 总结」接口。

文档：https://sessionhu.github.io/bilibili-API-collect/docs/video/summary.html
接口：GET /x/web-interface/view/conclusion/get （Cookie + Wbi 签名）

要回答的问题：
  1. 这个接口在本机能不能通？
  2. 它返回什么？和本项目自建的总结相比如何？
  3. 覆盖率如何（随机几个视频有多少有官方总结）？
  4. 它能不能替代本项目的一部分工作？

用法：
    python scripts/probe_official_summary.py                 # 用默认样本
    python scripts/probe_official_summary.py BV1xx BV1yy     # 指定视频
"""

from __future__ import annotations

import argparse
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

from vd.bilibili import BiliClient, ts_hms  # noqa: E402
from vd.credentials import load_operator_credentials  # noqa: E402

API = "https://api.bilibili.com/x/web-interface/view/conclusion/get"

# 默认样本：本项目处理过的视频 + 热门榜前几个
DEFAULT = ["BV1NXH661EpG", "BV1dFem67EYt", "BV1zQRWBTEhU"]

DATA_CODE = {
    0: "有摘要",
    1: "无摘要（未识别到语音）",
    -1: "不支持（敏感内容或其他原因）",
}


def probe(client: BiliClient, bvid: str, verbose: bool = False) -> dict:
    info = client.video_info(bvid)
    out = {"bvid": bvid, "title": info.title, "duration": info.duration}
    try:
        data = client._signed(
            API,
            {"bvid": bvid, "cid": info.cid, "up_mid": info.owner_mid},
        ).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["code"] = data.get("code")
    out["code_label"] = DATA_CODE.get(data.get("code"), f"未知({data.get('code')})")
    out["stid"] = data.get("stid")
    out["like_num"] = data.get("like_num")
    out["dislike_num"] = data.get("dislike_num")

    mr = data.get("model_result") or {}
    out["result_type"] = mr.get("result_type")
    summary = (mr.get("summary") or "").strip()
    outline = mr.get("outline") or []
    subs = mr.get("subtitle") or []
    out["has_summary"] = bool(summary)
    out["summary_chars"] = len(summary)
    out["outline_sections"] = len(outline)
    out["outline_points"] = sum(len(s.get("part_outline") or []) for s in outline)
    out["subtitle_items"] = sum(len(s.get("part_subtitle") or []) for s in subs)

    if verbose and summary:
        print(f"\n{'=' * 66}\n{bvid}  {info.title}\n{'=' * 66}")
        print(f"时长 {ts_hms(info.duration)}　官方总结 {len(summary)} 字　"
              f"提纲 {len(outline)} 段 / {out['outline_points']} 点　"
              f"AI 字幕 {out['subtitle_items']} 条")
        print(f"\n【官方摘要】\n{summary}")
        if outline:
            print("\n【官方提纲】")
            for i, sec in enumerate(outline[:4], 1):
                print(f"  {i}. [{ts_hms(sec.get('timestamp', 0))}] {sec.get('title','')[:70]}")
                for p in (sec.get("part_outline") or [])[:4]:
                    print(f"       · [{ts_hms(p.get('timestamp',0))}] "
                          f"{str(p.get('content',''))[:64]}")
        if subs:
            ps = subs[0].get("part_subtitle") or []
            print(f"\n【官方 AI 字幕】前 3 条（共 {len(ps)} 条）")
            for s in ps[:3]:
                print(f"  [{ts_hms(s.get('start_timestamp',0))}] "
                      f"{str(s.get('content',''))[:60]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid", nargs="*", default=[])
    ap.add_argument("--ranking", type=int, default=0, help="额外抽样热门榜前 N 个")
    ap.add_argument("--verbose", action="store_true", default=True)
    args = ap.parse_args()

    creds = load_operator_credentials()
    client = BiliClient(creds)
    print(f"凭证：操作号 UID={creds.uid} uname={creds.uname}")

    targets = list(args.bvid) or list(DEFAULT)
    if args.ranking:
        data = client._get_json(
            "https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
        )["data"]
        for item in (data.get("list") or [])[: args.ranking]:
            if item.get("bvid") and item["bvid"] not in targets:
                targets.append(item["bvid"])

    results = []
    for i, b in enumerate(targets):
        r = probe(client, b, verbose=args.verbose and i < 2)
        results.append(r)
        time.sleep(0.8)

    print(f"\n\n{'=' * 66}")
    print(f"覆盖率统计（{len(results)} 个视频）")
    print("=" * 66)
    print(f"{'BV 号':16s} {'时长':>7s}  {'官方总结':>8s}  {'提纲':>10s}  {'AI字幕':>7s}  状态")
    for r in results:
        if r.get("error"):
            print(f"{r['bvid']:16s} {'-':>7s}  {'-':>8s}  {'-':>10s}  {'-':>7s}  ❌ {r['error'][:30]}")
            continue
        dur = ts_hms(r["duration"])
        sm = f"{r['summary_chars']}字" if r["has_summary"] else "无"
        ol = f"{r['outline_sections']}段/{r['outline_points']}点"
        print(f"{r['bvid']:16s} {dur:>7s}  {sm:>8s}  {ol:>10s}  "
              f"{r['subtitle_items']:>7d}  {r['code_label']}")

    ok = sum(1 for r in results if r.get("has_summary"))
    print(f"\n有官方总结：{ok}/{len(results)}"
          f"（{ok / len(results):.0%}）" if results else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
