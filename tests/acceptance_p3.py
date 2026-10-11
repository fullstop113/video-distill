"""P3 端到端验收：真实 HTTP 路由 + 真实工作线程 + 真实验证过的视频。

## 为什么不用"开发模式跳过登录"

为了让测试能提交任务，最容易的做法是加一个 `VD_DEV_NO_AUTH=1` 之类的开关。
**那是把测试便利变成长期安全风险** —— 开关会被忘记关掉，而且一旦存在就会被依赖。

所以这里用 `TestClient` 在**进程内**走真实路由：会话通过 `web._login_for_test()`
（与会话结构同一个定义）注入，任务由**真实 Worker** 消费，
产物写到临时目录。整个过程不需要给生产代码开后门。

用法：
    python tests/acceptance_p3.py            # 用已缓存视频，约 10 秒
    python tests/acceptance_p3.py --bvid XXX # 指定视频
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
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

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001
        FAIL.append(f"{name} —— {type(exc).__name__}: {exc}")
        print(f"  ❌ {name}\n       {type(exc).__name__}: {exc}")
        return
    PASS.append(name)
    print(f"  ✅ {name}" + (f"\n       {detail}" if isinstance(detail, str) else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", default="BV1NXH661EpG", help="默认用已缓存过的视频，跑得快")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    print("=" * 70)
    print("P3 端到端验收（真实路由 + 真实 worker）")
    print("=" * 70)

    from fastapi.testclient import TestClient

    from vd import tasks as tstore
    from vd import web
    from vd.credentials import load_operator_credentials, operator_uid
    from vd.worker import Worker

    # 用临时目录做产物与任务库，避免污染真实 data/ 与 output/
    tmp = Path(tempfile.mkdtemp(prefix="vd-p3-"))
    orig_tasks_db = tstore.DB_PATH
    tstore.DB_PATH = tmp / "tasks.sqlite3"
    orig_out = web.OUTPUT_DIR
    web.OUTPUT_DIR = tmp / "output"
    web.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    worker = Worker(web.OUTPUT_DIR, poll_interval=0.3)
    web.set_worker(worker)
    client = TestClient(web.app)

    try:
        print("\n【1】前置条件")
        check("操作号凭据可用", lambda: (
            f"UID={load_operator_credentials().uid}"
        ))
        check("工作线程已启动", lambda: (worker.start() or "已启动"))

        print("\n【2】会话与权限（真实路由）")
        # 本机访问现在是免登录的（凭据已在磁盘上），
        # 所以"必须登录"这条边界要用**远程来源**来验证
        from fastapi.testclient import TestClient

        remote = TestClient(web.app, client=("203.0.113.7", 41000))
        r = remote.post("/tasks", data={"bvid": args.bvid}, follow_redirects=False)
        check("远程无会话被挡回登录", lambda: (
            r.status_code == 303 and "need_login" in r.headers["location"]
            or (_ for _ in ()).throw(
                AssertionError(f"远程无会话竟被放行：HTTP {r.status_code}")
            )
        ) and f"HTTP {r.status_code} → {r.headers['location']}")

        r2 = remote.get("/")
        check("远程访问显示登录卡片", lambda: (
            "需要扫码登录" in r2.text
            or (_ for _ in ()).throw(AssertionError("远程首页没有登录入口"))
        ) and "已显示")

        # 伪造 Host 头不能白拿本机待遇
        r3 = remote.get("/", headers={"Host": "localhost:8788"})
        check("伪造 Host 头无效", lambda: (
            "需要扫码登录" in r3.text
            or (_ for _ in ()).throw(AssertionError("Host 头伪造成功！"))
        ) and "仍要求登录")

        token = web._login_for_test(operator_uid(), "验收测试")
        client.cookies.set(web.SESSION_COOKIE, token)
        check("本机自动会话可访问首页", lambda: (
            client.get("/").status_code == 200
            or (_ for _ in ()).throw(AssertionError("首页失败"))
        ) and "HTTP 200")

        print("\n【3】提交任务并等待完成")
        r = client.post("/tasks", data={"bvid": args.bvid}, follow_redirects=False)
        assert r.status_code == 303, f"提交失败：HTTP {r.status_code} {r.text[:200]}"
        loc = r.headers["location"]
        task_id = loc.rsplit("/", 1)[-1]
        print(f"       任务 {task_id} 已入队")

        deadline = time.time() + args.timeout
        last_stage = ""
        while time.time() < deadline:
            data = client.get(f"/tasks/{task_id}/json").json()
            if data["stage"] != last_stage:
                last_stage = data["stage"]
                print(f"       [{data['progress']:3d}%] {data['stage_label']} — {data['message'][:60]}")
            if data["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        else:
            raise AssertionError(f"任务在 {args.timeout}s 内未完成")

        data = client.get(f"/tasks/{task_id}/json").json()
        assert data["status"] == "done", f"任务失败：{data.get('error')}"
        res = data["result"]
        check("任务完成", lambda: (
            f"{res['points']} 条要点（{res['anchored_points']} 条带锚点）/ "
            f"{res['chars']} 字 / ¥{res['cost_cny']} / 路线 {res['transcript_source']}"
        ))

        print("\n【4】产物与下载")
        check("逐字稿文件已生成", lambda: (
            (web.OUTPUT_DIR / res["transcript_file"]).exists()
            or (_ for _ in ()).throw(AssertionError("逐字稿不存在"))
        ) and res["transcript_file"])
        check("总结文件已生成", lambda: (
            (web.OUTPUT_DIR / res["summary_file"]).exists()
            or (_ for _ in ()).throw(AssertionError("总结不存在"))
        ) and res["summary_file"])
        check("HTTP 可下载总结", lambda: (
            client.get(f"/files/{res['summary_file']}").status_code == 200
            or (_ for _ in ()).throw(AssertionError("下载失败"))
        ) and "HTTP 200")
        check("下载拒绝路径穿越", lambda: (
            client.get("/files/../tasks.sqlite3").status_code in (400, 404)
            or (_ for _ in ()).throw(AssertionError("穿越成功了！"))
        ) and "已拒绝")

        print("\n【5】重复提交复用")
        r2 = client.post("/tasks", data={"bvid": args.bvid}, follow_redirects=False)
        check("同一视频不重复排队", lambda: (
            tstore.stats()["total"] == 1
            or (_ for _ in ()).throw(AssertionError(f"任务数 {tstore.stats()['total']}"))
        ) and "队列里仍只有 1 个任务")

        print("\n【6】产物内容抽查")
        md = (web.OUTPUT_DIR / res["summary_file"]).read_text(encoding="utf-8")
        check("产物含免责声明", lambda: (
            "请勿转载" in md or (_ for _ in ()).throw(AssertionError("缺声明"))
        ) and "已包含")
        check("要点带时间戳锚点", lambda: (
            md.count("[0") >= 3 or (_ for _ in ()).throw(AssertionError("锚点太少"))
        ) and f"{md.count('[0')} 处时间戳")

        print("\n【7】免责声明页面渲染")
        h = web._disclaimer_html()
        check("无残留 Markdown 标记", lambda: (
            "**" not in h or (_ for _ in ()).throw(AssertionError("仍有 ** 标记"))
        ) and "干净")
        check("结构化为标题与列表", lambda: (
            h.count("<h4>") >= 5 and h.count("<li>") >= 15
            or (_ for _ in ()).throw(AssertionError("结构缺失"))
        ) and f"{h.count('<h4>')} 个章节 / {h.count('<li>')} 条")

        print("\n【8】二维码通道")
        qs = client.post("/login/start").json()
        check("登录二维码可生成并出图", lambda: (
            qs["ok"] and client.get(f"/login/qr.png?key={qs['key']}").status_code == 200
            or (_ for _ in ()).throw(AssertionError("二维码接口失败"))
        ) and f"有效期 {qs['expires_in']}s")

    finally:
        worker.stop()
        web.OUTPUT_DIR = orig_out
        tstore.DB_PATH = orig_tasks_db
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)}   失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    print("=" * 70)
    print("VERDICT " + ("PASS" if not FAIL else "FAIL"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
