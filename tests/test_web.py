"""P3 测试：任务队列、管线编排、Web 层（全部离线）。

重点不在"页面能打开"，而在几条**安全与正确性边界**：
  1. 队列的原子认领（将来误开多线程也不能重复消费）；
  2. 未受邀账号不能提交任务；
  3. **Web 登录不能绕过操作号白名单** —— 这是最容易被 Web 层写漏的一条；
  4. 配额不足在管线里被拦住，且**失败任务不占额度**；
  5. 路径穿越不能下载到 output 之外的文件。
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("VD_SECRET_KEY", "test-secret-for-unit-tests-only")
os.environ.setdefault("VD_OWNER_BILI_UID", "11111")
os.environ.setdefault("VD_OPERATOR_BILI_UID", "22222")


# ======================================================= 任务存储


@pytest.fixture()
def tstore(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.tasks")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "tasks.sqlite3")
    return mod


def test_create_and_get(tstore):
    t = tstore.create("BV1AAA", requester="22222")
    got = tstore.get(t.id)
    assert got.bvid == "BV1AAA"
    assert got.status is tstore.TaskStatus.QUEUED
    assert got.stage == "queued"


def test_claim_next_is_fifo(tstore):
    a = tstore.create("BV1AAA")
    b = tstore.create("BV1BBB")
    assert tstore.claim_next().id == a.id
    assert tstore.claim_next().id == b.id
    assert tstore.claim_next() is None


def test_claim_next_marks_running_once(tstore):
    """乐观锁语义：第二次认领拿不到同一个任务。"""
    t = tstore.create("BV1AAA")
    first = tstore.claim_next()
    second = tstore.claim_next()
    assert first.id == t.id
    assert second is None


def test_finish_and_fail(tstore):
    t1 = tstore.create("BV1AAA")
    tstore.claim_next()
    tstore.finish(t1.id, result={"points": 5})
    assert tstore.get(t1.id).status is tstore.TaskStatus.DONE
    assert tstore.get(t1.id).result["points"] == 5

    t2 = tstore.create("BV1BBB")
    tstore.claim_next()
    tstore.fail(t2.id, "出错了")
    got = tstore.get(t2.id)
    assert got.status is tstore.TaskStatus.FAILED
    assert "出错了" in got.error


def test_find_active_reuses_queued_task(tstore):
    """同一视频不应被重复排队 —— 那会白烧两次 GPU。"""
    t = tstore.create("BV1AAA")
    assert tstore.find_active("BV1AAA").id == t.id
    tstore.claim_next()
    assert tstore.find_active("BV1AAA").id == t.id  # running 也算
    tstore.finish(t.id)
    assert tstore.find_active("BV1AAA") is None     # 完成后可重跑


def test_find_reusable_includes_recently_done(tstore):
    """刚成功的任务也可复用 —— 否则连点两次提交会白跑一遍 LLM。"""
    t = tstore.create("BV1AAA")
    tstore.claim_next()
    tstore.finish(t.id)
    assert tstore.find_reusable("BV1AAA").id == t.id


def test_find_reusable_never_reuses_failed(tstore):
    """失败的任务必须能重试，不能被"复用"卡住。"""
    t = tstore.create("BV1AAA")
    tstore.claim_next()
    tstore.fail(t.id, "出错了")
    assert tstore.find_reusable("BV1AAA") is None


def test_find_reusable_window_is_configurable(tstore, monkeypatch):
    t = tstore.create("BV1AAA")
    tstore.claim_next()
    tstore.finish(t.id)
    monkeypatch.setenv("VD_TASK_REUSE_SECONDS", "0")
    assert tstore.find_reusable("BV1AAA") is None       # 关闭复用
    monkeypatch.setenv("VD_TASK_REUSE_SECONDS", "3600")
    assert tstore.find_reusable("BV1AAA").id == t.id


def test_reuse_window_handles_garbage_env(tstore, monkeypatch):
    monkeypatch.setenv("VD_TASK_REUSE_SECONDS", "abc")
    assert tstore.reuse_window() == tstore.DEFAULT_REUSE_WINDOW
    monkeypatch.delenv("VD_TASK_REUSE_SECONDS")
    assert tstore.reuse_window() == tstore.DEFAULT_REUSE_WINDOW


def test_queue_position(tstore):
    a = tstore.create("BV1AAA")
    b = tstore.create("BV1BBB")
    assert tstore.queue_position(a.id) == 1
    assert tstore.queue_position(b.id) == 2
    tstore.claim_next()
    assert tstore.queue_position(a.id) == 0  # 运行中不再是排队状态


def test_requeue_stale_revives_zombie(tstore):
    """进程被强杀会留下永远 RUNNING 的任务，启动时要能恢复。"""
    t = tstore.create("BV1AAA")
    tstore.claim_next()
    assert tstore.get(t.id).status is tstore.TaskStatus.RUNNING
    # 用负的超时把 cutoff 推到未来，保证判定为过期
    n = tstore.requeue_stale(timeout_seconds=-1)
    assert n == 1
    got = tstore.get(t.id)
    assert got.status is tstore.TaskStatus.QUEUED
    assert "重新排队" in got.message


def test_requeue_stale_leaves_fresh_task_alone(tstore):
    t = tstore.create("BV1AAA")
    tstore.claim_next()
    assert tstore.requeue_stale(timeout_seconds=3600) == 0
    assert tstore.get(t.id).status is tstore.TaskStatus.RUNNING


def test_list_recent_filters_by_requester(tstore):
    tstore.create("BV1AAA", requester="22222")
    tstore.create("BV1BBB", requester="33333")
    assert [t.bvid for t in tstore.list_recent(requester="22222")] == ["BV1AAA"]
    assert len(tstore.list_recent()) == 2


def test_stats_counts_by_status(tstore):
    tstore.create("BV1AAA")
    tstore.create("BV1BBB")
    tstore.claim_next()
    assert tstore.stats()["running"] == 1
    assert tstore.stats()["queued"] == 1
    tstore.finish(tstore.claim_next().id)
    s = tstore.stats()
    assert s["done"] == 1
    assert s["queued"] == 0
    assert s["total"] == 2


def test_no_sqlite_handle_leak(tstore):
    tstore.create("BV1AAA")
    tstore.stats()
    db = tstore.DB_PATH
    db.unlink()  # Windows 上句柄未释放会抛 PermissionError
    assert not db.exists()


def test_stage_progress_is_monotonic(tstore):
    """阶段进度不能倒退 —— 前端进度条会看起来在往回跑。"""
    stages = [k for k, _, _ in tstore.STAGES]
    values = [tstore.STAGE_PROGRESS[s] for s in stages]
    assert values == sorted(values)
    assert values[0] == 0 and values[-1] == 100


# ======================================================= 权限


@pytest.fixture()
def web(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.web")
    tstore = importlib.import_module("vd.tasks")
    monkeypatch.setattr(tstore, "DB_PATH", tmp_path / "tasks.sqlite3")
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path / "output")
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    # 角色集合必须由每个测试显式设定：
    # `admin_uids()` 会把 VD_OWNER/VD_OPERATOR 并进管理员集合，
    # 如果不在 fixture 里清空，前一个测试设的操作号会污染后一个测试的权限判断。
    monkeypatch.setenv("VD_OWNER_BILI_UID", "11111")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "22222")
    monkeypatch.delenv("VD_ADMIN_UIDS", raising=False)
    monkeypatch.delenv("VD_INVITED_UIDS", raising=False)
    monkeypatch.delenv("VD_OPERATOR_IS_ADMIN", raising=False)  # 用默认值（操作号算管理员）
    return mod


def test_admin_can_submit(web):
    ok, role = web.can_submit("11111")   # owner
    assert ok and role == "管理员"
    ok, role = web.can_submit("22222")   # operator
    assert ok and role == "管理员"


def test_invited_user_can_submit(web, monkeypatch):
    monkeypatch.setenv("VD_INVITED_UIDS", "33333, 44444")
    ok, role = web.can_submit("33333")
    assert ok and role == "受邀使用者"
    assert web.can_submit("44444")[0] is True


def test_uninvited_user_cannot_submit(web):
    ok, role = web.can_submit("99999")
    assert not ok and role == "未受邀"


def test_invited_list_ignores_blanks(web, monkeypatch):
    monkeypatch.setenv("VD_INVITED_UIDS", " , 33333 ,, ")
    assert web.invited_uids() == {"33333"}


# ======================================================= 管线（用假依赖）


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.pipeline")
    quota = importlib.import_module("vd.quota")
    cache = importlib.import_module("vd.cache")
    monkeypatch.setattr(quota, "DB_PATH", tmp_path / "quota.sqlite3")
    monkeypatch.setattr(cache, "DB_PATH", tmp_path / "cache.sqlite3")
    return mod


class _Info:
    def __init__(self):
        self.bvid = "BV1AAA"
        self.cid = 1
        self.title = "测试视频"
        self.duration = 600
        self.owner_name = "up"
        self.owner_mid = 1
        self.desc = ""

    def __iter__(self):  # 便于 **info 展开
        return iter([])


def _fake_transcript():
    from vd.bilibili import VideoInfo
    from vd.transcript import Segment, Transcript, TranscriptSource

    segs = [Segment(i * 5.0, i * 5.0 + 5, f"第{i}句内容够长一些") for i in range(10)]
    info = VideoInfo(bvid="BV1AAA", cid=1, title="测试视频", duration=600, owner_name="up")
    return Transcript(
        bvid="BV1AAA", title="测试视频", source=TranscriptSource.SUBTITLE,
        segments=segs, info=info,
    )


class _FakeSummary:
    def __init__(self):
        from vd.summarize import KeyPoint, Strategy, Usage

        self.bvid = "BV1AAA"
        self.title = "测试视频"
        self.overall = "这是总结。" * 20
        self.key_points = [KeyPoint(f"要点{i}", [float(i * 5)]) for i in range(4)]
        self.strategy = Strategy.SINGLE
        self.model = "deepseek-flash"
        self.usage = Usage(prompt_tokens=100, completion_tokens=50)
        self.elapsed = 0.5
        self.dropped_points = 0
        self.notes = []
        self.cost_cny = 0.001


def test_pipeline_writes_both_artifacts(pipeline, tmp_path, monkeypatch):
    from vd.bilibili import VideoInfo

    monkeypatch.setattr(
        pipeline, "fetch_transcript", lambda *a, **k: _fake_transcript()
    )
    monkeypatch.setattr(pipeline, "summarize", lambda *a, **k: _FakeSummary())

    class C:
        def video_info(self, bvid):
            return VideoInfo(bvid=bvid, cid=1, title="t", duration=600, owner_name="up")

    stages: list[str] = []
    res = pipeline.run_pipeline(
        C(), "BV1AAA", uid="22222", out_dir=tmp_path / "out",
        enforce_quota=False, on_stage=lambda s, m: stages.append(s),
    )
    assert res.transcript_path.exists()
    assert res.summary_path.exists()
    assert stages[0] == pipeline.STAGE_META
    assert stages[-1] == pipeline.STAGE_RENDER


def test_pipeline_rejects_when_quota_exceeded(pipeline, tmp_path, monkeypatch):
    """配额不足时必须在取逐字稿**之前**就拦住。"""
    from vd.bilibili import VideoInfo

    called = {"transcript": False}

    def spy(*a, **k):
        called["transcript"] = True
        return _fake_transcript()

    monkeypatch.setattr(pipeline, "fetch_transcript", spy)
    monkeypatch.setattr(pipeline, "summarize", lambda *a, **k: _FakeSummary())

    class C:
        def video_info(self, bvid):
            return VideoInfo(bvid=bvid, cid=1, title="t", duration=7200, owner_name="up")

    # 访客当日额度 3 小时 = 10800 秒；先占满
    from vd.quota import commit

    commit("99999", "BVOTHER", 10800)

    with pytest.raises(pipeline.QuotaExceeded):
        pipeline.run_pipeline(
            C(), "BV1AAA", uid="99999", out_dir=tmp_path / "out",
            enforce_quota=True,
        )
    assert not called["transcript"], "配额不足却已经去取逐字稿了"


def test_failed_pipeline_does_not_consume_quota(pipeline, tmp_path, monkeypatch):
    """失败的任务不该占用用户的每日额度。"""
    from vd.bilibili import VideoInfo
    from vd.quota import report

    def boom(*a, **k):
        raise RuntimeError("模拟取数失败")

    monkeypatch.setattr(pipeline, "fetch_transcript", boom)

    class C:
        def video_info(self, bvid):
            return VideoInfo(bvid=bvid, cid=1, title="t", duration=600, owner_name="up")

    before = report("88888").used_videos
    with pytest.raises(RuntimeError):
        pipeline.run_pipeline(
            C(), "BV1AAA", uid="88888", out_dir=tmp_path / "out", enforce_quota=True
        )
    assert report("88888").used_videos == before


# ======================================================= Web 层


@pytest.fixture()
def client(web, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    return TestClient(web.app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_index_without_credentials_shows_login(client):
    """没有凭据时应给出登录入口，而不是 500。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "扫码登录" in r.text


def test_index_with_credentials_shows_form(client, tmp_path, monkeypatch):
    storage = importlib.import_module("vd.storage")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "开始蒸馏" in r.text
    assert "小号" in r.text


def test_index_shows_disclaimer(client):
    """免责声明的关键内容必须出现在使用者看得到的页面上，而不是只躺在 docs 里。"""
    r = client.get("/")
    assert "4.2.11" in r.text                      # 协议条款引用
    assert "商业目的" in r.text                     # 非商业使用声明
    assert "注销账号" in r.text or "被处置" in r.text  # 账号风险告知
    assert "风险" in r.text                         # 账号风险提示


# ---------------- 免责声明的 HTML 渲染（踩过三个坑，逐个钉住） ----------------


def test_disclaimer_has_no_raw_markdown(web):
    """使用者看到的法律文本里不能残留 Markdown 标记。

    踩过的坑：最初只把换行换成 `<br>`，于是 `**不对外开放注册**` 原样显示。
    半成品标记会直接削弱法律文本的严肃性。
    """
    h = web._disclaimer_html()
    assert "**" not in h
    assert "##" not in h
    assert "<strong>" in h        # 粗体必须真的渲染出来


def test_disclaimer_merges_bold_across_lines(web):
    """**跨行的粗体**必须被合并，否则每行各剩一个 `**`，谁也配不上对。

    真实样本：
        10. 逐字稿是对原视频内容的转换结果，**不得对外发布、转载、再分发，
            不得用于任何商业用途，不得用于训练商业模型**。
    """
    h = web._disclaimer_html()
    m = re.search(r"<li>逐字稿是对原视频内容.*?</li>", h, re.S)
    assert m, "没找到该条目"
    assert "<strong>不得对外发布、转载、再分发，不得用于任何商业用途" in m.group(0)
    assert "**" not in m.group(0)


def test_disclaimer_keeps_indented_numbered_items_separate(web):
    """缩进的编号条目是**同级条目**，不能被当成续行拼进上一条。

    踩过的坑：免责声明原文的条目本身就带 2 空格缩进（`  1. …`），
    用"缩进 = 续行"的宽松判定会把所有条目粘成一大段，结构彻底破坏 ——
    表现是条目数骤减、每条被塞进多个编号。
    """
    h = web._disclaimer_html()
    items = re.findall(r"<li>(.*?)</li>", h, re.S)
    assert len(items) >= 15, f"条目数只有 {len(items)}，疑似被粘连"

    # 每条都应是完整的一句话，不应吞并下一条
    for it in items:
        assert it.count("。") <= 4, f"条目疑似粘连了多条：{it[:60]}"

    # 具体钉住：删除凭证那条必须自成一条（完整包含句末），
    # 且不能被提示块粘住
    target = next(
        (it for it in items if "随时要求删除你的凭证" in it), None
    )
    assert target, "找不到'删除凭证'这一条"
    assert "删除后本服务立即无法再以你的身份访问哔哩哔哩。" in target

    # 也不能把它们粘在一起（旧 bug 的表现）
    flat = "".join(items)
    assert "哔哩哔哩。⚠️" not in flat


def test_disclaimer_warning_lines_become_notes(web):
    """⚠️ 开头的提示要渲染成醒目块，而不是混进列表。

    这里还有一层顺序陷阱：提示行里含数字，若先判"编号条目"就会把它当普通列表项。
    """
    h = web._disclaimer_html()
    notes = re.findall(r'<div class="note">(.*?)</div>', h, re.S)
    assert notes, "没有任何提示块"
    # 提示块要告知账号风险
    assert any("风险" in n for n in notes), f"提示块未说明账号风险：{notes}"
    # 提示行不应出现在 <li> 里
    items = "".join(re.findall(r"<li>(.*?)</li>", h, re.S))
    assert "⚠️" not in items


def test_disclaimer_headings_are_structured(web):
    h = web._disclaimer_html()
    assert "<h3>" in h and "使用须知与免责声明" in h
    assert h.count("<h4>") >= 5, "章节标题没有被结构化"
    for section in ("本服务的性质", "自动化取数", "登录凭证", "内容与版权"):
        assert section in h


def test_disclaimer_switches_operator_variant(monkeypatch):
    """免责声明不再根据操作号配置切换文案，统一展示账号风险提示。

    原来测试的是操作号隔离的两种文案（已隔离/未隔离），现在内部细节不对外展示，
    免责声明统一提示账号风险即可。
    """
    import vd.disclaimer as disc

    # 无论操作号怎么配，full_text 都能正常渲染且包含风险提示
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "999")
    monkeypatch.setenv("VD_OWNER_BILI_UID", "111")
    assert "风险" in disc.full_text()

    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "")
    assert "风险" in disc.full_text()


def test_full_text_is_renderable(web):
    """`full_text()` 必须能被调用并完整渲染。

    它是登录页与"使用须知"的数据源，一旦某个占位符没有对应实参，
    `str.format` 会抛 KeyError/NameError —— **首页直接 500**。
    这条测试专门防止那类"从没被调用过所以从没暴露"的缺失。
    """
    import vd.disclaimer as disc

    t = disc.full_text()
    assert len(t) > 500
    assert "{" not in t and "}" not in t, "有占位符没有被替换"
    assert disc.full_text(public_service=True) != t
    # Web 渲染路径也要通
    assert "使用须知" in web._disclaimer_html()


def test_task_creation_requires_login_when_remote(web, tmp_path, monkeypatch):
    """**关键安全测试**：远程来源没有会话就不能提交任务。

    注意本机来源是免登录的（凭据已在磁盘上，登录只是仪式感），
    所以必须用**非本机对端地址**来测这条边界。

    这条是被真实服务"打脸"后补上的：最初只检查磁盘上有没有操作号凭据，
    而那份凭据是长期存在的 —— 于是任何能访问端口的人都能提交任务，
    无需登录、烧的是主人的配额与 GPU。
    """
    from fastapi.testclient import TestClient

    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    c = TestClient(web.app, client=("203.0.113.7", 41000))
    r = c.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    assert r.status_code == 303
    assert "need_login" in r.headers["location"], "远程无会话竟被放行"


def test_task_page_requires_login_when_remote(web, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    tstore = importlib.import_module("vd.tasks")
    t = tstore.create("BV1AAA", requester="22222")
    c = TestClient(web.app, client=("203.0.113.8", 41001))
    r = c.get(f"/tasks/{t.id}", follow_redirects=False)
    assert r.status_code == 303
    assert "need_login" in r.headers["location"]


def test_task_page_rejects_other_users_task(client, tmp_path, monkeypatch):
    """别人的任务不能看（管理员除外）。"""
    web = importlib.import_module("vd.web")
    monkeypatch.setenv("VD_OWNER_BILI_UID", "11111")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "22222")
    monkeypatch.setenv("VD_ADMIN_UIDS", "11111")
    # 明确：操作号不是管理员，否则它会自动获得查看别人任务的权限
    monkeypatch.setenv("VD_OPERATOR_IS_ADMIN", "false")
    tstore = importlib.import_module("vd.tasks")
    t = tstore.create("BV1AAA", requester="99999")
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("22222"))
    assert client.get(f"/tasks/{t.id}").status_code == 403


def test_admin_can_view_any_task(client, tmp_path, monkeypatch):
    web = importlib.import_module("vd.web")
    monkeypatch.setenv("VD_OWNER_BILI_UID", "11111")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "22222")
    tstore = importlib.import_module("vd.tasks")
    t = tstore.create("BV1AAA", requester="99999")
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("11111"))
    r = client.get(f"/tasks/{t.id}")
    assert r.status_code == 200


def test_task_creation_without_credentials_redirects_to_login(web, tmp_path, monkeypatch):
    """磁盘上没有操作号凭据 → 跳回登录页（含自建会话也一样）。"""
    from fastapi.testclient import TestClient

    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")  # 空目录
    c = TestClient(web.app, client=("203.0.113.9", 41002))
    c.cookies.set(web.SESSION_COOKIE, web._login_for_test("22222"))
    r = c.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    assert r.status_code == 303
    assert "need_login" in r.headers["location"]


def test_task_creation_rejects_bad_bvid(client, tmp_path, monkeypatch):
    web = importlib.import_module("vd.web")
    storage = importlib.import_module("vd.storage")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("22222"))
    r = client.post("/tasks", data={"bvid": "not-a-bvid"}, follow_redirects=False)
    assert r.status_code == 400


def test_operator_is_always_allowed_by_design(web, monkeypatch):
    """操作号按定义就在管理员集合里，因此**永远**可以提交。

    这纠正了我自己先前的一个误解：我一度想测"操作号未受邀 → 403"，
    但那个状态在设计上不存在（服务端自己决定谁是操作号，
    而 `admin_uids()` 必然并入 `VD_OPERATOR_BILI_UID`）。

    `VD_INVITED_UIDS` 因此是**前瞻性**配置：等 P3 之后支持"多个使用者各自登录"
    时，它才会真正生效。现在它不改变任何可达路径 —— 这一点必须诚实记录，
    而不是写一个永远不会失败、也没测到东西的测试。
    """
    monkeypatch.delenv("VD_INVITED_UIDS", raising=False)
    ok, role = web.can_submit("22222")   # 操作号
    assert ok and role == "管理员"
    ok, role = web.can_submit("11111")   # 大号
    assert ok and role == "管理员"


def test_non_admin_non_invited_is_rejected_by_can_submit(web, monkeypatch):
    """权限判定函数本身对"既非管理员也未受邀"的账号返回拒绝。

    这是**单元级**验证：即便当前没有可达的 HTTP 路径能触发它
    （因为取数凭据总是操作号），判定逻辑本身也必须正确 ——
    将来加入多用户登录时它就是第一道闸门。
    """
    monkeypatch.delenv("VD_INVITED_UIDS", raising=False)
    ok, role = web.can_submit("99999")
    assert not ok and role == "未受邀"
    # 列入邀请名单后放行
    monkeypatch.setenv("VD_INVITED_UIDS", "99999")
    ok, role = web.can_submit("99999")
    assert ok and role == "受邀使用者"


def test_invited_operator_can_submit(client, tmp_path, monkeypatch):
    """受邀路径的 HTTP 全链路（用非管理员的操作号模拟受邀使用者）。"""
    web = importlib.import_module("vd.web")
    storage = importlib.import_module("vd.storage")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "55555")
    monkeypatch.setenv("VD_ADMIN_UIDS", "11111")   # 55555 不在管理员集合里
    monkeypatch.setenv("VD_INVITED_UIDS", "55555")
    storage.save_credentials(
        storage.Credentials(
            uid="55555", uname="受邀者",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "55555"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("55555"))
    r = client.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    assert r.status_code == 303
    assert "/tasks/" in r.headers["location"]


def test_uninvited_session_is_rejected_over_http(client, tmp_path, monkeypatch):
    """未受邀的会话即使凭据齐全也不能提交（403，而非 401/409）。"""
    web = importlib.import_module("vd.web")
    storage = importlib.import_module("vd.storage")
    monkeypatch.setenv("VD_OWNER_BILI_UID", "11111")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "55555")
    monkeypatch.setenv("VD_ADMIN_UIDS", "11111")
    monkeypatch.setenv("VD_OPERATOR_IS_ADMIN", "false")   # 操作号≠管理员
    monkeypatch.delenv("VD_INVITED_UIDS", raising=False)
    storage.save_credentials(
        storage.Credentials(
            uid="55555", uname="路人",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "55555"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    assert web.can_submit("55555")[0] is False
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("55555"))
    r = client.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    assert r.status_code == 403


def test_duplicate_submission_reuses_active_task(client, tmp_path, monkeypatch):
    """同一视频重复提交应复用队列中的任务，而不是排队两次白烧 GPU。"""
    web = importlib.import_module("vd.web")
    storage = importlib.import_module("vd.storage")
    tstore = importlib.import_module("vd.tasks")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("22222"))
    r1 = client.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    r2 = client.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    assert r1.headers["location"] == r2.headers["location"]
    assert tstore.stats()["total"] == 1


def test_download_blocks_path_traversal(client, tmp_path):
    """只允许下载 output 下的 .md，杜绝 ../ 穿越。"""
    web = importlib.import_module("vd.web")
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    for bad in ["../secret.md", "..%2Fsecret.md", "/etc/passwd", "a.txt", "x/y.md"]:
        r = client.get(f"/files/{bad}")
        assert r.status_code in (400, 404), f"{bad} 竟然返回 {r.status_code}"


def test_download_serves_existing_markdown(client):
    web = importlib.import_module("vd.web")
    (web.OUTPUT_DIR / "BV1AAA.md").write_text("# hi", encoding="utf-8")
    r = client.get("/files/BV1AAA.md")
    assert r.status_code == 200
    assert "# hi" in r.text


def test_task_page_404_for_unknown(client):
    """已登录但任务不存在 → 404（顺序：先验会话，再查任务）。"""
    web = importlib.import_module("vd.web")
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("22222"))
    assert client.get("/tasks/nope").status_code == 404


def test_task_json_reports_queue_position(client):
    tstore = importlib.import_module("vd.tasks")
    t = tstore.create("BV1AAA", requester="22222")
    r = client.get(f"/tasks/{t.id}/json")
    assert r.status_code == 200
    assert r.json()["queue_position"] == 1
    assert r.json()["status"] == "queued"


def test_apikey_check_does_not_leak_key(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-1234567890abcdefghij")
    r = client.get("/apikey-check")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert "1234567890abcdefghij" not in str(body)  # 不能回显完整 key


def test_login_poll_unknown_key(client):
    assert client.get("/login/poll?key=nope").json()["status"] == "missing"


def test_login_qr_png_404_for_unknown_key(client):
    assert client.get("/login/qr.png?key=nope").status_code == 404


def test_login_qr_png_works_with_key_param(client):
    """**回归测试**：二维码图片接口必须真的能出图。

    踩过的坑：前端最初传 `?k=`，后端读的是 `key`，参数名不匹配导致**永远 404** ——
    也就是说二维码从来没有显示出来过，而当时没有任何测试覆盖这条路径。
    """
    r = client.post("/login/start")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["key"]
    assert d.get("expires_in"), "前端要靠它显示倒计时"

    g = client.get(f"/login/qr.png?key={d['key']}")
    assert g.status_code == 200, f"二维码接口失败：{g.status_code}"
    assert g.headers["content-type"] == "image/png"
    assert g.content[:8] == b"\x89PNG\r\n\x1a\n", "返回的不是有效 PNG"
    assert "no-store" in g.headers.get("cache-control", "")


def test_login_page_uses_matching_param_name(client):
    """页面脚本用的参数名必须和后端一致（防止再次出现 k/key 不匹配）。"""
    html = client.get("/").text
    if "login/start" not in html:
        pytest.skip("当前环境为本机自动会话，不显示登录卡片")
    assert "login/qr.png?key=" in html
    assert "login/qr.png?k=" not in html, "前端仍在用 k= 参数"
    assert "login/poll?key=" in html


def test_index_local_access_needs_no_login(client, tmp_path, monkeypatch):
    """本机访问且凭据已在磁盘上 → 直接可用，不该逼用户扫码。

    这是修过的设计矛盾：原先"要求会话"与"凭据已在磁盘"冲突，
    导致首页有表单、提交却被拦、页面又找不到登录入口。
    """
    storage = importlib.import_module("vd.storage")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "开始蒸馏" in r.text
    # 本机访问时不应弹出登录卡片
    assert "需要扫码登录" not in r.text


def test_remote_access_requires_login(web, tmp_path, monkeypatch):
    """非本机来源必须登录 —— 这是真正的访问控制。"""
    from fastapi.testclient import TestClient

    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    c = TestClient(web.app, client=("192.168.1.50", 51234))
    r = c.get("/")
    assert r.status_code == 200
    assert "需要扫码登录" in r.text
    assert "开始蒸馏" not in r.text
    # 远程提交也要被挡回登录
    rr = c.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    assert rr.status_code == 303
    assert "need_login" in rr.headers["location"]


def test_trust_lan_opt_in(web, tmp_path, monkeypatch):
    """显式打开 VD_WEB_TRUST_LAN 后，局域网来源才免登录。"""
    from fastapi.testclient import TestClient

    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    monkeypatch.setenv("VD_WEB_TRUST_LAN", "true")
    c = TestClient(web.app, client=("192.168.1.50", 51234))
    assert "开始蒸馏" in c.get("/").text
    monkeypatch.setenv("VD_WEB_TRUST_LAN", "false")
    c2 = TestClient(web.app, client=("192.168.1.51", 51234))
    assert "需要扫码登录" in c2.get("/").text


def test_host_header_cannot_fake_local(web, tmp_path, monkeypatch):
    """**安全关键**：Host 头是客户端可控的，不能用来判定"是否本机"。

    判定必须基于 socket 对端地址，否则远程攻击者只要发
    `Host: localhost` 就能白拿本机免登录待遇。
    """
    from fastapi.testclient import TestClient

    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    c = TestClient(web.app, client=("203.0.113.9", 40000))
    r = c.get("/", headers={"Host": "localhost:8788"})
    assert "需要扫码登录" in r.text, "伪造 Host 头竟然拿到了本机免登录！"


def test_status_and_quota_pages_render_html(client, tmp_path, monkeypatch):
    """状态与配额必须是排版过的页面，不是一坨纯文本/JSON。"""
    storage = importlib.import_module("vd.storage")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    for path in ("/status", "/quota"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/html"), path
        assert "<style>" in r.text and "card" in r.text, f"{path} 缺少样式"
        assert not r.text.lstrip().startswith("{"), f"{path} 返回了 JSON"


def test_bvid_accepts_full_url(client, tmp_path, monkeypatch):
    """粘贴整条链接也应能识别出 BV 号（用户不会只复制 BV 号）。"""
    web = importlib.import_module("vd.web")
    storage = importlib.import_module("vd.storage")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    c = client  # 本机自动会话
    url = "https://www.bilibili.com/video/BV1NXH661EpG/?spm_id_from=333.999"
    r = c.post("/tasks", data={"bvid": url}, follow_redirects=False)
    assert r.status_code == 303, r.text[:200]
    tstore = importlib.import_module("vd.tasks")
    assert tstore.list_recent(limit=1)[0].bvid == "BV1NXH661EpG"


def test_bvid_rejects_garbage(client, tmp_path, monkeypatch):
    storage = importlib.import_module("vd.storage")
    storage.save_credentials(
        storage.Credentials(
            uid="22222", uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "22222"},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    r = client.post("/tasks", data={"bvid": "随便写点东西"}, follow_redirects=False)
    assert r.status_code == 400


def test_session_lookup_ignores_forged_cookie(client):
    """伪造的 cookie 不应被当成有效会话。"""
    web = importlib.import_module("vd.web")
    client.cookies.set(web.SESSION_COOKIE, "forged-token")

    class FakeReq:
        cookies = {web.SESSION_COOKIE: "forged-token"}

    assert web._session_of(FakeReq()) is None


# ======================================================= 结果页：箭头时间轴与要点


def _done_task_html(client):
    """造一个已完成任务，返回详情页 HTML。

    注意要给客户端一个会话：任务详情页要求登录，
    且测试隔离目录里没有凭据，所以本机免登录那条路也走不通。
    """
    web = importlib.import_module("vd.web")
    tstore = importlib.import_module("vd.tasks")
    client.cookies.set(web.SESSION_COOKIE, web._login_for_test("22222"))
    t = tstore.create("BV1dFem67EYt", requester="22222")
    tstore.claim_next()
    tstore.finish(t.id, result={
        "title": "测试视频", "duration": 1749,
        "transcript_source": "B 站站内字幕", "uses_gpu": False,
        "chars": 9074, "segments": 822, "points": 3,
        "strategy": "single", "cost_cny": 0.0482,
        "transcript_file": "x.md", "summary_file": "y.md",
        "tl_dr": "一句话速览内容。",
        "overall": "第一句总结。第二句总结。",
        # 关键节点只保留文字（已去掉时间戳与跳转）
        "highlights": [
            {"text": "错失 DOTA"},
            {"text": "HGC 取消"},
        ],
        "key_points": [
            {"topic": "错失 DOTA", "text": "详细陈述内容。", "insight": "补充说明内容",
             "category": "事实"},
            {"topic": "电竞崩盘", "text": "HGC 取消经过。", "insight": "",
             "category": "结论"},
        ],
    })
    return client.get(f"/tasks/{t.id}").text


def test_result_page_has_key_nodes(client, tmp_path, monkeypatch):
    """关键节点以编号列表展示（原时间轴已去掉时间戳与跳转）。"""
    html = _done_task_html(client)
    assert "关键节点" in html
    assert "<li>错失 DOTA</li>" in html
    assert "<li>HGC 取消</li>" in html
    # 不再有时间轴交互元素
    assert 'class="tl-axis"' not in html
    assert 'class="tl-node"' not in html
    assert 'class="tl-bubble"' not in html


def test_key_points_are_sections_with_topic(client, tmp_path, monkeypatch):
    """要点是**分段陈述**：带主题标题、补充说明块。"""
    html = _done_task_html(client)
    assert 'id="kp-1"' in html and 'id="kp-2"' in html
    assert "错失 DOTA" in html and "电竞崩盘" in html
    assert 'class="kp-insight"' in html
    assert "补充说明内容" in html


def test_result_page_has_no_video_jump(client, tmp_path, monkeypatch):
    """去掉时间戳后，页面不再有跳原视频的时间戳链接。"""
    html = _done_task_html(client)
    assert "bilibili.com/video/BV1dFem67EYt?t=" not in html
    assert "跳原视频" not in html


# ======================================================= 悬浮「到下载」按钮


def test_result_page_has_floating_scroll_buttons(client, tmp_path, monkeypatch):
    """长文（实测 236~300 行）要有悬浮按钮快速到下载区。

    用户反馈："文稿总结页面右侧加一个悬浮按钮，点击快速滑到页面最下面，
    避免文章过长，让用户可以快速到下载的部分。"
    """
    html = _done_task_html(client)
    assert 'class="to-bottom"' in html
    assert 'id="to-bottom"' in html and "到下载" in html
    assert 'id="to-top"' in html and "顶部" in html
    assert "behavior: 'smooth'" in html
    # 只在滚动一屏后才出现，页面短的时候不该干扰
    assert "classList.toggle('show'" in html
    assert "window.innerHeight * 0.6" in html


def test_floating_container_is_fixed_positioned(client, tmp_path, monkeypatch):
    """**回归**：悬浮按钮的容器必须是 `position:fixed`。

    踩过的坑：我只给按钮写了 `position:fixed`，**外层容器没写**。
    容器是 static，于是 `right/bottom` 对按钮失效、按钮回到文档流，
    跟着内容一起滚走 —— 表现就是"悬浮按钮根本不悬浮"。
    实测用 Chrome 读计算样式才看出来（`position=static, right=auto`）。
    """
    html = _done_task_html(client)
    rule = html.split(".to-bottom {", 1)[1].split("}", 1)[0]
    assert "position:fixed" in rule, "容器缺 position:fixed，按钮会随页面滚走"
    assert "right:26px" in rule and "bottom:26px" in rule
    assert "pointer-events:none" in rule, "容器不该拦截点击"


def test_floating_button_visibility_is_not_transitioned(client, tmp_path, monkeypatch):
    """**回归**：可见性不能参与过渡。

    把 `visibility` 放进 `transition` 会引入两个问题：
      1. `getComputedStyle` 在过渡进行中返回**起始值**（hidden），
         自动化验证会误判成"样式没生效"（我为这个绕了很久）；
      2. 过渡期元素已参与命中测试，语义含混。
    可见性应**立即翻转**，只让位移与透明度做动效。
    """
    html = _done_task_html(client)
    rule = html.split(".to-bottom button {", 1)[1].split("}", 1)[0]
    assert "transition:opacity .18s, transform .18s;" in rule
    assert "visibility .18s" not in rule, "visibility 仍在过渡列表里"


def test_floating_buttons_only_on_done_page(client, tmp_path, monkeypatch):
    """进度页与失败页都很短，不该有悬浮按钮。"""
    tstore = importlib.import_module("vd.tasks")
    web_mod = importlib.import_module("vd.web")
    client.cookies.set(web_mod.SESSION_COOKIE, web_mod._login_for_test("22222"))
    t = tstore.create("BV1RUNNING", requester="22222")
    html = client.get(f"/tasks/{t.id}").text
    assert 'class="to-bottom"' not in html
    assert "进度" in html


# ======================================================= 登出


def _save_creds(tmp_path, monkeypatch, uid="22222"):
    storage = importlib.import_module("vd.storage")
    monkeypatch.setattr(storage, "CRED_DIR", tmp_path / "creds")
    storage.save_credentials(
        storage.Credentials(
            uid=uid, uname="小号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": uid},
            logged_in_at="2026-01-01T00:00:00",
        )
    )


def test_logout_actually_logs_out(client, tmp_path, monkeypatch):
    """**回归测试**：登出必须真的登出。

    踩过的坑：登出只删 Cookie，而本机访问是免登录的（凭据在磁盘上就自动放行），
    于是下一个请求立刻又被自动登录 —— 用户按了登出却发现自己还在里面。
    这就是"按了没作用"的原因。

    修复：写一个哨兵 Cookie，`current_session()` 见到它就不再自动放行。
    """
    web_mod = importlib.import_module("vd.web")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "22222")
    _save_creds(tmp_path, monkeypatch)

    # 登出前：本机自动放行
    assert "开始蒸馏" in client.get("/").text

    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert web_mod._SESSION_LOGGED_OUT in str(r.headers.get("set-cookie", ""))

    after = client.get("/")
    assert "需要扫码登录" in after.text, "登出无效：又被自动放行了"
    assert "开始蒸馏" not in after.text

    # 登出后也不能提交任务
    rr = client.post("/tasks", data={"bvid": "BV1AAAAAAAAA"}, follow_redirects=False)
    assert rr.status_code == 303
    assert "need_login" in rr.headers["location"]


def test_login_after_logout_restores_access(client, tmp_path, monkeypatch):
    """登出是"本次浏览器会话"的状态，重新登录要能恢复。"""
    web_mod = importlib.import_module("vd.web")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "22222")
    _save_creds(tmp_path, monkeypatch)
    client.post("/logout", follow_redirects=False)
    assert "需要扫码登录" in client.get("/").text
    client.cookies.set(web_mod.SESSION_COOKIE, web_mod._login_for_test("22222"))
    assert "开始蒸馏" in client.get("/").text


def test_logout_clears_existing_session(client, tmp_path, monkeypatch):
    """登出要同时让原会话失效，而不只是加个标记。"""
    web_mod = importlib.import_module("vd.web")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "22222")
    _save_creds(tmp_path, monkeypatch)
    token = web_mod._login_for_test("22222")
    client.cookies.set(web_mod.SESSION_COOKIE, token)
    assert token in web_mod._SESSIONS
    client.post("/logout", follow_redirects=False)
    assert token not in web_mod._SESSIONS, "原会话没有被清除"


# ======================================================= 页脚导航


def test_nav_omits_link_to_current_page(client, tmp_path, monkeypatch):
    """页脚不该放指向当前页面的死链接。

    用户反馈："首页最下面的四个按钮，首页这个按钮没有用"——
    在首页上再放一个"首页"链接，点了毫无变化。
    """
    web_mod = importlib.import_module("vd.web")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "22222")
    _save_creds(tmp_path, monkeypatch)

    home = client.get("/").text
    footer = home.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert 'href="/"' not in footer, "首页上仍有指向首页的链接"
    assert 'href="/status"' in footer and 'href="/quota"' in footer

    status = client.get("/status").text
    sfooter = status.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert 'href="/status"' not in sfooter, "状态页上仍有指向状态页的链接"
    assert 'href="/"' in sfooter


# ======================================================= 分页与筛选


TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.S)
ROW = re.compile(r"<tr>(?!<th)(.*?)</tr>", re.S)


def _rows(html: str) -> list[str]:
    """只数**表格内部**的行，避免匹配到样式表里的类名定义（踩过）。

    ⚠️ 正则用 `<table[^>]*>` 而不是 `<table>` —— 后者在给表格加上
    `class="tasks-table"` 之后就匹配不到了，会让一批测试静默变成假失败。
    属性是无关细节，断言不该绑死它。
    """
    m = TABLE.search(html)
    return ROW.findall(m.group(1)) if m else []


# 测试统一使用的 UID。
# ⚠️ 必须与 fixture 设的 `VD_OPERATOR_BILI_UID`（见文件头 os.environ.setdefault）
# 一致：`load_operator_credentials()` 要求"操作号 == 凭据 UID"，
# 不一致时会返回 None，页面就一直停在登录页，测试测的就不是列表逻辑了（踩过）。
TEST_UID = "22222"


def _client_for(client, monkeypatch, uid: str = TEST_UID):
    """造好操作号凭据并登录，返回 web 模块。"""
    web_mod = importlib.import_module("vd.web")
    storage = importlib.import_module("vd.storage")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", uid)
    storage.save_credentials(
        storage.Credentials(
            uid=uid, uname="操作号",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": uid},
            logged_in_at="2026-01-01T00:00:00",
        )
    )
    client.cookies.set(web_mod.SESSION_COOKIE, web_mod._login_for_test(uid, "操作号"))
    return web_mod


def _seed(uid: str, done: int, failed: int) -> None:
    tstore = importlib.import_module("vd.tasks")
    for i in range(done):
        t = tstore.create(f"BVOK{i:08d}", requester=uid)
        tstore.claim_next()
        tstore.finish(t.id, result={"title": f"完成视频 {i}", "points": i})
        tstore.set_title(t.id, f"完成视频 {i}")
    for i in range(failed):
        t = tstore.create(f"BVNG{i:08d}", requester=uid)
        tstore.claim_next()
        tstore.fail(t.id, f"失败原因 {i}")


def test_task_list_shows_video_title(client, tmp_path, monkeypatch):
    """列表里要显示**视频标题**，只给 BV 号看不清是哪个视频。"""
    _client_for(client, monkeypatch)
    _seed(TEST_UID, done=2, failed=1)
    html = client.get("/?status=done").text
    assert "完成视频 0" in html and "完成视频 1" in html
    # 失败任务没有标题，应显示明确占位而不是空白
    f = client.get("/?status=failed").text
    assert "（旧任务，无标题）" in f
    assert "失败原因 0" in f


def test_task_list_pagination(client, tmp_path, monkeypatch):
    """任务多了要分页：每页 10 条，共 25 条 → 3 页。"""
    _client_for(client, monkeypatch)
    _seed(TEST_UID, done=18, failed=7)

    assert len(_rows(client.get("/?page=1").text)) == 10
    assert len(_rows(client.get("/?page=2").text)) == 10
    assert len(_rows(client.get("/?page=3").text)) == 5
    assert "第 1/3 页" in client.get("/?page=1").text


def test_out_of_range_page_clamps_and_reports_correct_number(client, tmp_path, monkeypatch):
    """**回归**：页码越界要收敛，而且**显示的分页条也得是收敛后的页码**。

    踩过的坑：`page_tasks()` 内部把局部 `page` 改小了，但渲染分页条用的是
    调用方传进来的原值 —— 于是出现"数据是第 3 页、页码写着第 99/3 页"的错乱。
    收敛只发生在函数内部，调用方是看不见的，所以必须把它**返回**出来。
    """
    _client_for(client, monkeypatch)
    _seed(TEST_UID, done=18, failed=7)

    html = client.get("/?page=99").text
    assert len(_rows(html)) == 5, "越界页应显示最后一页的数据"
    assert "第 3/3 页" in html, "页码显示未收敛"
    assert "第 99/3 页" not in html


def test_task_list_filter_keeps_during_paging(client, tmp_path, monkeypatch):
    """筛选状态在翻页时必须保留，否则一翻页就跑到别的筛选里。"""
    _client_for(client, monkeypatch)
    _seed(TEST_UID, done=18, failed=7)

    html = client.get("/?status=done&page=2").text
    assert len(_rows(html)) == 8
    # ⚠️ 用**子串**断言而不是精确正则：我第一版正则漏了前导 `/`，
    # 只匹配到"上一页"那条，测出假失败并白查了一轮
    pager = html.split('class="pager"')[1]
    assert "status=done" in pager, "翻页链接丢了筛选条件"
    assert 'class="chip on"' in html


def test_task_list_filter_by_status(client, tmp_path, monkeypatch):
    _client_for(client, monkeypatch)
    _seed(TEST_UID, done=18, failed=7)
    assert len(_rows(client.get("/?status=failed").text)) == 7
    assert len(_rows(client.get("/?status=done").text)) == 10


# ======================================================= 列表样式与时间格式


def test_fmt_time_always_full_format():
    """时间统一为 `yyyy-MM-dd HH:mm:ss`。

    原来的写法是 `created_at[5:16].replace('T',' ')` —— 切片只得到
    `10-11 10:54`，**缺年份也缺秒**。用户要求固定成完整格式。
    """
    web_mod = importlib.import_module("vd.web")
    assert web_mod.fmt_time("2026-10-11T10:54:33") == "2026-10-11 10:54:33"
    assert web_mod.fmt_time("2026-10-11T10:54:33.123456") == "2026-10-11 10:54:33"
    assert web_mod.fmt_time("2026-10-11 10:54:33") == "2026-10-11 10:54:33"
    # 只有日期时补零点，不要输出半截
    assert web_mod.fmt_time("2026-10-11") == "2026-10-11 00:00:00"
    assert web_mod.fmt_time("") == ""
    # 解析失败不能抛异常（时间显示不该让整页 500），原样退回
    assert web_mod.fmt_time("garbage") == "garbage"
    assert web_mod.fmt_time(None) == ""
    assert web_mod.fmt_time("2026-10-11T10:54:33", False) == "2026-10-11 10:54"


def test_task_list_shows_full_timestamp(client, tmp_path, monkeypatch):
    _client_for(client, monkeypatch)
    _seed(TEST_UID, done=1, failed=0)
    html = client.get("/?status=done").text
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", html), "时间不是完整格式"


def test_pill_does_not_wrap(client, tmp_path, monkeypatch):
    """**回归**：状态徽章不能被拆成两行。

    用户截图指出"失败/完成看着不齐"——
    列一窄，"完成"两个字就换行了，徽章变成竖排的方块。

    ⚠️ 注意这与 `.tl-node .cap` 的坑**正好相反**：那边是继承了 `nowrap`
    导致长文字不换行，需要显式 `normal`；这里是必须显式禁止换行。
    同一个属性、两种需求，都不能靠继承碰运气。
    """
    _client_for(client, monkeypatch)
    html = client.get("/").text
    rule = html.split(".pill {", 1)[1].split("}", 1)[0]
    assert "white-space:nowrap" in rule, "徽章缺 nowrap，窄列时会被拆行"
    # 状态列要定宽居中，否则整列看着歪
    assert ".tasks-table th:nth-child(1)" in html
    assert "text-align:center" in html.split(".tasks-table th:nth-child(1)", 1)[1][:140]
    # 时间列不折行
    assert "white-space:nowrap" in html.split(".tasks-table th:nth-child(3)", 1)[1][:180]


def test_artifact_rows_show_full_timestamp(client, tmp_path, monkeypatch):
    tstore = importlib.import_module("vd.tasks")
    _client_for(client, monkeypatch)
    t = tstore.create("BVTS000001", requester=TEST_UID)
    tstore.claim_next()
    tstore.finish(t.id, result={"title": "时间测试", "points": 1})
    tstore.set_artifacts(t.id, [{"name": "a.md", "kind": "summary", "size": 10}])
    html = client.get("/artifacts").text
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", html)


# ======================================================= 我的产物页


def _seed_artifacts(uid: str, n: int) -> None:
    tstore = importlib.import_module("vd.tasks")
    for i in range(n):
        t = tstore.create(f"BVART{i:07d}", requester=uid)
        tstore.claim_next()
        tstore.finish(t.id, result={"title": f"产物视频 {i}", "points": 3,
                                    "chars": 1000, "cost_cny": 0.02})
        tstore.set_title(t.id, f"产物视频 {i}")
        tstore.set_artifacts(t.id, [
            {"name": f"sum-{t.id}.md", "kind": "summary", "size": 1000},
            {"name": f"tr-{t.id}.md", "kind": "transcript", "size": 2000},
        ])


def test_artifacts_page_lists_all_kinds(client, tmp_path, monkeypatch):
    """产物页要跨任务汇总，两种产物都列出来。"""
    _client_for(client, monkeypatch)
    _seed_artifacts(TEST_UID, 2)
    html = client.get("/artifacts").text
    assert html.count('<div class="art-row">') == 4
    assert "总结与要点" in html and "逐字稿" in html
    assert "产物视频 0" in html
    assert "/files/sum-" in html and "/files/tr-" in html


def test_artifacts_page_paginates(client, tmp_path, monkeypatch):
    """18 条任务 × 2 个文件 = 36 个 → 每页 15，共 3 页。"""
    _client_for(client, monkeypatch)
    _seed_artifacts(TEST_UID, 18)
    assert client.get("/artifacts").text.count('<div class="art-row">') == 15
    assert "共 36 条 · 第 1/3 页" in client.get("/artifacts").text
    assert client.get("/artifacts?page=3").text.count('<div class="art-row">') == 6
    assert "第 3/3 页" in client.get("/artifacts?page=99").text


def test_artifacts_page_filters_by_kind(client, tmp_path, monkeypatch):
    _client_for(client, monkeypatch)
    _seed_artifacts(TEST_UID, 18)
    html = client.get("/artifacts?kind=summary").text
    assert "共 18 条 · 第 1/2 页" in html
    assert "transcript" not in html.split('class="art-row"')[1][:300]


def test_artifacts_page_search(client, tmp_path, monkeypatch):
    _client_for(client, monkeypatch)
    _seed_artifacts(TEST_UID, 3)
    html = client.get("/artifacts?q=产物视频 1").text
    assert "产物视频 1" in html
    assert "产物视频 2" not in html
    assert "没有匹配的产物" in client.get("/artifacts?q=不存在的关键词xyz").text


def test_artifacts_page_merges_duplicate_versions(client, tmp_path, monkeypatch):
    """同一视频重复生成多次时，默认只显示最新一份。

    实测踩到：首次打开产物页，12 个文件里有 5 份是同一个视频的重复总结，
    列表被历史版本刷屏。但不偷偷丢 —— 要显示"另有 N 个旧版本"。
    """
    tstore = importlib.import_module("vd.tasks")
    _client_for(client, monkeypatch)
    for i in range(3):        # 同一 bvid 跑三次
        t = tstore.create("BVSAME00001", requester=TEST_UID)
        tstore.claim_next()
        tstore.finish(t.id, result={"title": "重复视频", "points": 1})
        tstore.set_title(t.id, "重复视频")
        tstore.set_artifacts(t.id, [{"name": f"v{i}.md", "kind": "summary", "size": 100}])

    merged = client.get("/artifacts").text
    assert merged.count('<div class="art-row">') == 1, "重复版本没有被合并"
    assert "另有 2 个旧版本" in merged
    # 展开后能看到全部
    assert client.get("/artifacts?all_versions=1").text.count('<div class="art-row">') == 3


def test_artifacts_page_requires_login(client, tmp_path, monkeypatch):
    """产物页是私有数据，未登录必须挡在门外。"""
    web_mod = importlib.import_module("vd.web")
    client.cookies.set(web_mod.SESSION_COOKIE, web_mod._SESSION_LOGGED_OUT)
    r = client.get("/artifacts", follow_redirects=False)
    assert r.status_code == 303
    assert "need_login" in r.headers["location"]
