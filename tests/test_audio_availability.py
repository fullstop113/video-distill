"""音频可用性分类测试（离线）。

实现方：`audio_url()` 的三种失败要分开、`transcribe_video()` 的**时长比对**。

## 这些测试从哪来

真实故障：一个充电专属视频（BV1f7HS68EZz）报
`BiliError: playurl 未返回 DASH 音频流`。

实测原始响应后发现，**问题根本不是"接口没给音频"**：

```
is_upower_exclusive = True            # 充电专属
is_ugc_pay_preview  = True            # 仅预览
preview_toast       = '为创作付费，购买观看完整视频|购买观看'
playurl: code=0, message='OK', 无 dash, 有 durl[1 段]
durl[0].length      = 249987 ms       # 250 秒
视频真实时长         = 1749 s          # 29 分钟
```

即：**接口"成功"返回了一段能正常播放的 250 秒预览。**
旧代码把这种情况糊成一句"未返回 DASH 音频流"，用户无从判断；
更危险的是——如果有人"顺手"加个 durl 降级而不校验时长，
就会生成一份 250 秒的逐字稿，使用者以为它是 29 分钟视频的。

这正是方案 §5.2③ 禁止的静默残废产物。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("VD_SECRET_KEY", "test-secret-for-unit-tests-only")

from vd.bilibili import (  # noqa: E402
    AudioUnavailableError,
    BiliClient,
    RestrictedVideoError,
    VideoInfo,
)


class FakeClient(BiliClient):
    """绕过网络：直接注入 playurl 响应。"""

    def __init__(self, data, info=None):
        self._data = data
        self.min_interval = 0.0
        self.creds = None
        import requests

        self.session = requests.Session()

    def _signed(self, url, params):
        return {"code": 0, "message": "OK", "data": self._data}


def _info(**kw) -> VideoInfo:
    base = dict(bvid="BV1TEST", cid=1, title="t", duration=1749, owner_name="up")
    base.update(kw)
    return VideoInfo(**base)


# ============================================ 权限标记


def test_charge_exclusive_is_flagged():
    info = _info(is_upower_exclusive=True, is_ugc_pay_preview=True,
                 preview_toast="为创作付费，购买观看完整视频|购买观看")
    assert info.restricted is True
    assert "充电专属" in info.restriction_reason()
    assert "仅预览" in info.restriction_reason()


def test_normal_video_is_not_restricted():
    assert _info().restricted is False
    assert _info().restriction_reason() == ""


def test_paid_upower_play_lifts_restriction():
    """已充电/已购（is_upower_play=True）不算受限 —— 此时能看完整版。"""
    info = _info(is_upower_exclusive=True, is_upower_play=True)
    assert info.restricted is False


def test_need_login_counts_as_restricted():
    assert _info(need_login=True).restricted is True
    assert "需要登录" in _info(need_login=True).restriction_reason()


# ============================================ audio_url 的三种失败


def test_restricted_video_rejected_before_request():
    """已知受限就别浪费请求，直接给出可读原因。"""
    client = FakeClient({})  # 就算返回空也不该走到解析
    with pytest.raises(RestrictedVideoError) as exc:
        client.audio_url(_info(is_upower_exclusive=True, is_ugc_pay_preview=True))
    assert "充电专属" in str(exc.value)


def test_dash_audio_is_preferred():
    data = {
        "dash": {
            "duration": 1749,
            "audio": [
                {"baseUrl": "https://cdn/low.m4s", "bandwidth": 64000},
                {"baseUrl": "https://cdn/best.m4s", "bandwidth": 204000},
            ],
        }
    }
    url, dur = FakeClient(data).audio_url(_info())
    assert url == "https://cdn/best.m4s"
    assert dur == 1749


def test_dash_without_duration_falls_back_to_timelength():
    """dash.duration 缺失时用外层的 timelength（毫秒）。"""
    data = {"timelength": 1_749_000, "dash": {"audio": [{"baseUrl": "u", "bandwidth": 1}]}}
    url, dur = FakeClient(data).audio_url(_info())
    assert dur == pytest.approx(1749.0)


def test_durl_route_returns_stream_duration():
    """老格式/受限视频只有 durl —— 要返回**媒体流自己的时长**，供上游比对。"""
    data = {"durl": [{"url": "https://cdn/seg1.mp4", "length": 249987, "size": 28962938}]}
    url, dur = FakeClient(data).audio_url(_info())
    assert url == "https://cdn/seg1.mp4"
    assert dur == pytest.approx(249.987)


def test_no_stream_at_all_raises_audio_unavailable():
    with pytest.raises(AudioUnavailableError) as exc:
        FakeClient({"something": 1}).audio_url(_info())
    assert "既没有 dash 也没有 durl" in str(exc.value)


def test_restricted_and_no_stream_reports_restriction():
    """受限但连流都没有时，应报受限而不是泛泛的接口异常。"""
    with pytest.raises(RestrictedVideoError):
        FakeClient({}).audio_url(_info(is_ugc_pay_preview=True))


# ============================================ 时长比对（核心防线）


def test_preview_stream_is_rejected_by_duration_check(monkeypatch, tmp_path):
    """**核心测试**：拿到的媒体流远短于视频 → 必须失败，不能产出残废逐字稿。

    真实数据：流 250s，视频 1749s（14%）。
    """
    import vd.asr as asr

    client = FakeClient({"durl": [{"url": "https://cdn/preview.mp4", "length": 249987}]})
    info = _info(duration=1749)

    # 让它走到时长比对就够，不需要真的下载
    with pytest.raises(RestrictedVideoError) as exc:
        asr.transcribe_video(client, info)
    msg = str(exc.value)
    assert "预览片段" in msg
    assert "00:04:09" in msg        # 250 秒
    assert "00:29:09" in msg        # 1749 秒


def test_matching_duration_passes_the_check(monkeypatch, tmp_path):
    """时长对得上就应继续（这里让下载/解码/转写都被替换掉）。"""
    import vd.asr as asr

    client = FakeClient({"dash": {"duration": 1749,
                                  "audio": [{"baseUrl": "u", "bandwidth": 1}]}})
    info = _info(duration=1749)

    monkeypatch.setattr(asr, "download_audio", lambda *a, **k: 1024)
    monkeypatch.setattr(asr, "to_wav16k", lambda *a, **k: 1749.0)
    monkeypatch.setattr(
        asr, "transcribe_wav",
        lambda wav, secs, **k: asr.AsrResult(
            text="x" * 100, segments=[], audio_seconds=secs,
            asr_seconds=1.0, device="cpu", model="m",
        ),
    )
    result = asr.transcribe_video(client, info)
    assert result.audio_seconds == 1749.0


def test_small_duration_drift_is_tolerated(monkeypatch):
    """B 站报的时长与流长常有几秒出入，5% 以内应放过。"""
    import vd.asr as asr

    client = FakeClient({"dash": {"duration": 1700,
                                  "audio": [{"baseUrl": "u", "bandwidth": 1}]}})
    info = _info(duration=1749)   # 1700/1749 = 97.2%
    monkeypatch.setattr(asr, "download_audio", lambda *a, **k: 1024)
    monkeypatch.setattr(asr, "to_wav16k", lambda *a, **k: 1700.0)
    monkeypatch.setattr(
        asr, "transcribe_wav",
        lambda wav, secs, **k: asr.AsrResult(
            text="x" * 100, segments=[], audio_seconds=secs,
            asr_seconds=1.0, device="cpu", model="m",
        ),
    )
    assert asr.transcribe_video(client, info).audio_seconds == 1700.0


def test_decoded_audio_shorter_than_expected_is_rejected(monkeypatch):
    """二次防线：即使地址声明的时长正常，解码后实际音频过短也要拦。"""
    import vd.asr as asr

    client = FakeClient({"dash": {"duration": 1749,
                                  "audio": [{"baseUrl": "u", "bandwidth": 1}]}})
    info = _info(duration=1749)
    monkeypatch.setattr(asr, "download_audio", lambda *a, **k: 1024)
    monkeypatch.setattr(asr, "to_wav16k", lambda *a, **k: 200.0)  # 实际只有 200 秒
    with pytest.raises(RestrictedVideoError) as exc:
        asr.transcribe_video(client, info)
    assert "解码后音频只有" in str(exc.value)


def test_unknown_duration_skips_check(monkeypatch):
    """两边都没有时长信息时不阻拦 —— 不能因为拿不到时长就拒绝一切。"""
    import vd.asr as asr

    client = FakeClient({"dash": {"audio": [{"baseUrl": "u", "bandwidth": 1}]}})
    info = _info(duration=0)
    monkeypatch.setattr(asr, "download_audio", lambda *a, **k: 1024)
    monkeypatch.setattr(asr, "to_wav16k", lambda *a, **k: 100.0)
    monkeypatch.setattr(
        asr, "transcribe_wav",
        lambda wav, secs, **k: asr.AsrResult(
            text="x" * 100, segments=[], audio_seconds=secs,
            asr_seconds=1.0, device="cpu", model="m",
        ),
    )
    assert asr.transcribe_video(client, info).audio_seconds == 100.0


# ============================================ 缓存不能丢权限标记


def test_video_info_cache_keeps_restriction_flags():
    """缓存序列化必须带上权限标记。

    否则读缓存时受限视频会被当成正常视频 —— 又回到"把 250 秒预览当完整逐字稿"那条老路。
    """
    from vd.pipeline import _info_to_dict

    d = _info_to_dict(_info(is_upower_exclusive=True, is_ugc_pay_preview=True,
                            preview_toast="购买观看"))
    for key in ("is_upower_exclusive", "is_ugc_pay_preview", "is_upower_play",
                "preview_toast", "need_login"):
        assert key in d, f"缓存缺少 {key}"
    restored = VideoInfo(**d)
    assert restored.restricted is True
    assert restored.preview_toast == "购买观看"


def test_video_info_cache_tolerates_legacy_shape():
    """旧缓存没有权限字段时不能炸（字段增删是常态）。"""
    legacy = {"bvid": "BVT", "cid": 1, "title": "t", "duration": 100,
              "owner_name": "u", "owner_mid": 0, "desc": ""}
    known = set(VideoInfo.__dataclass_fields__)
    info = VideoInfo(**{k: v for k, v in legacy.items() if k in known})
    assert info.restricted is False
