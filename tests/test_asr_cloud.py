"""云 ASR 与音频中转的单元测试（离线，不真调阿里云）。

覆盖三件最容易出错的事：
  1. 结果 JSON → Transcript 的换算（**毫秒 → 秒**，错一个量级锚点就全废）
  2. 中转的 token 生命周期（一次性、过期失效、文件被清）
  3. 没有公网地址时必须**明确报错**，而不是提交一个拉不到的链接
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.asr_cloud")
    relay = tmp_path / "relay"
    monkeypatch.setattr(mod, "RELAY_DIR", relay)
    # token 注册表现在是**磁盘上的 SQLite**（不是进程内字典），
    # 所以补丁打在 DB_PATH 上，而不是某个内存字典。
    monkeypatch.setattr(mod, "REGISTRY_DB", relay / "registry.sqlite3")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setenv("VD_PUBLIC_BASE_URL", "https://example.com")
    return mod


def _sample_result(*, speech_ms=3720, audio_ms=3834) -> dict:
    """照抄官方文档的结果样例结构。"""
    return {
        "file_url": "https://example.com/relay/abc.m4s",
        "properties": {
            "audio_format": "m4s",
            "channels": [0],
            "original_sampling_rate": 16000,
            "original_duration_in_milliseconds": audio_ms,
        },
        "transcripts": [
            {
                "channel_id": 0,
                "content_duration_in_milliseconds": speech_ms,
                "text": "Hello world，这里是阿里巴巴语音实验室。",
                "sentences": [
                    {
                        "begin_time": 760,
                        "end_time": 3240,
                        "text": "Hello world，这里是阿里巴巴语音实验室。",
                        "sentence_id": 1,
                        "words": [
                            {"begin_time": 760, "end_time": 1000,
                             "text": "Hello", "punctuation": ""},
                            {"begin_time": 1000, "end_time": 1120,
                             "text": " World", "punctuation": "，"},
                        ],
                    },
                    {
                        "begin_time": 4000,
                        "end_time": 5000,
                        "text": "第二句在这里。",
                        "sentence_id": 2,
                    },
                ],
            }
        ],
    }


# ---------------------------------------------------------------- 结果换算

def test_milliseconds_are_converted_to_seconds(cloud):
    """**关键换算**：官方字段是毫秒，项目内部一律用秒。

    算错一个量级的话，锚点会指向 1000 倍远的位置 —— 产物看起来正常，
    点进去完全不对。所以这条必须有。
    """
    res = cloud._to_result(_sample_result(), bvid="BVT", title="t", info=None,
                           task_id="task-1")
    segs = res.transcript.segments
    assert len(segs) == 2
    assert segs[0].start == pytest.approx(0.76)
    assert segs[0].end == pytest.approx(3.24)
    assert segs[1].start == pytest.approx(4.0)
    assert segs[1].end == pytest.approx(5.0)
    # 时间必须单调，否则锚点排序会乱
    assert segs[0].start < segs[1].start


def test_billing_fields_use_speech_duration(cloud):
    """计费口径取 `content_duration`（语音时长），不是音频总长。

    官方说明：非语音内容不计量不计费。所以这两个值必须分开存 ——
    用总长去记账会多扣用户的钱。
    """
    res = cloud._to_result(_sample_result(speech_ms=3720, audio_ms=60000),
                           bvid="BVT", title="t", info=None, task_id="t1")
    assert res.speech_seconds == pytest.approx(3.72)
    assert res.audio_seconds == pytest.approx(60.0)
    assert res.speech_seconds < res.audio_seconds


def test_source_is_marked_as_asr(cloud):
    """来源要标成 ASR —— 它是**唯一花钱**的来源，产物头部会据此提示。"""
    res = cloud._to_result(_sample_result(), bvid="BVT", title="t", info=None,
                           task_id="t1")
    src = res.transcript.source
    assert src is cloud.TranscriptSource.ASR
    assert src.costs_money is True
    assert src.uses_gpu is False
    assert "Paraformer" in src.label
    # 字幕来源不能被认为花钱
    assert cloud.TranscriptSource.SUBTITLE.costs_money is False


def test_empty_sentences_raises(cloud):
    """没有句子要明确报错，而不是产出一个空逐字稿。"""
    data = _sample_result()
    data["transcripts"][0]["sentences"] = []
    with pytest.raises(cloud.AsrCloudError, match="句子为空"):
        cloud._to_result(data, bvid="BVT", title="t", info=None, task_id="t1")


def test_inverted_timestamps_are_repaired(cloud):
    """`end <= start` 的句子要修正，不能把非法区间传下去。"""
    data = _sample_result()
    data["transcripts"][0]["sentences"] = [
        {"begin_time": 5000, "end_time": 4000, "text": "倒挂的时间戳"},
    ]
    res = cloud._to_result(data, bvid="BVT", title="t", info=None, task_id="t1")
    seg = res.transcript.segments[0]
    assert seg.end > seg.start, "end 必须大于 start"


# ---------------------------------------------------------------- 中转

def test_publish_requires_public_base_url(cloud, monkeypatch, tmp_path):
    """没有公网地址时必须**明确报错**。

    否则会提交一个阿里云拉不到的 127.0.0.1 链接，
    拿到语焉不详的 FILE_403_FORBIDDEN —— 实测就是这样绕了一圈。
    """
    monkeypatch.setenv("VD_PUBLIC_BASE_URL", "")
    f = tmp_path / "a.m4s"
    f.write_bytes(b"x" * 10)
    with pytest.raises(cloud.RelayUnavailable, match="VD_PUBLIC_BASE_URL"):
        cloud.publish(f)


def test_publish_moves_file_and_makes_unguessable_token(cloud, tmp_path):
    f = tmp_path / "a.m4s"
    f.write_bytes(b"audio")
    t1 = cloud.publish(f)
    assert t1.public_url.startswith("https://example.com/relay/")
    assert len(t1.token) >= 20, "token 太短，公网 URL 可被猜测"
    assert not f.exists(), "应当 move 而不是 copy（音频有几十 MB）"
    assert t1.path.exists()
    assert cloud.resolve(t1.token) == t1.path


def test_tokens_are_unique(cloud, tmp_path):
    toks = set()
    for i in range(5):
        p = tmp_path / f"{i}.m4s"
        p.write_bytes(b"x")
        toks.add(cloud.publish(p).token)
    assert len(toks) == 5


def test_release_removes_file_and_token(cloud, tmp_path):
    """取回结果后必须清理 —— 不给公网留可访问的文件。"""
    f = tmp_path / "a.m4s"
    f.write_bytes(b"audio")
    t = cloud.publish(f)
    cloud.forget(t.token)
    t.release()
    assert not t.path.exists()
    assert cloud.resolve(t.token) is None


def test_resolve_rejects_expired(cloud, tmp_path):
    f = tmp_path / "a.m4s"
    f.write_bytes(b"audio")
    t = cloud.publish(f, ttl=-1)          # 立刻过期
    assert cloud.resolve(t.token) is None


def test_resolve_rejects_missing_file(cloud, tmp_path):
    """文件被外部删掉时，token 也不该再解析出路径。"""
    f = tmp_path / "a.m4s"
    f.write_bytes(b"audio")
    t = cloud.publish(f)
    t.path.unlink()
    assert cloud.resolve(t.token) is None


def test_gc_clears_expired(cloud, tmp_path):
    for i in range(3):
        p = tmp_path / f"{i}.m4s"
        p.write_bytes(b"x")
        cloud.publish(p, ttl=-1)
    assert cloud.gc_tokens() == 3
    assert cloud.gc_tokens() == 0


def test_unknown_token_resolves_to_none(cloud):
    assert cloud.resolve("nope") is None


# ---------------------------------------------------------------- 跨进程可见性

def test_registry_is_visible_across_processes(cloud, tmp_path):
    """**回归（真实架构 bug）**：token 必须能被**另一个进程**看到。

    第一版把 token 存在进程内字典里，实测立刻炸：

        verify 脚本进程：publish() 写自己的内存
        服务进程：        /relay 路由查自己的内存 → 空 → 403

    阿里云因此拿到 `FILE_403_FORBIDDEN`（服务日志原文：
    "严格模式：无进行中的转写任务，拒绝中转请求"）。

    这个测试用**独立子进程**去读父进程发布的 token —— 如果退回内存字典，
    它会失败。
    """
    f = tmp_path / "a.m4s"
    f.write_bytes(b"AUDIO")
    ticket = cloud.publish(f)

    snippet = (
        "import sys; sys.path.insert(0, r'{src}');"
        "from pathlib import Path;"
        "import vd.asr_cloud as m;"
        "m.RELAY_DIR = Path(r'{relay}');"
        "m.REGISTRY_DB = Path(r'{db}');"
        "p = m.resolve('{token}');"
        "print('RESOLVED' if p else 'MISSING')"
    ).format(src=str(ROOT / "src"), relay=str(cloud.RELAY_DIR),
             db=str(cloud.REGISTRY_DB), token=ticket.token)

    proc = subprocess.run([sys.executable, "-c", snippet],
                          capture_output=True, text=True, timeout=120,
                          encoding="utf-8", errors="replace")
    assert "RESOLVED" in (proc.stdout or ""), (
        f"另一个进程读不到 token，说明注册表又退回内存了。\n"
        f"stdout={proc.stdout!r} stderr={(proc.stderr or '')[-300:]!r}"
    )


def test_has_pending_reflects_published_tickets(cloud, tmp_path):
    """严格模式依赖它：有中转时应为 True，清理后应为 False。"""
    assert cloud.has_pending() is False
    f = tmp_path / "a.m4s"
    f.write_bytes(b"AUDIO")
    t = cloud.publish(f)
    assert cloud.has_pending() is True
    cloud.forget(t.token)
    assert cloud.has_pending() is False


def test_has_pending_ignores_expired(cloud, tmp_path):
    f = tmp_path / "a.m4s"
    f.write_bytes(b"AUDIO")
    cloud.publish(f, ttl=-1)
    assert cloud.has_pending() is False, "过期 token 不该算作进行中"


# ---------------------------------------------------------------- 中转路由

def test_relay_route_serves_published_file(cloud, tmp_path, monkeypatch):
    """`/relay/<token>` 必须**免登录**可用 —— 阿里云没有本站凭据。"""
    from fastapi.testclient import TestClient

    from vd import web

    monkeypatch.setattr(web, "OUTPUT_DIR", tmp_path / "output")
    f = tmp_path / "a.m4s"
    f.write_bytes(b"AUDIODATA")
    ticket = cloud.publish(f)

    c = TestClient(web.app)
    r = c.get(f"/relay/{ticket.token}")
    assert r.status_code == 200
    assert r.content == b"AUDIODATA"
    assert r.headers["content-type"].startswith("audio/")


def test_relay_route_404_for_unknown_token(cloud, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from vd import web

    c = TestClient(web.app)
    assert c.get("/relay/deadbeef").status_code == 404


def test_relay_route_rejects_path_traversal(cloud, tmp_path, monkeypatch):
    """**安全测试**：公开路由不能变成任意文件读取。

    这里刻意用已存在的敏感文件名去撞，验证不拼接用户输入做路径。
    """
    from fastapi.testclient import TestClient

    from vd import web

    secret = tmp_path / "secret.m4s"
    secret.write_bytes(b"SECRET")
    c = TestClient(web.app)
    for bad in ("..%2Fsecret.m4s", "..%5Csecret.m4s", "a/b", "a\\b"):
        r = c.get(f"/relay/{bad}")
        assert r.status_code in (400, 404), f"{bad} 竟然返回了 {r.status_code}"
        assert b"SECRET" not in r.content
