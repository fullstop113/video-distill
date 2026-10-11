"""本地二维码看板：一个自动刷新的网页，永远显示**当前有效**的登录二维码。

为什么需要它：B 站二维码实测只有约 182 秒有效期（docs/p0-findings.md §10）。
"生成图片 → 发给使用者 → 等他扫"这种回合制很容易错过窗口，
而且图片文件同名时还会被浏览器缓存，导致扫到已经死掉的旧码。

这个看板直接读 `output/login-qr-*.png` 里**最新**的那张，
每 3 秒自动刷新，并显示"这张码是什么时候生成的、大概还能用多久"。
配合 `scripts/login_qr_loop.py`（持续生成新码）使用。

用法：
    python scripts/qr_board.py            # 默认 http://127.0.0.1:8799
    python scripts/qr_board.py --port 8800
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
# 实测二维码有效期约 182 秒
QR_LIFETIME = 182

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>B 站扫码登录</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background:#14161a; color:#e8eaed; display:flex; flex-direction:column;
         align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  h1 {{ font-size:20px; font-weight:600; margin:0 0 4px; }}
  .sub {{ color:#9aa0a6; font-size:13px; margin-bottom:18px; }}
  .card {{ background:#fff; padding:14px; border-radius:14px; box-shadow:0 8px 32px rgba(0,0,0,.5); }}
  img {{ display:block; width:300px; height:300px; image-rendering:pixelated; }}
  .meta {{ margin-top:16px; font-size:13px; color:#9aa0a6; text-align:center; line-height:1.7; }}
  .live {{ color:#34d399; font-weight:600; }}
  .warn {{ color:#fbbf24; font-weight:600; }}
  .dead {{ color:#f87171; font-weight:600; }}
  code {{ background:#22252a; padding:1px 6px; border-radius:4px; color:#c9ccd1; }}
</style>
</head>
<body>
  <h1>B 站扫码登录</h1>
  <div class="sub">请用 B 站手机客户端扫描，并在手机上确认</div>
  <div class="card"><img id="qr" src="/qr.png?t={t}" alt="二维码"></div>
  <div class="meta" id="meta">加载中…</div>
  <div class="meta">本页每 3 秒自动刷新，永远显示最新一张。<br>
    若提示已过期，稍等片刻即可，后台会持续生成新码。</div>
<script>
let shownAt = Date.now();
// 每次加载都请求一个不同的地址（?t=当前毫秒），从根上避免浏览器复用旧图。
// 仅靠响应头的 no-store 并不总是够用，实测踩过这个坑。
document.getElementById('qr').src = '/qr.png?t=' + Date.now();
function tick() {{
  const el = document.getElementById('meta');
  const age = Math.floor((Date.now() - shownAt) / 1000);
  const left = {lifetime} - age;
  if (left > 20) {{
    el.innerHTML = '当前二维码生成于 <b>' + new Date(shownAt).toLocaleTimeString('zh-CN') +
                   '</b>　<span class="live">● 有效</span>　剩余约 <b>' + left + '</b> 秒';
  }} else if (left > 0) {{
    el.innerHTML = '<span class="warn">⚠ 即将过期</span>　剩余约 <b>' + left + '</b> 秒，请稍候新码';
  }} else {{
    el.innerHTML = '<span class="dead">✕ 已过期</span>　正在等待新码…';
  }}
}}
setInterval(tick, 1000); tick();
setInterval(() => location.reload(), 3000);
</script>
</body>
</html>
"""


def newest_qr() -> tuple[Path | None, dt.datetime | None]:
    files = sorted(OUT.glob("login-qr-*.png"), key=lambda p: p.stat().st_mtime)
    files = [p for p in files if p.name != "login-qr-latest.png"] or files
    if not files:
        return None, None
    p = files[-1]
    return p, dt.datetime.fromtimestamp(p.stat().st_mtime)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 安静点
        pass

    def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 明确禁止缓存，否则浏览器会拿旧二维码
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/qr.png"):
            path, mtime = newest_qr()
            if path is None:
                self._send(404, "text/plain; charset=utf-8", "暂无二维码，请先运行 login_qr_loop.py".encode())
                return
            self._send(200, "image/png", path.read_bytes())
            return
        if self.path in ("/", "/index.html"):
            _, mtime = newest_qr()
            t = int(mtime.timestamp()) if mtime else 0
            html = PAGE.format(t=t, lifetime=QR_LIFETIME).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", html)
            return
        self._send(404, "text/plain", b"not found")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"QR_BOARD {url}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
