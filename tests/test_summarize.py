"""P2 总结层测试（离线，不调用真实 API）。

重点覆盖两类东西：
  1. **锚点校验** —— 方案 §5.2② 要求每条结论都能点回原视频位置。
     模型编造时间戳必须被拦下，这是"产物可被信任"的前提。
  2. **空产物校验与成本估算** —— 与取数层同一原则。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("VD_SECRET_KEY", "test-secret-for-unit-tests-only")

from vd.bilibili import VideoInfo  # noqa: E402
from vd.summarize import (  # noqa: E402
    API_MAX_TOKENS,
    CLIENT_MAX_TOKENS,
    MIN_SUMMARY_CHARS,
    OUT_RATIO,
    TOK_PER_CHAR,
    DeepSeekClient,
    KeyPoint,
    Strategy,
    SummarizeError,
    Summary,
    TimelinePoint,
    Usage,
    _ascii_arrow,
    _check_summary,
    _salvage_truncated_json,
    _split_sentences,
    chunk_segments,
    estimate_cost,
    is_peak_now,
    match_highlights_to_points,
    normalize_highlights,
    output_budget,
    parse_ts,
    render_for_prompt,
    to_markdown,
    validate_points,
)
from vd.transcript import Segment, Transcript, TranscriptSource  # noqa: E402


def _segs(*pairs) -> list[Segment]:
    return [Segment(start=float(a), end=float(b), text=t) for a, b, t in pairs]


SEGS = _segs((0, 5, "大家好"), (5, 12, "今天讲的是暴击期望"), (12, 20, "左边期望一点八"))


# ---------------- 时间戳解析 ----------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("00:00:00", 0), ("00:01:23", 83), ("01:00:00", 3600),
        ("12:30", 750), ("[00:05:10]", 310), ("  00:00:07 ", 7),
    ],
)
def test_parse_ts(label, expected):
    assert parse_ts(label) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "开头", "--:--:--"])
def test_parse_ts_rejects_garbage(bad):
    assert parse_ts(bad) is None


# ---------------- 锚点校验（核心） ----------------


def test_valid_anchor_is_kept():
    raw = [{"text": "要点", "anchors": ["00:00:05"], "category": "结论"}]
    pts, dropped = validate_points(raw, SEGS)
    assert pts[0].anchors == [5.0]
    assert dropped == 0


def test_rounding_within_tolerance_is_kept():
    """模型常把时间戳取整，2 秒内应接受。"""
    raw = [{"text": "要点", "anchors": ["00:00:06"], "category": "结论"}]
    pts, dropped = validate_points(raw, SEGS)
    assert pts[0].anchors == [5.0]
    assert dropped == 0


def test_fabricated_anchor_is_dropped():
    """关键断言：模型编造的时间戳必须被丢掉，不能进产物。"""
    raw = [{"text": "要点", "anchors": ["00:42:00"], "category": "结论"}]
    pts, dropped = validate_points(raw, SEGS)
    assert pts[0].anchors == []
    assert dropped == 1


def test_mixed_anchors_keeps_only_valid():
    raw = [{"text": "要点", "anchors": ["00:00:05", "99:00:00", "00:00:12"]}]
    pts, dropped = validate_points(raw, SEGS)
    assert pts[0].anchors == [5.0, 12.0]
    assert dropped == 1


def test_point_without_text_is_skipped():
    raw = [{"text": "  ", "anchors": ["00:00:05"]}, {"text": "有用", "anchors": []}]
    pts, _ = validate_points(raw, SEGS)
    assert len(pts) == 1
    assert pts[0].text == "有用"


def test_anchor_labels_are_formatted():
    p = KeyPoint(text="x", anchors=[83.0])
    assert p.anchor_labels() == ["00:01:23"]


# ---------------- 分片 ----------------


def test_chunking_never_splits_a_segment():
    segs = _segs(*[(i, i + 1, "字" * 10) for i in range(20)])
    chunks = chunk_segments(segs, max_chars=25)
    assert sum(len(c) for c in chunks) == len(segs)
    for c in chunks:
        assert c  # 不产生空片
    # 每片都不超上限（单片恰好一个超长 segment 除外）
    for c in chunks:
        assert sum(len(s.text) for s in c) <= 35


def test_chunking_single_chunk_when_small():
    assert len(chunk_segments(SEGS, max_chars=1000)) == 1


def test_render_for_prompt_includes_timestamps():
    out = render_for_prompt(SEGS)
    assert out.startswith("[00:00:00] 大家好")
    assert "[00:00:05]" in out
    assert render_for_prompt(SEGS, with_timestamps=False).startswith("大家好")


# ---------------- 空产物校验 ----------------


def test_check_summary_rejects_short_overall():
    with pytest.raises(SummarizeError):
        _check_summary("太短", [KeyPoint("a"), KeyPoint("b"), KeyPoint("c")], "t")


def test_check_summary_rejects_too_few_points():
    with pytest.raises(SummarizeError):
        _check_summary("字" * MIN_SUMMARY_CHARS, [KeyPoint("a")], "t")


def test_check_summary_accepts_valid():
    _check_summary("字" * MIN_SUMMARY_CHARS, [KeyPoint(f"p{i}") for i in range(3)], "t")


# ---------------- 成本估算 ----------------


def test_cost_uses_off_peak_price():
    u = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    # flash 空闲：输入 1 元/百万，输出 4 元/百万
    assert estimate_cost("deepseek-flash", u, peak=False) == pytest.approx(5.0)


def test_cost_is_double_at_peak():
    u = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    off = estimate_cost("deepseek-flash", u, peak=False)
    peak = estimate_cost("deepseek-flash", u, peak=True)
    assert peak == pytest.approx(off * 2)


def test_cache_hit_is_much_cheaper():
    full = Usage(prompt_tokens=1_000_000, completion_tokens=0)
    cached = Usage(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=1_000_000)
    assert estimate_cost("deepseek-flash", cached, peak=False) < estimate_cost(
        "deepseek-flash", full, peak=False
    ) / 10


def test_pro_costs_more_than_flash():
    u = Usage(prompt_tokens=100_000, completion_tokens=10_000)
    assert estimate_cost("deepseek-v4-pro", u, peak=False) > estimate_cost(
        "deepseek-flash", u, peak=False
    )


def test_unknown_model_costs_zero():
    assert estimate_cost("nope", Usage(prompt_tokens=1000), peak=False) == 0.0


@pytest.mark.parametrize(
    "when,expected",
    [
        (dt.datetime(2026, 10, 12, 10, 0), True),    # 周一 10:00 高峰
        (dt.datetime(2026, 10, 12, 13, 0), False),   # 周一 13:00 空闲
        (dt.datetime(2026, 10, 12, 15, 0), True),    # 周一 15:00 高峰
        (dt.datetime(2026, 10, 12, 20, 0), False),   # 周一 20:00 空闲
        (dt.datetime(2026, 10, 10, 10, 0), False),   # 周六全天空闲
        (dt.datetime(2026, 10, 11, 15, 0), False),   # 周日全天空闲
    ],
)
def test_peak_detection(when, expected):
    assert is_peak_now(when) is expected


# ---------------- 端到端（用假 client，不出网） ----------------


class FakeClient:
    """按 prompt 内容返回预设 JSON，模拟 DeepSeek。

    刻意做成"按 prompt 类型响应"而不是"按调用顺序弹列表"：
    后者要求测试预先算准分片数，分片逻辑一改测试就假失败，
    测的其实是分片实现细节而不是被测行为。
    """

    model = "deepseek-flash"

    def __init__(self, responses=None, empty_first: bool = False):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []
        self.empty_first = empty_first

    def json_call(self, system, user, *, max_tokens=4096, retries=3):
        self.calls.append((system, user))
        if self.empty_first and len(self.calls) == 1:
            # 模拟官方文档提到的"有概率返回空 content"
            raise SummarizeError("空 content")
        if self.responses:
            return self.responses.pop(0), Usage(prompt_tokens=100, completion_tokens=50, calls=1)
        if "合并" in system or "分段要点" in user:
            return (
                {"overall": "合并后的整体总结内容，覆盖全部片段。" * 4,
                 "key_points": [
                     {"text": f"合并要点{i}", "anchors": ["00:00:05"], "category": "结论"}
                     for i in range(4)
                 ]},
                Usage(prompt_tokens=200, completion_tokens=80, calls=1),
            )
        if "片段" in system:
            return (
                {"points": [
                    {"text": f"片段要点{i}", "anchors": ["00:00:05"], "category": "论据"}
                    for i in range(5)
                ]},
                Usage(prompt_tokens=150, completion_tokens=60, calls=1),
            )
        return (
            {"overall": "单次总结的整体内容描述，说明讲了什么以及结论。" * 4,
             "key_points": [
                 {"text": f"要点{i}", "anchors": ["00:00:05"], "category": "结论"}
                 for i in range(3)
             ]},
            Usage(prompt_tokens=120, completion_tokens=40, calls=1),
        )


def _transcript(n=3) -> Transcript:
    segs = _segs(*[(i * 5, i * 5 + 5, f"第{i}句内容够长一些") for i in range(n)])
    info = VideoInfo(bvid="BVT", cid=1, title="测试视频", duration=n * 5, owner_name="u")
    return Transcript(
        bvid="BVT", title="测试视频", source=TranscriptSource.SUBTITLE,
        segments=segs, info=info,
    )


def test_single_pass_produces_summary():
    client = FakeClient()
    s = __import__("vd.summarize", fromlist=["summarize"]).summarize(
        _transcript(), client=client, strategy="single"
    )
    assert s.strategy is Strategy.SINGLE
    assert len(s.key_points) == 3
    assert s.key_points[0].anchors == [5.0]
    assert s.usage.calls == 1


def test_auto_picks_map_reduce_for_long_transcript():
    """超过 single_pass_chars 时必须走分片。

    注意要同时压 `single_pass_chars` 和 `chunk_chars`：
    只压后者没用 —— 全文仍小于 single_pass_chars 时会走单次，测试就假通过了。
    """
    tr = _transcript(n=60)
    total = sum(len(s.text) for s in tr.segments)
    n_chunks = len(chunk_segments(tr.segments, max_chars=50))
    assert n_chunks > 1, "测试数据没触发分片，测试本身无效"
    client = FakeClient()
    mod = __import__("vd.summarize", fromlist=["summarize"])
    s = mod.summarize(tr, client=client, chunk_chars=50, single_pass_chars=total - 1)
    assert s.strategy is Strategy.MAP_REDUCE
    assert len(s.key_points) == 4
    assert s.usage.calls == n_chunks + 1, "应为 分片数 + 1 次合并调用"


def test_auto_picks_single_for_short_transcript():
    """短逐字稿不该白白走分片 —— 那会多花一倍的钱。"""
    tr = _transcript(n=3)
    client = FakeClient()
    mod = __import__("vd.summarize", fromlist=["summarize"])
    s = mod.summarize(tr, client=client, strategy="auto")
    assert s.strategy is Strategy.SINGLE
    assert s.usage.calls == 1


def test_empty_content_is_retried_not_accepted():
    """官方文档明说 JSON 模式有概率返回空 content；上层已重试，这里验证失败会抛出。"""
    client = FakeClient([], empty_first=True)
    mod = __import__("vd.summarize", fromlist=["summarize"])
    with pytest.raises(SummarizeError):
        mod.summarize(_transcript(), client=client, strategy="single")


def test_summarize_rejects_empty_transcript():
    tr = _transcript()
    tr.segments = []
    mod = __import__("vd.summarize", fromlist=["summarize"])
    with pytest.raises(SummarizeError) as exc:
        mod.summarize(tr, client=FakeClient([]))
    assert "逐字稿为空" in str(exc.value)


def test_markdown_marks_points_without_anchor():
    s = Summary(
        bvid="BVT", title="t", overall="总结" * 20,
        key_points=[KeyPoint("有出处", [83.0]), KeyPoint("没出处", [])],
        strategy=Strategy.SINGLE, model="deepseek-flash",
        usage=Usage(), elapsed=1.0,
    )
    md = to_markdown(s)
    assert "[00:01:23]" in md
    assert "无可靠出处" in md
    assert "DISCLAIMER" not in md  # 只检查没写错变量
    assert "请勿转载" in md


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(SummarizeError) as exc:
        DeepSeekClient(api_key="")
    assert "DEEPSEEK_API_KEY" in str(exc.value)


# ======================================================= 关键时间轴


def _info(duration=1749):
    return VideoInfo(bvid="BVT", cid=1, title="t", duration=duration, owner_name="u")


def test_highlights_are_normalized_and_sorted():
    raw = [
        {"at": "00:13:31", "text": "HGC 突然取消"},
        {"at": "00:01:41", "text": "错失 DOTA"},
        {"at": "00:05:16", "text": "推翻传统 MOBA 规则"},
    ]
    out, dropped = normalize_highlights(raw, _info())
    assert [h.label for h in out] == ["00:01:41", "00:05:16", "00:13:31"]
    assert dropped == 0


def test_highlights_drop_fabricated_timestamps():
    """模型编造的时间戳必须丢掉 —— 与要点锚点同一原则。"""
    raw = [
        {"at": "00:01:41", "text": "真实"},
        {"at": "99:00:00", "text": "编造的超范围时间"},
        {"at": "开头", "text": "模糊位置"},
        {"at": "00:02:00", "text": ""},          # 空文本
    ]
    out, dropped = normalize_highlights(raw, _info(duration=1749))
    assert [h.text for h in out] == ["真实"]
    assert dropped == 3


def test_highlights_tolerate_small_overshoot():
    """只超出视频末尾几秒不算编造（B 站时长本身有误差）。"""
    out, dropped = normalize_highlights(
        [{"at": "00:29:20", "text": "结尾"}], _info(duration=1749)
    )
    assert len(out) == 1 and dropped == 0


def test_highlights_cap_count():
    raw = [{"at": f"00:{i:02d}:00", "text": f"节点{i}"} for i in range(20)]
    out, dropped = normalize_highlights(raw, _info(duration=3600))
    assert len(out) == 12
    assert dropped == 8


def test_highlights_truncate_long_text():
    long_text = "很长" * 40
    out, _ = normalize_highlights([{"at": "00:01:00", "text": long_text}], _info())
    assert len(out[0].text) <= 41
    assert out[0].text.endswith("…")


def test_highlights_accept_alternate_field_name():
    """模型偶尔把 at 写成 time，不该因此丢掉整条。"""
    out, dropped = normalize_highlights([{"time": "00:01:00", "text": "x"}], _info())
    assert len(out) == 1 and dropped == 0


def test_highlights_empty_input():
    out, dropped = normalize_highlights([], _info())
    assert out == [] and dropped == 0


# ======================================================= 排版：按句换行


def test_split_sentences_never_changes_text():
    """**核心约束**：只加换行，不改一个字。

    排版函数一旦改动文字，就等于在产物里悄悄篡改了逐字稿/总结内容。
    """
    text = (
        "本视频回顾了过程。视频指出，暴雪错失机会，冰蛙被招走；"
        "随后推翻传统MOBA规则。2022年不再加入新付费内容，游戏进入停滞。"
        "结论是：只有持续承担未来才算复活。"
    )
    out = _split_sentences(text)
    assert "".join(out) == text.strip()


def test_split_sentences_breaks_single_long_paragraph():
    """295 字符的单段应变成多行 —— 这是用户反馈的核心诉求。"""
    text = (
        "本视频回顾了《风暴英雄》从诞生到被暴雪放弃、再到2026年突然推出新英雄萨拉塔斯的全过程。"
        "视频指出，暴雪当年因开发资源不足错失了接手DOTA的机会，冰蛙被Valve招走，"
        "英雄联盟与DOTA2抢先占领市场；暴雪随后以推翻传统MOBA规则的方式做出风暴英雄，"
        "共享经验、多地图和怪英雄让它极具创意，却因团队判断难以统一、个人成就感不足、"
        "快速匹配矛盾以及电竞HGC突然取消等问题始终未能做大体量。"
        "2022年暴雪明确不再加入新付费内容，游戏进入六年停滞但玩家社区未散。"
        "2026年萨拉塔斯上线被视为一次成本可控的试探，而非真正的复活。"
    )
    out = _split_sentences(text)
    assert len(out) >= 5, f"只切成 {len(out)} 行，扫读性没改善"
    assert max(len(x) for x in out) <= 90, "仍有超长行"
    assert "".join(out) == text.strip()


def test_split_sentences_handles_unpunctuated_text():
    """口语转录常见"逗号连缀、没有句号"的长文本，也要能切。"""
    text = ("这段文字没有句末标点所以只能靠逗号切开才行，" * 5).rstrip("，") + "收尾"
    out = _split_sentences(text)
    assert len(out) > 1, "没有句号就完全不切，等于没处理"
    assert "".join(out) == text.strip()


def test_split_sentences_edge_cases():
    assert _split_sentences("") == []
    assert _split_sentences("   ") == []
    assert _split_sentences("只有一句话。") == ["只有一句话。"]
    assert _split_sentences("没有标点的短句") == ["没有标点的短句"]


# ======================================================= 新产物结构


def _summary(**kw):
    from vd.summarize import Summary as S

    base = dict(
        bvid="BVT", title="测试标题", overall="总结内容。" * 12,
        key_points=[KeyPoint(f"要点{i}", [float(i * 60)]) for i in range(3)],
        strategy=Strategy.SINGLE, model="deepseek-flash",
        usage=Usage(prompt_tokens=1000, completion_tokens=500),
        elapsed=1.0, tl_dr="一句话速览。",
        # point_index 必须显式给：默认 -1 表示"未关联"，
        # 测试要覆盖的是**已关联**的正常路径
        highlights=[TimelinePoint(70.0, "第二个节点", 0), TimelinePoint(10.0, "第一个节点", 0)],
        video_duration=1749, transcript_chars=12000,
    )
    base.update(kw)
    return S(**base)


def test_markdown_has_new_sections():
    md = to_markdown(_summary())
    assert "## 速览" in md
    assert "## 总结" in md
    assert "## 关键时间轴" in md
    assert "## 要点" in md


def test_markdown_timeline_has_jump_links():
    """时间轴图例要有跳转到下方要点的链接（跳视频不在这里）。"""
    md = to_markdown(_summary())
    timeline = md.split("## 关键时间轴", 1)[1].split("## 要点", 1)[0]
    assert "跳到要点" in timeline
    assert "第一个节点" in timeline


def test_markdown_header_is_compact():
    """头部不能比要点还占地方（改版前占 17.6%）。"""
    md = to_markdown(_summary())
    head = md.split("## 速览")[0]
    # 标题 + 声明 3 行 + 元信息 1 行 + 空行
    assert head.count("\n> ") <= 4, f"头部元信息仍是多行：\n{head}"
    assert "1749" not in head or "29 分钟" in head   # 时长以可读形式出现


def test_markdown_summary_is_paragraph_split():
    """总结正文不再是一整块。"""
    md = to_markdown(_summary(overall="第一句。" * 20))
    body = md.split("## 总结", 1)[1].split("##", 1)[0]
    assert body.count("\n\n") >= 2, "总结仍是一整段"


def test_markdown_kind_is_summary_not_transcript():
    """**回归**：总结文件的声明不能说自己是"逐字稿"。

    这是实测产物里的真实错误：P2 直接复用了逐字稿的声明模板。
    给使用者看的法律文本写错自己是什么，比排版问题严重。
    """
    md = to_markdown(_summary())
    assert "本总结由个人自用工具生成" in md
    assert "本逐字稿由个人自用工具生成" not in md


def test_markdown_shows_video_duration():
    assert "29 分钟" in to_markdown(_summary())


def test_markdown_without_highlights_omits_section():
    md = to_markdown(_summary(highlights=[]))
    assert "## 关键时间轴" not in md
    assert "## 要点" in md


def test_markdown_without_tldr_omits_section():
    md = to_markdown(_summary(tl_dr=""))
    assert "## 速览" not in md


# ======================================================= 时间轴 ↔ 要点 映射


def _pts():
    return [
        KeyPoint("暴雪错过 DOTA，冰蛙被 Valve 招走", [101.0, 131.0], "事实", "错失 DOTA"),
        KeyPoint("推翻传统 MOBA 规则", [317.0, 362.0], "方法", "设计取舍"),
        KeyPoint("HGC 突然取消", [811.0, 815.0], "事实", "电竞崩盘"),
    ]


def test_match_highlights_to_nearest_point():
    hs = [
        TimelinePoint(101.0, "暴雪讨论 DOTA"),
        TimelinePoint(131.0, "Valve 招走冰蛙"),
        TimelinePoint(811.0, "HGC 取消"),
    ]
    match_highlights_to_points(hs, _pts())
    assert [h.point_index for h in hs] == [0, 0, 2]


def test_match_highlights_never_leaves_node_without_target():
    """**兜底**：只要要点里有任何锚点，节点就必须被关联到某一条。

    实测踩过：有个节点落在两条要点的锚点之间，前两种判定都不命中，
    于是显示"未关联要点" —— 用户点击没反应，**看起来像坏了**。
    "跳到时间上最近那一段"虽然不精确，但远比"点了不动"好。
    """
    hs = [TimelinePoint(1600.0, "很久以后的结尾")]
    match_highlights_to_points(hs, _pts())
    assert hs[0].point_index >= 0, "仍然允许出现'点了没反应'的节点"


def test_match_highlights_without_any_anchors_stays_unmatched():
    """要点里一个锚点都没有时无法兜底，只能标未关联（此时也没得可跳）。"""
    pts = [KeyPoint("无锚点 A", [], "事实"), KeyPoint("无锚点 B", [], "事实")]
    hs = [TimelinePoint(100.0, "x")]
    match_highlights_to_points(hs, pts)
    assert hs[0].point_index == -1


def test_ascii_arrow_nodes_sit_on_the_axis():
    """节点标记必须**落在轴线上**，而不是悬在下方。

    第一版把标记放在轴线下一行，看起来像散落的字，不成"轴上有节点"。
    """
    hs = [TimelinePoint(float(i * 60), f"节点{i}") for i in range(5)]
    art = _ascii_arrow(hs)
    assert "\n" not in art, "箭头图应只有一行"
    assert art.startswith("├") and art.endswith("▶")
    for mark in "❶❷❸❹❺":
        assert mark in art
    assert art.count("─") > 20


def test_match_highlights_uses_range_fallback():
    """锚点都不够近时，退一步用「要点的时间跨度覆盖它」来判断。"""
    pts = [KeyPoint("一段横跨很久的叙述", [100.0, 900.0], "事实", "长段")]
    hs = [TimelinePoint(500.0, "中间某处")]
    match_highlights_to_points(hs, pts)
    assert hs[0].point_index == 0


def test_match_highlights_ignores_point_without_anchors():
    pts = [KeyPoint("没有锚点的要点", [], "事实", "无出处")]
    hs = [TimelinePoint(100.0, "x")]
    match_highlights_to_points(hs, pts)
    assert hs[0].point_index == -1


def test_multiple_highlights_can_share_one_point():
    """一个话题有多个里程碑是正常的，它们都滚到同一段。"""
    hs = [TimelinePoint(101.0, "A"), TimelinePoint(131.0, "B")]
    match_highlights_to_points(hs, _pts())
    assert hs[0].point_index == hs[1].point_index == 0


# ======================================================= 箭头图 / 新要点结构


def test_ascii_arrow_has_axis_and_nodes():
    hs = [TimelinePoint(float(i * 60), f"节点{i}") for i in range(5)]
    art = _ascii_arrow(hs)
    assert art.startswith("├") and art.endswith("▶")
    for mark in "❶❷❸❹❺":
        assert mark in art, f"缺少节点标记 {mark}"


def test_ascii_arrow_single_node():
    art = _ascii_arrow([TimelinePoint(0.0, "唯一节点")])
    assert "❶" in art and art.endswith("▶")


def test_keypoint_topic_fallback():
    """模型没给主题时用首句截断，不能让小标题空着。"""
    p = KeyPoint("这是一个没有主题的要点。第二句在这里。", [1.0])
    assert p.display_topic.startswith("这是一个没有主题的要点")
    p2 = KeyPoint("主题" * 20 + "。", [1.0], "事实", "我给的主题")
    assert p2.display_topic == "我给的主题"


def test_validate_points_reads_new_fields():
    """point / insight 两个新字段要被读出来，并兼容旧字段名。"""
    from vd.transcript import Segment

    segs = [Segment(100.0, 110.0, "内容")]
    raw = [{
        "point": "错失 DOTA", "text": "详细陈述内容够长一些才行。",
        "anchors": ["00:01:40"], "category": "事实", "insight": "这里体现了决策迟缓",
    }]
    pts, dropped = validate_points(raw, segs)
    assert pts[0].topic == "错失 DOTA"
    assert pts[0].insight == "这里体现了决策迟缓"
    assert dropped == 0

    # 旧字段名也要认
    raw2 = [{"topic": "别名主题", "note": "别名补充", "text": "内容够长了这是陈述部分。",
             "anchors": ["00:01:40"]}]
    pts2, _ = validate_points(raw2, segs)
    assert pts2[0].topic == "别名主题"
    assert pts2[0].insight == "别名补充"


def test_markdown_points_are_sections_not_list_items():
    """要点必须是**分段陈述**（每条一个 ### 小节），而不是一句话列表项。

    这是用户对内容定位的反馈：「最后的要点章节不能漏掉任何一条有价值的信息，
    不是说要总结了，而是分段陈述/精炼，要全面要深刻」。
    """
    md = to_markdown(_summary(
        key_points=[
            KeyPoint("完整陈述内容，可以多句。第二句在这里。", [60.0], "事实",
                     "错失 DOTA", "补充：体现了决策迟缓"),
            KeyPoint("第二条陈述。", [120.0], "论据", "设计取舍", ""),
        ]
    ))
    assert "### 1. " in md and "### 2. " in md
    assert "错失 DOTA" in md
    assert "补充理解" in md and "体现了决策迟缓" in md
    # 不应再有旧的列表项格式：`1. `事实` …　— [00:00:60]`
    # 注意 `### 1. `事实`` 是新格式的标题，不能误伤，所以断言"每个编号项都带 ###"
    numbered = re.findall(r"^(\d+)\.\s", md, re.M)
    headings = re.findall(r"^### (\d+)\.\s", md, re.M)
    assert numbered == headings, f"存在不是小节标题的编号项：{set(numbered) - set(headings)}"


def test_markdown_point_anchors_link_to_video_with_timestamp():
    """跳视频的动作落在**要点段落里的时间戳**上（用户要求的位置）。"""
    md = to_markdown(_summary(
        key_points=[KeyPoint("陈述。", [83.0, 131.0], "事实", "主题", "")]
    ))
    assert "https://www.bilibili.com/video/BVT?t=83" in md
    assert "https://www.bilibili.com/video/BVT?t=131" in md
    assert "出处" in md


def test_markdown_timeline_legend_links_down_to_points():
    """时间轴图例要能跳到下方要点段落，**且不带跳视频**。

    按用户要求，跳原视频的动作统一放在要点段落里。
    第一版这里还带着"▶ 跳视频"，与 Web 端不一致 ——
    同一份产物在网页和文件里行为不同，是"两套排版各写一遍"的漂移。
    """
    md = to_markdown(_summary(
        key_points=[KeyPoint("陈述。", [70.0], "事实", "主题", "")],
        highlights=[TimelinePoint(70.0, "节点", 0)],
    ))
    timeline = md.split("## 关键时间轴", 1)[1].split("## 要点", 1)[0]
    assert "↓ 跳到要点 1" in timeline
    assert "#1-" in timeline
    assert "bilibili.com" not in timeline, "时间轴里不该再出现跳视频链接"


def test_insight_prefix_is_not_duplicated():
    """模型自带的「补充：」前缀要剥掉，否则显示成 `补充理解：补充：…`。

    实测 12 条全部重复 —— prompt 建议模型用「补充：」开头，
    而渲染模板又加了一遍前缀。
    """
    from vd.summarize import _clean_insight

    assert _clean_insight("补充：这场商标战让项目失去品牌支撑") == "这场商标战让项目失去品牌支撑"
    assert _clean_insight("补充:半角冒号也要处理") == "半角冒号也要处理"
    assert _clean_insight("没有前缀就原样保留") == "没有前缀就原样保留"
    assert _clean_insight("") == ""

    md = to_markdown(_summary(
        key_points=[KeyPoint("陈述。", [60.0], "事实", "主题", "补充：重复前缀测试")]
    ))
    assert "补充理解**：补充：" not in md
    assert "补充理解**：重复前缀测试" in md


def test_insight_is_trimmed_to_one_sentence():
    """`insight` 必须真是一句话（用户明确要求「改成一句话总结」）。

    只靠 prompt 约束不够 —— 模型很容易又展开成两三句，所以有机械保证。
    """
    from vd.summarize import MAX_INSIGHT_CHARS, _trim_insight

    assert _trim_insight("短句") == "短句"
    assert _trim_insight("补充：去掉前缀") == "去掉前缀"
    # 多句 → 取第一句
    assert _trim_insight("第一句话。第二句多余。") == "第一句话"
    # 超长单句 → 截断，且**长度不得超过上限**（省略号也算进预算）
    long_one = "这是一句" + "非常" * 30 + "长的说明"
    out = _trim_insight(long_one)
    assert len(out) <= MAX_INSIGHT_CHARS, f"截断后仍超长：{len(out)}"
    assert out.endswith("…")
    # 有逗号时优先在逗号处断，读起来更自然
    with_comma = "前半句有一个逗号，" + "后面还有很长很长的内容" * 3
    assert _trim_insight(with_comma).endswith("…")


def test_validate_points_trims_insight():
    """校验环节就要压成一句话，不能等到渲染。"""
    from vd.transcript import Segment

    segs = [Segment(100.0, 110.0, "内容")]
    raw = [{
        "text": "陈述内容够长了这是主体的部分。", "anchors": ["00:01:40"],
        "insight": "第一句话含义。第二句是多余的展开。",
    }]
    pts, _ = validate_points(raw, segs)
    assert pts[0].insight == "第一句话含义"


def test_markdown_timeline_shows_arrow_block():
    md = to_markdown(_summary(
        key_points=[KeyPoint("陈述。", [70.0], "事实", "主题", "")],
        highlights=[TimelinePoint(float(i * 60), f"节点{i}", 0) for i in range(3)],
    ))
    assert "```" in md
    assert "▶" in md
    assert "❶" in md


# ======================================================= max_tokens 预算与截断抢救


def test_measured_token_ratio_is_documented():
    """比例常数要基于实测，不是拍脑袋。

    `scripts/measure_ratio.py` 实测：中文输入侧 0.548~0.556、输出侧 0.560，
    所以 `TOK_PER_CHAR=0.55` 是对的；而产出比原文更"结构化"（要点+主题+
    一句话含义+时间轴+JSON 骨架），规划系数取 1.0 再用 headroom 吸收波动。
    """
    assert 0.5 <= TOK_PER_CHAR <= 0.6
    assert OUT_RATIO >= 0.743, "规划系数不该低于实测输出需求 0.743"


def test_headroom_covers_the_worst_observed_case():
    """**回归**：余量必须覆盖实测的最坏样本。

    实测两例的输出/输入比例：
      · BV1dFem67EYt  9074 字 → 6743 tok   比例 0.743
      · BV1Q3pe6GERF 12692 字 → 21490 tok  比例 **1.693**（信息密集的财经分析）

    同一份输入规模，输出量能差 2.3 倍 —— 所以余量要按**最坏**取。
    第一版 headroom 取 1.5，对该样本**不够**（预算 20,062 < 实际 21,490），
    那次能过是靠重试兜住的，不是靠预算。这就是这条测试存在的原因。
    """
    worst_ratio = 21490 / 12692          # ≈1.693
    budget = output_budget(12692)
    assert budget >= 21490, (
        f"预算 {budget} 覆盖不了实测最坏样本 21490；"
        f"当前有效余量 {budget / 12692:.2f}，需要 ≥{worst_ratio:.2f}"
    )


def test_cap_kicks_in_before_chunking_threshold():
    """封顶必须**早于**分片阈值。

    否则会出现"单次通道预算不够、又还没到分片阈值"的必失败区间 ——
    这种区间在测试里很难发现，但在线上就是"某些长度的视频必然失败"。
    """
    from vd.summarize import CLIENT_MAX_TOKENS, DEFAULT_HEADROOM

    linear_end = (CLIENT_MAX_TOKENS - 1024) / (OUT_RATIO * DEFAULT_HEADROOM)
    assert linear_end < 60_000, (
        f"线性段到 {linear_end:,.0f} 字才封顶，晚于 60,000 字的分片阈值"
    )
    # 封顶点附近两边都要有定义
    assert output_budget(int(linear_end) - 1000) < CLIENT_MAX_TOKENS
    assert output_budget(int(linear_end) + 1000) == CLIENT_MAX_TOKENS


def test_output_budget_scales_with_input():
    """**回归**：预算必须随输入增长，不能写死。

    写死 8192 的后果：输入越长越容易撞上限，而**重试多少次都一样**
    （上限没变），最终整条任务失败。实测就撞过一次 ——
    输出到 14537 字符（≈8000 token）被截断，重试 3 次全失败。
    """
    small = output_budget(3_000)
    mid = output_budget(9_074)
    big = output_budget(40_000)
    assert small < mid < big, "预算没有随输入增长"

    # 21.6 分钟视频（9074 字）实测需要 6743 输出 token，预算必须有余量
    assert mid > 6743 * 1.5
    # 那次失败的任务：约 19565 字逐字稿，旧上限 8192 必然截断
    assert output_budget(19_565) > 14_537 * TOK_PER_CHAR


def test_output_budget_respects_limits():
    """预算要落在服务端合法区间内，并且有自己的封顶。"""
    assert output_budget(1) >= 4096, "太小会让模型没空间写"
    assert output_budget(10**7) == CLIENT_MAX_TOKENS, "必须有封顶"
    assert CLIENT_MAX_TOKENS <= API_MAX_TOKENS, "客户端封顶不应超过服务端上限"
    # 封顶只影响 max_tokens（上限），不影响实际计费 —— 所以可以给得宽裕
    assert output_budget(10**7) < API_MAX_TOKENS, "没必要贴着服务端上限要"


def test_repair_unescaped_quotes_fixes_real_failure():
    """**回归**：修复"字符串值里有未转义半角引号"导致的 JSON 语法错。

    实测两次真实失败都是这个形态，服务端错误原文：

        Expecting ',' delimiter: line 2 column 18 (char 19)
        开头：{"tl_dr": "拆解券商"阿里云利润300亿增速50%"的说法，...

    模型**转述别人的话**时直接写了半角引号，没转义。这跟 max_tokens 无关 ——
    它已经写完整了（末尾是完整的 `}`），纯粹是转义错。
    **加预算修不了，必须做转义修复。**
    """
    from vd.summarize import _repair_unescaped_quotes

    bad = (
        '{\n  "tl_dr": "拆解券商"阿里云利润300亿增速50%"的说法，讲清业务本质。",\n'
        '  "overall": "视频从一条券商预测切入。"\n}'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)

    fixed = _repair_unescaped_quotes(bad)
    assert fixed is not None, "应当能修"
    data = json.loads(fixed)
    assert data["tl_dr"] == '拆解券商"阿里云利润300亿增速50%"的说法，讲清业务本质。'
    assert data["overall"] == "视频从一条券商预测切入。"


def test_repair_unescaped_quotes_is_conservative():
    """合法 JSON **不该被改动** —— 修复只应在确实有裸引号时动手。

    这条很重要：一个"总想改点什么"的修复函数会把正确数据搞坏。
    """
    from vd.summarize import _repair_unescaped_quotes

    for good in (
        '{"a": "值", "b": [1,2]}',
        '{"a": "他说: 你好"}',                 # 内容含冒号
        '{"a": "他说\\"你好\\""}',             # 已正确转义
        '{"a": ""}',                          # 空串后紧跟新键
        '{"a": {"b": "x"}, "c": "y"}',        # 嵌套
        '{"list": ["甲", "乙", "丙"]}',
        '{"n": 1.5, "b": true, "z": null}',
    ):
        assert json.loads(good) is not None, f"夹具本身非法：{good}"
        assert _repair_unescaped_quotes(good) is None, f"合法 JSON 被改动了：{good}"


def test_repair_unescaped_quotes_handles_control_chars():
    """字符串里的裸换行/制表符同样非法，要一并转义。"""
    from vd.summarize import _repair_unescaped_quotes

    bad = '{"a": "第一行\n第二行\t带制表符"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    fixed = _repair_unescaped_quotes(bad)
    assert fixed is not None
    assert json.loads(fixed)["a"] == "第一行\n第二行\t带制表符"


def test_sanitize_no_proxy_removes_bracketed_ipv6(monkeypatch):
    """**回归**：`no_proxy` 里带方括号的 IPv6 会让 httpx 崩在构造阶段。

    实测：`no_proxy='localhost,...,[::1]'` → `InvalidURL: Invalid port: ':1]'`，
    **DeepSeekClient() 构造都失败**，而且报错栈指向 httpx 内部，
    完全看不出跟 no_proxy 有关。单片段隔离（独立子进程）验证过：
    只去掉 `[::1]` 即恢复。

    注意裸 `::1` 是好用的（本机走代理时还需要它），所以**只**去方括号形式。
    """
    from vd.summarize import sanitize_no_proxy

    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,.example.com,[::1]")
    assert sanitize_no_proxy() == ["[::1]"]
    assert os.environ["no_proxy"] == "localhost,127.0.0.1,::1,.example.com"
    assert "::1" in os.environ["no_proxy"], "裸 ::1 必须保留"
    # 大小写两边都要同步，否则不同库读到不同值
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"]

    # 已经干净时不该动它，也不该谎报"移除了什么"
    monkeypatch.setenv("no_proxy", "localhost,::1")
    assert sanitize_no_proxy() == []
    assert os.environ["no_proxy"] == "localhost,::1"

    # 全是方括号条目时清空变量，而不是留下空串
    monkeypatch.setenv("no_proxy", "[::1]")
    assert sanitize_no_proxy() == ["[::1]"]
    assert "no_proxy" not in os.environ


def test_salvage_keeps_complete_points_only():
    """截断抢救：保留**完整**的要点，丢掉半截的。

    宁可不救也不能产出错数据 —— 半句话的要点写进产物比缺一条更糟。
    """
    full = {
        "tl_dr": "速览", "overall": "总结",
        "key_points": [
            {"point": "主题一", "text": "陈述一", "anchors": ["00:01:00"]},
            {"point": "主题二", "text": "陈述二", "anchors": ["00:05:00"]},
            {"point": "主题三", "text": "半句话被截", "anchors": ["00:09:00"]},
        ],
    }
    raw = json.dumps(full, ensure_ascii=False)

    # 完好时原样解析
    assert len(_salvage_truncated_json(raw)["key_points"]) == 3

    # 数组中途截断 → 丢掉半截的第三条
    cut = raw[: raw.rindex('{"point": "主题三"')]
    got = _salvage_truncated_json(cut)
    assert got is not None
    assert [p["point"] for p in got["key_points"]] == ["主题一", "主题二"]

    # 字符串中途截断 → 修复未闭合引号后仍能救出
    cut2 = raw[: raw.index("半句话被截") + 3]
    got2 = _salvage_truncated_json(cut2)
    assert got2 is not None and len(got2["key_points"]) == 3

    # 救不出来就返回 None，绝不返回半个对象
    assert _salvage_truncated_json("") is None
    assert _salvage_truncated_json("not json at all") is None
    assert _salvage_truncated_json("[1,2,3]") is None, "顶层不是对象应拒绝"


def test_usage_truncated_flag_propagates():
    """截断标记要能合并传递，最终写进产物说明。"""
    a, b = Usage(), Usage()
    b.truncated = True
    a.add(b)
    assert a.truncated is True
    a.add(Usage())
    assert a.truncated is True, "合并后不该被覆盖掉"


def test_markdown_warns_when_truncated():
    """**产物必须如实说明自己不完整** —— 它会被当作可信记录反复引用。"""
    md = to_markdown(_summary(notes=["⚠️ 本次响应撞到了输出上限，**产物可能不完整**（已保留完整部分）。"]))
    assert "产物可能不完整" in md
