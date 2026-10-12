"""截图核验：首页任务列表（含标题）与"我的产物"页。

直接请求真实服务，把返回的 HTML 存成文件交给无头 Chrome 截图。
这样截的就是**服务真正渲染的页面**，不是我拼出来的片段。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BROWSER = next(
    (b for b in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ] if Path(b).exists()),
    None,
)

PAGES = {
    "home": "/",
    "artifacts": "/artifacts",
    "artifacts_all": "/artifacts?all_versions=1",
    "failed": "/?status=failed",
    "balance": "/balance",
    # 任务详情页：需要任务 id，用 --task <id> 传
}


def fetch(path: str, port: int) -> str:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("page", choices=sorted(PAGES) + ["task"])
    ap.add_argument("--task", default="", help="page=task 时的任务 id")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--height", type=int, default=980)
    args = ap.parse_args()

    if not BROWSER:
        print("找不到浏览器")
        return 2

    if args.page == "task":
        if not args.task:
            print("page=task 需要 --task <任务id>")
            return 2
        path = f"/tasks/{args.task}"
    else:
        path = PAGES[args.page]

    try:
        html = fetch(path, args.port)
    except Exception as exc:  # noqa: BLE001
        print(f"取页面失败：{type(exc).__name__}: {exc}")
        return 1

    tmp = Path(ROOT / "output" / "_shots")
    tmp.mkdir(parents=True, exist_ok=True)
    page_file = tmp / f"{args.page}.html"
    page_file.write_text(html, encoding="utf-8")
    out = (tmp / f"{args.page}.png").resolve()

    subprocess.run(
        [BROWSER, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1",
         f"--window-size=1240,{args.height}",
         "--virtual-time-budget=2000", f"--screenshot={out}", page_file.as_uri()],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    if not out.exists():
        print("截图失败")
        return 1
    print(f"OK {out}（{out.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
