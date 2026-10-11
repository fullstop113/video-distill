"""诊断：命中视频的字幕轨道到底是人工还是 AI。"""
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

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from vd.bilibili import BiliClient
from vd.credentials import load_operator_credentials

creds = load_operator_credentials()
client = BiliClient(creds)

data = json.loads((ROOT / "data" / "hit_rate_result.json").read_text(encoding="utf-8"))
ok = [r for r in data["results"] if r["outcome"] == "ok"]

print(f"命中 {len(ok)} 个视频，逐个列出全部字幕轨道的原始元数据：\n")
for r in ok:
    info = client.video_info(r["bvid"])
    tracks = client.subtitle_tracks(info)
    print(f"{r['bvid']}  {info.title[:32]}")
    for t in tracks:
        print(
            f"    lan={t.lan!r:16s} lan_doc={t.lan_doc!r:22s} "
            f"is_ai={str(t.is_ai):5s} {t.url[:52]}"
        )
    # 额外：也看看 ai_status / type 原始字段
    print()
