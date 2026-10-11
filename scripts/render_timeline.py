"""把时间轴组件单独渲染成 HTML 并截图，用于人工核验版面。

为什么需要它：前两轮"修好了"都是靠读代码判断的，结果用户一连发现两个视觉问题
（文字连成一行、悬停蓝块覆盖过大）。**CSS 的问题必须看渲染结果**，
而我没法在浏览器里点开页面看，所以用无头 Chrome 截图。

用法：
    python scripts/render_timeline.py            # 生成 HTML 并截图到 output/_timeline_preview.png
    python scripts/render_timeline.py --keep     # 保留 HTML 便于手工打开
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from vd.web import PAGE_FOOT, PAGE_HEAD, _timeline_html  # noqa: E402

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 用真实产物的规模造数据：11 个节点、简介长短不一（含超长）
SAMPLE = [
    ("00:00:14", "六年无新英雄后2026嘉年华公布萨拉塔斯", 0),
    ("00:00:45", "DOTA从魔兽争霸3地图编辑器起源", 1),
    ("00:01:05", "冰蛙接手DOTA维护并走红", 1),
    ("00:02:11", "Valve招走冰蛙开发DOTA2", 2),
    ("00:03:44", "Valve申请注册DOTA商标暴雪反对", 2),
    ("00:04:06", "双方和解项目改名后成风暴英雄", 3),
    ("00:05:17", "风暴英雄掀桌式差异化设计，共享经验多地图怪英雄", 3),
    ("00:11:47", "风暴英雄2.0上线并押注HGC电竞", 4),
    ("00:13:31", "暴雪突然宣布取消2019年HGC与大学联赛", 4),
    ("00:15:01", "暴雪停止付费内容更新进入维持状态", 4),
    ("00:17:50", "六年英雄列表未变直到2026年9月萨拉塔斯出现", 4),
]


def build_html(force_open: int = 0) -> str:
    highlights = [
        {
            "at": at,
            "sec": i * 60,
            "text": text,
            "point_index": idx,
            "detail": f"这是节点 {i + 1} 对应要点的陈述内容，用来检查气泡里的正文换行与宽度是否合适。"
                      "再补一句让长度接近真实情况，看看会不会溢出或过于拥挤。",
        }
        for i, (at, text, idx) in enumerate(SAMPLE)
    ]
    frag = _timeline_html("BVTEST", highlights)
    if force_open:
        # 靠 JS 打开不可靠：截图模式下 load / setTimeout 的执行时机与 --dump-dom 不同，
        # 实测两次都没生效。改用**纯 CSS 覆盖**强制显示。
        # ⚠️ 选择器必须只命中目标气泡：第一版写成 `.tl-card.preview .tl-bubble`
        # 把所有气泡都显示了，截图里出现两个叠着的框。
        node_w = 124          # 与 CSS 里的 .tl-node width 保持一致
        left = node_w * (force_open - 1) + node_w // 2
        frag += f"""
<style>
#tl-b-{force_open} {{ opacity:1 !important; visibility:visible !important;
                     transform:none !important;
                     left:{left}px !important; top:12px !important; }}
.tl-node:nth-child({force_open}) .dot {{
  background:var(--acc) !important; transform:scale(1.28); }}
</style>
"""
    return PAGE_HEAD + frag + PAGE_FOOT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--open-node", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "output" / "_timeline_preview.png"))
    args = ap.parse_args()

    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser:
        print("找不到 Chrome/Edge，无法截图")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="vd-tl-"))
    page = tmp / "preview.html"
    page.write_text(build_html(args.open_node), encoding="utf-8")

    out = Path(args.out)
    if not out.is_absolute():
        # Chrome 以自身进程的 CWD 解析相对路径，必须转成绝对路径（踩过）
        out = (ROOT / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1280,760",
        "--virtual-time-budget=1500",
        f"--screenshot={out}",
        page.as_uri(),
    ]
    # ⚠️ 必须显式指定 utf-8：Chrome 的 stderr 含非 GBK 字节，
    # Windows 默认编码下 `text=True` 会在读取阶段抛 UnicodeDecodeError（踩过）。
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    if not out.exists():
        print("截图失败：")
        print((proc.stderr or proc.stdout or "")[-800:])
        return 1
    print(f"OK {out}（{out.stat().st_size} 字节）")
    if args.keep:
        print(f"HTML {page}")
    else:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
