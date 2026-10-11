"""R1 回归测试：字幕静默失败防护（完全离线，无需网络与登录）。

依据 `docs/p0-findings.md` 记录的真实响应形状：
匿名请求 /x/player/wbi/v2 返回 code=0 + message=OK，
且 subtitle 字段**存在**、内容为 {'allow_submit': False, 'subtitles': []}。

任何只看 code 的实现都会把它误判成「该视频没有字幕」，进而产出空文件。
本文件就是把那个错误钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vd.bilibili import (  # noqa: E402
    BiliClient,
    BiliError,
    NoSubtitleError,
    NotLoggedInError,
    SubtitleTrack,
    VideoInfo,
)

INFO = VideoInfo(bvid="BV1TEST", cid=123, title="t", duration=60)

# 实测拿到的匿名响应形状
ANON_SUBTITLE = {
    "allow_submit": False,
    "lan": "",
    "lan_doc": "",
    "subtitles": [],
    "subtitle_position": None,
    "font_size_type": 0,
}
NEED_LOGIN_SUBTITLE = {**ANON_SUBTITLE, "allow_submit": True}


class FakeClient(BiliClient):
    """绕过网络：直接注入接口响应。"""

    def __init__(self, data, nav=None, sessdata=None, creds=None):
        self._data = data
        self._nav = nav or {"isLogin": False, "uname": ""}
        self.min_interval = 0.0
        self.creds = creds
        import requests

        self.session = requests.Session()
        if sessdata:
            self.session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")

    def _signed(self, url, params):
        return {"code": 0, "data": self._data}

    def nav(self):
        return self._nav


def test_anonymous_empty_list_is_not_confirmed_absent():
    """核心断言：匿名 + 空列表 ≠ 无字幕。"""
    client = FakeClient({"subtitle": ANON_SUBTITLE, "need_login_subtitle": True})
    with pytest.raises(NotLoggedInError):
        client.subtitle_tracks(INFO)


def test_logged_in_empty_list_is_confirmed_absent():
    """已登录 + 空列表 = 可以确认没有站内字幕，放行 ASR 兜底。"""
    client = FakeClient(
        {"subtitle": ANON_SUBTITLE, "need_login_subtitle": False},
        nav={"isLogin": True, "uname": "me"},
        sessdata="valid",
        creds=object(),
    )
    with pytest.raises(NoSubtitleError):
        client.subtitle_tracks(INFO)


def test_expired_cookie_is_not_confirmed_absent():
    """本地有 Cookie 但服务端判定未登录（凭证失效）→ 同样只能说「无法判定」。"""
    client = FakeClient(
        {"subtitle": ANON_SUBTITLE},
        nav={"isLogin": False},
        sessdata="stale-cookie",
        creds=object(),
    )
    with pytest.raises(NotLoggedInError):
        client.subtitle_tracks(INFO)


def test_missing_subtitle_field_when_logged_in_is_error_not_absent():
    """字段缺失是接口变更，不是「没有字幕」—— 不能静默走 ASR。"""
    client = FakeClient(
        {"something_else": 1}, nav={"isLogin": True}, sessdata="valid", creds=object()
    )
    with pytest.raises(BiliError) as exc:
        client.subtitle_tracks(INFO)
    assert not isinstance(exc.value, NoSubtitleError)


def test_tracks_are_returned_when_present():
    data = {
        "subtitle": {
            **ANON_SUBTITLE,
            "subtitles": [
                {
                    "lan": "zh-CN",
                    "lan_doc": "中文（中国）",
                    "subtitle_url": "//aisubtitle.hdslb.com/bfs/x.json",
                    "type": 0,
                },
                {
                    "lan": "ai-zh",
                    "lan_doc": "中文（自动生成）",
                    "subtitle_url": "https://aisubtitle.hdslb.com/bfs/y.json",
                    "ai_type": 1,
                },
            ],
        }
    }
    client = FakeClient(data, nav={"isLogin": True}, sessdata="valid", creds=object())
    tracks = client.subtitle_tracks(INFO)
    assert [t.lan for t in tracks] == ["zh-CN", "ai-zh"]
    assert tracks[0].url.startswith("https://aisubtitle.hdslb.com")
    assert tracks[0].is_ai is False
    assert tracks[1].is_ai is True


def test_anonymous_guard_does_not_need_nav_call():
    """匿名判定只看有没有 SESSDATA，不必再打一次 nav（省一次请求、少一次风控风险）。"""
    client = FakeClient({"subtitle": ANON_SUBTITLE})

    def explode():
        raise AssertionError("匿名路径不应调用 nav()")

    client.nav = explode
    with pytest.raises(NotLoggedInError):
        client.subtitle_tracks(INFO)


def test_is_logged_in_property():
    assert FakeClient({}).is_logged_in is False
    assert FakeClient({}, sessdata="x").is_logged_in is True


def test_fetch_cues_handles_empty_url():
    client = FakeClient({})
    assert client.fetch_cues(SubtitleTrack(lan="zh-CN", lan_doc="", url="")) == []
