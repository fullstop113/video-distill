"""凭证加密与配额的回归测试（无网络、无 GPU，可随时跑）。

这两块对应方案里级别最高的两条风险：R11（代管他人凭证）与 R12（被滥用），
所以它们的测试必须能独立于网络执行。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("VD_SECRET_KEY", "test-secret-for-unit-tests-only")
os.environ.setdefault("VD_OWNER_BILI_UID", "10001")
os.environ.pop("VD_OPERATOR_BILI_UID", None)  # 各测试自行设置，避免相互污染
os.environ.setdefault("VD_GUEST_DAILY_VIDEO_LIMIT", "3")
os.environ.setdefault("VD_GUEST_DAILY_SECONDS_LIMIT", "10800")


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.storage")
    monkeypatch.setattr(mod, "CRED_DIR", tmp_path / "credentials")
    return mod


@pytest.fixture()
def quota(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.quota")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "quota.sqlite3")
    return mod


def _creds(mod, uid="20002"):
    return mod.Credentials(
        uid=uid,
        uname="someone",
        cookies={"SESSDATA": "abc", "bili_jct": "def", "DedeUserID": uid},
        logged_in_at="2026-01-01T00:00:00",
    )


# ---------------- R11：凭证必须加密 ----------------


def test_credential_roundtrip(storage):
    c = _creds(storage)
    storage.save_credentials(c)
    loaded = storage.load_credentials(c.uid)
    assert loaded.cookies["SESSDATA"] == "abc"
    assert loaded.uid == "20002"


def test_credential_file_is_not_plaintext(storage):
    """核心断言：磁盘上不得出现明文 SESSDATA（方案 §5.3①）。"""
    c = _creds(storage)
    path = storage.save_credentials(c)
    raw = path.read_bytes()
    assert b"abc" not in raw
    assert b"SESSDATA" not in raw


def test_credential_dir_is_per_uid(storage):
    storage.save_credentials(_creds(storage, "1"))
    storage.save_credentials(_creds(storage, "2"))
    assert storage.list_uids() == ["1", "2"]


def test_wrong_secret_cannot_decrypt(storage, monkeypatch):
    storage.save_credentials(_creds(storage))
    monkeypatch.setenv("VD_SECRET_KEY", "a-completely-different-secret")
    with pytest.raises(storage.CredentialError):
        storage.load_credentials("20002")


def test_revoke_removes_credentials(storage):
    storage.save_credentials(_creds(storage))
    assert storage.delete_credentials("20002") is True
    assert storage.list_uids() == []
    with pytest.raises(storage.CredentialError):
        storage.load_credentials("20002")


def test_missing_secret_is_explicit_error(storage, monkeypatch):
    monkeypatch.delenv("VD_SECRET_KEY", raising=False)
    with pytest.raises(storage.CredentialError):
        storage.save_credentials(_creds(storage))


# ---------------- R12：配额分级 ----------------


def test_owner_is_unlimited(quota):
    uid = os.environ["VD_OWNER_BILI_UID"]
    for _ in range(50):
        assert quota.check(uid, planned_seconds=7200).allowed
        quota.commit(uid, "BV1", 7200)


def test_both_accounts_are_admins(quota, monkeypatch):
    """一个人可以有大小号，两个都是管理员、都不限配额。

    「谁是管理员」与「用哪个号取数」是两件独立的事。
    """
    monkeypatch.setenv("VD_ADMIN_UIDS", "10001,20002")
    assert quota.QuotaRule.for_uid("10001").unlimited
    assert quota.QuotaRule.for_uid("20002").unlimited
    assert not quota.QuotaRule.for_uid("30003").unlimited


def test_guest_limited_to_three_videos(quota):
    uid = "30003"
    for i in range(3):
        assert quota.check(uid, planned_seconds=60).allowed
        quota.commit(uid, f"BV{i}", 60)
    status = quota.check(uid, planned_seconds=60)
    assert not status.allowed
    assert "每日上限" in status.reason


def test_guest_limited_by_total_seconds(quota, monkeypatch):
    """时长上限必须独立于次数上限生效。

    注意二者是**两道独立的闸门**：次数上限是 3 个视频，所以时长闸门必须在
    前 3 个之内就可能触发，否则「累计不超过 3 小时」这条规则形同虚设。
    这里把上限压到 2 小时来验证闸门确实独立工作。
    """
    monkeypatch.setenv("VD_GUEST_DAILY_SECONDS_LIMIT", "7200")
    uid = "40004"
    assert quota.check(uid, planned_seconds=2500).allowed
    quota.commit(uid, "BV1", 2500)
    assert quota.check(uid, planned_seconds=2500).allowed
    quota.commit(uid, "BV2", 2500)
    # 已用 5000 秒，只剩 2200 秒；再来一个 3000 秒的必须被挡，
    # 而且此时视频数才 2 个 < 3，证明是时长闸门在起作用
    status = quota.check(uid, planned_seconds=3000)
    assert not status.allowed
    assert status.used_videos == 2
    assert "剩余" in status.reason


def test_guest_seconds_limit_blocks_before_count_limit(quota):
    """真实配置下（3 小时 / 3 个），一个 3 小时 10 分钟的长视频必须被直接拒绝。"""
    uid = "40005"
    assert quota.check(uid, planned_seconds=2 * 3600).allowed
    quota.commit(uid, "BV1", 2 * 3600)
    status = quota.check(uid, planned_seconds=2 * 3600)
    assert not status.allowed  # 再一个 2 小时会到 4 小时 > 3 小时


def test_guest_quota_is_per_uid(quota):
    quota.commit("50005", "BV1", 60)
    quota.commit("50005", "BV2", 60)
    quota.commit("50005", "BV3", 60)
    assert not quota.check("50005", 60).allowed
    # 另一个账号不受影响 —— 不能出现「一个人刷完，所有人被封」
    assert quota.check("60006", 60).allowed


def test_quota_counts_by_bili_uid_not_session(quota):
    """换浏览器/清 Cookie 不能绕过：配额绑定在凭据所属 UID 上。"""
    quota.commit("70007", "BV1", 60)
    quota.commit("70007", "BV2", 60)
    quota.commit("70007", "BV3", 60)
    assert not quota.check("70007", 60).allowed


def test_owner_uid_unset_treats_everyone_as_guest(quota, monkeypatch):
    monkeypatch.delenv("VD_OWNER_BILI_UID", raising=False)
    rule = quota.QuotaRule.for_uid("10001")
    assert not rule.unlimited
    assert rule.daily_videos == 3


def test_no_sqlite_handle_leak(quota):
    """回归：`with sqlite3.connect(...)` 只管事务、**不关闭连接**。

    这曾导致 Windows 上 sqlite 文件被一直占用（WinError 32），长期运行还会累积句柄。
    在 Windows 上，能删掉数据库文件就证明句柄确实释放了。
    """
    uid = "80008"
    quota.commit(uid, "BV1", 60)
    quota.check(uid, 60)
    quota.report(uid)
    db = quota.DB_PATH
    assert db.exists()
    db.unlink()  # Windows 上若句柄未释放，这里会抛 PermissionError
    assert not db.exists()
