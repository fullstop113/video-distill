"""实测 B 站评论接口：@ 触发式机器人到底能不能做。

背景：用户看到有人在 B 站评论区 @ 一下就出视频总结，问怎么实现的。
网上有现成开源项目，但"在这台机器上到底能不能跑"必须实测：

  1. 读评论（找"@ 我的"评论）—— 匿名 vs 登录分别是什么情况？
  2. 发评论（回复）—— 需要什么凭证、什么权限门槛？
  3. 限流 —— 响应里有没有明确的频率限制字段？
  4. 能不能拿到"这条评论 @ 了谁"

**本脚本只读不写**：发评论只做能力推断（看错误码），不产生任何实际写入。
"""

from __future__ import annotations

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

from vd.bilibili import BiliClient, _cached_keys, wbi_sign  # noqa: E402
from vd.credentials import load_operator_credentials  # noqa: E402

SAMPLE_BVID = "BV1NXH661EpG"


def brief(obj, limit: int = 10) -> str:
    if isinstance(obj, dict):
        items = list(obj.items())[:limit]
        parts = [
            f"{k}={v!r}" if not isinstance(v, (dict, list)) else f"{k}:{type(v).__name__}[{len(v)}]"
            for k, v in items
        ]
        return "  " + "\n  ".join(parts)
    if isinstance(obj, list):
        return f"  列表[{len(obj)}]"
    return f"  {obj!r}"


def main() -> int:
    creds = load_operator_credentials()
    client = BiliClient(creds)

    view = client._get_json(
        "https://api.bilibili.com/x/web-interface/view", {"bvid": SAMPLE_BVID}
    )["data"]
    aid, cid = view["aid"], view["cid"]
    print(f"【样本视频】{view['title']}")
    print(f"  aid={aid}  cid={cid}  评论数（接口声明）={view.get('stat', {}).get('reply')}")

    bili_jct = client.session.cookies.get("bili_jct", "")
    img_key, sub_key = _cached_keys(bili_jct)

    # ---------- 1. 读评论（匿名） ----------
    print("\n" + "=" * 64)
    print("【1】匿名读评论")
    print("=" * 64)
    anon = BiliClient(None)
    try:
        r = anon._get_json(
            "https://api.bilibili.com/x/v2/reply",
            {"type": 1, "oid": aid, "pn": 1, "ps": 20, "sort": 2},
        )
        d = r.get("data") or {}
        page = d.get("page") or {}
        replies = d.get("replies") or []
        print(f"  code={r.get('code')}  message={r.get('message')!r}")
        print(f"  拿到的评论数={len(replies)}  总数={page.get('count')}")
        if replies:
            one = replies[0]
            print("  单条评论的字段：")
            print(brief({k: one.get(k) for k in (
                "rpid", "oid", "mid", "content", "ctime", "like", "rcount",
                "member", "replies", "parent", "root", "dialog",
            )}))
            content = one.get("content") or {}
            print(f"  正文: {str(content.get('message'))[:80]!r}")
            # 关键：能不能看出这条评论 @ 了谁
            print(f"  content 里是否有 at 信息: "
                  f"{'at' in str(content.keys()) or 'members' in str(content.keys())}")
    except Exception as exc:  # noqa: BLE001
        print(f"  失败：{type(exc).__name__}: {exc}")

    # ---------- 2. 读评论（登录） ----------
    print("\n" + "=" * 64)
    print("【2】登录态读评论")
    print("=" * 64)
    try:
        r = client._get_json(
            "https://api.bilibili.com/x/v2/reply/wbi/main",
            wbi_sign({"oid": aid, "type": 1, "mode": 3, "ps": 20, "pn": 1},
                     img_key, sub_key),
            tolerate_codes=(-352, -412),
        )
        d = r.get("data") or {}
        print(f"  code={r.get('code')}  message={r.get('message')!r}")
        print(f"  data 字段: {sorted(d.keys())[:12]}")
        replies = d.get("replies") or []
        print(f"  拿到评论={len(replies)}")
        if replies:
            # 找带 at 的评论，看结构
            for rp in replies[:20]:
                content = rp.get("content") or {}
                members = content.get("members")
                if members:
                    print(f"  ✅ 发现带 @ 的评论：{str(content.get('message'))[:60]!r}")
                    print(f"     members 结构: {brief(members[0] if members else {})}")
                    break
            else:
                print("  这 20 条里没有带 @ 的评论（换个视频或多翻几页能看到）")
    except Exception as exc:  # noqa: BLE001
        print(f"  失败：{type(exc).__name__}: {exc}")

    # ---------- 3. @我的消息（这是机器人最该用的接口） ----------
    print("\n" + "=" * 64)
    print("【3】「@我的」消息接口（机器人真正的入口）")
    print("=" * 64)
    for name, url, params in [
        ("回复我的",
         "https://api.bilibili.com/x/msgfeed/reply",
         {"platform": "web", "build": 0, "mobi_app": "web"}),
        ("@我的",
         "https://api.bilibili.com/x/msgfeed/at",
         {"platform": "web", "build": 0, "mobi_app": "web"}),
    ]:
        try:
            r = client._get_json(url, params, tolerate_codes=(-101, -352))
            code = r.get("code")
            d = r.get("data") or {}
            print(f"\n  {name}: code={code} message={r.get('message')!r}")
            if isinstance(d, dict):
                print(f"    字段: {sorted(d.keys())[:10]}")
                items = d.get("items") or []
                print(f"    消息条数: {len(items)}")
                if items:
                    it = items[0]
                    print(f"    单条字段: {sorted(it.keys())[:14]}")
                    print(f"      用户: {(it.get('user') or {}).get('nickname')!r}")
                    print(f"      内容: {str((it.get('item') or {}).get('source_content'))[:60]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} 失败：{type(exc).__name__}: {exc}")

    # ---------- 4. 发评论能力（只推断，不写入） ----------
    print("\n" + "=" * 64)
    print("【4】发评论能力探测（不产生实际写入）")
    print("=" * 64)
    nav = client.nav()
    print(f"  当前账号: {(nav.get('uname'))!r}  isLogin={nav.get('isLogin')}  "
          f"level={(nav.get('level_info') or {}).get('current_level')}")
    print(f"  有 bili_jct（发评论必需）: {bool(bili_jct)}")
    print("  ⚠️ 未实际发送：发评论会在视频下留下真实痕迹，属于对外可见行为，")
    print("     必须由你明确同意后才做。这里只确认前提条件是否具备。")
    print(f"  → 结论：{'具备发评论的前提条件（有登录态 + bili_jct）' if bili_jct else '缺少 bili_jct，无法发评论'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
