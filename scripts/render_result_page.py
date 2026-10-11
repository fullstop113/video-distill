"""截图核验：悬浮「到下载」按钮 + 页脚导航 + 登出后的登录卡片。

复用 scripts/render_timeline.py 的验证思路：CSS 问题必须看渲染结果。
本脚本直接从真实服务取页面片段（或用组件的 HTML 函数），再交给无头 Chrome 截图。

用法：
    python scripts/render_result_page.py --shot bottom
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from vd.web import PAGE_FOOT, PAGE_HEAD, _login_card, _to_bottom_html  # noqa: E402

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def body_for(shot: str) -> str:
    if shot == "bottom":
        # 造一段很长的正文，让页面真的能滚动，才能看出按钮是否出现
        filler = "".join(
            f"<p class='kp-t'>第 {i} 段占位正文，用来把页面撑长，验证悬浮按钮的显示逻辑。</p>"
            for i in range(1, 60)
        )
        return (
            "<header><h1>BV1dFem67EYt</h1></header>"
            "<p class='sub'>任务 e0c9fa27f1c2 · 第 60 段之后就是下载区</p>"
            f"<div class='card'>{filler}</div>"
            "<h2>下载</h2><div class='card row'>"
            "<a class='pill' href='#'>↓ 总结与要点</a>"
            "<a class='pill' href='#'>↓ 逐字稿</a></div>"
            "<footer><a href='/'>← 返回首页</a></footer>"
            + _to_bottom_html()
            # 截图前先滚到中部，这样"到下载"按钮应当可见
            + "<script>window.addEventListener('load',function(){"
              "setTimeout(function(){window.scrollTo(0, 900);},30);});</script>"
        )
    if shot == "login":
        return (
            "<header><h1>video-distill</h1></header>"
            "<p class='sub'>把 B 站视频蒸馏成可读、可检索的文字资产</p>"
            + _login_card("你正在从其它设备访问本服务，需要先登录。")
            + "<footer>服务状态 · 配额</footer>"
        )
    raise SystemExit(f"未知的 shot：{shot}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default="bottom", choices=["bottom", "login"])
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser:
        print("找不到浏览器")
        return 2

    out = Path(args.out) if args.out else ROOT / "output" / f"_ui_{args.shot}.png"
    if not out.is_absolute():
        out = (ROOT / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="vd-ui-"))
    page = tmp / "p.html"
    page.write_text(PAGE_HEAD + body_for(args.shot) + PAGE_FOOT, encoding="utf-8")

    proc = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1", "--window-size=1180,780",
         "--virtual-time-budget=2500", f"--screenshot={out}", page.as_uri()],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    if not out.exists():
        print("截图失败：")
        print((proc.stderr or proc.stdout or "")[-600:])
        return 1
    print(f"OK {out}（{out.stat().st_size} 字节）")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
