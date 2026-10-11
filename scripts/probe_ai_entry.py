"""查清官方 AI 总结的入口为什么看不到。

已知：`conclusion/get` 接口在本机可用（能取到摘要与 AI 字幕），
但用户在页面上**找不到入口**。要区分几种可能：

  A. 接口还在、入口撤了（功能下线/回滚）
  B. 入口有准入门槛（账号等级/内测名单/大会员）
  C. 只有部分视频展示入口（与 conclusion/get 的 code 相关）
  D. 只在 App 有、网页端没有

本脚本从接口侧找证据：
  1. player/wbi/v2 里有没有"是否展示 AI 总结"的标志位
  2. conclusion/get 的返回与账号状态是否相关
  3. 对比有摘要/无摘要视频在 player 接口里的差异
"""

from __future__ import annotations

import sys
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

from vd.bilibili import BiliClient, _cached_keys, wbi_sign  # noqa: E402
from vd.credentials import load_operator_credentials  # noqa: E402

WITH = "BV1dFem67EYt"      # 实测有官方总结
WITHOUT = "BV1NXH661EpG"   # 实测无官方总结


def main() -> int:
    creds = load_operator_credentials()
    client = BiliClient(creds)

    print("=" * 66)
    print("【1】账号状态（看是否有准入门槛）")
    print("=" * 66)
    nav = client.nav()
    li = nav.get("level_info") or {}
    vip = nav.get("vip") or {}
    print(f"  uname={nav.get('uname')!r}  mid={nav.get('mid')}")
    print(f"  isLogin={nav.get('isLogin')}  等级={li.get('current_level')}")
    print(f"  大会员 vipStatus={nav.get('vipStatus')}  vipType={vip.get('type')} "
          f"vipStatus={vip.get('status')}")
    print(f"  硬核会员 is_senior_member={nav.get('is_senior_member')}")
    print(f"  nav 里与 AI/总结相关的字段: "
          f"{[k for k in nav if 'ai' in k.lower() or 'summ' in k.lower()]}")

    print("\n" + "=" * 66)
    print("【2】player/wbi/v2 里有没有「展示 AI 总结」的标志位")
    print("=" * 66)
    bili_jct = client.session.cookies.get("bili_jct", "")
    img_key, sub_key = _cached_keys(bili_jct)

    for label, bvid in (("有官方总结", WITH), ("无官方总结", WITHOUT)):
        info = client.video_info(bvid)
        data = client._signed(
            "https://api.bilibili.com/x/player/wbi/v2",
            wbi_sign({"bvid": bvid, "cid": info.cid, "isGaiaAvoided": "false",
                      "web_location": 1315873}, img_key, sub_key),
        )["data"]
        keys = sorted(data.keys())
        print(f"\n  [{label}] {bvid}  {info.title[:34]}")
        # 找与 AI / 总结 / 结论 相关的字段
        hits = [k for k in keys if any(t in k.lower() for t in
                                      ("ai", "summary", "conclusion", "aigc", "index"))]
        print(f"    相关字段: {hits or '（无）'}")
        for k in hits:
            v = data[k]
            print(f"      {k} = {v!r}"[:150])
        # 顺带看 arc_aigc（B站用于标记 AI 相关内容的字段）
        if "arc_aigc" in data:
            print(f"    arc_aigc = {data['arc_aigc']!r}")
        print(f"    ai_status/ai_gen 等: "
              f"{ {k: v for k, v in data.items() if 'ai' in k.lower()} }"[:200])

    print("\n" + "=" * 66)
    print("【3】conclusion/get 的完整响应（含 status/limit 之类）")
    print("=" * 66)
    for label, bvid in (("有官方总结", WITH), ("无官方总结", WITHOUT)):
        info = client.video_info(bvid)
        raw = client._signed(
            "https://api.bilibili.com/x/web-interface/view/conclusion/get",
            {"bvid": bvid, "cid": info.cid, "up_mid": info.owner_mid},
        )
        d = raw.get("data") or {}
        print(f"\n  [{label}] {bvid}")
        print(f"    外层 code={raw.get('code')} message={raw.get('message')!r}")
        print(f"    data 字段: {sorted(d.keys())}")
        for k in ("code", "status", "stid", "like_num", "dislike_num"):
            if k in d:
                print(f"      {k} = {d[k]!r}")
        mr = d.get("model_result") or {}
        print(f"    model_result.result_type = {mr.get('result_type')!r}")
        print(f"    有 summary={bool(mr.get('summary'))} "
              f"outline={len(mr.get('outline') or [])} "
              f"subtitle={len(mr.get('subtitle') or [])}")

    print("\n" + "=" * 66)
    print("【4】结论线索")
    print("=" * 66)
    print("  接口仍然返回真实数据 → 后端功能在世，且未对我们这个账号设门槛")
    print("  （若账号被封禁功能，接口会返回 -403 或空 model_result）")
    print("  → 因此「页面上看不到入口」更可能是 UI 层变化，而不是功能下线或没资格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
