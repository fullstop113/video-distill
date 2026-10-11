"""元信息缓存（R4 对策之一）。

方案 §4.4 实测过风控：不带会话 Cookie 直接请求排行接口会 `-352`，
而且请求越频繁越容易触发。所以"同一个视频不重复问 B 站"既是省时间，也是降风险。

选择 SQLite 而不是 JSON 文件：需要并发读、需要 TTL 清理、需要原子写。
（注意 sqlite3 的连接必须 `closing()` —— 见 quota.py 里踩过的坑。）
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "cache.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_cache (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_kv_created ON kv_cache(created_at);
"""

# 默认 TTL（秒）
DEFAULT_TTL = {
    "video_info": 7 * 24 * 3600,   # 视频元信息基本不变，缓存一周
    "subtitle": 30 * 24 * 3600,    # 字幕一旦生成很少变
    "asr": 30 * 24 * 3600,         # ASR 结果很贵（占 GPU），值得长期缓存
    "ranking": 3600,               # 排行榜变化快
    "history": 300,
}


def _connect() -> sqlite3.Connection:
    """打开连接。调用方必须用 `closing()` 包裹（sqlite3 的 with 不关闭连接）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.executescript(SCHEMA)
    return conn


def get(namespace: str, key: str, ttl: float | None = None) -> Any | None:
    """读缓存。过期或不存在返回 None。"""
    if ttl is None:
        ttl = DEFAULT_TTL.get(namespace, 3600)
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT value, created_at FROM kv_cache WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()
    if not row:
        return None
    value, created = row
    if ttl > 0 and time.time() - created > ttl:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def put(namespace: str, key: str, value: Any) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """INSERT INTO kv_cache(namespace, key, value, created_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(namespace, key) DO UPDATE SET
                     value = excluded.value,
                     created_at = excluded.created_at""",
                (namespace, key, json.dumps(value, ensure_ascii=False), time.time()),
            )


def invalidate(namespace: str, key: str | None = None) -> int:
    """删一条或整个命名空间。返回删除行数。"""
    with closing(_connect()) as conn:
        with conn:
            if key is None:
                cur = conn.execute("DELETE FROM kv_cache WHERE namespace=?", (namespace,))
            else:
                cur = conn.execute(
                    "DELETE FROM kv_cache WHERE namespace=? AND key=?", (namespace, key)
                )
            return cur.rowcount


def purge_expired() -> int:
    """清掉所有命名空间里已过期的条目。"""
    now = time.time()
    removed = 0
    with closing(_connect()) as conn:
        with conn:
            for ns, ttl in DEFAULT_TTL.items():
                cur = conn.execute(
                    "DELETE FROM kv_cache WHERE namespace=? AND created_at < ?",
                    (ns, now - ttl),
                )
                removed += cur.rowcount
    return removed


def stats() -> list[tuple[str, int]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT namespace, COUNT(*) FROM kv_cache GROUP BY namespace ORDER BY 2 DESC"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]
