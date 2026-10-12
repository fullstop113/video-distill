"""pipeline 接入云 ASR 的测试（离线，不真调阿里云）。

覆盖的核心是**钱的边界** —— 哪条路径会花钱、哪条不会、什么时候扣：

  · 有字幕          → 免费，**绝不能**触发 ASR
  · 无字幕 + 已开ASR + 余额够 → 走 ASR，登记应扣金额
  · 无字幕 + 余额不够         → 拒绝，且**绝不能先下载音频**
  · 无字幕 + 没配 ASR         → 明确报错（而不是含糊的栈）
  · 无字幕 + 未开 ASR         → 抛原始异常
  · 字幕存在但过短            → **不**走 ASR（试看预览那类，走了也白花钱）
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vd.bilibili import NoSubtitleError, VideoInfo  # noqa: E402
from vd.summarize import KeyPoint, Strategy, Summary, Usage  # noqa: E402
from vd.transcript import (  # noqa: E402
    NoTranscriptError,
    Segment,
    Transcript,
    TranscriptSource,
)


def _info(duration: int = 600) -> VideoInfo:
    return VideoInfo(bvid="BVTEST", cid=1, title="测试视频",
                     duration=duration, owner_name="up")


def _fake_summary(transcript: Transcript) -> Summary:
    return Summary(
        bvid="BVTEST", title="测试视频", overall="这是一段足够长的总结内容用于通过下限校验。" * 2,
        key_points=[KeyPoint("要点内容够长一些。", [], "事实", "主题", "")],
        strategy=Strategy.SINGLE, model="fake",
        usage=Usage(), elapsed=0.1, video_duration=600,
    )


@pytest.fixture()
def pipe(tmp_path, monkeypatch):
    """隔离 pipeline + billing + asr_cloud，并桩掉 summarize。"""
    mod = importlib.import_module("vd.pipeline")
    billing = importlib.import_module("vd.billing")
    monkeypatch.setattr(billing, "DB_PATH", tmp_path / "billing.sqlite3")
    monkeypatch.setenv("VD_ASR_PRICE_PER_SECOND", "0.00008")
    monkeypatch.setenv("VD_INITIAL_BALANCE", "1")
    monkeypatch.setenv("VD_BALANCE_FLOOR", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setenv("VD_PUBLIC_BASE_URL", "https://example.com")
    # 不让配额闸门挡路（配额是另一套，这里只测余额）
    monkeypatch.setattr(mod, "check",
                        lambda uid, planned_seconds=0: SimpleNamespace(
                            allowed=True, describe=lambda: "配额充足"))
    monkeypatch.setattr(mod, "commit", lambda *a, **k: None)
    monkeypatch.setattr(mod, "summarize",
                        lambda tr, **kw: _fake_summary(tr))
    return mod


def _stub_asr(pipe, monkeypatch, *, speech=300.0, audio=600.0):
    """桩掉云 ASR，并记录它是否被调用过。"""
    cloud = importlib.import_module("vd.asr_cloud")
    calls = {"n": 0, "downloaded": False}

    def fake_transcribe_video(client, info, *, title="", out_dir, download=None,
                              progress=None):
        calls["n"] += 1
        calls["downloaded"] = True
        segs = [Segment(0.0, 2.0, "第一句转写内容。"), Segment(3.0, 5.0, "第二句。")]
        tr = Transcript(bvid=info.bvid, title=title, source=TranscriptSource.ASR,
                        segments=segs, info=info, notes=["由云 ASR 生成"])
        return cloud.AsrCloudResult(transcript=tr, speech_seconds=speech,
                                    audio_seconds=audio, task_id="t1")

    monkeypatch.setattr(cloud, "transcribe_video", fake_transcribe_video)
    return calls


class _FakeClient:
    """最小 BiliClient：只提供 pipeline 用到的接口。"""

    def __init__(self, *, subtitle=None, has_subtitle_track=True):
        self._subtitle = subtitle
        self._has_track = has_subtitle_track

    def video_info(self, bvid):
        return _info()

    def _fill_restriction(self, info):
        return None

    def audio_url(self, info):
        return "https://cdn.example.com/a.m4s", 600.0


def _with_subtitle_client(monkeypatch,
                          text="字幕正文内容，需要足够长才能通过下限校验（下限 80 字）。" * 4):
    """造一个"有字幕"的 client：让它返回真实的字幕轨与 cues。

    ⚠️ 字幕正文必须超过 `MIN_TRANSCRIPT_CHARS`（80 字），
    否则会被判为"字幕过短"——那是**另一条**路径（不走 ASR），
    测试会测错对象。
    """
    from vd.bilibili import SubtitleTrack

    tr_mod = importlib.import_module("vd.transcript")

    track = SubtitleTrack(lan="zh-CN", lan_doc="中文（自动生成）",
                          url="https://x/sub.json", is_ai=True)

    class C:
        def video_info(self, bvid):
            return _info()

        def _fill_restriction(self, info):
            return None

        def subtitle_tracks(self, info):
            return [track]

        def fetch_cues(self, t):
            return [{"from": 0.0, "to": 3.0, "content": text}]

        def audio_url(self, info):
            return "https://cdn.example.com/a.m4s", 600.0

    return C()


def _no_subtitle_client():
    class C:
        def video_info(self, bvid):
            return _info()

        def _fill_restriction(self, info):
            return None

        def subtitle_tracks(self, info):
            raise NoSubtitleError("确认无字幕")

        def audio_url(self, info):
            return "https://cdn.example.com/a.m4s", 600.0

    return C()


def _short_subtitle_client():
    """字幕轨存在但正文过短 —— 这类视频走 ASR 也是白花钱。"""
    from vd.bilibili import SubtitleTrack

    track = SubtitleTrack(lan="zh-CN", lan_doc="中文", url="https://x/s.json",
                          is_ai=False)

    class C:
        def video_info(self, bvid):
            return _info()

        def _fill_restriction(self, info):
            return None

        def subtitle_tracks(self, info):
            return [track]

        def fetch_cues(self, t):
            return [{"from": 0.0, "to": 1.0, "content": "短"}]

        def audio_url(self, info):
            return "https://cdn.example.com/a.m4s", 600.0

    return C()


# ======================================================= 免费路径

def test_subtitle_path_never_touches_asr(pipe, tmp_path, monkeypatch):
    """**有字幕就绝不能花钱** —— 这是成本可控的关键。"""
    calls = _stub_asr(pipe, monkeypatch)
    client = _with_subtitle_client(monkeypatch)
    charges = {"uid": "90001", "amount": 0.0, "bvid": "BVTEST"}

    res = pipe.run_pipeline(client, "BVTEST", uid="90001",
                            out_dir=tmp_path, use_cache=False,
                            allow_asr=True, charges=charges)

    assert calls["n"] == 0, "有字幕却调用了云 ASR —— 白花钱"
    assert res.asr_cost_cny == 0.0
    assert charges["amount"] == 0.0
    assert res.transcript.source is TranscriptSource.SUBTITLE


def test_short_subtitle_does_not_fall_back_to_asr(pipe, tmp_path, monkeypatch):
    """字幕轨存在但内容过短 → **不**走 ASR。

    这类视频（试看预览、受限内容）下载音频多半也拿不到完整音轨，
    走 ASR 是白花钱。
    """
    calls = _stub_asr(pipe, monkeypatch)
    with pytest.raises(NoTranscriptError):
        pipe.run_pipeline(_short_subtitle_client(), "BVTEST", uid="90002",
                          out_dir=tmp_path, use_cache=False, allow_asr=True)
    assert calls["n"] == 0, "字幕过短却也走了 ASR"


# ======================================================= 付费路径

def test_no_subtitle_uses_asr_and_records_charge(pipe, tmp_path, monkeypatch):
    """无字幕 + 已开启 + 余额够 → 走 ASR，并登记应扣金额。"""
    calls = _stub_asr(pipe, monkeypatch)
    charges = {"uid": "90003", "amount": 0.0, "bvid": "BVTEST"}

    res = pipe.run_pipeline(_no_subtitle_client(), "BVTEST", uid="90003",
                            out_dir=tmp_path, use_cache=False,
                            allow_asr=True, charges=charges)

    assert calls["n"] == 1
    assert res.transcript.source is TranscriptSource.ASR
    # 600 秒 × 0.00008 = 0.048
    assert res.asr_cost_cny == pytest.approx(0.048, rel=1e-6)
    assert charges["amount"] == pytest.approx(0.048, rel=1e-6), \
        "没登记应扣金额，worker 就不会扣费 → 白花钱"


def test_no_subtitle_without_allow_asr_raises(pipe, tmp_path, monkeypatch):
    """未开启 allow_asr 时保持原行为（抛 NoTranscriptError）。"""
    calls = _stub_asr(pipe, monkeypatch)
    with pytest.raises(NoTranscriptError):
        pipe.run_pipeline(_no_subtitle_client(), "BVTEST", uid="90004",
                          out_dir=tmp_path, use_cache=False, allow_asr=False)
    assert calls["n"] == 0


def test_missing_asr_config_raises_actionable_error(pipe, tmp_path, monkeypatch):
    """缺配置要**明确说是缺什么**，而不是给出一个 httpx 的栈。"""
    monkeypatch.delenv("VD_PUBLIC_BASE_URL", raising=False)
    with pytest.raises(pipe.AsrNotConfigured) as exc:
        pipe.run_pipeline(_no_subtitle_client(), "BVTEST", uid="90005",
                          out_dir=tmp_path, use_cache=False, allow_asr=True)
    msg = str(exc.value)
    assert "VD_PUBLIC_BASE_URL" in msg
    assert "check_deploy" in msg, "错误信息要给出自检命令"


def test_missing_dashscope_key_raises(pipe, tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(pipe.AsrNotConfigured, match="DASHSCOPE_API_KEY"):
        pipe.run_pipeline(_no_subtitle_client(), "BVTEST", uid="90006",
                          out_dir=tmp_path, use_cache=False, allow_asr=True)


# ======================================================= 钱的边界（最重要）

def test_insufficient_balance_blocks_before_download(pipe, tmp_path, monkeypatch):
    """**核心边界**：余额不足时必须在**下载音频之前**拒绝。

    顺序反了就会"先把音频交给阿里云（钱已产生）、然后发现余额不够"，
    那笔钱追不回来。用户明确提过这个顾虑。
    """
    calls = _stub_asr(pipe, monkeypatch)
    billing = importlib.import_module("vd.billing")

    # 用户初始 1 元，花掉 0.99 → 剩 0.01；地板 1.0
    billing.get("90007")
    billing.charge("90007", 0.99)
    assert billing.get("90007").balance == pytest.approx(0.01)

    # 视频 600 秒 → 预估 0.048；0.01 − 0.048 = −0.038 > −1.0 → **仍放行**
    # 所以要构造真正会破地板的：把时长拉到 4 小时
    class LongClient:
        def video_info(self, bvid):
            return _info(duration=14400)      # 4 小时

        def _fill_restriction(self, info):
            return None

        def subtitle_tracks(self, info):
            raise NoSubtitleError("确认无字幕")

        def audio_url(self, info):
            return "https://cdn.example.com/a.m4s", 14400.0

    with pytest.raises(pipe.QuotaExceeded) as exc:
        pipe.run_pipeline(LongClient(), "BVTEST", uid="90007",
                          out_dir=tmp_path, use_cache=False, allow_asr=True)
    assert "余额不足" in str(exc.value)
    assert calls["n"] == 0, "余额不足却已经调用了 ASR —— 钱已经花了！"


def test_balance_shortfall_message_says_how_much(pipe, tmp_path, monkeypatch):
    """拒绝信息要能指导用户行动：差多少、怎么充值。"""
    _stub_asr(pipe, monkeypatch)
    billing = importlib.import_module("vd.billing")

    class LongClient:
        def video_info(self, bvid):
            return _info(duration=36000)      # 10 小时 → 预估 2.88 元

        def _fill_restriction(self, info):
            return None

        def subtitle_tracks(self, info):
            raise NoSubtitleError("确认无字幕")

        def audio_url(self, info):
            return "https://cdn.example.com/a.m4s", 36000.0

    with pytest.raises(pipe.QuotaExceeded) as exc:
        pipe.run_pipeline(LongClient(), "BVTEST", uid="90008",
                          out_dir=tmp_path, use_cache=False, allow_asr=True)
    msg = str(exc.value)
    assert "还差" in msg and "邀请码" in msg


def test_admin_is_not_blocked_by_balance(pipe, tmp_path, monkeypatch):
    """管理员不受余额限制（花的是自己的钱）。"""
    monkeypatch.setenv("VD_ADMIN_UIDS", "90009")
    monkeypatch.setenv("VD_OPERATOR_BILI_UID", "90009")
    calls = _stub_asr(pipe, monkeypatch)
    billing = importlib.import_module("vd.billing")
    billing.get("90009")
    billing.charge("90009", 50.0)         # 欠 49 元

    charges = {"uid": "90009", "amount": 0.0, "bvid": "BVTEST"}
    res = pipe.run_pipeline(_no_subtitle_client(), "BVTEST", uid="90009",
                            out_dir=tmp_path, use_cache=False,
                            allow_asr=True, charges=charges)
    assert calls["n"] == 1
    assert res.transcript.source is TranscriptSource.ASR


def test_charge_amount_follows_video_duration_not_speech(pipe, tmp_path, monkeypatch):
    """应扣金额按**视频时长**算，不是按语音时长。

    阿里云实际只对语音内容计费（实测占 57%），所以这样预扣偏保守：
    账面扣得比真实成本多，对账号主人更安全（用户明确要求这个口径）。
    """
    _stub_asr(pipe, monkeypatch, speech=60.0, audio=600.0)   # 语音只占 10%
    charges = {"uid": "90010", "amount": 0.0, "bvid": "BVTEST"}
    pipe.run_pipeline(_no_subtitle_client(), "BVTEST", uid="90010",
                      out_dir=tmp_path, use_cache=False,
                      allow_asr=True, charges=charges)
    # 600 秒 × 0.00008 = 0.048，而不是 60 × 0.00008 = 0.0048
    assert charges["amount"] == pytest.approx(0.048, rel=1e-6)


def test_no_charges_dict_is_tolerated(pipe, tmp_path, monkeypatch):
    """不传 charges 也要能跑（CLI 直接调 pipeline 的场景）。"""
    calls = _stub_asr(pipe, monkeypatch)
    res = pipe.run_pipeline(_no_subtitle_client(), "BVTEST", uid="90011",
                            out_dir=tmp_path, use_cache=False, allow_asr=True)
    assert calls["n"] == 1
    assert res.asr_cost_cny > 0
