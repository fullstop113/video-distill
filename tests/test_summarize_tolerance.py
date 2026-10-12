"""模型返回"意外形态"时的容错测试。

## 为什么单独一个文件

提示词要求结构化的 JSON，但**模型不总是照做**。实测事故：

    AttributeError: 'str' object has no attribute 'get'
    summarize.py:966  text = (item.get("text") or "").strip()

`highlights` 要求 `[{"text": ...}]`，模型返回了 `["定义薄肌", "体脂分级"]`
—— 整条任务失败，而内容其实完全可用，只是格式简单了点。

同一个文件里的 `_normalize_viewpoints()` 早就做了这种容错，
`normalize_highlights()` 和 `validate_points()` 却漏了 —— 两套标准。

这些测试用**模型真实返回过的形态**来锁住行为：
字符串列表、对象列表、混合、以及垃圾值。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vd.summarize import (  # noqa: E402
    MAX_HIGHLIGHT_CHARS,
    normalize_highlights,
    validate_points,
)
from vd.transcript import Segment  # noqa: E402

SEGS = [Segment(0.0, 2.0, "片段")]


# ======================================================= highlights

def test_highlights_accepts_plain_string_list():
    """**回归（真实事故）**：字符串列表不能崩。

    这是 BV1xa4k6vEpp 失败的直接原因。
    """
    raw = ["定义薄肌：肌肉量+低体脂", "体脂分级：10%以下才是传奇皮肤"]
    out, dropped = normalize_highlights(raw, None)
    assert len(out) == 2, "字符串形态的 highlights 被丢掉了"
    assert out[0].text == "定义薄肌：肌肉量+低体脂"
    assert dropped == 0


def test_highlights_accepts_object_list():
    """标准形态不能因为加了容错而回归。"""
    out, _ = normalize_highlights([{"text": "标准形态"}], None)
    assert len(out) == 1
    assert out[0].text == "标准形态"


def test_highlights_accepts_alternate_field_names():
    """模型偶尔改口用 caption / title，不该因此丢内容。"""
    out, _ = normalize_highlights(
        [{"caption": "用 caption"}, {"title": "用 title"}], None)
    assert [h.text for h in out] == ["用 caption", "用 title"]


def test_highlights_drops_junk_but_keeps_valid():
    """混合输入：好的留下，垃圾丢掉，不崩。"""
    out, dropped = normalize_highlights(
        ["字符串", {"text": "对象"}, None, 123, "", {"text": "   "}], None)
    assert [h.text for h in out] == ["字符串", "对象"]
    assert dropped == 4


def test_highlights_handles_empty_and_none():
    """`data.get("highlights") or []` 传进来的可能是空列表。"""
    assert normalize_highlights([], None) == ([], 0)
    assert normalize_highlights(None, None) == ([], 0)


def test_highlights_truncates_overlong_text():
    out, _ = normalize_highlights(["字" * (MAX_HIGHLIGHT_CHARS + 50)], None)
    assert len(out[0].text) <= MAX_HIGHLIGHT_CHARS + 1   # +1 是省略号
    assert out[0].text.endswith("…")


def test_highlights_respects_max_items():
    out, dropped = normalize_highlights([f"第{i}条" for i in range(50)],
                                        None, max_items=3)
    assert len(out) == 3
    assert dropped == 47


# ======================================================= key_points

def test_points_accept_string_list():
    """`key_points` 是要点**正文**，丢了损失比 highlights 更大。"""
    out, _ = validate_points(["要点一的内容", "要点二的内容"], SEGS)
    assert [p.text for p in out] == ["要点一的内容", "要点二的内容"]


def test_points_accept_object_list():
    out, _ = validate_points(
        [{"text": "正文", "category": "事实", "topic": "主题",
          "insight": "补充说明"}], SEGS)
    assert out[0].text == "正文"
    assert out[0].category == "事实"
    assert out[0].display_topic == "主题"
    assert out[0].insight == "补充说明"


def test_points_accept_alternate_topic_field_names():
    """模型改口用 point / title 时，主题不该丢。"""
    out, _ = validate_points(
        [{"text": "a", "point": "用point"},
         {"text": "b", "title": "用title"}], SEGS)
    assert out[0].display_topic == "用point"
    assert out[1].display_topic == "用title"


def test_points_drops_junk():
    out, _ = validate_points(["好的", None, 42, {"text": ""}, ""], SEGS)
    assert [p.text for p in out] == ["好的"]


def test_points_handles_none():
    assert validate_points(None, SEGS) == ([], 0)


def test_points_never_produce_anchors():
    """锚点能力已移除：即使用字符串形态，也不该冒出锚点。"""
    out, dropped = validate_points(["要点"], SEGS)
    assert out[0].anchors == []
    assert dropped == 0
