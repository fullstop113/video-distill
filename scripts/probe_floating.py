"""用无头 Chrome 读取悬浮按钮的**计算样式与几何**。

为什么不用截图：悬浮按钮的显示依赖 scroll 事件，而截图模式（--screenshot）
下 JS 的滚动与事件时序不可靠 —— 实测截出来是空白页。
所以改为**程序化滚动 + 读取计算样式**，结果比截图更精确也可断言。

用法：
    python scripts/probe_floating.py
"""

from __future__ import annotations

import html as _html
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vd.web import PAGE_FOOT, PAGE_HEAD, _to_bottom_html  # noqa: E402

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
  const filler = document.getElementById('filler');
  for (let i = 0; i < 80; i++) {
    const p = document.createElement('p');
    p.className = 'kp-t';
    p.textContent = '第 ' + i + ' 段占位正文，用来把页面撑长，验证悬浮按钮。';
    filler.appendChild(p);
  }
  const out = {docHeight: document.documentElement.scrollHeight,
                viewHeight: window.innerHeight};
  const b = document.getElementById('to-bottom');
  const t = document.getElementById('to-top');
  const box = document.querySelector('.to-bottom');
  const g = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return {position: s.position, right: s.right, bottom: s.bottom,
            visibility: s.visibility, opacity: s.opacity, display: s.display,
            zIndex: s.zIndex, pointerEvents: s.pointerEvents,
            top: Math.round(r.top), left: Math.round(r.left),
            width: Math.round(r.width), height: Math.round(r.height),
            text: (el.textContent || '').trim()};
  };
  // ⚠️ 要检查**容器**的 position：按钮是容器的 flex 子项，它自己理应是 static。
  // 第一版查的是按钮，看到 static 就以为 CSS 没生效，白折腾一轮。
  out.container = g(box);
  out.beforeScroll = {bottom: g(b), top: g(t), cls: b.className};
  window.scrollTo(0, 900);
  window.dispatchEvent(new Event('scroll'));
  out.afterScroll = {scrollY: window.scrollY, bottom: g(b), top: g(t),
                     cls: b.className, tcls: t.className};
  // 把样式表里与 .to-bottom 相关的规则原样列出来 ——
  // 用来判断"类加上了但样式没生效"到底是规则缺失还是被覆盖
  // 直接给元素打标记并读回，确认我们操作与读取的是**同一个**元素
  b.dataset.probeMark = 'yes';
  const reread = document.querySelector('[data-probe-mark="yes"]');
  out.identity = {
    sameElement: reread === b,
    tag: b.tagName, id: b.id, cls: b.className,
    markReadBack: reread ? reread.dataset.probeMark : null,
    // 同一元素在加类后立刻读一次
    immediate: getComputedStyle(b).visibility,
  };
  // 过渡是否被冻结：headless 的 --virtual-time-budget 下动画/过渡可能不推进，
  // 于是 getComputedStyle 会停在过渡**起始值**（hidden）。
  // getAnimations() 能直接告诉我们有没有在跑的过渡。
  out.transition = {
    transitionProperty: getComputedStyle(b).transitionProperty,
    transitionDuration: getComputedStyle(b).transitionDuration,
    runningAnimations: (b.getAnimations ? b.getAnimations().length : 'n/a'),
    animationNames: (b.getAnimations ? b.getAnimations().map(a => a.transitionProperty || a.animationName) : []),
  };
  // 几何：按钮是否真的落在视口内（fixed 定位应当如此）
  const rect = b.getBoundingClientRect();
  out.geometry = {
    top: Math.round(rect.top), bottom: Math.round(rect.bottom),
    left: Math.round(rect.left), right: Math.round(rect.right),
    inViewport: rect.bottom > 0 && rect.top < window.innerHeight &&
                rect.right > 0 && rect.left < window.innerWidth,
  };
  // 命中测试：这个坐标上最上层是谁？能命中按钮说明它真的在那儿
  const hit = document.elementFromPoint(
    Math.round(rect.left + rect.width / 2), Math.round(rect.top + rect.height / 2));
  out.geometry.hitIsButton = hit === b || (hit && b.contains(hit));
  out.geometry.hitTag = hit ? (hit.tagName + '#' + (hit.id || '') + '.' + (hit.className || '')) : null;
  const rules = [];
  for (const sheet of document.styleSheets) {
    let list;
    try { list = sheet.cssRules; } catch (e) { rules.push('ERR:' + e); continue; }
    for (const r of list) {
      const t = r.selectorText || '';
      if (t.indexOf('to-bottom') >= 0) {
        rules.push(t + ' { ' + r.style.cssText + ' }');
      }
    }
  }
  out.rules = rules;
  // 再取一次计算值：确认不是读取时机问题
  out.afterReflow = {visibility: getComputedStyle(b).visibility,
                     opacity: getComputedStyle(b).opacity};
  const y = window.scrollY || document.documentElement.scrollTop;
  out.computed = {
    scrollY: y,
    threshold: window.innerHeight * 0.6,
    docHeight: document.documentElement.scrollHeight,
    innerHeight: window.innerHeight,
    nearBottom: y + window.innerHeight >= document.documentElement.scrollHeight - 120,
    shouldShowBottom: y > window.innerHeight * 0.6 &&
      !(y + window.innerHeight >= document.documentElement.scrollHeight - 120),
  };
  const pre = document.createElement('pre');
  pre.id = 'probe-out';
  pre.textContent = JSON.stringify(out, null, 1);
  document.body.appendChild(pre);
});
</script>
"""


def main() -> int:
    if not BROWSER:
        print("找不到浏览器")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="vd-float-"))
    page = tmp / "p.html"
    page.write_text(
        PAGE_HEAD
        + "<header><h1>BV1TEST</h1></header><div class='card' id='filler'></div>"
        + "<h2>下载</h2><div class='card row'><a class='pill' href='#'>↓ 总结与要点</a></div>"
        + _to_bottom_html() + PROBE + PAGE_FOOT,
        encoding="utf-8",
    )
    proc = subprocess.run(
        [BROWSER, "--headless=new", "--disable-gpu", "--virtual-time-budget=2500",
         "--window-size=1180,780", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    dom = proc.stdout or ""
    start = dom.find('<pre id="probe-out">')
    if start < 0:
        print("未取到探针输出：")
        print(dom[-900:])
        return 1
    body = dom[start + len('<pre id="probe-out">'):]
    data = json.loads(_html.unescape(body[: body.find("</pre>")]))

    print(f"文档高度 {data['docHeight']}px  视口 {data['viewHeight']}px")

    box = data.get("container") or {}
    print("\n--- 容器 .to-bottom（**这个才该是 fixed**） ---")
    print(f"   position={box.get('position')} right={box.get('right')} "
          f"bottom={box.get('bottom')} zIndex={box.get('zIndex')}")

    for phase in ("beforeScroll", "afterScroll"):
        blk = data.get(phase) or {}
        print(f"\n--- {phase} ---")
        if "scrollY" in blk:
            print(f"   scrollY = {blk['scrollY']}")
        for key in ("bottom", "top"):
            v = blk.get(key)
            if not v:
                continue
            print(f"   {key:7s} visibility={v['visibility']} opacity={v['opacity']} "
                  f"尺寸={v['width']}x{v['height']} className={blk.get('cls', '')!r}")
    c = data.get("computed") or {}
    print("\n--- sync() 的判据重算 ---")
    for k, v in c.items():
        print(f"   {k:18s} {v}")

    print("\n--- 样式表里解析出的 .to-bottom 规则 ---")
    for r in data.get("rules") or []:
        print("   " + r[:160])
    print("\n--- 强制回流后再读一次 ---")
    print("  ", data.get("afterReflow"))
    print("\n--- 元素身份核对 ---")
    for k, v in (data.get("identity") or {}).items():
        print(f"   {k:16s} {v}")
    print("\n--- 过渡是否被冻结 ---")
    for k, v in (data.get("transition") or {}).items():
        print(f"   {k:20s} {v}")
    print("\n--- 几何与命中测试 ---")
    for k, v in (data.get("geometry") or {}).items():
        print(f"   {k:20s} {v}")
    # 关键断言：滚动一屏后按钮必须可见，且容器固定在视口上
    ok = (box.get("position") == "fixed"
          and (data.get("afterScroll", {}).get("bottom", {}).get("visibility") == "visible"))
    print("\n" + ("VERDICT PASS" if ok else "VERDICT FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
