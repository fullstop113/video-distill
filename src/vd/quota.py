"""配额控制（方案 §5.3⑤ / R12）。

规则由工作区主人指定：
  - 主人自己的 B 站账号（VD_OWNER_BILI_UID）         ：不限次数、不限时长
  - 其他任何账号                                    ：每日 3 次，且当日累计时长 <= 3 小时

实现要点：
  1. **服务端强校验**，绝不相信前端传来的身份或用量。
  2. 按「登录的 B 站账号」计数，而不是按浏览器会话 —— 换浏览器、清 Cookie
     都不能绕过，因为配额绑定在凭据所属的 UID 上。
  3. 跨天按**本地日期**重置（托管在本机，使用者也在同一时区）。
  4. 判定发生在**下载音频之前**：配额不足时早点拒绝，别先烧掉带宽和 GPU。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "quota.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    uid       TEXT NOT NULL,
    day       TEXT NOT NULL,
    videos    INTEGER NOT NULL DEFAULT 0,
    seconds   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uid, day)
);
CREATE TABLE IF NOT EXISTS ledger (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    uid       TEXT NOT NULL,
    bvid      TEXT NOT NULL,
    seconds   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_uid_created ON ledger(uid, created_at);
"""


@dataclass(frozen=True)
class QuotaRule:
    daily_videos: int
    daily_seconds: int
    unlimited: bool = False

    @staticmethod
    def for_uid(uid: str) -> "QuotaRule":
        """管理员集合内的账号配额不限，其余按访客规则。

        管理员集合来自 `VD_ADMIN_UIDS`（逗号分隔），并自动并入
        `VD_OWNER_BILI_UID` 与 `VD_OPERATOR_BILI_UID`。

        注意：一个人可以既有大号又有小号，**两个都是管理员**，都不受配额限制。
        "谁是管理员"与"用哪个号的凭据取数"是**两件独立的事**——
        后者由 vd.credentials 的白名单强制约束。
        """
        raw = os.getenv("VD_ADMIN_UIDS", "")
        admins = {u.strip() for u in raw.split(",") if u.strip()}
        for key in ("VD_OWNER_BILI_UID", "VD_OPERATOR_BILI_UID"):
            value = os.getenv(key, "").strip()
            if value:
                admins.add(value)
        if str(uid) in admins:
            return QuotaRule(daily_videos=0, daily_seconds=0, unlimited=True)
        return QuotaRule(
            daily_videos=int(os.getenv("VD_GUEST_DAILY_VIDEO_LIMIT", "3")),
            daily_seconds=int(os.getenv("VD_GUEST_DAILY_SECONDS_LIMIT", "10800")),
        )


@dataclass
class QuotaStatus:
    allowed: bool
    reason: str
    rule: QuotaRule
    used_videos: int = 0
    used_seconds: int = 0

    def describe(self) -> str:
        if self.rule.unlimited:
            return "配额：不限（管理员账号）"
        return (
            f"配额：今日已用 {self.used_videos}/{self.rule.daily_videos} 个视频，"
            f"{self.used_seconds // 60}/{self.rule.daily_seconds // 60} 分钟"
        )


def _connect() -> sqlite3.Connection:
    """打开连接。

    ⚠️ 调用方必须用 `with closing(_connect()) as conn:` 包裹。

    这里踩过一个真实的坑：`sqlite3.Connection` 的 `with` 协议**只管事务提交/回滚，
    不关闭连接**（Python 标准库里少见的语义）。用 `with _connect() as conn:` 会让
    文件句柄一直挂着，在 Windows 上表现为后续无法删除/替换该 sqlite 文件
    （WinError 32），长期运行还会累积句柄。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.executescript(SCHEMA)
    return conn


def _today() -> str:
    return dt.date.today().isoformat()


def check(uid: str, planned_seconds: int = 0) -> QuotaStatus:
    """在真正开始处理前调用，判定这次请求是否放行。

    注意用 closing() 而不是裸 with：见 _connect() 的说明。
    """
    rule = QuotaRule.for_uid(uid)
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT videos, seconds FROM usage WHERE uid=? AND day=?",
            (str(uid), _today()),
        ).fetchone()
    used_videos, used_seconds = (row if row else (0, 0))

    if rule.unlimited:
        return QuotaStatus(True, "主人账号，不限额", rule, used_videos, used_seconds)

    if used_videos + 1 > rule.daily_videos:
        return QuotaStatus(
            False,
            f"已达每日上限（{rule.daily_videos} 个视频），请明天再试。",
            rule,
            used_videos,
            used_seconds,
        )
    if used_seconds + planned_seconds > rule.daily_seconds:
        remaining = max(rule.daily_seconds - used_seconds, 0)
        return QuotaStatus(
            False,
            f"当日累计时长将超过上限（剩余 {remaining // 60} 分钟，"
            f"本视频 {planned_seconds // 60} 分钟）。",
            rule,
            used_videos,
            used_seconds,
        )
    return QuotaStatus(True, "配额充足", rule, used_videos, used_seconds)


def commit(uid: str, bvid: str, seconds: int) -> None:
    """任务成功受理后记账。seconds 用视频真实时长，不能用估算值。"""
    with closing(_connect()) as conn:
        with conn:  # 事务边界
            conn.execute(
                """INSERT INTO usage(uid, day, videos, seconds) VALUES(?,?,1,?)
                   ON CONFLICT(uid, day) DO UPDATE SET
                     videos = videos + 1,
                     seconds = seconds + excluded.seconds""",
                (str(uid), _today(), int(seconds)),
            )
            conn.execute(
                "INSERT INTO ledger(uid, bvid, seconds, created_at) VALUES(?,?,?,?)",
                (str(uid), bvid, int(seconds), dt.datetime.now().isoformat(timespec="seconds")),
            )


def report(uid: str) -> QuotaStatus:
    rule = QuotaRule.for_uid(uid)
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT videos, seconds FROM usage WHERE uid=? AND day=?",
            (str(uid), _today()),
        ).fetchone()
    used_videos, used_seconds = (row if row else (0, 0))
    return QuotaStatus(True, "统计", rule, used_videos, used_seconds)
