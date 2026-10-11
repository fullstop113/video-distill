"""用无头 Chrome 读取时间轴节点的**计算样式与盒模型**，定位版面问题。

前两轮"看代码觉得对、实际不对"的教训：CSS 必须看渲染结果。
本脚本把真实计算结果打印出来，比截图更精确。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vd.web import PAGE_FOOT, PAGE_HEAD, _timeline_html  # noqa: E402

BROWSER = next(
    (b for b in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ] if Path(b).exists()),
    None,
)

PROBE = """
<script>
window.addEventListener('load', function () {
  const out = {};
  const node = document.querySelector('.tl-node');
  const cap = document.querySelector('.tl-node .cap');
  const at = document.querySelector('.tl-node .at');
  const axis = document.querySelector('.tl-axis');
  const scroll = document.querySelector('.tl-scroll');
  const cs = el => el ? getComputedStyle(el) : null;
  function info(el, name) {
    if (!el) return null;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
      name,
      display: s.display,
      width: Math.round(r.width * 10) / 10,
      height: Math.round(r.height * 10) / 10,
      offsetWidth: el.offsetWidth,
      scrollWidth: el.scrollWidth,
      overflow: s.overflow,
      webkitLineClamp: s.webkitLineClamp || s['-webkit-line-clamp'],
      webkitBoxOrient: s.webkitBoxOrient || s['-webkit-box-orient'],
      whiteSpace: s.whiteSpace,
      textLength: (el.textContent || '').length,
    };
  }
  out.node = info(node, 'tl-node');
  out.at = info(at, 'tl-node .at');
  out.cap = info(cap, 'tl-node .cap');
  out.axis = info(axis, 'tl-axis');
  out.scroll = info(scroll, 'tl-scroll');
  out.bubble = info(document.querySelector('.tl-bubble'), 'tl-bubble');
  out.nodeCount = document.querySelectorAll('.tl-node').length;
  // 相邻节点的水平间距：用于确认节点之间是否留有视觉分隔
  const ns = [...document.querySelectorAll('.tl-node')].map(
    n => n.getBoundingClientRect());
  out.gaps = ns.slice(1).map((r, i) => Math.round(r.left - ns[i].right));
  out.nodePadding = getComputedStyle(node).paddingLeft + '/' +
                    getComputedStyle(node).paddingRight;
  const pre = document.createElement('pre');
  pre.id = 'probe-out';
  pre.textContent = JSON.stringify(out, null, 1);
  document.body.appendChild(pre);
});
</script>
"""


def build_html() -> str:
    hs = [
        {"at": f"00:0{i}:00", "sec": i * 60,
         "text": "这是一个比较长的节点简介用来检查是否会换行显示",
         "point_index": i, "detail": "气泡正文"}
        for i in range(4)
    ]
    return PAGE_HEAD + _timeline_html("BVTEST", hs) + PROBE + PAGE_FOOT


def main() -> int:
    if not BROWSER:
        print("找不到浏览器")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="vd-probe-"))
    page = tmp / "p.html"
    page.write_text(build_html(), encoding="utf-8")
    # 用 dump-dom 让 JS 执行后的 DOM 回传
    proc = subprocess.run(
        [BROWSER, "--headless=new", "--disable-gpu", "--virtual-time-budget=2000",
         "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
    )
    dom = proc.stdout or ""
    start = dom.find('<pre id="probe-out">')
    if start < 0:
        print("未取到探针输出，DOM 片段：")
        print(dom[-1200:])
        return 1
    body = dom[start + len('<pre id="probe-out">'):]
    body = body[: body.find("</pre>")]
    import html as _html

    data = json.loads(_html.unescape(body))
    for key, v in data.items():
        if v is None:
            print(f"{key}: (未找到)")
            continue
        if isinstance(v, int):
            print(f"{key}: {v}")
            continue
        print(f"--- {v['name']} ---")
        for k in ("display", "width", "height", "offsetWidth", "scrollWidth",
                  "overflow", "webkitLineClamp", "webkitBoxOrient", "whiteSpace",
                  "textLength"):
            print(f"   {k:18s} {v.get(k)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
