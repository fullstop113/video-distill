"""余额页面的 Web 层测试（离线）。

重点：
  · **只有管理员能生成邀请码** —— 这是钱，权限必须卡住
  · 充值失败要给人话（重定向 + 提示），不是 400 页面
  · 余额显示的三档状态（正/可欠/已破底）
  · 未登录不能看余额页
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("VD_SECRET_KEY", "test-secret-for-unit-tests-only")
os.environ.setdefault("VD_OWNER_BILI_UID", "11111")

TEST_UID = "22222"          # 也用作操作号 UID（凭据存在它名下）
ADMIN_UID = "11111"
OPERATOR_UID = "22222"      # 凭据 UID，必须与 VD_OPERATOR_BILI_UID 一致


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 billing 库与凭据目录，并固定余额参数。

    ## 两个容易踩的坑（都记在这里）

    1. **操作号会被自动并入管理员** —— `credentials.admin_uids()` 默认把
       `VD_OPERATOR_BILI_UID` 也算作管理员（`VD_OPERATOR_IS_ADMIN` 默认 true）。
       所以用操作号 UID 根本测不出"非管理员"的行为。
       要测权限边界必须用**另一个 UID**（本文件用 `PLAIN_UID`）。

    2. 凭据必须存成**操作号**的 UID，否则 `load_operator_credentials()`
       返回 None，首页会显示登录卡片而不是余额。
    """
    from fastapi.testclient import TestClient

    from vd import web

    billing = importlib.import_module("vd.billing")
    storage = importlib.import_module("vd.storage")
    tstore = importlib.import_module("vd.tasks")

    monkeypatch.setattr(billing, "DB_PATH", tmp_path / "billing.sqlite3")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    monkeypatch.setattr(tstore, "DB_PATH", tmp_path / "tasks.sqlite3")
    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path / "output")
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VD_OPERATOR_BILI_UID", OPERATOR_UID)
    monkeypatch.setenv("VD_ADMIN_UIDS", ADMIN_UID)
    # 关掉"操作号自动算管理员"，这样 OPERATOR_UID 就是**普通用户**
    monkeypatch.setenv("VD_OPERATOR_IS_ADMIN", "false")
    monkeypatch.setenv("VD_ASR_PRICE_PER_SECOND", "0.00008")
    monkeypatch.setenv("VD_INITIAL_BALANCE", "1")
    monkeypatch.setenv("VD_BALANCE_FLOOR", "")
    monkeypatch.setenv("VD_INVITE_CREDIT", "2.00")
    monkeypatch.setenv("VD_INVITE_BATCH", "5")
    monkeypatch.setenv("VD_INVITE_TTL_SECONDS", "3600")

    storage.save_credentials(
        storage.Credentials(
            uid=OPERATOR_UID, uname="操作号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": OPERATOR_UID},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    client = TestClient(web.app)
    return {"client": client, "web": web, "billing": billing, "storage": storage}


def _login(env, uid: str, uname: str = "测试用户"):
    c = env["client"]
    c.cookies.set(env["web"].SESSION_COOKIE, env["web"]._login_for_test(uid, uname))
    return c


# ======================================================= 余额页

def test_balance_page_requires_login(env):
    c = env["client"]
    c.cookies.set(env["web"].SESSION_COOKIE, env["web"]._SESSION_LOGGED_OUT)
    r = c.get("/balance", follow_redirects=False)
    assert r.status_code == 303
    assert "need_login" in r.headers["location"]


def test_balance_page_shows_balance_and_redeem_form(env):
    c = _login(env, TEST_UID)
    html = c.get("/balance").text
    assert "余额" in html
    assert "¥1.00" in html, "新用户初始 1 元应显示出来"
    assert 'action="/redeem"' in html
    assert 'name="code"' in html
    assert "流水" in html


def test_balance_page_shows_floor(env):
    """要告诉用户"能欠多少" —— 否则他不理解为什么余额为负还能用。

    ⚠️ 那句提示**只在非管理员分支**渲染。管理员看到的是"不限"，
    所以这条必须用普通用户测（本 fixture 已把操作号设为非管理员）。
    """
    c = _login(env, OPERATOR_UID)
    html = c.get("/balance").text
    assert "可用到" in html, "没告诉用户能欠多少"
    assert "1.00" in html
    assert "不限" not in html, "普通用户不该看到'不限'"


def test_admin_sees_generate_button(env):
    c = _login(env, ADMIN_UID, "管理员")
    html = c.get("/balance").text
    assert 'action="/invites"' in html, "管理员没看到生成邀请码按钮"
    # ⚠️ 不要精确匹配整句：模板里换行 + 缩进会把 "有效期 60 分钟" 拆开，
    # 精确断言会假失败（踩过）。用正则容忍空白。
    assert re.search(r"生成\s*5\s*个邀请码", html)
    assert re.search(r"有效期\s*60\s*分钟", html)
    assert re.search(r"每个\s*¥2\.00", html)


def test_non_admin_cannot_see_generate_button(env):
    """**权限边界**：普通用户不能生成邀请码 —— 那是钱。"""
    c = _login(env, OPERATOR_UID)
    html = c.get("/balance").text
    assert 'action="/invites"' not in html
    assert "生成 5 个邀请码" not in html


def test_admin_balance_shows_unlimited(env):
    c = _login(env, ADMIN_UID, "管理员")
    html = c.get("/balance").text
    assert "不限" in html


def test_ledger_shows_initial_grant(env):
    c = _login(env, TEST_UID)
    html = c.get("/balance").text
    assert "新用户赠送" in html


# ======================================================= 充值

def test_redeem_success_redirects_with_message(env):
    c = _login(env, OPERATOR_UID)
    codes = env["billing"].generate_invites(ADMIN_UID, count=1)
    code = codes[0]["code"]

    r = c.post("/redeem", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "ok=" in loc
    # ⚠️ 中文提示在 URL 里是 **percent-encoded** 的，
    # 直接断言 "充值成功" 会失败（踩过）。要先解码。
    assert "充值成功" in unquote(loc)

    after = c.get("/balance").text
    assert "¥3.00" in after, "1 元初始 + 2 元充值"
    assert "邀请码充值" in after


def test_redeem_failure_shows_readable_message(env):
    """**失败要给人话**，不是 400 页面或一坨 JSON。"""
    c = _login(env, OPERATOR_UID)
    r = c.post("/redeem", data={"code": "BOGUSCOD"}, follow_redirects=False)
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    # 跟着跳转看页面
    page = c.get(r.headers["location"]).text
    assert "邀请码无效" in page
    assert "⚠️" in page


def test_redeem_used_code_reports_used(env):
    c = _login(env, TEST_UID)
    code = env["billing"].generate_invites(ADMIN_UID, count=1)[0]["code"]
    c.post("/redeem", data={"code": code}, follow_redirects=False)
    r2 = c.post("/redeem", data={"code": code}, follow_redirects=False)
    page = c.get(r2.headers["location"]).text
    assert "已被使用" in page


def test_redeem_requires_login(env):
    c = env["client"]
    c.cookies.set(env["web"].SESSION_COOKIE, env["web"]._SESSION_LOGGED_OUT)
    r = c.post("/redeem", data={"code": "ABCDEFGH"}, follow_redirects=False)
    assert r.status_code == 303
    assert "need_login" in r.headers["location"]


# ======================================================= 生成邀请码

def test_generate_invites_creates_five(env):
    c = _login(env, ADMIN_UID, "管理员")
    r = c.post("/invites", follow_redirects=False)
    assert r.status_code == 303
    assert "ok=" in r.headers["location"]

    page = c.get("/balance").text
    # 5 个码都应出现在列表里
    items = env["billing"].list_invites(ADMIN_UID)
    assert len(items) == 5
    for i in items:
        assert i["code"] in page


def test_generate_invites_forbidden_for_non_admin(env):
    """**核心权限测试**：普通用户调这个接口必须被拒。

    注意不能只测"页面上没有按钮" —— 接口本身也得挡住，
    否则手写一个 POST 就能给自己发钱。
    """
    c = _login(env, TEST_UID)
    r = c.post("/invites", follow_redirects=False)
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "只有管理员" in page
    # 确认真的没生成
    assert env["billing"].list_invites(ADMIN_UID) == []


def test_generated_code_visible_with_state(env):
    c = _login(env, ADMIN_UID, "管理员")
    c.post("/invites", follow_redirects=False)
    html = c.get("/balance").text
    assert "可用" in html
    assert "面额" in html


# ======================================================= 首页余额展示

def test_index_shows_balance(env):
    """首页要显示余额 —— 用户提交前就该知道够不够。"""
    c = _login(env, TEST_UID)
    env["storage"].save_credentials(
        env["storage"].Credentials(
            uid=TEST_UID, uname="操作号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": TEST_UID},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    html = c.get("/").text
    assert "余额" in html
    assert "¥1.00" in html
    assert 'href="/balance"' in html


def test_nav_has_balance_link(env):
    c = _login(env, TEST_UID)
    html = c.get("/artifacts").text
    footer = html.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert 'href="/balance"' in footer
