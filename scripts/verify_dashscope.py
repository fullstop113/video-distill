"""实测阿里云百炼 Paraformer 是否可用：
  1. Key 是否有效
  2. **B 站音频链接能否被阿里云直接拉取** ← 决定架构（一步到位 vs 先下载再上传）
  3. 返回的时间戳字段形态

这一步必须先做：如果 B 站 CDN 拒绝外部拉流，架构就得改成"先下载再上传"。

用法：
    python scripts/verify_dashscope.py            # 用 BV1d8p56tEFC（无字幕那个）
    python scripts/verify_dashscope.py BV1xxxx
"""

from __future__ import annotations

import json
import os
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

from vd.summarize import sanitize_no_proxy  # noqa: E402

sanitize_no_proxy()

import httpx  # noqa: E402

BVID = sys.argv[1] if len(sys.argv) > 1 else "BV1d8p56tEFC"

KEY = os.getenv("DASHSCOPE_API_KEY", "").strip()
MODEL = os.getenv("DASHSCOPE_MODEL", "paraformer-v2").strip()
if not KEY:
    raise SystemExit("未配置 DASHSCOPE_API_KEY")
print(f"Key: {KEY[:14]}…（{len(KEY)} 字符）")
print(f"模型: {MODEL}")

# 官方文档给的默认域名（WorkspaceId 专属域名是可选的优化）
BASE = "https://dashscope.aliyuncs.com/api/v1"
SUBMIT = f"{BASE}/services/audio/asr/transcription"

print("\n=== 取 B 站音频链接 ===")
from vd.bilibili import BiliClient  # noqa: E402
from vd.credentials import load_operator_credentials  # noqa: E402

cli = BiliClient(load_operator_credentials())
info = cli.video_info(BVID)
url, dur = cli.audio_url(info)
print(f"  视频：{info.title[:40]}")
print(f"  时长：{dur:.0f} 秒 = {dur / 60:.1f} 分钟")
print(f"  音频链接：{url[:110]}…")

print("\n=== 提交转写任务（让阿里云自己去拉音频） ===")
payload = {
    "model": MODEL,
    "input": {"file_urls": [url]},
    "parameters": {
        "language_hints": ["zh", "en"],
        "timestamp_alignment_enabled": True,
    },
}
headers = {
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
    "X-DashScope-Async": "enable",
}
with httpx.Client(timeout=60) as c:
    r = c.post(SUBMIT, json=payload, headers=headers)
print(f"  HTTP {r.status_code}")
body = r.json() if r.content else {}
if r.status_code != 200:
    print(f"  提交失败：{json.dumps(body, ensure_ascii=False)[:600]}")
    raise SystemExit(1)

task_id = (body.get("output") or {}).get("task_id")
print(f"  task_id = {task_id}")

print("\n=== 轮询结果 ===")
QUERY = f"{BASE}/tasks/{task_id}"
final = None
for i in range(60):          # 最多等 5 分钟
    time.sleep(5)
    with httpx.Client(timeout=60) as c:
        r = c.get(QUERY, headers={"Authorization": f"Bearer {KEY}"})
    body = r.json()
    status = (body.get("output") or {}).get("task_status")
    print(f"  [{i * 5:>3}s] {status}")
    if status in ("SUCCEEDED", "FAILED"):
        final = body
        break

if not final:
    raise SystemExit("超时未完成")

out = final.get("output") or {}
for res in out.get("results") or []:
    sub = res.get("subtask_status")
    print(f"\n  子任务 {sub}")
    if sub != "SUCCEEDED":
        print(f"    code={res.get('code')}  message={res.get('message')}")
        continue
    turl = res.get("transcription_url")
    print(f"  结果链接：{turl[:100]}…")
    with httpx.Client(timeout=120) as c:
        raw = c.get(turl).text
    data = json.loads(raw)
    print("\n=== 结论 ===")
    tr = (data.get("transcripts") or [{}])[0]
    props = data.get("properties") or {}
    print(f"  ✅ B 站链接**可被阿里云直接拉取**（这是关键结论）")
    print(f"  原始时长：{props.get('original_duration_in_milliseconds', 0) / 1000:.0f} 秒")
    print(f"  语音时长：{tr.get('content_duration_in_milliseconds', 0) / 1000:.0f} 秒"
          f"  ← **按这个计费**")
    sents = tr.get("sentences") or []
    print(f"  句子数：{len(sents)}")
    words = sum(len(s.get("words") or []) for s in sents)
    print(f"  词级时间戳点数：{words}")
    if sents:
        s0 = sents[0]
        print(f"  首句 [{s0.get('begin_time')}ms - {s0.get('end_time')}ms] "
              f"{(s0.get('text') or '')[:40]}")
        w = (s0.get("words") or [])[:3]
        for x in w:
            print(f"      词 [{x.get('begin_time')}-{x.get('end_time')}] "
                  f"{(x.get('text') or '')!r} punct={(x.get('punctuation') or '')!r}")
    speech_s = tr.get("content_duration_in_milliseconds", 0) / 1000
    price = float(os.getenv("VD_ASR_PRICE_PER_SECOND", "0.00008"))
    print(f"\n  本次计费：{speech_s:.0f} 秒 × {price} 元/秒 = **{speech_s * price:.4f} 元**")
    break
