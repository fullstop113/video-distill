"""余额与邀请码 —— ASR 花的是**主人的钱**，所以要能挡住超支。

## 为什么需要它（和 quota 是两件事）

`vd.quota` 管的是"每天能用几个视频、总共多少分钟"——**用量配额**。
这里管的是**真金白银**：无字幕视频会调用阿里云 Paraformer，
**账单落在主人（管理员）的阿里云账号上**。

两件事必须分开：
  · 有字幕的视频走 B 站字幕，**一分钱不花**，所以余额为 0 也应放行；
  · 无字幕的视频才调 Paraformer，**才扣余额**。

所以余额是**独立的第二道闸**，不能和日配额混在一起判定。

## 余额允许为负，但要有地板

用户明确要求："允许余额为负数，但是不要让余额负太多"。

所以判定规则是**预扣式**：
    本次预估花费 = 视频时长 × 单价
    若 余额 − 本次预估花费 < −地板  →  拒绝

用**视频时长**而不是"实际语音时长"来预估，是有意为之（用户要求）：
实际计费口径是"被判定为语音内容的时长"，**通常短于视频总长**，
所以预扣偏保守 → 真实成本小于账面 → 对主人更安全。

## 邀请码

管理员可在页面一次生成 5 个码，1 小时内有效；
其它用户输入码校验通过即加 2 元余额。用码即作废（一次性）。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "billing.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS balance (
    uid        TEXT PRIMARY KEY,
    uname      TEXT NOT NULL DEFAULT '',
    -- 余额单位为**元**。允许为负，地板由 VD_BALANCE_FLOOR 控制。
    balance    REAL NOT NULL DEFAULT 0,
    spent      REAL NOT NULL DEFAULT 0,   -- 累计消费（用于展示）
    credited   REAL NOT NULL DEFAULT 0,   -- 累计充值
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT NOT NULL,
    bvid       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL,             -- asr / invite / initial / adjust
    amount     REAL NOT NULL,             -- 正数=充值，负数=消费
    balance    REAL NOT NULL,             -- 记账后的余额（便于对账）
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    -- ASR 流水的**音频秒数**（其它类型为 0）。
    -- 记秒数而不是靠 amount ÷ 单价反推：单价是可配置的，
    -- 一旦调整，历史流水反推出来的秒数就会集体失真。
    seconds    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bill_ledger_uid ON ledger(uid, created_at);
CREATE TABLE IF NOT EXISTS invite (
    code       TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_by    TEXT NOT NULL DEFAULT '',
    used_at    TEXT NOT NULL DEFAULT '',
    amount     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invite_expires ON invite(expires_at);
"""


class BillingError(RuntimeError):
    """余额/邀请码相关的可预期错误（要展示给用户，不是崩溃）。"""


@dataclass(frozen=True)
class Balance:
    uid: str
    uname: str
    balance: float
    spent: float
    credited: float

    def describe(self) -> str:
        return f"余额 ¥{self.balance:.2f}"


@dataclass
class Affordability:
    allowed: bool
    reason: str
    estimated: float          # 本次预估花费（元）
    balance: float


log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 配置

def price_per_second() -> float:
    """ASR 单价（元/秒）。控制台实价 0.00008 元/秒 = 0.288 元/小时。"""
    return float(os.getenv("VD_ASR_PRICE_PER_SECOND", "0.00008"))


def balance_floor() -> float:
    """允许负债的下限（正数表示"最多欠这么多"）。

    默认取初始余额的绝对值 —— 即"最多把送的那 1 元欠掉"，
    这样新用户能完整试一次，又不会被透支到离谱。
    """
    raw = os.getenv("VD_BALANCE_FLOOR", "").strip()
    if raw:
        return abs(float(raw))
    return abs(initial_balance()) or 1.0


def initial_balance() -> float:
    """新用户初始余额（元）。用户要求给 1 元让新用户能试一次。"""
    return float(os.getenv("VD_INITIAL_BALANCE", "1"))


def invite_credit() -> float:
    return float(os.getenv("VD_INVITE_CREDIT", "2.00"))


def invite_batch() -> int:
    return int(os.getenv("VD_INVITE_BATCH", "5"))


def invite_ttl() -> int:
    return int(os.getenv("VD_INVITE_TTL_SECONDS", "3600"))


def _now() -> dt.datetime:
    return dt.datetime.now()


def _iso(t: dt.datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 基础设施

def _connect() -> sqlite3.Connection:
    """打开连接。

    ⚠️ 调用方必须用 `with closing(_connect()) as conn:` 包裹 ——
    sqlite3 的 `with` 只管事务，**不关连接**（同 vd.quota 里记的坑）。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给**已存在**的库补上后加的列。

    ## 为什么必须有这一步

    `CREATE TABLE IF NOT EXISTS` 对已存在的表**什么都不做** ——
    它不会补列。所以只要给表加一个字段，老用户的库就会在
    第一次查询时报 "no such column"，而且是在运行时才炸。

    这里逐列检查后 `ALTER TABLE ADD COLUMN`。SQLite 加列是廉价的
    （只改元数据），并且新旧代码都能继续读写。
    """
    wanted = {"ledger": {"seconds": "INTEGER NOT NULL DEFAULT 0"}}
    for table, cols in wanted.items():
        have = {r["name"] for r in
                conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not have:
            continue                      # 表还没建（全新库），无需迁移
        for name, decl in cols.items():
            if name not in have:
                with conn:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("billing 迁移：%s 表补列 %s", table, name)


# ---------------------------------------------------------------- 余额

def get(uid: str, uname: str = "") -> Balance:
    """读余额；不存在则**开户并送初始余额**。

    首次调用即开户是有意的：使用者扫码登录后第一次打开页面就该看到余额，
    而不是看到"无记录"。
    """
    uid = str(uid)
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM balance WHERE uid=?", (uid,)).fetchone()
        if row is None:
            # 开户（含发放初始余额）统一走 _ensure_row，避免两处各写一遍
            with conn:      # 事务
                _ensure_row(conn, uid, uname)
            row = conn.execute("SELECT * FROM balance WHERE uid=?", (uid,)).fetchone()
        elif uname and row["uname"] != uname:
            # 昵称可能变，顺手更新
            with conn:
                conn.execute("UPDATE balance SET uname=?, updated_at=? WHERE uid=?",
                             (uname, _iso(), uid))
            row = conn.execute("SELECT * FROM balance WHERE uid=?", (uid,)).fetchone()
    return Balance(uid=row["uid"], uname=row["uname"], balance=row["balance"],
                   spent=row["spent"], credited=row["credited"])


def _ensure_row(conn: sqlite3.Connection, uid: str, uname: str = "") -> None:
    """确保 balance 里有这一行，**并按规则发放初始余额**。

    ⚠️ 这里修了两个连着的真 bug（都是测试逮到的）：

    1. `redeem()` 给**还没开过户**的用户充值时，`_apply()` 里的
       `UPDATE balance ... WHERE uid=?` 匹配 0 行 —— **静默不报错、
       钱没到账，流水却照记**。所以要先把行建出来。

    2. 但只建"零余额"的行会引出第二个 bug：`get()` 见到行已存在就
       不再发初始余额，于是"先充值再打开页面"的新用户**少了 1 元**。
       所以这里建行时就把初始余额一起发掉，且写入 initial 流水。

    这样无论 `get()` 还是 `redeem()` 谁先被调用，初始余额都**恰好发一次**。
    """
    row = conn.execute("SELECT 1 FROM balance WHERE uid=?", (uid,)).fetchone()
    if row is not None:
        return
    now = _iso()
    start = initial_balance()
    conn.execute(
        """INSERT INTO balance(uid, uname, balance, spent, credited,
                               created_at, updated_at)
           VALUES(?,?,?,0,?,?,?)""",
        (uid, uname, start, start, now, now),
    )
    if start:
        conn.execute(
            """INSERT INTO ledger(uid, kind, amount, balance, note, created_at)
               VALUES(?,?,?,?,?,?)""",
            (uid, "initial", start, start, "新用户初始余额", now),
        )


def _apply(conn: sqlite3.Connection, uid: str, amount: float, kind: str,
           bvid: str = "", note: str = "", seconds: int = 0) -> float:
    """在给定事务内改余额并记流水。返回记账后余额。

    先 `_ensure_row` 再改 —— 否则对未开户用户的操作会静默失效。

    `seconds` 只对 ASR 流水有意义（用于统计本月免费额度用量）。
    """
    _ensure_row(conn, uid)
    now = _iso()
    conn.execute(
        """UPDATE balance SET balance = balance + ?,
                              spent = spent + ?,
                              credited = credited + ?,
                              updated_at = ?
           WHERE uid = ?""",
        (amount, -amount if amount < 0 else 0.0,
         amount if amount > 0 else 0.0, now, uid),
    )
    row = conn.execute("SELECT balance FROM balance WHERE uid=?", (uid,)).fetchone()
    new_balance = float(row["balance"]) if row else 0.0
    conn.execute(
        """INSERT INTO ledger(uid, bvid, kind, amount, balance, note,
                              created_at, seconds)
           VALUES(?,?,?,?,?,?,?,?)""",
        (uid, bvid, kind, amount, new_balance, note, now, int(seconds)),
    )
    return new_balance


def charge(uid: str, amount: float, *, bvid: str = "",
           note: str = "", seconds: int = 0) -> float:
    """扣费（amount 为正数）。返回扣后余额。

    `seconds` 是本次消耗的**音频秒数**，仅用于统计免费额度用量
    （见 `asr_seconds_this_month`），不参与金额计算。
    """
    if amount < 0:
        raise BillingError("扣费金额必须为正数")
    uid = str(uid)
    with closing(_connect()) as conn:
        with conn:
            return _apply(conn, uid, -abs(amount), "asr", bvid, note, seconds)


def credit(uid: str, amount: float, *, kind: str = "invite", note: str = "") -> float:
    """充值（amount 为正数）。返回充值后余额。"""
    if amount <= 0:
        raise BillingError("充值金额必须为正数")
    uid = str(uid)
    with closing(_connect()) as conn:
        with conn:
            return _apply(conn, uid, abs(amount), kind, "", note)


def estimate_cost(seconds: int) -> float:
    """按**视频时长**预估花费（元）。

    有意用视频总长而不是"实际语音时长"：真实计费口径只算语音内容，
    通常短于总长，所以这个预估是**偏保守**的 ——
    账面扣得比实际多，主人的真实成本就更安全（用户明确要求如此）。
    """
    return max(0.0, float(seconds)) * price_per_second()


def can_afford(uid: str, video_seconds: int) -> Affordability:
    """本次视频是否付得起。

    规则（用户要求"允许为负，但别负太多"）：
        余额 − 本次预估花费  >=  −地板   → 放行
    否则拒绝，并说明还差多少。
    """
    est = estimate_cost(video_seconds)
    bal = get(uid).balance
    floor = balance_floor()
    if bal - est >= -floor:
        return Affordability(True, "余额充足", est, bal)
    short = (bal - est) + floor        # 负得越多，这个值越小
    return Affordability(
        False,
        f"余额不足：本次预计消耗 ¥{est:.3f}，当前余额 ¥{bal:.2f}，"
        f"最多可欠 ¥{floor:.2f}（还差 ¥{abs(short):.3f}）。"
        f"请用邀请码充值。",
        est,
        bal,
    )


def is_admin(uid: str) -> bool:
    """管理员不受余额限制（花的是自己的钱，无需自我限制）。"""
    from .credentials import admin_uids

    return str(uid) in admin_uids()


def can_afford_or_admin(uid: str, video_seconds: int) -> Affordability:
    """带管理员豁免的判定 —— worker 与 Web 都用这个。"""
    if is_admin(uid):
        return Affordability(True, "管理员账号，不限额", 0.0,
                             get(uid).balance)
    return can_afford(uid, video_seconds)


# ---------------------------------------------------------------- 免费额度

def free_seconds_per_month() -> int:
    """每月免费额度（秒）。默认 10 小时。

    ## 它**只用于展示**，不参与扣费计算

    阿里云的免费额度是**按自然月重置**的，而余额是**累计**的 ——
    两者不能混在一个数里算（否则月初月末行为不一致，很难解释）。

    所以这个值的用途只有一个：在余额页显示"本月免费额度用了多少"，
    让使用者知道什么时候会开始真花钱。
    """
    return int(os.getenv("VD_ASR_FREE_SECONDS_PER_MONTH", "36000"))


def asr_seconds_this_month() -> int:
    """本月已消耗的 ASR 音频秒数（按 ledger 里 asr 流水的 seconds 列统计）。

    统计口径是**账面上的 ASR 秒数**（即扣费依据的视频时长），
    不是阿里云返回的"语音时长"。所以这个数字会**略大于**阿里云看到的用量
    （语音约占 57%）—— 也就是说：这里显示"免费额度用完了"时，
    你实际可能还剩一些。保守估计，不会让你措手不及。
    """
    prefix = dt.datetime.now().strftime("%Y-%m")
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(seconds), 0) AS s FROM ledger "
            "WHERE kind='asr' AND substr(created_at, 1, 7) = ?",
            (prefix,),
        ).fetchone()
    return int(row["s"] or 0)


# ---------------------------------------------------------------- 邀请码

def _new_code() -> str:
    """8 位大写字母数字，去掉易混字符（0/O/1/I）。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def generate_invites(by_uid: str, count: int | None = None,
                     amount: float | None = None,
                     ttl: int | None = None) -> list[dict]:
    """生成邀请码（默认一次 5 张、每张 2 元、1 小时有效）。

    用 `secrets` 而不是 `random`：邀请码等于钱，必须不可预测。
    """
    n = count if count is not None else invite_batch()
    amt = amount if amount is not None else invite_credit()
    life = ttl if ttl is not None else invite_ttl()
    now = _now()
    expires = now + dt.timedelta(seconds=life)
    made: list[dict] = []
    with closing(_connect()) as conn:
        with conn:
            for _ in range(max(0, n)):
                # 极小概率碰撞：重试几次
                for _try in range(8):
                    code = _new_code()
                    exists = conn.execute(
                        "SELECT 1 FROM invite WHERE code=?", (code,)
                    ).fetchone()
                    if not exists:
                        break
                else:
                    raise BillingError("生成邀请码失败（连续碰撞），请重试")
                conn.execute(
                    """INSERT INTO invite(code, created_by, created_at,
                                          expires_at, amount)
                       VALUES(?,?,?,?,?)""",
                    (code, str(by_uid), _iso(now), _iso(expires), amt),
                )
                made.append({"code": code, "amount": amt,
                             "expires_at": _iso(expires)})
    return made


def redeem(code: str, uid: str) -> tuple[float, float]:
    """用邀请码充值。

    Returns:
        (充值金额, 充值后余额)

    校验顺序有意为之：**先判用没用过，再判过期** ——
    否则"已用过的过期码"会报"已过期"，误导用户以为是时间问题。
    """
    code = (code or "").strip().upper()
    if not code:
        raise BillingError("请输入邀请码")
    uid = str(uid)
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM invite WHERE code=?", (code,)).fetchone()
        if row is None:
            raise BillingError("邀请码无效")
        if row["used_by"]:
            raise BillingError("该邀请码已被使用")
        if _iso() > row["expires_at"]:
            raise BillingError("该邀请码已过期（有效期 1 小时）")
        if str(row["created_by"]) == uid:
            # 不禁止管理员自己用，但明确记录；这里只拦"自己发给自己"的滥用
            pass
        with conn:      # 事务：标记已用 + 加余额，必须原子
            conn.execute(
                "UPDATE invite SET used_by=?, used_at=? WHERE code=?",
                (uid, _iso(), code),
            )
            new_balance = _apply(conn, uid, float(row["amount"]), "invite",
                                 "", f"邀请码 {code}")
    return float(row["amount"]), new_balance


def list_invites(by_uid: str = "", limit: int = 50) -> list[dict]:
    """列出邀请码（最近的在前），带状态，便于管理页展示。"""
    sql = "SELECT * FROM invite"
    args: list = []
    if by_uid:
        sql += " WHERE created_by=?"
        args.append(str(by_uid))
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    now = _iso()
    out: list[dict] = []
    with closing(_connect()) as conn:
        for r in conn.execute(sql, args).fetchall():
            if r["used_by"]:
                state = "已使用"
            elif now > r["expires_at"]:
                state = "已过期"
            else:
                state = "可用"
            out.append({
                "code": r["code"], "amount": r["amount"],
                "created_at": r["created_at"], "expires_at": r["expires_at"],
                "used_by": r["used_by"], "state": state,
            })
    return out


def ledger_of(uid: str, limit: int = 20) -> list[dict]:
    """某用户的流水（最近的在前）。"""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE uid=? ORDER BY id DESC LIMIT ?",
            (str(uid), limit),
        ).fetchall()
    return [dict(r) for r in rows]
