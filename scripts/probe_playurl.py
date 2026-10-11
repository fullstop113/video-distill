"""诊断：playurl 接口的**原始响应**，用于判断"取不到音频流"的真实原因。

存在的理由：`audio_url()` 之前只检查 `dash.audio` 是否为空，把几种完全不同的
情况混成同一个 `BiliError("playurl 未返回 DASH 音频流")`：

  - 权限不足（充电专属 / 大会员 / 付费预览）
  - 地区限制
  - 视频本身没有音频轨（纯音乐 PV、旧投稿）
  - 接口结构变更
  - 风控拒绝

这几类的**处置方式完全不同**（有的该提示用户，有的该降级，有的该重试），
糊成一个错误等于让用户和排查者都在猜。本脚本把原文打出来，据此区分。

用法：
    python scripts/probe_playurl.py BV1xxxxxxxxx
"""

from __future__ import annotations

import argparse
import json
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


def shape(obj, depth: int = 0, max_depth: int = 2) -> str:
    pad = "  " * depth
    if isinstance(obj, dict):
        if depth >= max_depth:
            return f"{{{', '.join(list(obj.keys())[:12])}}}"
        out = []
        for k, v in list(obj.items())[:30]:
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: {shape(v, depth + 1, max_depth)}")
            else:
                out.append(f"{pad}{k} = {v!r}")
        return "\n".join(out)
    if isinstance(obj, list):
        if not obj:
            return "[] （空）"
        return f"[{len(obj)} 项] 首项 {shape(obj[0], depth + 1, max_depth)}"
    return repr(obj)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid")
    args = ap.parse_args()

    creds = load_operator_credentials()
    client = BiliClient(creds)
    print(f"【凭证】操作号 UID={creds.uid} uname={creds.uname}")

    info = client.video_info(args.bvid)
    print(f"【视频】{info.title}")
    print(f"        UP={info.owner_name}  时长={info.duration}s  cid={info.cid}")

    # 视图接口里常带权限/付费标记
    print("\n【1】view 接口的权限相关字段")
    view = client._get_json(
        "https://api.bilibili.com/x/web-interface/view", {"bvid": args.bvid}
    )["data"]
    for key in ("rights", "is_upower_exclusive", "is_upower_play",
                "is_upower_exclusive_with_qa", "elec_high_level", "state",
                "duration", "copyright", "videos"):
        if key in view:
            print(f"  {key} = {shape(view[key], 1, 2)}")
    # 权限位里找"付费/会员/预览"
    rights = view.get("rights") or {}
    print(f"  rights 里为真的项：{[k for k, v in rights.items() if v]}")

    print("\n【2】player/wbi/v2 里的付费预览标记")
    bili_jct = client.session.cookies.get("bili_jct", "")
    img_key, sub_key = _cached_keys(bili_jct)
    p = client._get_json(
        "https://api.bilibili.com/x/player/wbi/v2",
        wbi_sign({"bvid": info.bvid, "cid": info.cid,
                  "isGaiaAvoided": "false", "web_location": 1315873}, img_key, sub_key),
    )["data"]
    for key in ("is_upower_exclusive", "is_upower_play", "is_ugc_pay_preview",
                "preview_toast", "is_upower_exclusive_with_qa", "allow_bp",
                "max_limit", "need_login_subtitle"):
        if key in p:
            print(f"  {key} = {p[key]!r}")

    print("\n【3】playurl 原始响应（关键）")
    try:
        raw = client._signed(
            "https://api.bilibili.com/x/player/wbi/playurl",
            {"bvid": info.bvid, "cid": info.cid, "fnval": 16, "fnver": 0, "fourk": 1},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  请求抛异常：{type(exc).__name__}: {exc}")
        return 1

    print(f"  code = {raw.get('code')}  message = {raw.get('message')!r}")
    data = raw.get("data")
    if data is None:
        print("  data = None —— 接口没有返回数据体")
        return 2

    print(f"  data 顶层字段：{sorted(data.keys())}")
    dash = data.get("dash")
    durl = data.get("durl")
    print(f"  dash 存在 = {dash is not None}")
    if isinstance(dash, dict):
        print(f"    dash 子字段：{sorted(dash.keys())}")
        print(f"    audio 条数 = {len(dash.get('audio') or [])}")
        print(f"    video 条数 = {len(dash.get('video') or [])}")
        print(f"    duration = {dash.get('duration')}")
    print(f"  durl 存在 = {durl is not None}"
          + (f"（{len(durl)} 段，说明是 FLV/MP4 老格式）" if isinstance(durl, list) else ""))
    for key in ("quality", "format", "timelength", "accept_quality",
                "accept_description", "support_formats"):
        if key in data:
            v = data[key]
            print(f"  {key} = {v if not isinstance(v, list) else f'[{len(v)} 项]'}")

    print("\n【4】判定")
    if dash and (dash.get("audio") or []):
        print("  ✅ 有 DASH 音频流 —— 正常")
    elif durl:
        print("  ⚠️ 没有 DASH 但有 durl（老格式）→ 可从 durl 取音频，需要代码支持降级")
    else:
        toast = p.get("preview_toast") or ""
        pay = any(p.get(k) for k in ("is_upower_exclusive", "is_ugc_pay_preview"))
        print("  ❌ 既无 DASH 也无 durl")
        if pay or toast:
            print(f"  → 判定为**权限受限**：付费/充电专属")
            if toast:
                print(f"     平台提示语：{toast!r}")
            print("     这类视频只能看预览片段，无法取全量音频 → 应当明确提示用户，而不是报接口错误")
        else:
            print("  → 没有明显的付费标记，可能是地区限制、视频无音轨或接口变更")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
