"""诊断：匿名/登录态下字幕接口的**原始响应形状**。

存在的理由：R1（静默失败）的判定必须建立在真实响应字段之上，而不是猜测。
本脚本不做任何"友好封装"，直接把接口原文打出来。

用法：
    python scripts/raw_probe.py                 # 匿名探测（热门榜第 1 个视频）
    python scripts/raw_probe.py BV1xxxxxxxxx    # 指定视频，匿名
    python scripts/raw_probe.py BV1xxxxxxxxx --logged   # 用已保存凭证
"""

from __future__ import annotations

import argparse
import json
import logging
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

from vd.bilibili import BiliClient, wbi_sign  # noqa: E402
from vd.credentials import load_operator_credentials  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def shape(obj, depth: int = 0, max_depth: int = 3) -> str:
    """打印结构而不是全文，避免刷屏，同时保留判定所需的字段存在性。"""
    pad = "  " * depth
    if isinstance(obj, dict):
        if depth >= max_depth:
            return f"{{{', '.join(obj.keys())}}}"
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(shape(v, depth + 1, max_depth))
            else:
                lines.append(f"{pad}{k} = {v!r}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return f"{pad}[] (空列表，长度 0)"
        lines = [f"{pad}[{len(obj)} 项]"]
        lines.append(shape(obj[0], depth + 1, max_depth))
        return "\n".join(lines)
    return f"{pad}{obj!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid", nargs="?", default="")
    ap.add_argument("--logged", action="store_true", help="使用已保存凭证")
    args = ap.parse_args()

    creds = None
    if args.logged:
        creds = load_operator_credentials()
        print(f"【模式】操作号登录态 UID={creds.uid} uname={creds.uname}")
    else:
        print("【模式】匿名（无任何 Cookie，仅首页 buvid3）")

    client = BiliClient(creds)

    bvid = args.bvid
    if not bvid:
        data = client._get_json(
            "https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
        )["data"]
        bvid = (data.get("list") or [{}])[0].get("bvid", "")
        print(f"【选片】热门榜第 1 个：{bvid}")

    info = client.video_info(bvid)
    print(f"【视频】{info.title} | cid={info.cid} | 时长={info.duration}s")

    print("\n【1】会话 Cookie 现状")
    print(f"  SESSDATA 存在 = {bool(client.session.cookies.get('SESSDATA'))}")
    print(f"  bili_jct 存在 = {bool(client.session.cookies.get('bili_jct'))}")
    print(f"  全部 Cookie 名 = {sorted(client.session.cookies.keys())}")

    print("\n【2】nav 接口的登录判定（最可靠的登录态来源）")
    nav = client._get_json(
        "https://api.bilibili.com/x/web-interface/nav", tolerate_codes=(-101,)
    )
    print(f"  code = {nav.get('code')}  message = {nav.get('message')!r}")
    nd = nav.get("data") or {}
    print(f"  isLogin = {nd.get('isLogin')}   uname = {nd.get('uname')!r}")
    is_login = bool(nd.get("isLogin"))

    print("\n【3】/x/player/wbi/v2 原始响应（关键）")
    bili_jct = client.session.cookies.get("bili_jct", "")
    from vd.bilibili import _cached_keys

    img_key, sub_key = _cached_keys(bili_jct)
    params = wbi_sign(
        {
            "bvid": info.bvid,
            "cid": info.cid,
            "isGaiaAvoided": "false",
            "web_location": 1315873,
        },
        img_key,
        sub_key,
    )
    raw = client._get_json("https://api.bilibili.com/x/player/wbi/v2", params)
    print(f"  code = {raw.get('code')}  message = {raw.get('message')!r}")
    rd = raw.get("data") or {}
    print(f"  data 顶层字段 = {sorted(rd.keys())}")

    has_subtitle_field = "subtitle" in rd
    sub = rd.get("subtitle")
    print(f"\n  ★ 'subtitle' 字段是否存在 = {has_subtitle_field}")
    print(f"  ★ 'subtitle' 值 = {sub!r}")
    if isinstance(sub, dict):
        print(f"  ★ subtitle 子字段 = {sorted(sub.keys())}")
        subs = sub.get("subtitles")
        print(f"  ★ subtitle.subtitles 存在 = {subs is not None}，"
              f"长度 = {len(subs) if isinstance(subs, list) else 'N/A'}")
        print(f"  ★ allow_submit = {sub.get('allow_submit')!r}")
        if isinstance(subs, list) and subs:
            print("\n  字幕轨道形状：")
            print(shape(subs[0], 1))

    print("\n【4】结论（程序据此判定，不是人肉猜）")
    if not has_subtitle_field:
        print("  → subtitle 字段**不存在**：无法区分「无字幕」与「无权限」，必须报未登录。")
    elif isinstance(sub, dict) and not sub.get("subtitles"):
        if is_login:
            print("  → 已登录 + 空列表 = **确认该视频没有站内字幕** → 走 ASR 兜底。")
        else:
            print("  → 未登录 + 空列表 = **静默失败（R1）**：绝不能当成「无字幕」。")
    else:
        print("  → 拿到了字幕轨道（正常命中）。")

    print("\n【5】完整 data 结构（前 3 层）")
    print(shape(rd, 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
