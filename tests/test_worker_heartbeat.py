"""僵尸任务恢复测试。

## 背景（实测的 bug）

服务进程被强杀时任务会永远停在 `RUNNING`。为此有 `tasks.requeue_stale()`
在 worker 启动时回收，但实测**它一条都收不回来**：

    f30a8c9c0978  running  heartbeat='2026-10-12T19:46:39'
    cutoff = 18:48:42      会被回收的条数 = 0

原因有两层，缺一不可：

1. **假心跳** —— `heartbeat` 原先只在任务开始和结束时各写一次。
   所以刚被杀的僵尸任务心跳是"几分钟前"，
   **既不满足 `heartbeat=''`，也不满足 `heartbeat < now-3600s`**。
2. **阈值过大** —— 默认 3600 秒，僵尸任务要卡满 1 小时。

修法：运行期每 15 秒刷新心跳；回收阈值 90 秒
（= 6 个心跳周期，容忍偶发延迟而不误杀在跑的任务）。
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.tasks")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "tasks.sqlite3")
    return mod


def _heartbeat_of(store, task_id: str) -> str:
    with closing(store._connect()) as conn:
        row = conn.execute("SELECT heartbeat FROM tasks WHERE id=?",
                           (task_id,)).fetchone()
    return row["heartbeat"] if row else ""


def _set_heartbeat(store, task_id: str, value: str) -> None:
    with closing(store._connect()) as conn:
        with conn:
            conn.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (value, task_id))


def _claim(store, task_id: str, stage: str = "transcript") -> None:
    """把任务推进到 running 状态，并**附带新鲜心跳**（模拟真实认领）。

    ⚠️ 两个都容易漏：
      · `tasks.update(stage=...)` **不会**改 status —— 它只改
        stage/message/heartbeat。所以必须显式置位，否则
        `requeue_stale`（只处理 running）看不到它。
      · 心跳也要一起写。真实的 `claim_next()` 会写，
        测试里若不写，任务会被当成"心跳为空的僵尸"而误回收
        —— 我第一版就是这样，导致"活任务不该被动"那条测试假失败。
    """
    with closing(store._connect()) as conn:
        with conn:
            conn.execute(
                "UPDATE tasks SET status='running', stage=?, heartbeat=? "
                "WHERE id=?",
                (stage, dt.datetime.now().isoformat(timespec="seconds"),
                 task_id),
            )


# ======================================================= 心跳真的在跳

def test_heartbeat_advances_while_task_runs(store, monkeypatch):
    """**回归**：运行期间心跳必须真的被刷新。

    ⚠️ 等待时间必须**大于心跳时间戳的精度**（`_now()` 用
    `timespec="seconds"`，即 1 秒）。等 0.6 秒的话两次读数会
    落在同一秒里，测试会假失败 —— 我第一版就是这么写的。
    这里等 2.2 秒，配合 0.15 秒的间隔，保证至少刷新过 2 次。
    """
    from vd.worker import _heartbeat

    t = store.create("BVHEART")
    _claim(store, t.id, "meta")
    first = _heartbeat_of(store, t.id)

    with _heartbeat(t.id, interval=0.15):
        time.sleep(2.2)

    later = _heartbeat_of(store, t.id)
    assert later > first, (
        f"心跳没有前进（{first!r} → {later!r}）—— 又是假心跳了"
    )


def test_heartbeat_stops_after_context_exit(store):
    """退出后不该继续刷 —— 否则僵尸任务永远不满足回收条件。"""
    from vd.worker import _heartbeat

    t = store.create("BVSTOP")
    with _heartbeat(t.id, interval=0.1):
        time.sleep(0.3)
    after_exit = _heartbeat_of(store, t.id)
    time.sleep(0.5)
    assert _heartbeat_of(store, t.id) == after_exit, "心跳线程没停"


# ======================================================= 僵尸回收

def test_requeue_stale_recovers_old_heartbeat(store):
    """心跳停很久的任务要被放回队列。"""
    t = store.create("BVDEAD")
    _claim(store, t.id)                              # 进入 running
    old = (dt.datetime.now() - dt.timedelta(seconds=600)).isoformat(
        timespec="seconds")
    _set_heartbeat(store, t.id, old)

    assert store.get(t.id).status.value == "running"
    assert store.requeue_stale(timeout_seconds=90) == 1
    assert store.get(t.id).status.value == "queued", "僵尸任务没被回收"


def test_requeue_stale_keeps_fresh_heartbeat(store):
    """**关键**：心跳新鲜的任务不能被动 —— 那是正在跑的任务。"""
    t = store.create("BVALIVE")
    _claim(store, t.id, "summarize")
    assert store.requeue_stale(timeout_seconds=90) == 0
    assert store.get(t.id).status.value == "running", "正在跑的任务被误回收了"


def test_requeue_stale_recovers_empty_heartbeat(store):
    """心跳为空（老数据 / 从未更新）也要回收。"""
    t = store.create("BVEMPTY")
    with closing(store._connect()) as conn:
        with conn:
            conn.execute("UPDATE tasks SET status='running', heartbeat='' "
                         "WHERE id=?", (t.id,))
    assert store.requeue_stale(timeout_seconds=90) == 1


def test_recovered_task_gets_requeued_message(store):
    """要让使用者看到"上次中断"而不是一脸茫然。"""
    t = store.create("BVMSG")
    _claim(store, t.id)
    _set_heartbeat(store, t.id,
                   (dt.datetime.now() - dt.timedelta(seconds=600)).isoformat(
                       timespec="seconds"))
    store.requeue_stale(timeout_seconds=90)
    got = store.get(t.id)
    assert "中断" in (got.message or "")
    assert got.stage == "queued"


# ======================================================= 阈值与间隔的关系

def test_recovery_threshold_is_much_larger_than_heartbeat_interval():
    """阈值必须**明显大于**心跳间隔，否则会把在跑的任务误回收。

    实测教训：阈值 3600 秒 + 假心跳 = 僵尸任务一条收不回来。
    但反过来把阈值调到小于心跳间隔同样有害（正在跑的会被反复打断）。
    这条测试锁住两者的关系。
    """
    import inspect

    mod = importlib.import_module("vd.worker")
    src = inspect.getsource(mod.Worker.start)
    assert "requeue_stale(timeout_seconds=90)" in src, \
        "回收阈值被改动，请重新确认它与 HEARTBEAT_INTERVAL 的关系"

    interval = mod.HEARTBEAT_INTERVAL
    assert interval < 90 / 3, (
        f"心跳间隔 {interval}s 相对 90s 阈值太接近 —— "
        f"网络抖动或 GC 停顿就可能让活任务被误判为死亡"
    )
