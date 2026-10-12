"""云 ASR 成本模型：按用户真实用量，对比"租服务器" vs "调 API"。

价格来源（均为官方文档实价，2026-10 抓取）：
  阿里云智能语音交互 录音文件识别     2.50 元/小时（0~299 小时档）
  阿里云录音文件识别闲时版            1.00 元/小时（0~10000 小时）
  阿里云百炼 Paraformer 语音识别      **0.288 元/小时**（不限量，无梯度）
  阿里云资源包 40 小时               100 元 → 2.50 元/小时
  阿里云资源包 1000 小时             1200 元 → 1.20 元/小时

对照：腾讯云轻量 4核8G = 450 元/年（能跑本地 ASR）

用户用量（已知）：
  · 配额规则：其它人登录时每天最多 3 个视频、总长 ≤3 小时
  · 实际使用：目前 9 个成功任务，视频偏长（45 分钟 ~ 104 分钟）
"""

from __future__ import annotations

import sys

# ---- 云 ASR 单价（元/小时） ----
CLOUD = {
    "阿里云 百炼 Paraformer": 0.288,
    "阿里云 闲时版": 1.00,
    "阿里云 录音文件识别（标准）": 2.50,
    "阿里云 极速版": 3.30,
}
SERVER_YEAR = 450.0          # 腾讯云 4核8G 年付

print("=" * 74)
print("一、单价对比")
print("=" * 74)
print(f"{'方案':32s} {'元/小时':>10s} {'104分钟视频':>12s}")
print("-" * 74)
for name, price in CLOUD.items():
    print(f"{name:32s} {price:>10.3f} {price * 104.3 / 60:>11.2f} 元")

print("\n" + "=" * 74)
print("二、你的用量下，两种方案的年成本")
print("=" * 74)
print(f"{'用量假设':34s} {'云ASR(0.288)':>14s} {'租服务器':>12s} {'谁更省':>10s}")
print("-" * 74)

scenarios = [
    ("轻度：每天 1 个 × 1 小时", 1 * 1 * 30 * 12),
    ("中度：每天 2 个 × 1 小时", 2 * 1 * 30 * 12),
    ("重度：每天 3 个 × 1 小时（配额满）", 3 * 1 * 30 * 12),
    ("极端：每天 3 个 × 3 小时（时长也满）", 3 * 3 * 30 * 12),
    ("实际近况：按已用 9 个长视频/月", 9 * 1.5 * 12),
]

for label, hours_per_year in scenarios:
    cloud = hours_per_year * 0.288
    cheaper = "云 ASR" if cloud < SERVER_YEAR else "租服务器"
    save = abs(cloud - SERVER_YEAR)
    print(f"{label:34s} {cloud:>12.0f} 元 {SERVER_YEAR:>10.0f} 元 "
          f"{cheaper:>8s}（差 {save:.0f}）")

print("\n" + "=" * 74)
print("三、盈亏平衡点")
print("=" * 74)
be = SERVER_YEAR / 0.288
print(f"  云 ASR 单价 0.288 元/小时，服务器 450 元/年")
print(f"  盈亏平衡 = 450 / 0.288 = **{be:.0f} 小时/年** = {be / 12:.1f} 小时/月")
print(f"  即：每月转录超过 {be / 12:.0f} 小时音频，租服务器才划算")
print()
for daily, note in ((1, "每天 1 个 1 小时视频"), (2, "每天 2 个"), (3, "每天 3 个")):
    monthly = daily * 1 * 30
    print(f"    {note:22s} 月 {monthly:>3.0f} 小时 → "
          f"{'云 ASR 更省' if monthly < be / 12 else '服务器更省'}")

print("\n" + "=" * 74)
print("四、结论")
print("=" * 74)
print("  你的实际用量（偶尔用、一个人）远低于平衡点 → **云 ASR 明显更省**")
print(f"  而且零运维：不用管内存、不用常驻进程、不用为闲置付费")
