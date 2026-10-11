"""P1 取数层验收测试（离线，不需要网络与 GPU）。

覆盖的是"取数逻辑"本身，而不是接口连通性：
  - 空产物校验（方案 §5.2③：绝不静默放过）
  - 字幕优先、ASR 兜底的路由决策
  - 缓存命中/未命中
  - 热词修正共用性
  - 相邻重复行去除

真实接口的连通性由 scripts/transcript_probe.py 与 tests/acceptance_p0.py 覆盖，
两者刻意分开：接口会变，逻辑不该跟着一起挂。
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

from vd.bilibili import VideoInfo  # noqa: E402
from vd.hotwords import Correction, apply_corrections  # noqa: E402
from vd.transcript import (  # noqa: E402
    MIN_TRANSCRIPT_CHARS,
    NoTranscriptError,
    Segment,
    TranscriptSource,
    _dedupe_adjacent,
    _pick_track,
    _validate,
)


# ---------------- 空产物校验 ----------------


def test_validate_rejects_empty():
    with pytest.raises(NoTranscriptError) as exc:
        _validate("", "测试")
    assert "过短" in str(exc.value)


def test_validate_rejects_whitespace_only():
    with pytest.raises(NoTranscriptError):
        _validate("   \n\t  \u3000 ", "测试")


def test_validate_rejects_short():
    with pytest.raises(NoTranscriptError):
        _validate("短" * (MIN_TRANSCRIPT_CHARS - 1), "测试")


def test_validate_accepts_at_threshold():
    _validate("字" * MIN_TRANSCRIPT_CHARS, "测试")  # 不抛即通过


def test_validate_ignores_whitespace_when_counting():
    """长度按有效字符算，不按字节数——否则一堆空格就能骗过校验。"""
    text = "字" * MIN_TRANSCRIPT_CHARS + " " * 500
    _validate(text, "测试")


# ---------------- 字幕轨道选择 ----------------


class _T:
    def __init__(self, lan, lan_doc="", url="u", is_ai=False):
        self.lan, self.lan_doc, self.url, self.is_ai = lan, lan_doc, url, is_ai


def test_pick_track_prefers_manual():
    tracks = [_T("ai-zh"), _T("zh-CN")]
    assert _pick_track(tracks).lan == "zh-CN"


def test_pick_track_prefers_chinese_manual_over_other_manual():
    tracks = [_T("en-US"), _T("zh-CN")]
    assert _pick_track(tracks).lan == "zh-CN"


def test_pick_track_falls_back_to_ai_chinese():
    tracks = [_T("ai-en"), _T("ai-zh")]
    assert _pick_track(tracks).lan == "ai-zh"


def test_pick_track_returns_something_when_only_foreign():
    tracks = [_T("ai-ja")]
    assert _pick_track(tracks).lan == "ai-ja"


# ---------------- 去重 ----------------


def test_dedupe_adjacent_merges_identical():
    segs = [
        Segment(0, 1, "你好"),
        Segment(1, 2, "你好"),
        Segment(2, 3, "世界"),
    ]
    out = _dedupe_adjacent(segs)
    assert [s.text for s in out] == ["你好", "世界"]
    assert out[0].end == 2  # 合并后时间跨度保留


def test_dedupe_merges_repeat_within_window_even_with_gap():
    """窗口内的重复一律合并，即使中间夹了别的行。

    这条测试是从旧行为改过来的：原版断言"非紧邻重复要保留"，
    但那正是导致产物读起来像结巴的原因（见下一条测试的真实样本）。
    """
    segs = [Segment(0, 1, "A"), Segment(1, 2, "B"), Segment(2, 3, "A")]
    out = _dedupe_adjacent(segs, window=10.0)
    assert [s.text for s in out] == ["A", "B"]
    assert out[0].end == 3


def test_dedupe_catches_near_duplicate_within_window():
    """间隔几秒、中间夹了别的行，也要去重。

    这是实测遇到的形态：`找到deep sekai的依赖` 在 [00:07:30] 出现两次，
    中间夹了一行，第一版只处理紧邻重复所以漏掉了，产物读起来像结巴。
    """
    segs = [
        Segment(0, 4, "找到NPM缓存"),
        Segment(4, 8, "找到deep sekai的依赖"),
        Segment(8, 9, "（别的一行）"),
        Segment(9, 13, "找到deep sekai的依赖"),
        Segment(13, 17, "删除即可"),
    ]
    out = _dedupe_adjacent(segs, window=10.0)
    assert [s.text for s in out].count("找到deep sekai的依赖") == 1
    # 合并到较早那条，时间跨度覆盖两处
    merged = next(s for s in out if s.text == "找到deep sekai的依赖")
    assert merged.start == 4
    assert merged.end == 13


def test_dedupe_respects_window_boundary():
    """超出窗口的重复是真实重复内容（如口头禅），不能一律删掉。"""
    segs = [
        Segment(0, 2, "对"),
        Segment(100, 102, "对"),
    ]
    assert len(_dedupe_adjacent(segs, window=10.0)) == 2


# ---------------- 热词修正：两条路线共用 ----------------


def test_correction_applies_to_subtitle_style_error():
    """B 站 AI 字幕实测错误：DeepSeek → DPCARNE。"""
    r = apply_corrections("本期视频带来DPCARNE官方桌面端的安装教程")
    assert "DeepSeek" in r.text
    assert r.total >= 1


def test_correction_applies_to_asr_style_error():
    """FunASR 实测错误：花鼓灯 → 花舞灯。"""
    r = apply_corrections("安徽花舞灯的基本功")
    assert "花鼓灯" in r.text


def test_correction_longer_rule_wins():
    """`DPC harness` 必须优先于 `DPC`，否则会被拆坏。"""
    r = apply_corrections("DPC harness 的安装教程")
    assert "DeepSeek Harness" in r.text
    assert "DPCARNE" not in r.text


def test_correction_records_hits():
    r = apply_corrections("DPCARNE 和 DPCARNE")
    assert r.total == 2
    assert r.hits[0].wrong == "DPCARNE"
    assert r.hits[0].count == 2


def test_context_scoped_rule_does_not_overreach():
    """带上下文的规则只在触发词附近的窗口内替换，避免全篇误伤。

    这里钉住一个真实踩过的实现 bug：旧版"在匹配片段内部替换"，
    而上下文正则匹配到的只是触发词本身，误识别词在它外面 → 永远替换不到。
    现在语义是"上下文窗口"。
    """
    rules = [Correction("犯油", "红油", context=r"包子|烧心")]
    # 触发词在误识别词之前（窗口内）
    assert apply_corrections("这包子吃完不犯油", rules).text == "这包子吃完不红油"
    # 触发词在误识别词之后（窗口内）
    assert apply_corrections("不犯油，这包子不错", rules).text == "不红油，这包子不错"
    # 附近没有任何触发词 → 不动
    assert apply_corrections("他犯油腻的毛病", rules).text == "他犯油腻的毛病"


def test_context_window_is_bounded():
    """窗口是有界的：离得太远就不该替换，否则等于没有上下文限制。"""
    rules = [Correction("犯油", "红油", context=r"包子")]
    far = "包子" + "啊" * 200 + "犯油"
    assert apply_corrections(far, rules).text.endswith("犯油")
    near = "包子" + "啊" * 5 + "犯油"
    assert apply_corrections(near, rules).text.endswith("红油")


def test_context_rule_with_no_trigger_is_noop():
    rules = [Correction("犯油", "红油", context=r"包子")]
    r = apply_corrections("他犯油腻的毛病", rules)
    assert r.text == "他犯油腻的毛病"
    assert r.total == 0


def test_bare_dpc_rule_is_context_limited():
    """裸 `DPC` 是高风险规则（别的领域也用这个缩写）。

    这里逐个钉住"该改的"与"不该改的"。上下文限定是安全关键，
    不能只靠人眼验一次——规则表会持续增长，很容易某天放宽过头。
    """
    # 该修正的：B 站/工具语境
    for text in [
        "本期视频带来DPCCARNE官方桌面端的安装教程",
        "登录自己的DPCK账号",
        "DPC 官方客户端下载",
        "DPC 桌面端安装教程",
    ]:
        assert apply_corrections(text).text != text, f"应修正但没动：{text}"

    # 不该动的：同一缩写在其它领域的正常用法
    for text in [
        "DPC 是数据保护委员会的缩写",
        "Windows 的 DPC 延迟过高",
        "DPC watchdog violation 蓝屏",
        "这场 DPC 比赛很精彩",
    ]:
        assert apply_corrections(text).text == text, f"不该改却改了：{text}"


def test_correction_does_not_reintroduce_wrong_form():
    """修正后再次修正应稳定（幂等），避免"改了又改回去"这类循环。"""
    text = "本期带来DPCCARNE官方桌面端，登录DPCK账号"
    once = apply_corrections(text).text
    twice = apply_corrections(once).text
    assert once == twice
    assert "DPCCARNE" not in once and "DPCK" not in once


def test_correction_is_idempotent():
    once = apply_corrections("DPCARNE").text
    twice = apply_corrections(once).text
    assert once == twice


def test_default_hotwords_file_created(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.hotwords")
    target = tmp_path / "hotwords.json"
    p = mod.ensure_default_file(target)
    assert p.exists()
    # 幂等：再调一次不覆盖
    before = p.read_text(encoding="utf-8")
    mod.ensure_default_file(p)
    assert p.read_text(encoding="utf-8") == before


# ---------------- 缓存 ----------------


@pytest.fixture()
def cache_mod(tmp_path, monkeypatch):
    mod = importlib.import_module("vd.cache")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "c.sqlite3")
    return mod


def test_cache_roundtrip(cache_mod):
    cache_mod.put("video_info", "BV1", {"title": "t"})
    assert cache_mod.get("video_info", "BV1") == {"title": "t"}


def test_cache_miss_returns_none(cache_mod):
    assert cache_mod.get("video_info", "nope") is None


def test_cache_respects_ttl(cache_mod, monkeypatch):
    cache_mod.put("ranking", "r", [1, 2, 3])
    assert cache_mod.get("ranking", "r", ttl=3600) == [1, 2, 3]
    assert cache_mod.get("ranking", "r", ttl=0.0) is not None  # ttl<=0 表示不过期
    # 时间倒推，模拟过期
    import sqlite3, time
    from contextlib import closing

    with closing(cache_mod._connect()) as conn:
        with conn:
            conn.execute(
                "UPDATE kv_cache SET created_at=? WHERE namespace='ranking'",
                (time.time() - 7200,),
            )
    assert cache_mod.get("ranking", "r", ttl=3600) is None


def test_cache_invalidate(cache_mod):
    cache_mod.put("subtitle", "a", 1)
    cache_mod.put("subtitle", "b", 2)
    assert cache_mod.invalidate("subtitle", "a") == 1
    assert cache_mod.get("subtitle", "a") is None
    assert cache_mod.get("subtitle", "b") == 2
    assert cache_mod.invalidate("subtitle") == 1


def test_no_sqlite_handle_leak(cache_mod):
    """与 quota 同样的坑：sqlite3 的 with 不关闭连接。"""
    cache_mod.put("video_info", "BV1", {"x": 1})
    cache_mod.get("video_info", "BV1")
    db = cache_mod.DB_PATH
    db.unlink()  # Windows 上句柄未释放会抛 PermissionError
    assert not db.exists()


# ---------------- 来源标注 ----------------


def test_source_labels_and_gpu_flag():
    assert TranscriptSource.SUBTITLE.uses_gpu is False
    assert TranscriptSource.SUBTITLE_CACHE.uses_gpu is False
    for src in TranscriptSource:
        assert src.label  # 每种都有可读标签
        assert src.uses_gpu is False  # 已移除 ASR 兜底，所有来源都不占 GPU


# ---------------- 无字幕时直接报错，不做 ASR 兜底 ----------------


def test_no_subtitle_raises_no_asr_fallback(cache_mod):
    """已确认无字幕 → 直接抛 NoTranscriptError，不再走 ASR。

    架构变更：本项目不再做"无字幕 → 本地语音识别"兜底，
    没有人工字幕与 AI 字幕的视频直接报错。
    """
    import vd.transcript as tr
    from vd.bilibili import NoSubtitleError

    info = VideoInfo(bvid="BVNOSUB", cid=1, title="t", duration=60, owner_name="u")

    class FakeClient:
        def video_info(self, bvid):
            return info

        def subtitle_tracks(self, _info):
            raise NoSubtitleError("确认无字幕")

        def fetch_cues(self, _t):
            return []

    with pytest.raises(NoTranscriptError) as exc:
        tr.fetch_transcript(FakeClient(), "BVNOSUB", use_cache=True)
    msg = str(exc.value)
    assert "字幕" in msg
    # 确认没有写入 ASR 缓存（旧逻辑即使失败也可能写缓存）
    assert cache_mod.get("asr", "BVNOSUB") is None


def test_short_subtitle_raises_no_asr_fallback(cache_mod):
    """字幕轨道存在但正文过短 → 同样直接报错，不降级到 ASR。"""
    import vd.transcript as tr
    from vd.bilibili import SubtitleTrack

    info = VideoInfo(bvid="BVSHORT", cid=1, title="t", duration=60, owner_name="u")
    track = SubtitleTrack(lan="ai-zh", lan_doc="中文（AI）", url="http://x/1.json", is_ai=True)

    class FakeClient:
        def video_info(self, bvid):
            return info

        def subtitle_tracks(self, _info):
            return [track]

        def fetch_cues(self, _t):
            return [{"from": 0, "to": 1, "content": "太短"}]

    with pytest.raises(NoTranscriptError) as exc:
        tr.fetch_transcript(FakeClient(), "BVSHORT", use_cache=True)
    assert "字幕" in str(exc.value)
    assert cache_mod.get("asr", "BVSHORT") is None


# ---------------- ASR 必须请求句级时间戳 ----------------


def test_asr_requests_sentence_timestamps(tmp_path, monkeypatch):
    """必须传 sentence_timestamp=True。

    踩过的真实 bug：不传时 FunASR 只返回 key/text/timestamp，没有 sentence_info，
    逐字稿退化成"整片一个时间戳"。方案 §5.2② 要求每条都能点回原视频位置，
    一个锚点不合格 —— 而且这个退化**不会报错**，很容易一路带到产物里。
    """
    import vd.asr as asr

    captured: dict = {}

    class FakeModel:
        def generate(self, **kwargs):
            captured.update(kwargs)
            return [{
                "text": "你好。世界。",
                "sentence_info": [
                    {"start": 0, "end": 1200, "text": "你好。"},
                    {"start": 1200, "end": 2600, "text": "世界。"},
                ],
            }]

    monkeypatch.setattr(asr, "load_model", lambda *a, **k: FakeModel())
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")

    result = asr.transcribe_wav(wav, 2.6, device="cpu")

    assert captured.get("sentence_timestamp") is True, "没传 sentence_timestamp，会退化成单时间戳"
    assert len(result.segments) == 2
    # 毫秒 → 秒
    assert result.segments[0].start == 0.0
    assert abs(result.segments[1].start - 1.2) < 1e-6
    assert abs(result.segments[1].end - 2.6) < 1e-6


def test_asr_falls_back_when_param_unsupported(tmp_path, monkeypatch):
    """老版本 FunASR 不认该参数时要降级重试，而不是直接失败。"""
    import vd.asr as asr

    calls: list[dict] = []

    class OldModel:
        def generate(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("sentence_timestamp"):
                raise TypeError("unexpected keyword argument 'sentence_timestamp'")
            return [{"text": "降级结果" * 5}]

    monkeypatch.setattr(asr, "load_model", lambda *a, **k: OldModel())
    wav = tmp_path / "b.wav"
    wav.write_bytes(b"RIFF")

    result = asr.transcribe_wav(wav, 10.0, device="cpu")
    assert len(calls) == 2, "应重试一次"
    assert result.text.startswith("降级结果")
    assert result.segments == []  # 没有句级信息，交给上层兜底


def test_asr_rejects_overlong_video():
    """超长视频必须早拒，而不是默默占用串行 GPU 几十分钟。"""
    import vd.asr as asr
    from vd.bilibili import VideoInfo

    info = VideoInfo(bvid="BV4H", cid=1, title="t", duration=5 * 3600, owner_name="u")

    class C:
        session = type("S", (), {"headers": {"User-Agent": "x"}})()

    with pytest.raises(asr.AsrError) as exc:
        asr.transcribe_video(C(), info, max_seconds=4 * 3600)
    assert "超过 ASR 上限" in str(exc.value)
