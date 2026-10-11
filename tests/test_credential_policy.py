"""大号/小号分离策略的回归测试（离线）。

对应需求："大号小号都是我，都是管理员，管理员仅仅使用小号的凭据操作，
为了保证我的大号安全。"

这里钉住的核心不变量是：**大号凭据永远不会被用于取数**。
最容易犯的错是"取第一个凭证"——它在大号登录后会静默地开始用大号抓取，
且没有任何报错。所以策略必须是白名单，不是优先选择。
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

OWNER = "11111"     # 大号
OPERATOR = "22222"  # 小号


@pytest.fixture()
def creds_mod(tmp_path, monkeypatch):
    """把凭证目录指到临时目录，并清空相关环境变量。"""
    storage = importlib.import_module("vd.storage")
    mod = importlib.import_module("vd.credentials")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "credentials")
    for key in ("VD_OWNER_BILI_UID", "VD_OPERATOR_BILI_UID", "VD_ADMIN_UIDS"):
        monkeypatch.delenv(key, raising=False)
    return mod


def _make(storage_mod, uid: str):
    return storage_mod.Credentials(
        uid=uid,
        uname=f"user{uid}",
        cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": uid},
        logged_in_at="2026-01-01T00:00:00",
    )


@pytest.fixture()
def storage_mod():
    return importlib.import_module("vd.storage")


# ---------------- 核心：大号凭据不得用于取数 ----------------


def test_owner_credentials_never_used_for_scraping(creds_mod, storage_mod, monkeypatch):
    """关键测试：即使大号凭据已落盘，取数也只会用小号的。"""
    storage_mod.save_credentials(_make(storage_mod, OWNER))
    storage_mod.save_credentials(_make(storage_mod, OPERATOR))
    monkeypatch.setenv("VD_OWNER_BILI_UID", OWNER)
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", OPERATOR)

    creds = creds_mod.load_operator_credentials()
    assert creds.uid == OPERATOR
    assert creds.uid != OWNER


def test_refuses_when_operator_not_configured(creds_mod, storage_mod, monkeypatch):
    """未配置操作号时必须拒绝，而不是退回"用第一个凭证"。"""
    storage_mod.save_credentials(_make(storage_mod, OWNER))
    monkeypatch.setenv("VD_OWNER_BILI_UID", OWNER)
    with pytest.raises(creds_mod.CredentialPolicyError) as exc:
        creds_mod.load_operator_credentials()
    assert "VD_OPERATOR_BILI_UID" in str(exc.value)


def test_refuses_when_operator_equals_owner(creds_mod, storage_mod, monkeypatch):
    """把大号同时配成操作号 = 隔离失效，必须拒绝并说明原因。"""
    storage_mod.save_credentials(_make(storage_mod, OWNER))
    monkeypatch.setenv("VD_OWNER_BILI_UID", OWNER)
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", OWNER)
    with pytest.raises(creds_mod.CredentialPolicyError) as exc:
        creds_mod.load_operator_credentials()
    assert "相同" in str(exc.value)


def test_refuses_when_operator_credential_missing(creds_mod, storage_mod, monkeypatch):
    """配了操作号但还没登录 → 明确报错，不能悄悄用别的账号。"""
    storage_mod.save_credentials(_make(storage_mod, OWNER))
    monkeypatch.setenv("VD_OWNER_BILI_UID", OWNER)
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", OPERATOR)
    with pytest.raises(creds_mod.CredentialPolicyError) as exc:
        creds_mod.load_operator_credentials()
    assert "尚未登录" in str(exc.value)


def test_optional_mode_returns_none_instead_of_raising(
    creds_mod, storage_mod, monkeypatch
):
    """Web 层需要用它渲染"请先登录"页面，而不是崩掉。"""
    monkeypatch.setenv("VD_OWNER_BILI_UID", OWNER)
    assert creds_mod.load_operator_credentials(required=False) is None


# ---------------- assert_allowed：每次取数前的最后一道闸 ----------------


def test_assert_allowed_rejects_owner_when_operator_set(
    creds_mod, monkeypatch
):
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", OPERATOR)
    creds_mod.assert_allowed(OPERATOR)  # 不抛
    with pytest.raises(creds_mod.CredentialPolicyError):
        creds_mod.assert_allowed(OWNER)


def test_assert_allowed_rejects_anything_without_operator(creds_mod, monkeypatch):
    monkeypatch.delenv("VD_OPERATOR_BILI_UID", raising=False)
    with pytest.raises(creds_mod.CredentialPolicyError):
        creds_mod.assert_allowed(OPERATOR)


# ---------------- 管理员集合 ----------------


def test_admin_uids_union(creds_mod, monkeypatch):
    monkeypatch.setenv("VD_OWNER_BILI_UID", OWNER)
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", OPERATOR)
    monkeypatch.setenv("VD_ADMIN_UIDS", "33333, 44444")
    assert creds_mod.admin_uids() == {OWNER, OPERATOR, "33333", "44444"}


def test_admin_uids_empty_by_default(creds_mod):
    assert creds_mod.admin_uids() == set()


def test_quota_unlimited_follows_admin_uids(creds_mod, monkeypatch):
    """管理员与否只看集合，与"哪个号取数"无关。"""
    quota = importlib.import_module("vd.quota")
    monkeypatch.setenv("VD_ADMIN_UIDS", f"{OWNER},{OPERATOR}")
    assert quota.QuotaRule.for_uid(OWNER).unlimited
    assert quota.QuotaRule.for_uid(OPERATOR).unlimited
    assert not quota.QuotaRule.for_uid("99999").unlimited
