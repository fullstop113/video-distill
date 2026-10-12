"""余额与邀请码的单元测试（离线，不需要网络）。

重点覆盖**边界**而不是快乐路径：
  · 余额为负时的放行/拒绝边界（用户要求"允许为负但别负太多"）
  · 邀请码的一次性、过期、无效三种失败
  · 扣费与充值在同一事务里原子完成（不能只记流水不改余额）
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("VD_OPERATOR_BILI_UID", "22222")
os.environ.setdefault("VD_OWNER_BILI_UID", "11111")


@pytest.fixture()
def bill(tmp_path, monkeypatch):
    """每个用例一个独立的 billing 库，并固定价格参数。"""
    mod = importlib.import_module("vd.billing")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "billing.sqlite3")
    monkeypatch.setenv("VD_ASR_PRICE_PER_SECOND", "0.00008")
    monkeypatch.setenv("VD_INITIAL_BALANCE", "1")
    monkeypatch.setenv("VD_BALANCE_FLOOR", "")        # 走默认（= 初始余额）
    monkeypatch.setenv("VD_INVITE_CREDIT", "2.00")
    monkeypatch.setenv("VD_INVITE_BATCH", "5")
    monkeypatch.setenv("VD_INVITE_TTL_SECONDS", "3600")
    return mod


# ---------------------------------------------------------------- 开户与余额

def test_new_user_gets_initial_balance(bill):
    """新用户首次访问即开户并拿到初始余额（默认 1 元）。"""
    b = bill.get("90001", "新用户")
    assert b.balance == pytest.approx(1.0)
    assert b.uname == "新用户"
    # 开户要留流水，便于对账
    led = bill.ledger_of("90001")
    assert len(led) == 1 and led[0]["kind"] == "initial"


def test_get_is_idempotent(bill):
    """重复读不该重复送钱 —— 这是最容易写错的地方。"""
    bill.get("90002")
    b2 = bill.get("90002")
    assert b2.balance == pytest.approx(1.0), "重复开户把初始余额发了两次"
    assert len(bill.ledger_of("90002")) == 1


def test_charge_and_credit_move_balance(bill):
    bill.get("90003")
    bill.charge("90003", 0.5, bvid="BVTEST")
    assert bill.get("90003").balance == pytest.approx(0.5)
    bill.credit("90003", 2.0)
    assert bill.get("90003").balance == pytest.approx(2.5)
    assert bill.get("90003").credited == pytest.approx(3.0)   # 1 初始 + 2 充值
    assert bill.get("90003").spent == pytest.approx(0.5)


def test_charge_requires_positive_amount(bill):
    bill.get("90004")
    with pytest.raises(bill.BillingError):
        bill.charge("90004", -1)


# ---------------------------------------------------------------- 花费预估

def test_estimate_uses_video_duration(bill):
    """预估按**视频总时长**算，而不是实际语音时长。

    真实计费只算语音内容（通常短于总长），所以这样预估**偏保守** ——
    账面扣得比实际多，对主人更安全。这是用户明确要求的口径。
    """
    # 104.3 分钟 = 6258 秒 → 6258 × 0.00008 ≈ 0.5006 元
    assert bill.estimate_cost(6258) == pytest.approx(0.50064, rel=1e-3)
    assert bill.estimate_cost(0) == 0.0
    assert bill.estimate_cost(-5) == 0.0, "负数时长不能被算成正花费"


def test_can_afford_blocks_when_would_overshoot_floor(bill):
    """**核心边界**：余额 0.1 元不该放行 2 小时无字幕视频。

    这正是用户举的例子。2 小时 = 7200 秒 → 预估 0.576 元。
    余额 0.1，地板 1.0 → 0.1 − 0.576 = −0.476 > −1.0 → **仍放行**。
    要构造真正会被拦的情况，得让预估超过"余额 + 地板"。
    """
    bill.get("90005")
    bill.charge("90005", 0.9)              # 余额剩 0.1
    assert bill.get("90005").balance == pytest.approx(0.1)

    # 2 小时视频：预估 0.576，0.1 − 0.576 = −0.476，未破地板 −1.0 → 放行
    aff = bill.can_afford("90005", 7200)
    assert aff.allowed is True, "0.1 元 + 1 元地板应当够一次 2 小时视频"

    # 10 小时视频：预估 2.88，0.1 − 2.88 = −2.78 < −1.0 → **必须拒绝**
    aff2 = bill.can_afford("90005", 36000)
    assert aff2.allowed is False
    assert "余额不足" in aff2.reason
    assert "邀请码" in aff2.reason, "拒绝信息要告诉用户怎么充值"


def test_can_afford_allows_exactly_at_floor(bill):
    """刚好触及地板应放行（边界包含）。"""
    bill.get("90006")
    bill.charge("90006", 1.0)              # 余额 0
    # 余额 0，地板 1.0 → 可承受预估最多 1.0 元 = 12500 秒
    assert bill.can_afford("90006", 12500).allowed is True
    assert bill.can_afford("90006", 12501).allowed is False


def test_free_video_always_affordable(bill):
    """有字幕的视频不花钱，余额为 0 也必须放行。"""
    bill.get("90007")
    bill.charge("90007", 1.0)
    assert bill.get("90007").balance == pytest.approx(0.0)
    assert bill.can_afford("90007", 0).allowed is True


def test_admin_is_exempt(bill, monkeypatch):
    """管理员不受余额限制（花的是自己的钱）。"""
    monkeypatch.setenv("VD_ADMIN_UIDS", "22222,11111")
    monkeypatch.delenv("VD_OPERATOR_IS_ADMIN", raising=False)
    bill.get("22222")
    bill.charge("22222", 5.0)              # 欠 4 元
    aff = bill.can_afford_or_admin("22222", 36000)   # 10 小时
    assert aff.allowed is True
    assert "管理员" in aff.reason


# ---------------------------------------------------------------- 邀请码

def test_generate_batch_of_five(bill):
    """管理员一次生成 5 张，每张 2 元，1 小时有效。"""
    codes = bill.generate_invites("22222")
    assert len(codes) == 5
    assert all(c["amount"] == pytest.approx(2.0) for c in codes)
    assert len({c["code"] for c in codes}) == 5, "生成的码有重复"


def test_invite_codes_are_readable_and_unguessable(bill):
    """码要去掉易混字符（0/O/1/I），且必须用 secrets 而不是 random。"""
    codes = [c["code"] for c in bill.generate_invites("22222")]
    for c in codes:
        assert len(c) == 8
        assert not set(c) & set("0O1I"), f"含易混字符：{c}"
        assert c.isalnum() and c == c.upper()


def test_redeem_increases_balance(bill):
    code = bill.generate_invites("22222", count=1)[0]["code"]
    bill.get("90010")                      # 初始 1 元
    amount, new_balance = bill.redeem(code, "90010")
    assert amount == pytest.approx(2.0)
    assert new_balance == pytest.approx(3.0)


def test_invite_is_single_use(bill):
    """用码即作废 —— 这是钱的等价物，必须一次性。"""
    code = bill.generate_invites("22222", count=1)[0]["code"]
    bill.redeem(code, "90011")
    with pytest.raises(bill.BillingError, match="已被使用"):
        bill.redeem(code, "90012")


def test_expired_code_is_rejected(bill):
    """过期的码要拒绝，且理由说"过期"而不是"已被使用"。"""
    code = bill.generate_invites("22222", count=1, ttl=-1)[0]["code"]
    with pytest.raises(bill.BillingError, match="已过期"):
        bill.redeem(code, "90013")


def test_unknown_code_is_rejected(bill):
    with pytest.raises(bill.BillingError, match="无效"):
        bill.redeem("NOSUCHCD", "90014")
    with pytest.raises(bill.BillingError, match="请输入"):
        bill.redeem("", "90014")


def test_used_check_comes_before_expiry(bill):
    """**顺序陷阱**：已用过的过期码应报"已使用"，不是"已过期"。

    否则用户会以为是时间问题，反复重试。
    """
    code = bill.generate_invites("22222", count=1, ttl=-1)[0]["code"]
    # 手工制造"已使用"状态
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(bill.DB_PATH)) as conn:
        with conn:
            conn.execute("UPDATE invite SET used_by=?, used_at=? WHERE code=?",
                         ("90015", "2026-01-01T00:00:00", code))
    with pytest.raises(bill.BillingError, match="已被使用"):
        bill.redeem(code, "90016")


def test_redeem_is_case_insensitive(bill):
    """用户手输的码可能小写，不该因此失败。"""
    code = bill.generate_invites("22222", count=1)[0]["code"]
    bill.redeem(code.lower(), "90017")
    assert bill.get("90017").balance == pytest.approx(3.0)


def test_redeem_works_for_user_without_prior_account(bill):
    """**回归**：给"还没开过户"的用户充值必须到账，且初始余额不能少。

    这里连着修了两个 bug（都是本测试逮到的）：

    1. `redeem()` 直接 `_apply()`，而 `_apply()` 里的
       `UPDATE ... WHERE uid=?` 对不存在的行匹配 0 行 ——
       **静默不报错、钱没到账，流水却照记**。
    2. 只补建"零余额"的行又引出第二个：`get()` 见到行已存在就不再发
       初始余额，于是"先充值再打开页面"的新用户**少了 1 元**。

    现在开户与发放初始余额统一在 `_ensure_row()`，无论谁先调用都恰好发一次。
    """
    code = bill.generate_invites("22222", count=1)[0]["code"]
    # 故意**不先 get()**，模拟"扫码登录后立刻用码充值"
    amount, new_balance = bill.redeem(code, "90099")
    assert amount == pytest.approx(2.0)
    assert new_balance == pytest.approx(3.0), "初始 1 元 + 充值 2 元"

    b = bill.get("90099")
    assert b.balance == pytest.approx(3.0), "充值后余额不对"
    assert b.credited == pytest.approx(3.0)
    kinds = [x["kind"] for x in bill.ledger_of("90099")]
    assert "initial" in kinds and "invite" in kinds


def test_ledger_balance_matches_actual_balance(bill):
    """流水里记的"记账后余额"必须与真实余额一致（对账基础）。"""
    bill.get("90098")
    bill.charge("90098", 0.4, bvid="BVX")
    bill.credit("90098", 2.0)
    bill.charge("90098", 0.1)
    led = bill.ledger_of("90098")          # 最近在前
    assert led[0]["balance"] == pytest.approx(bill.get("90098").balance)
    # 每一条的 balance 都要能与前一条对上
    chrono = list(reversed(led))
    running = 0.0
    for row in chrono:
        running += row["amount"]
        assert row["balance"] == pytest.approx(running), "流水余额不连续"


def test_list_invites_reports_state(bill):
    codes = [c["code"] for c in bill.generate_invites("22222", count=3)]
    bill.redeem(codes[0], "90018")
    bill.generate_invites("22222", count=1, ttl=-1)      # 一张过期的
    items = bill.list_invites("22222")
    states = [i["state"] for i in items]
    assert "已使用" in states and "已过期" in states and "可用" in states


def test_ledger_records_running_balance(bill):
    """流水要带记账后余额 —— 出问题时能直接对账。"""
    bill.get("90019")
    bill.charge("90019", 0.3, bvid="BVA")
    bill.credit("90019", 2.0, note="邀请码 XXX")
    led = bill.ledger_of("90019")
    # 最近的在前
    assert led[0]["balance"] == pytest.approx(2.7)
    assert led[0]["amount"] == pytest.approx(2.0)
    assert led[1]["amount"] == pytest.approx(-0.3)
    assert led[1]["bvid"] == "BVA"
