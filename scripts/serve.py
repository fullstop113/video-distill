"""启动 Web 服务：FastAPI + 串行工作线程。

用法：
    python scripts/serve.py                    # http://127.0.0.1:8788
    python scripts/serve.py --port 9000 --host 0.0.0.0
    python scripts/serve.py --reload           # 开发模式
"""

from __future__ import annotations

import argparse
import logging
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("serve")

DEFAULT_PORT = 8788


def main() -> int:
    ap = argparse.ArgumentParser(description="video-distill Web 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--out-dir", default=str(ROOT / "output"))
    ap.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = ap.parse_args()

    # 启动前先做几项硬检查，避免"服务起来了但根本没法用"
    from vd.credentials import operator_uid, owner_uid
    from vd.storage import list_uids

    if not operator_uid():
        log.warning("未配置 VD_OPERATOR_BILI_UID —— 取数会被拒绝，请先在 .env 里设置")
    if not owner_uid():
        log.warning("未配置 VD_OWNER_BILI_UID —— 大号不会被识别为管理员")
    uids = list_uids()
    log.info("已保存凭据：%s", uids or "（无，请到网页上扫码登录）")
    if operator_uid() and operator_uid() not in uids:
        log.warning("操作号 UID=%s 尚未登录，请到网页上扫码", operator_uid())

    import uvicorn

    from vd.web import app, set_worker
    from vd.worker import Worker

    if args.reload:
        # reload 模式下 uvicorn 会再起一个进程，工作线程放在应用内会重复；
        # 这里明确不支持 reload，避免出现两个工作线程抢同一个队列。
        log.error("--reload 与内置工作线程不兼容（会产生两个消费者）。请直接运行。")
        return 2

    worker = Worker(
        Path(args.out_dir),
    )
    set_worker(worker)
    worker.start()

    url = f"http://{args.host}:{args.port}/"
    log.info("服务已启动：%s", url)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
