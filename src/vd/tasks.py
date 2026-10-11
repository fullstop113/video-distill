"""任务存储：SQLite 持久化的任务队列与结果。

## 为什么不用 Celery / Redis

方案 §5.2① 自己写了：

> 需要一个队列（建议先用简单的进程内队列 + SQLite 持久化，别一上来上 Celery/Redis）

理由在本项目里更充分：**GPU 是串行资源，队列的并发度永远是 1**。
一个并发度恒为 1 的队列，用不上 Celery 的分布式调度能力，
却要额外背一个 Redis 进程和一套部署复杂度。

所以这里是「SQLite 表 + 进程内工作线程」：
  - 持久化：进程重启后队列不丢（SQLite）；
  - 串行：工作线程只有一个，天然满足"GPU 串行"；
  - 可观测：任务状态、阶段、进度、错误都能直接查表。

## 连接管理

注意 `sqlite3` 的 `with` **只管事务、不关闭连接**（在 vd/quota.py 里踩过）。
所有连接一律用 `closing()` 包裹。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "tasks.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    bvid        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',     -- 视频标题（冗余存储，列表页不用解析 result）
    up_name     TEXT NOT NULL DEFAULT '',     -- UP 主昵称（冗余存储，列表页不用解析 result）
    status      TEXT NOT NULL,
    stage       TEXT NOT NULL DEFAULT '',
    progress    INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    requester   TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT '{}',
    artifacts   TEXT NOT NULL DEFAULT '{}',   -- 产物清单 [{name,kind,size}]，用于"我的产物"页
    lineage     TEXT NOT NULL DEFAULT '{}',   -- 生成条件（分片数、ASR 设备等）
    created_at  TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    heartbeat   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_requester ON tasks(requester, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_bvid ON tasks(bvid);
"""
# ⚠️ 队列顺序一律用自增 seq，**不要用 created_at**。
# 踩过的坑：created_at 只有秒级精度，同一秒内创建的两个任务排序不确定，
# 于是"你在队列第 2 位"可能算成第 1 位 —— 队列位置是给用户看的，错了就是骗人。

# 给已存在的库补列。`CREATE TABLE IF NOT EXISTS` 不会改动旧表结构，
# 所以新增字段必须显式 ALTER —— 否则老库升级后会直接报 "no such column"。
_MIGRATIONS: list[tuple[str, str]] = [
    ("title", "ALTER TABLE tasks ADD COLUMN title TEXT NOT NULL DEFAULT ''"),
    ("up_name", "ALTER TABLE tasks ADD COLUMN up_name TEXT NOT NULL DEFAULT ''"),
    ("artifacts", "ALTER TABLE tasks ADD COLUMN artifacts TEXT NOT NULL DEFAULT '{}'"),
    ("lineage", "ALTER TABLE tasks ADD COLUMN lineage TEXT NOT NULL DEFAULT '{}'"),
]


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED)


# 阶段 → 展示名与进度百分比。进度是"阶段级"的，不做假精度。
STAGES: list[tuple[str, str, int]] = [
    ("queued", "排队中", 0),
    ("meta", "获取视频信息", 10),
    ("transcript", "获取逐字稿", 35),
    ("summarize", "生成总结与要点", 70),
    ("render", "写入产物", 90),
    ("done", "完成", 100),
]
STAGE_LABEL = {k: label for k, label, _ in STAGES}
STAGE_PROGRESS = {k: pct for k, _, pct in STAGES}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补新增的列（幂等）。

    注意用 PRAGMA 读**现有列**再决定是否 ALTER，
    而不是靠 try/except：后者会把"列已存在"和"ALTER 语法错误"混为一谈。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, ddl in _MIGRATIONS:
        if col not in have:
            conn.execute(ddl)
    conn.commit()


@dataclass
class Task:
    id: str
    bvid: str
    title: str = ""
    up_name: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    stage: str = "queued"
    progress: int = 0
    message: str = ""
    error: str = ""
    requester: str = ""
    result: dict = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    lineage: dict = field(default_factory=dict)
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    heartbeat: str = ""
    seq: int = 0          # 队列顺序（自增，唯一可靠的排序依据）

    @property
    def stage_label(self) -> str:
        return STAGE_LABEL.get(self.stage, self.stage)

    @property
    def display_title(self) -> str:
        """列表页展示用标题，拿不到就退回 BV 号。"""
        return self.title or self.result.get("title") or ""

    @property
    def display_up_name(self) -> str:
        """列表页展示用 UP 主昵称，冗余列优先，回退到 result。"""
        return self.up_name or self.result.get("owner_name") or ""

    def to_public(self) -> dict:
        """给前端看的字段（隐藏内部细节）。"""
        return {
            "id": self.id,
            "bvid": self.bvid,
            "title": self.display_title,
            "up_name": self.display_up_name,
            "status": self.status.value,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "artifacts": self.artifacts,
            "result": self.result,
            "seq": self.seq,
        }


def _load_json(raw, default):
    """容错解析 JSON 列。坏数据不该让整行查询失败。"""
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_task(row: sqlite3.Row) -> Task:
    keys = row.keys()
    return Task(
        id=row["id"],
        bvid=row["bvid"],
        title=row["title"] if "title" in keys else "",
        up_name=row["up_name"] if "up_name" in keys else "",
        status=TaskStatus(row["status"]),
        stage=row["stage"],
        progress=row["progress"],
        message=row["message"],
        error=row["error"],
        requester=row["requester"],
        result=_load_json(row["result"], {}),
        artifacts=_load_json(row["artifacts"], []) if "artifacts" in keys else [],
        lineage=_load_json(row["lineage"], {}) if "lineage" in keys else {},
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        heartbeat=row["heartbeat"],
        seq=row["seq"],
    )


def create(bvid: str, requester: str = "") -> Task:
    task = Task(id=uuid.uuid4().hex[:12], bvid=bvid, requester=requester, created_at=_now())
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """INSERT INTO tasks(id, bvid, status, stage, progress, requester, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (task.id, task.bvid, task.status.value, task.stage, task.progress,
                 task.requester, task.created_at),
            )
    return task


def get(task_id: str) -> Task | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def find_active(bvid: str) -> Task | None:
    """同一视频已有排队/运行中的任务就复用它，避免重复烧 GPU。"""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE bvid=? AND status IN (?,?) ORDER BY seq LIMIT 1",
            (bvid, TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
        ).fetchone()
    return _row_to_task(row) if row else None


# 刚完成的任务在多长时间内可以直接复用（秒）。
# 起因：只复用"活跃"任务时，用户连点两次提交就会跑两遍 —— 第二遍白花 LLM 的钱。
# 而"刚生成过"的内容重跑一遍通常也没有意义。0 表示不复用已完成任务。
DEFAULT_REUSE_WINDOW = 600


def reuse_window() -> int:
    raw = os.getenv("VD_TASK_REUSE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_REUSE_WINDOW
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_REUSE_WINDOW


def find_reusable(bvid: str, window: int | None = None) -> Task | None:
    """找出可以复用的任务：活跃的，或刚刚成功完成的。

    复用"刚完成的"要谨慎 —— 用户可能确实想重跑。所以：
      - 只复用 `done`，**不复用 `failed`**（失败就该能重试）；
      - 窗口默认 10 分钟，可用 `VD_TASK_REUSE_SECONDS` 调整，设 0 关闭。
    """
    active = find_active(bvid)
    if active:
        return active
    w = reuse_window() if window is None else window
    if w <= 0:
        return None
    cutoff = (dt.datetime.now() - dt.timedelta(seconds=w)).isoformat(timespec="seconds")
    with closing(_connect()) as conn:
        row = conn.execute(
            """SELECT * FROM tasks WHERE bvid=? AND status=? AND finished_at >= ?
               ORDER BY seq DESC LIMIT 1""",
            (bvid, TaskStatus.DONE.value, cutoff),
        ).fetchone()
    return _row_to_task(row) if row else None


def claim_next() -> Task | None:
    """原子地取下一个待处理任务并标记为运行中。

    用 `UPDATE ... WHERE status='queued'` 的 rowcount 做乐观锁：
    即使将来误开了多个工作线程，也不会两个线程拿到同一个任务。
    """
    with closing(_connect()) as conn:
        with conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE status=? ORDER BY seq LIMIT 1",
                (TaskStatus.QUEUED.value,),
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                """UPDATE tasks SET status=?, stage=?, progress=?, started_at=?, heartbeat=?
                   WHERE id=? AND status=?""",
                (TaskStatus.RUNNING.value, "meta", STAGE_PROGRESS["meta"], _now(), _now(),
                 row["id"], TaskStatus.QUEUED.value),
            )
            if cur.rowcount != 1:
                return None
        return get(row["id"])


def update(
    task_id: str,
    *,
    stage: str | None = None,
    message: str | None = None,
    result: dict | None = None,
    touch: bool = True,
) -> None:
    sets: list[str] = []
    args: list = []
    if stage is not None:
        sets.append("stage=?")
        args.append(stage)
        sets.append("progress=?")
        args.append(STAGE_PROGRESS.get(stage, 0))
    if message is not None:
        sets.append("message=?")
        args.append(message)
    if result is not None:
        sets.append("result=?")
        args.append(json.dumps(result, ensure_ascii=False))
    if touch:
        sets.append("heartbeat=?")
        args.append(_now())
    if not sets:
        return
    args.append(task_id)
    with closing(_connect()) as conn:
        with conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)


def finish(task_id: str, *, result: dict | None = None) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """UPDATE tasks SET status=?, stage=?, progress=?, finished_at=?, heartbeat=?,
                   result=? WHERE id=?""",
                (TaskStatus.DONE.value, "done", 100, _now(), _now(),
                 json.dumps(result or {}, ensure_ascii=False), task_id),
            )


def fail(task_id: str, error: str, *, result: dict | None = None) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """UPDATE tasks SET status=?, error=?, finished_at=?, heartbeat=?,
                   result=? WHERE id=?""",
                (TaskStatus.FAILED.value, error[:2000], _now(), _now(),
                 json.dumps(result or {}, ensure_ascii=False), task_id),
            )


def list_recent(limit: int = 30, requester: str = "", offset: int = 0) -> list[Task]:
    with closing(_connect()) as conn:
        if requester:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE requester=? ORDER BY seq DESC LIMIT ? OFFSET ?",
                (requester, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY seq DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
    return [_row_to_task(r) for r in rows]


def count_tasks(requester: str = "", status: str = "") -> int:
    """分页用的总数。`status` 传 TaskStatus.value 可只数某一类。"""
    sql = "SELECT COUNT(*) AS n FROM tasks WHERE 1=1"
    args: list = []
    if requester:
        sql += " AND requester=?"
        args.append(requester)
    if status:
        sql += " AND status=?"
        args.append(status)
    with closing(_connect()) as conn:
        return int(conn.execute(sql, args).fetchone()["n"])


def page_tasks(
    *, limit: int = 20, page: int = 1, requester: str = "", status: str = ""
) -> tuple[list[Task], int, int, int]:
    """分页查询。

    Returns:
        (当前页任务, 总条数, 总页数, **收敛后的页码**)

    页码越界时收敛到有效范围，而且必须把收敛后的页码**返回给调用方**：
    踩过的坑 —— 这里把局部变量 `page` 改小了，但渲染分页条用的是调用方
    传进来的原值，于是出现"数据是第 3 页、页码写着第 99/3 页"的错乱。
    收敛只发生在函数内部，调用方是看不见的。
    """
    total = count_tasks(requester=requester, status=status)
    pages = max(1, (total + limit - 1) // limit)
    page = min(max(page, 1), pages)
    sql = "SELECT * FROM tasks WHERE 1=1"
    args: list = []
    if requester:
        sql += " AND requester=?"
        args.append(requester)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY seq DESC LIMIT ? OFFSET ?"
    args += [limit, (page - 1) * limit]
    with closing(_connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_task(r) for r in rows], total, pages, page


def set_title(task_id: str, title: str) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute("UPDATE tasks SET title=? WHERE id=?", (title[:300], task_id))


def set_up_name(task_id: str, up_name: str) -> None:
    """冗余存 UP 主昵称，列表页不用解析 result。"""
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                "UPDATE tasks SET up_name=? WHERE id=?",
                (up_name[:100], task_id),
            )


def set_artifacts(task_id: str, artifacts: list[dict]) -> None:
    """记录产物清单（名称/类型/字节数）。

    冗余存进表里，是为了"我的产物"页能直接列出**大小与类型**而不必每次去
    扫 output 目录；同时保留 task → 产物 的关联（目录列表给不了这个信息）。
    """
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                "UPDATE tasks SET artifacts=? WHERE id=?",
                (json.dumps(artifacts, ensure_ascii=False), task_id),
            )


def set_lineage(task_id: str, *, n_chunks: int = 0, asr_device: str = "") -> None:
    """记录产物的生成条件，供"我的产物"页展示。

    只存**对复现有意义**的字段，不是把整个 result 再存一遍。
    """
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                "UPDATE tasks SET lineage=? WHERE id=?",
                (json.dumps({"n_chunks": n_chunks, "asr_device": asr_device},
                            ensure_ascii=False), task_id),
            )


def all_artifacts(requester: str = "", limit: int = 500) -> list[dict]:
    """列出所有已生成产物（跨任务），按时间倒序。

    产物是**交付物**，任务记录只是"某次运行"。两者生命周期不同：
    任务会越堆越多，产物才是要沉淀下来反复取用的东西。
    """
    sql = ("SELECT id, bvid, title, requester, artifacts, lineage, finished_at, result "
           "FROM tasks WHERE status=? AND artifacts != '{}'")
    args: list = [TaskStatus.DONE.value]
    if requester:
        sql += " AND requester=?"
        args.append(requester)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    out: list[dict] = []
    with closing(_connect()) as conn:
        for row in conn.execute(sql, args).fetchall():
            try:
                items = json.loads(row["artifacts"] or "[]")
            except json.JSONDecodeError:
                continue
            result = _load_json(row["result"], {})
            lineage = _load_json(row["lineage"], {})
            for it in items:
                out.append({
                    "task_id": row["id"],
                    "bvid": row["bvid"],
                    "title": row["title"] or result.get("title") or row["bvid"],
                    "kind": it.get("kind", ""),
                    "name": it.get("name", ""),
                    "size": int(it.get("size") or 0),
                    "finished_at": row["finished_at"],
                    "points": result.get("points"),
                    "chars": result.get("chars"),
                    "cost_cny": result.get("cost_cny"),
                    "strategy": result.get("strategy", ""),
                    "asr_device": lineage.get("asr_device", ""),
                })
    return out


def stats() -> dict[str, int]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
    out = {s.value: 0 for s in TaskStatus}
    for r in rows:
        out[r["status"]] = r["n"]
    out["total"] = sum(out[s.value] for s in TaskStatus)
    return out


def queue_position(task_id: str) -> int:
    """返回 1 表示下一个。非排队状态返回 0。"""
    t = get(task_id)
    if not t or t.status is not TaskStatus.QUEUED:
        return 0
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status=? AND seq < ?",
            (TaskStatus.QUEUED.value, t.seq),
        ).fetchone()
    return int(row["n"]) + 1


def requeue_stale(timeout_seconds: int = 3600) -> int:
    """把心跳超时的 RUNNING 任务放回队列。

    进程被强杀时任务会永远停在 RUNNING，前端看起来像"卡住了"。
    启动时调用一次，把这类僵尸任务恢复。
    """
    cutoff = dt.datetime.now() - dt.timedelta(seconds=timeout_seconds)
    with closing(_connect()) as conn:
        with conn:
            cur = conn.execute(
                """UPDATE tasks SET status=?, stage=?, progress=0,
                   message='（上次运行中断，已重新排队）', started_at='', heartbeat=''
                   WHERE status=? AND (heartbeat='' OR heartbeat < ?)""",
                (TaskStatus.QUEUED.value, "queued",
                 TaskStatus.RUNNING.value, cutoff.isoformat(timespec="seconds")),
            )
            # rowcount 必须在连接关闭前取；出了 with 块 cursor 就失效了
            count = cur.rowcount
    return count
