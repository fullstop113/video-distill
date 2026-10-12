"""P2 总结层：逐字稿 → 总结 + 要点。

## 为什么**不做**时间戳锚点（2026 设计变更，推翻早前方案）

早前按方案 §5.2② 要求"每条结论都能点回原视频位置"，做法是把带
`[HH:MM:SS]` 的逐字稿喂给模型、让它为每条要点标注锚点，再本地校验锚点真实存在。

**这条路线已被移除**，原因是实测出的代价与收益不成比例：

| 项 | 实测值 |
| --- | --- |
| 带时间戳的 prompt | 19,193 token |
| 不带时间戳的 prompt | 8,108 token |
| **省下** | **11,085 token（58%）** |

时间戳占了输入的一半以上 —— 因为每一段都要渲染一个 `[HH:MM:SS]`，
而一段往往只有一句话。去掉它，输入 token 直接砍半。

而收益侧很有限：**产物本身就是总结**，用户读总结就好，不必再跳回原视频
逐条核对。所以"每条结论能点回原视频"这个能力，实际很少被用到。

结论：**去掉时间戳，换掉一半的输入成本**。这也让
`render_for_prompt(with_timestamps=False)` 成为默认路径。

## 遗留影响（改动留下的痕迹，写清楚免得后人困惑）

· `KeyPoint.anchors` / `TimelinePoint.at` 等字段**保留但不再使用** ——
  删掉它们会牵动产物格式与缓存，收益不大；
· `validate_points` 不再做锚点校验，只做"内容非空"过滤；
· `parse_ts` / `anchor_labels` / `match_highlights_to_points` 已无调用方，
  标记为 deprecated。

## 单次 vs map-reduce：先用实测数字决定

P0 实测 1 小时中文视频约 10400 字，而 `deepseek-flash` 上下文 1M——
所以**大多数视频根本不需要 map-reduce**。成本实测（空闲时段）：

| 方案 | ¥/小时视频 |
| --- | --- |
| 一次过 | 0.022 |
| map-reduce 放大 2.5 倍输入 | 0.042 |

因此默认策略是 `auto`：全文放得下就一次过，放不下才分片。
不要为了"架构好看"而无条件 map-reduce，那等于白花一倍的钱。

## 关于思考模式（实测踩过的坑）

DeepSeek **默认开启思考模式且 effort=high**。开着的后果：
- 思考内容会吃掉 `max_tokens`，小 `max_tokens` 下直接返回空 content；
- 我们做的是"抽取+压缩"，不是难题推理，思考的收益很低。

所以这里默认显式关闭 —— 见 `thinking_body()`，
可用 `DEEPSEEK_THINKING=enabled` 打开做对比（**开了要相应增大 max_tokens**，
否则思考内容会把输出预算吃光，返回空 content）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from .transcript import Segment, Transcript, ts

log = logging.getLogger(__name__)

# 成本价目（元 / 百万 token）。来源：DeepSeek 官方价目页，2026 实测抓取。
# 空闲/高峰按北京时间判定：周一至周五 9-12、14-18 为高峰。
PRICING = {
    "deepseek-flash": {
        "in_miss_off": 1.0, "in_miss_peak": 2.0,
        "in_hit_off": 0.02, "in_hit_peak": 0.04,
        "out_off": 4.0, "out_peak": 8.0,
    },
    "deepseek-v4-pro": {
        "in_miss_off": 4.5, "in_miss_peak": 9.0,
        "in_hit_off": 0.15, "in_hit_peak": 0.30,
        "out_off": 13.5, "out_peak": 27.0,
    },
}


class SummarizeError(RuntimeError):
    """总结失败。空产物一律走这里，不返回"空总结"。"""


class Strategy(str, Enum):
    SINGLE = "single"        # 全文一次过
    MAP_REDUCE = "map_reduce"  # 分片 → 分段总结 → 合并


@dataclass
class KeyPoint:
    """一条**分段精炼陈述**（不是摘要）。

    字段对应 prompt 里的 `point` / `text` / `insight`：

    - `topic`：这一段的主题，作为小标题 —— 让"要点"这一节可以扫读
    - `text` ：完整陈述。**去废话但不丢信息**，是产物的主体
    - `insight`：可选的补充理解。**必须与逐字稿一致**，不得引入外部事实
    """

    text: str
    anchors: list[float] = field(default_factory=list)   # 秒
    category: str = ""
    topic: str = ""
    insight: str = ""

    def anchor_labels(self) -> list[str]:
        return [ts(a) for a in self.anchors]

    @property
    def display_topic(self) -> str:
        """标题回退：模型没给主题时用首句截断。"""
        if self.topic:
            return self.topic
        first = re.split(r"[。！？!?]", self.text)[0]
        return (first[:18] + "…") if len(first) > 18 else first


@dataclass
class TimelinePoint:
    """关键时间轴上的一个节点 —— 用于「5 秒内决定跳到哪看」。

    与 KeyPoint 的区别：要点是为了**读**（内容完整、可加补充），
    时间轴是为了**跳**（一个时间点 + 一个短语 + 指向某条要点的链接）。
    所以在 Web 上它是一个可冒泡的节点，点击气泡滚到对应要点。
    """

    at: float                 # 秒
    text: str                 # 短语，建议 ≤ 20 字
    point_index: int = -1     # 指向 key_points 的下标（0 基）；-1 表示未匹配

    @property
    def label(self) -> str:
        return ts(self.at)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0
    # 是否撞过 max_tokens 上限（含"抢救成功"的情况）。
    # 这个标记要一路带到产物里：抢救出来的结果是**不完整**的，
    # 使用者有权知道，而不是被蒙在鼓里。
    truncated: bool = False

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.calls += other.calls
        self.truncated = self.truncated or other.truncated


@dataclass
class Summary:
    bvid: str
    title: str
    overall: str
    key_points: list[KeyPoint]
    strategy: Strategy
    model: str
    usage: Usage
    elapsed: float
    tl_dr: str = ""                                  # 一行速览
    highlights: list[TimelinePoint] = field(default_factory=list)   # 关键时间轴
    video_duration: int = 0                          # 秒，用于元信息
    transcript_chars: int = 0
    dropped_points: int = 0       # 锚点校验没过的条数（模型编造时间戳）
    notes: list[str] = field(default_factory=list)

    @property
    def cost_cny(self) -> float:
        return estimate_cost(self.model, self.usage)


def estimate_cost(model: str, usage: Usage, peak: bool | None = None) -> float:
    """按官方价目估算成本（元）。peak=None 时按当前时间自动判定。"""
    p = PRICING.get(model)
    if not p:
        return 0.0
    if peak is None:
        peak = is_peak_now()
    suffix = "peak" if peak else "off"
    miss = p[f"in_miss_{suffix}"]
    hit = p[f"in_hit_{suffix}"]
    out = p[f"out_{suffix}"]
    fresh = max(usage.prompt_tokens - usage.cached_tokens, 0)
    return (
        fresh / 1e6 * miss
        + usage.cached_tokens / 1e6 * hit
        + usage.completion_tokens / 1e6 * out
    )


def is_peak_now(now=None) -> bool:
    """高峰时段：北京时间周一至周五 9:00-12:00 与 14:00-18:00。"""
    import datetime as dt

    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    h = now.hour
    return (9 <= h < 12) or (14 <= h < 18)


# ---------------------------------------------------------------- 客户端


def _repair_unescaped_quotes(text: str) -> str | None:
    """修复"字符串值里出现未转义半角引号"导致的 JSON 语法错误。

    ## 这个 bug 有实测证据

    两次真实失败都是同一形态，服务端错误原文：

        Expecting ',' delimiter: line 2 column 18 (char 19)
        开头：{"tl_dr": "拆解券商"阿里云利润300亿增速50%"的说法，...

    模型在**转述别人的话**时直接写了半角引号（`"300亿"`），没有转义成 `\\"`，
    于是 JSON 结构被破坏。**这与 max_tokens 无关，加预算修不了** ——
    它已经写完整了（末尾是完整的 `}`），纯粹是转义错误。

    ## 修复思路

    JSON 里字符串内的裸引号总是**成对**出现（引用一段话）。
    所以按状态机扫描，遇到"看起来像字符串结束、但后面跟着的不是合法
    分隔符"的引号时，判定它是**内容引号**并补上转义。

    合法后继：`:` `,` `}` `]` 或字符串结尾（允许空白）。
    如果是别的字符（字母、数字、另一个引号…），说明这个引号在内容里。

    Returns:
        修复后的文本；修不动就返回 None（交给上层重试）。
    """
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
            continue
        if esc:
            out.append(ch)
            esc = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            i += 1
            continue
        if ch == '"':
            # 看后面第一个非空白字符，判断这是"结束引号"还是"内容引号"
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            nxt = text[j] if j < n else ""
            if nxt in (":", ",", "}", "]", "", '"'):
                # 合法后继 → 正常结束（`"` 也接受：`""` 是空串后紧跟新串）
                out.append(ch)
                in_str = False
            else:
                # 内容引号 → 转义
                out.append('\\"')
            i += 1
            continue
        # 字符串内的裸控制字符也要转义，否则同样非法
        if ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
        else:
            out.append(ch)
        i += 1
    fixed = "".join(out)
    return fixed if fixed != text else None


def _salvage_truncated_json(text: str) -> dict | None:
    """从被 `max_tokens` 截断的 JSON 里抢救出**完整的那部分**。

    ## 为什么不能只靠"调大 max_tokens"

    调大上限能解决绝大多数情况，但**没有上限是无限的**：视频再长、
    模型再啰嗦，总会撞到某个边界。撞到之后现在的行为是**整条任务失败**，
    已经生成的十几条要点全部丢弃 —— 而它们其实完好地躺在响应里。

    ## 做法

    截断的响应形如 `{"key_points": [ {...}, {...}, {"text": "半句话`。
    所以：逐字符扫描，跟踪字符串/转义状态，从**最后一个完整的顶层元素**
    处截断，再补齐未闭合的括号。抢救不出来就返回 None（宁可不救，不产出错数据）。

    Returns:
        能解析就返回 dict，否则 None。
    """
    s = text.strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        pass
    else:
        # 早返回也必须校验类型：`[1,2,3]` 是合法 JSON，
        # 但调用方要的是对象，直接返回会让上层拿到 list 再炸在别处（踩过）。
        return parsed if isinstance(parsed, dict) else None

    # 扫描出"字符串外的最后一个安全截断点"（即不在字符串中间的逗号）
    depth_stack: list[str] = []
    in_str = False
    esc = False
    last_safe = -1          # 最后一个"元素之间"的位置
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth_stack.append(ch)
        elif ch in "}]":
            if depth_stack:
                depth_stack.pop()
        elif ch == "," and len(depth_stack) >= 2:
            # 数组/对象内部、且**不在字符串里** → 一个完整的元素刚结束
            last_safe = i

    if last_safe < 0:
        return None

    # 从安全点截断，然后补齐括号
    candidate = s[:last_safe]
    # 重新计算候选串的未闭合栈
    stack: list[str] = []
    in_str = esc = False
    for ch in candidate:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        candidate += '"'
    for opener in reversed(stack):
        candidate += "}" if opener == "{" else "]"
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- 输出预算
#
# 实测（`scripts/measure_ratio.py`）：中文 **0.55 token/字符**
#   · 输入侧 0.548 ~ 0.556
#   · 输出侧 0.560
# 两侧一致，所以一个常数就够。
TOK_PER_CHAR = 0.55

# 实测（BV1dFem67EYt，21.6 分钟 / 9074 字逐字稿 → 6743 输出 token）：
# 输出 ≈ 逐字稿字数 × 0.743。
#
# ⚠️ 但**信息密集的视频会高得多**：BV1Q3pe6GERF（45 分钟财经分析，
# 12692 字逐字稿）实际输出了 21490 token，比例 **1.693**。
# 同一份输入，不同内容的输出量差 2.3 倍 —— 所以规划系数必须按**最坏**取，
# 不能按平均取。取 1.0 作为 OUT_RATIO，再用 headroom 吸收波动。
OUT_RATIO = 1.0

# 服务端硬上限，实测报错原文：
#   "the valid range of max_tokens is [1, 393216]"
API_MAX_TOKENS = 393216

# 我们的封顶 = 服务端上限。
#
# ## 为什么提到服务端上限（用户要求，且我之前的一个反对理由是错的）
#
# 我原先设 65536，理由是"单次输出过长时质量下滑"。但**这个理由站不住**：
# 实测同一 prompt 只改 max_tokens（2000/8000/32000/65536），
# 自然结束时的输出量分别是 3038/3157/2792 —— **输出量与上限无关**。
# `max_tokens` 只是 `min()` 的第二个参数：它只会**截断**，不会**诱导**。
# 所以"上限设大导致模型多写"不成立；真出现超长输出是模型自己的选择，
# 那时让它写完（而不是截断）反而更好。
#
# 另外实测过分段质量（前/中/后 1/3 的锚点率与 insight 率）：
# 单次全文全程 100%/100%，后段陈述字数还更长（263→315→346）——
# "后半段敷衍"在我测过的规模内**不成立**。
#
# ## 代价（如实记录）
#
# 提到上限后，"分片阈值 `single_pass_chars`"成为单次通道的**真正**约束。
# 也就是说：一个 6 万字以内的视频，现在可以拿到 39 万 token 的输出预算。
# 这**不会**增加常规费用（只付实际生成），但理论上一次超长输出的费用上界变高了。
# 未被验证的范围：我测过的自然输出最长约 32.5k token；
# 更大规模（几十万 token 单次输出）的质量**没有实测**，不作保证。
CLIENT_MAX_TOKENS = API_MAX_TOKENS

# 余量系数。取值依据（实测，见 `scripts/_check_headroom.py` 的推导）：
#   最坏观测比例 1.693 -> 1.5 不够（本次就超了 7%，靠重试才过）；
#   取 2.0 对最坏样本仍有 23% 余量。
# 调大 headroom **不会增加费用** —— 只付实际生成的 token，max_tokens 只是上限。
# 它的唯一代价是"真撞上限时截断得更晚"，而那种情况本来就该走分片。
DEFAULT_HEADROOM = 2.0


def output_budget(text_chars: int, headroom: float = DEFAULT_HEADROOM) -> int:
    """按输入规模计算本次调用该给多少输出 token。

    ## 为什么不再写死 8192

    写死上限时，**输入越长越容易撞上限**，而撞上限的表现是"整条任务失败"。
    实测：BV1Q3pe6GERF（45 分钟）实际需要 21490 token，旧上限 8192 ——
    **重试 3 次全部失败，因为上限没变，重试多少次都一样**。

    改成按规模给预算后，该视频拿到 26,408 token，一次通过。

    Args:
        text_chars: 输入文本（逐字稿）字符数
        headroom: 余量系数，吸收"内容密度"带来的输出波动
    """
    need = int(text_chars * OUT_RATIO * headroom) + 1024
    return max(4096, min(need, CLIENT_MAX_TOKENS))



def sanitize_no_proxy() -> list[str]:
    """把 `no_proxy` 里**带方括号的 IPv6 字面量**去掉，返回被移除的条目。

    ## 为什么需要

    实测踩到的坑：环境里 `no_proxy` 含 `[::1]`（某些工具链/终端会自动生成这种写法），
    httpx 解析代理配置时抛 `InvalidURL: Invalid port: ':1]'` ——
    **客户端连构造都失败**，所有 API 调用直接崩，而且报错栈指向 httpx 内部，
    完全看不出跟 `no_proxy` 有关。

    实测对照（单片段隔离，每个用例独立子进程）：
        no_proxy='[::1]'   → ❌ InvalidURL
        no_proxy='::1'     → ✅ 正常
        no_proxy 含 [::1] 但去掉 → ✅ 正常

    所以只去掉方括号形式。裸 `::1` 是好用的，保留它 ——
    本机走代理时它是必要的绕过项。
    """
    raw = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    keep = [p for p in parts
            if not (p.startswith("[") and p.endswith("]"))]
    dropped = [p for p in parts if p not in keep]
    if not dropped:
        return []
    if keep:
        # 大小写不一致会让不同库读到不同的值，两边都写
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = ",".join(keep)
    else:
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
    log.warning("已从 no_proxy 移除无法解析的 IPv6 方括号条目：%s", dropped)
    return dropped


def thinking_body() -> dict:
    """构造 `extra_body`，决定是否开启思考模式。

    ## 默认关闭（实测结论）

    DeepSeek 默认开启思考且 `effort=high`，而思考内容**会占用 `max_tokens`**：
    输出预算小的时候，思考把预算吃光，`content` 直接返回空字符串。

    我们做的是"抽取 + 压缩"而不是难题推理，思考收益很低，所以默认关闭。

    ## 为什么做成可配置

    原先这里是硬编码的 `{"thinking": {"type": "disabled"}}`，
    但 `.env.example` 里却写了 `DEEPSEEK_THINKING=disabled` ——
    **配了不生效**，比没有这个变量更糟（用户以为调过了）。

    现在两者一致了：设 `DEEPSEEK_THINKING=enabled` 真的会打开。

    ⚠️ 打开时要**相应调大输出预算**，否则会得到空 content。
    预算由 `output_budget()` 计算，它会因为思考内容而低估。
    """
    raw = os.getenv("DEEPSEEK_THINKING", "disabled").strip().lower()
    if raw in ("enabled", "on", "true", "1", "yes"):
        return {"thinking": {"type": "enabled"}}
    return {"thinking": {"type": "disabled"}}


class DeepSeekClient:
    """极薄的封装。存在的意义是把"必须关思考模式"和"必须重试空 content"固化下来。"""

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "",
                 timeout: float = 180.0):
        from openai import OpenAI

        # 建客户端**之前**要净化代理配置，否则 httpx 在构造阶段就抛
        # InvalidURL，报错完全指不到根因（见 sanitize_no_proxy 的说明）。
        sanitize_no_proxy()

        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise SummarizeError("缺少 DEEPSEEK_API_KEY（P2 必需）")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.timeout = timeout
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout)

    def json_call(
        self, system: str, user: str, *, max_tokens: int = 4096, retries: int = 3
    ) -> tuple[dict, Usage]:
        """要求 JSON 输出并解析。空 content 有概率出现（官方文档明说），故重试。"""
        # 官方要求：prompt 里必须出现 "json" 字样并给出格式样例
        if "json" not in system.lower() and "json" not in user.lower():
            raise SummarizeError("JSON 模式下 prompt 必须包含 json 字样与格式样例")

        last_err: Exception | None = None
        usage = Usage()
        for attempt in range(retries):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=max_tokens,
                    # 思考模式默认开启且 effort=high，会吃掉 max_tokens。
                    # 我们做的是抽取压缩而非难题推理，默认关闭更省更稳。
                    # 但**做成可配置**：万一某类视频（如专业分析）需要
                    # 更强推理，可以打开它对比效果。
                    extra_body=thinking_body(),
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning("DeepSeek 调用失败（第 %d 次）：%s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue

            u = resp.usage
            usage.calls += 1
            if u:
                usage.prompt_tokens += u.prompt_tokens or 0
                usage.completion_tokens += u.completion_tokens or 0
                details = getattr(u, "prompt_tokens_details", None)
                if details is not None:
                    usage.cached_tokens += getattr(details, "cached_tokens", 0) or 0

            content = (resp.choices[0].message.content or "").strip()
            if not content:
                # 文档明确提示这种情况存在，必须重试而不是当成"没内容"
                last_err = SummarizeError("API 返回空 content（JSON 模式已知问题）")
                log.warning("空 content，重试 %d/%d", attempt + 1, retries)
                time.sleep(1 + attempt)
                continue

            # 先用 finish_reason 判断是否被截断，而不是等 JSON 解析报错。
            # 解析报错的信息（"Unterminated string at char 14537"）指向的是
            # 症状，finish_reason=length 才是**原因**，日志里应当直接写原因。
            finish = getattr(resp.choices[0], "finish_reason", "") or ""
            try:
                return json.loads(content), usage
            except json.JSONDecodeError as exc:
                if finish == "length":
                    # 撞了 max_tokens：先尝试抢救完整部分，抢救到就接受（并记录）
                    salvaged = _salvage_truncated_json(content)
                    if salvaged is not None:
                        usage.truncated = True
                        log.warning(
                            "响应被 max_tokens 截断（%d 字符），已抢救出完整部分",
                            len(content),
                        )
                        return salvaged, usage
                    last_err = SummarizeError(
                        f"响应被 max_tokens 截断且无法抢救（已输出 {len(content)} 字符）。"
                        f"当前 max_tokens={max_tokens}，应调大。"
                    )
                else:
                    # 没被截断，纯粹是 JSON 语法错。
                    # 最常见的形态是**字符串值里有未转义的半角引号**
                    # （模型转述别人的话时写成 `"300亿"`）—— 实测两次失败都是它。
                    # 加 max_tokens 修不了这个，必须做转义修复。
                    repaired = _repair_unescaped_quotes(content)
                    if repaired is not None:
                        try:
                            data = json.loads(repaired)
                        except json.JSONDecodeError:
                            pass
                        else:
                            log.warning(
                                "JSON 有未转义的引号，已修复后解析成功（%d 字符）",
                                len(content),
                            )
                            return data, usage
                    # 前后都要打印：`Expecting ',' delimiter: line 2 column 18`
                    # 这类错误的原因在**开头**，而"是否被截断"的线索在**结尾**。
                    # 只打一头会让人误判 —— 实测吃过亏：看到末尾是完整的 `}`
                    # 就以为没截断，其实问题出在第 2 行。
                    last_err = SummarizeError(
                        f"返回内容不是合法 JSON：{exc}；"
                        f"全文 {len(content)} 字符。"
                        f"开头 120 字符：{content[:120]!r}；"
                        f"末尾 80 字符：{content[-80:]!r}"
                    )
                log.warning("JSON 解析失败，重试 %d/%d：%s", attempt + 1, retries, last_err)
                time.sleep(1 + attempt)
        raise SummarizeError(f"JSON 调用失败（已重试 {retries} 次）：{last_err}")


# ---------------------------------------------------------------- 分片


def chunk_segments(
    segments: list[Segment], max_chars: int = 9000, max_seconds: float = 0
) -> list[list[Segment]]:
    """按字符数分片，**不切断句子**（以一个 segment 为最小单位）。

    时间戳锚点的前提是"每个字都能定位"，所以分片必须落在 segment 边界上。
    """
    chunks: list[list[Segment]] = []
    cur: list[Segment] = []
    size = 0
    for seg in segments:
        if cur and size + len(seg.text) > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += len(seg.text)
        if max_seconds and cur and (seg.end - cur[0].start) > max_seconds:
            chunks.append(cur)
            cur, size = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def render_for_prompt(segments: list[Segment], with_timestamps: bool = False) -> str:
    """把逐字稿渲染成喂给模型的文本。

    ⚠️ 默认 `with_timestamps=False`（2026 设计变更）。

    时间戳占 prompt 的 **58%**（实测 19,193 → 8,108 token），
    而产物本身就是总结、不需要跳回原视频，所以默认不带。

    保留这个开关是为了：
      · 让测试能对比两种渲染的 token 差（`scripts/measure_ts_savings.py`）；
      · 万一以后要恢复锚点能力，改动面小。

    但**默认值必须是 False** —— 否则一个不传参的调用点就会
    悄悄把输入成本翻倍，而且行为上看不出来。
    """
    if not with_timestamps:
        return "".join(s.text for s in segments)
    return "\n".join(f"[{ts(s.start)}] {s.text}" for s in segments)


# ---------------------------------------------------------------- Prompt


SYSTEM_SINGLE = """你是一个严谨的中文视频内容分析师。用户会给你一份逐字稿。

你的任务：产出速览、脉络总结、关键节点与**详细要点**。**必须输出 JSON**，格式如下（严格遵守字段名）：

{
  "tl_dr": "一句话说清这个视频讲了什么，控制在 60 字以内",
  "overall": "对整体内容的脉络总结，3-6 句，说清讲了什么、脉络如何、结论是什么",
  "highlights": [
    {"text": "错失 DOTA，冰蛙被 Valve 招走"}
  ],
  "key_points": [
    {"point": "这一段的主题，10-16 字的短语（会作为小标题）",
     "text": "**详细陈述**：把这一段的价值信息完整讲清楚，可以多句",
     "category": "事实",
     "insight": "一句话点出这一层的含义（≤25 字，不要复述内容）"}
  ]
}

## key_points 的定位（这一节最重要，务必按此标准）

**它不是"摘要"，而是"分段精炼陈述"。** 目标是让人**只读这一节就够，不必再看视频**。

具体要求：
1. **去废话、取精华**：删掉口头禅、寒暄、重复、跑题、与主题无关的插叙。
2. **不漏任何有价值的信息**：数字、时间、人名、产品名、结论、因果关系、
   反对意见、前提条件、例外情况 —— **一个都不能丢**。
   宁可这一节长一点，也不要为了"简洁"漏掉事实。
3. **`insight` 是一句话总结，不是补充段落**：
   ≤25 字，点出这一层的**含义或性质**（如"这些设计适合拉朋友，却拿掉了个人进步的标尺"）。
   **不要复述 text、不要写"补充："、不要展开成多句。**
   例：`"含蓄放在竞技游戏里就叫憋屈"`。
4. **段落化**：每一条对应视频里的一个**话题段落**，不是一句话。
   `point` 是这一段的主题（10-16 字），`text` 是这一段的完整陈述。
5. 顺序**按视频时间顺序**，条数 6-15 条（宁可充分，不要压缩）。
6. `category` 只能取：结论 / 论据 / 方法 / 事实 / 行动项。
7. **人称要准确**：不要把"游戏""视频""项目"写成"他/她"。指代不明时重复名词。
8. 全部用中文。

## highlights 的定位

5-10 个**转折点/里程碑**，`text` ≤20 字，只写"发生了什么"。
用途是让人快速扫读视频脉络，**宁可少而准**。
按视频中的先后顺序排列。
"""

SYSTEM_MAP = """你是一个严谨的中文视频内容分析师。用户会给你一份**逐字稿片段**
（这是长视频的一部分，不是全部）。

你的任务：把这一片段转为**精炼陈述**。**必须输出 JSON**，格式如下（严格遵守字段名）：

{
  "points": [
    {"point": "本段主题，10-16 字",
     "text": "本段的完整陈述：去掉废话，但**不丢任何有价值的信息**",
     "category": "论据",
     "insight": "一句话点出含义（≤25 字）"}
  ]
}

硬性规则：
1. **去废话、取精华，但不漏信息**：数字、时间、人名、因果、结论、例外都要保留。
2. `insight` 是**一句话总结**（≤25 字），点出含义，不要复述、不要展开成多句。
3. category 只能取：结论 / 论据 / 方法 / 事实 / 行动项。
4. 只依据片段内容，不要推测片段之外的内容。
5. **人称要准确**：不要把"游戏""视频""项目"写成"他/她"。
6. 全部用中文。"""

SYSTEM_REDUCE = """你是一个严谨的中文视频内容分析师。用户会给你一份**分段要点清单**
（来自同一个长视频的不同片段）。

你的任务：合并成速览、脉络总结、关键节点与**详细要点**。**必须输出 JSON**，格式如下：

{
  "tl_dr": "一句话说清这个视频讲了什么，≤60 字",
  "overall": "对整体内容的脉络总结，3-6 句",
  "highlights": [
    {"text": "转折点短语，≤20 字"}
  ],
  "key_points": [
    {"point": "主题短语，10-16 字",
     "text": "详细陈述：合并同一话题的内容，去废话但不丢信息",
     "category": "结论",
     "insight": "一句话点出含义（≤25 字）"}
  ]
}

## 合并要求

1. **同一话题的分段要点要合并成一条**，不要保留碎片化的重复条目。
2. **合并时不得丢信息**：原来各段里的数字、时间、人名、结论都要保留下来。
   合并的目标是"去重"和"去废话"，不是"压缩丢信息"。
3. `point` 是主题（10-16 字），`text` 是这一话题的完整陈述（可多句）。
4. 条数 6-15 条，按时间顺序排列。
5. `insight` 是**一句话总结**（≤25 字），点出这一层的含义。
   **不要复述 text、不要写"补充："前缀、不要展开成多句。**
   没有可写的一句话含义时就留空字符串。
6. category 只能取：结论 / 论据 / 方法 / 事实 / 行动项。
7. **highlights** 选 5-10 个转折点，`text` ≤20 字，按视频中的先后顺序排列。
8. **人称要准确**：不要把"游戏""视频""项目"写成"他/她"。
9. 全部用中文。"""


# ---------------------------------------------------------------- 锚点校验


def parse_ts(label: str) -> float | None:
    """把 HH:MM:SS / MM:SS 解析成秒。"""
    parts = str(label).strip().strip("[]").split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def validate_points(
    raw_points: list[dict], segments: list[Segment], tolerance: float = 2.0
) -> tuple[list[KeyPoint], int]:
    """校验要点文本。

    去掉时间戳后，不再做锚点真实性校验 —— 模型不输出 anchors。
    `segments` 与 `tolerance` 参数保留仅为向后兼容调用签名，当前不再使用。
    只过滤空文本要点，并解析 text / category / topic / insight 字段。

    Returns:
        (校验后的要点, 被丢弃的锚点数，恒为 0)
    """
    out: list[KeyPoint] = []
    for p in raw_points:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        out.append(
            KeyPoint(
                text=text,
                anchors=[],  # 不再使用锚点
                category=(p.get("category") or "").strip(),
                # 兼容模型偶尔改口用的字段名，避免因此丢掉主题/补充说明
                topic=(p.get("point") or p.get("topic") or p.get("title") or "").strip(),
                # 机械保证"一句话"：模型很容易又把 insight 展开成多句
                insight=_trim_insight(p.get("insight") or p.get("note") or ""),
            )
        )
    return out, 0


def match_highlights_to_points(
    highlights: list[TimelinePoint], points: list[KeyPoint], tolerance: float = 15.0
) -> None:
    """把时间轴节点关联到要点（原地修改 `point_index`）。

    为什么需要它：Web 上"点气泡 → 跳到对应要点段落"必须知道**跳到哪一段**。
    让模型直接输出对应关系不可靠（条目会变、下标会错），
    所以在本地按时间邻近度匹配：时间轴节点落在那条要点的锚点附近，
    就认为它属于那一条。

    多条时间轴节点落到同一条要点是正常的（一个话题可能有多个里程碑）——
    气泡里各显示各的内容，点击都滚到同一段。
    """
    for h in highlights:
        best_idx = -1
        best_dist = None
        for i, p in enumerate(points):
            if not p.anchors:
                continue
            dist = min(abs(h.at - a) for a in p.anchors)
            if dist <= tolerance and (best_dist is None or dist < best_dist):
                best_idx, best_dist = i, dist
        if best_idx < 0:
            # 锚点都对不上时退一步：找时间范围覆盖它的那条要点
            for i, p in enumerate(points):
                if len(p.anchors) >= 2:
                    lo, hi = min(p.anchors), max(p.anchors)
                    if lo - tolerance <= h.at <= hi + tolerance:
                        best_idx = i
                        break
        if best_idx < 0 and points:
            # 最后兜底：找锚点时间上最接近的那条要点。
            #
            # 为什么需要：实测有一版产物里有个节点落在两条要点的锚点之间，
            # 前两种判定都不命中，于是它变成"未关联" —— 用户点击没有反应，
            # **看起来像坏了**。而"跳到时间上最近的那一段"虽然不是精确答案，
            # 但远比"点了没反应"好。宁可跳得略偏，也不要点了不动。
            candidates = [
                (min(abs(h.at - a) for a in p.anchors), i)
                for i, p in enumerate(points) if p.anchors
            ]
            if candidates:
                best_idx = min(candidates)[1]
        h.point_index = best_idx


# ---------------------------------------------------------------- 编排

MIN_SUMMARY_CHARS = 40          # overall 的长度下限
MIN_POINTS = 3                  # 要点数下限
MAX_INSIGHT_CHARS = 30          # 一句话总结的长度上限（用户明确要求"改成一句话"）
MAX_HIGHLIGHT_CHARS = 40        # 时间轴短语的长度上限（超了会失去"扫一眼"的意义）
MAX_HIGHLIGHTS = 12             # 时间轴条数上限


def _trim_insight(text: str, limit: int = MAX_INSIGHT_CHARS) -> str:
    """把「一句话总结」压到一句话。

    只靠 prompt 约束不够 —— 模型很容易又展开成两三句。
    这里做机械保证，顺序很重要：

      1. 先剥掉「补充：」之类的前缀；
      2. **再看是不是多句** —— 是多句就取第一句（**不管总长是否超限**）；
      3. 剩下的若仍超长，在逗号处断，实在不行才硬截。

    ⚠️ 第 2 步的顺序踩过坑：第一版把 `len(t) <= limit` 的早返回放在最前面，
    于是"第一句话。第二句多余。"（11 字）**原样返回了两句** ——
    长度检查通过了，但"一句话"这个要求根本没被执行。
    短不等于是一句话。
    """
    t = _clean_insight(text)
    if not t:
        return ""

    # 多句 → 取第一句（无论总长）
    parts = [p.strip() for p in re.split(r"[。！？!?；;]", t) if p.strip()]
    if len(parts) > 1:
        t = parts[0].rstrip("，。、；：,;:")

    if len(t) <= limit:
        return t
    # 仍超长：在逗号处找断点，把省略号算进预算
    cut = t[: limit - 1]
    for sep in ("，", ",", "、", "："):
        idx = cut.rfind(sep)
        if idx > limit // 2:
            return cut[:idx] + "…"
    return cut + "…"


def normalize_highlights(
    raw: list[dict],
    info,
    *,
    max_items: int = MAX_HIGHLIGHTS,
) -> tuple[list[TimelinePoint], int]:
    """校验并归一化关键节点（原"时间轴"，已去掉时间戳）。

    去掉时间戳后，highlights 退化为"转折点短语列表"，只校验文本内容与条数上限。
    `info` 参数保留是为了向后兼容调用签名，当前不再使用。

    Returns:
        (节点列表, 被丢弃的条数)
    """
    out: list[TimelinePoint] = []
    dropped = 0
    for item in raw:
        text = (item.get("text") or "").strip()
        if not text:
            dropped += 1
            continue
        if len(text) > MAX_HIGHLIGHT_CHARS:
            text = text[:MAX_HIGHLIGHT_CHARS].rstrip("，。、 ") + "…"
        # at / point_index 字段保留但不再使用（无时间戳，不做要点关联）
        out.append(TimelinePoint(at=0.0, text=text))

    if len(out) > max_items:
        dropped += len(out) - max_items
        out = out[:max_items]
    return out, dropped


def _check_summary(overall: str, points: list[KeyPoint], where: str) -> None:
    """空产物校验。与取数层同一原则：宁可报错，不产出空文件。"""
    if len(overall.strip()) < MIN_SUMMARY_CHARS:
        raise SummarizeError(
            f"{where} 的总结过短（{len(overall.strip())} 字 < 下限 {MIN_SUMMARY_CHARS}）→ 判为失败。"
        )
    if len(points) < MIN_POINTS:
        raise SummarizeError(
            f"{where} 的要点只有 {len(points)} 条（下限 {MIN_POINTS}）→ 判为失败。"
        )


def summarize(
    transcript: Transcript,
    *,
    client: DeepSeekClient | None = None,
    strategy: str = "auto",
    chunk_chars: int = 9000,
    single_pass_chars: int = 60000,
    progress=None,
) -> Summary:
    """逐字稿 → 总结 + 要点。

    Args:
        strategy: auto（默认，按长度自动选）/ single / map_reduce。
        single_pass_chars: "全文一次过"的字数上限，默认 6 万字。
            1M 上下文塞得下更多，留余量是为了控制单次请求时长与输出质量，
            不是模型限制。超过就走分片。
    """
    client = client or DeepSeekClient()
    segments = transcript.segments
    if not segments:
        raise SummarizeError(f"{transcript.bvid} 的逐字稿为空，无法总结")

    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    total_chars = sum(len(s.text) for s in segments)
    if strategy == "auto":
        chosen = Strategy.SINGLE if total_chars <= single_pass_chars else Strategy.MAP_REDUCE
    else:
        chosen = Strategy(strategy)

    usage = Usage()
    dropped = 0
    dropped_hl = 0
    notes: list[str] = []
    tl_dr = ""
    highlights: list[TimelinePoint] = []
    t0 = time.time()

    if chosen is Strategy.SINGLE:
        say(f"单次总结（全文 {total_chars} 字）")
        # 发给大模型的逐字稿不带时间戳：用户不看原视频，时间戳无意义，
        # 且会占据大量 token、增加成本。
        user = (
            f"标题：{transcript.title}\n\n逐字稿：\n"
            f"{render_for_prompt(segments, with_timestamps=False)}"
        )
        data, u = client.json_call(
            SYSTEM_SINGLE, user, max_tokens=output_budget(total_chars)
        )
        usage.add(u)
        tl_dr = (data.get("tl_dr") or "").strip()
        overall = (data.get("overall") or "").strip()
        points, dropped = validate_points(data.get("key_points") or [], segments)
        highlights, dropped_hl = normalize_highlights(
            data.get("highlights") or [], transcript.info
        )
    else:
        chunks = chunk_segments(segments, max_chars=chunk_chars)
        say(f"分片总结：{len(chunks)} 片（全文 {total_chars} 字）")
        merged_raw: list[dict] = []
        for i, chunk in enumerate(chunks, 1):
            say(f"  第 {i}/{len(chunks)} 片：{ts(chunk[0].start)} - {ts(chunk[-1].end)}")
            # 分片同样不带时间戳，与单次通道保持一致
            user = (
                f"标题：{transcript.title}\n"
                f"这是第 {i}/{len(chunks)} 片\n\n"
                f"逐字稿片段：\n{render_for_prompt(chunk, with_timestamps=False)}"
            )
            data, u = client.json_call(
                SYSTEM_MAP, user, max_tokens=output_budget(len(render_for_prompt(chunk)))
            )
            usage.add(u)
            merged_raw.extend(data.get("points") or [])

        if not merged_raw:
            raise SummarizeError("分片阶段没有产出任何要点")
        say(f"合并 {len(merged_raw)} 条分段要点")
        listing = "\n".join(
            f"- [{p.get('category', '')}] {p.get('text', '')}"
            for p in merged_raw
        )
        user = (
            f"标题：{transcript.title}\n"
            f"以下是该视频各片段的要点（共 {len(merged_raw)} 条），"
            f"请合并成整体速览、总结、关键时间轴与要点：\n\n{listing}"
        )
        data, u = client.json_call(
            SYSTEM_REDUCE, user, max_tokens=output_budget(len(listing))
        )
        usage.add(u)
        tl_dr = (data.get("tl_dr") or "").strip()
        overall = (data.get("overall") or "").strip()
        points, dropped = validate_points(data.get("key_points") or [], segments)
        highlights, dropped_hl = normalize_highlights(
            data.get("highlights") or [], transcript.info
        )

    _check_summary(overall, points, transcript.bvid)

    if dropped_hl:
        notes.append(f"关键节点丢弃 {dropped_hl} 条无效或超量条目")
    if usage.truncated:
        # 不隐瞒：抢救出来的结果**缺了尾部内容**，使用者有权知道。
        # 这条要足够醒目 —— 产物会被当作可信记录反复引用。
        notes.append(
            "⚠️ 本次响应撞到了输出上限，**产物可能不完整**（已保留完整部分）。"
            "建议用更短的分片重新生成，或调大输出预算。"
        )
    notes.append(f"用时 {time.time() - t0:.1f}s，{usage.calls} 次调用")
    notes.append(
        f"tokens：输入 {usage.prompt_tokens}（缓存命中 {usage.cached_tokens}）"
        f" / 输出 {usage.completion_tokens}"
    )

    return Summary(
        bvid=transcript.bvid,
        title=transcript.title,
        overall=overall,
        key_points=points,
        strategy=chosen,
        model=client.model,
        usage=usage,
        elapsed=time.time() - t0,
        tl_dr=tl_dr,
        highlights=highlights,
        video_duration=int(getattr(transcript.info, "duration", 0) or 0),
        transcript_chars=total_chars,
        dropped_points=dropped,
        notes=notes,
    )


def _split_sentences(text: str, target: int = 70) -> list[str]:
    """把长段落重排成便于扫读的多行。**只加换行，不改一个字。**

    实测的产物里 `overall` 是 295 字符的单一长段落、最长句 91 字符，
    整个视频脉络挤成一块，只能通读、无法扫读。

    做法是"贪心累积到目标长度"：

      1. 先按句末标点切成句子；
      2. 依次累加，达到 `target` 就换行 —— 这样既不会一行半句，
         也不会把整段挤成一块；
      3. 单句超过 `target` 时，在分号/逗号处再切一刀
         （口语转录常见"逗号连缀、没有句号"的长句）。

    第一版按"行短于 target/2 就并回上一行"写，结果并完可能超长，
    而超长检查只在 >110 时触发，于是 82 字的行照样留着。贪心累积更简单也更可控。
    """
    text = (text or "").strip()
    if not text:
        return []

    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?])", text) if s.strip()]
    lines: list[str] = []
    cur = ""
    for s in sentences:
        if len(s) > target:
            if cur:
                lines.append(cur)
                cur = ""
            piece = ""
            for frag in re.split(r"(?<=[；;，,])", s):
                if piece and len(piece) + len(frag) > target:
                    lines.append(piece)
                    piece = frag
                else:
                    piece += frag
            if piece:
                cur = piece
            continue
        if cur and len(cur) + len(s) > target:
            lines.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        lines.append(cur)
    return lines


def _ascii_arrow(highlights: list[TimelinePoint], width: int = 68) -> str:
    """把时间轴画成一条横向箭头，节点直接坐在轴线上。

    Markdown 无法做真正的悬停冒泡，所以在文件里退化成「箭头图 + 图例」：
    箭头图给**整体形状**（有几个转折点、分布在哪），
    图例给**可点击的跳转**（跳视频 / 跳下方要点段落）。

    ⚠️ 第一版把节点标记放在**轴线下方**，看起来像悬空的散字，不成"轴上有节点"。
    现在直接把标记写进轴线字符串里，节点就落在线上。
    """
    n = len(highlights)
    if n == 0:
        return ""
    marks = "❶❷❸❹❺❻❼❽❾❿⓫⓬"[:n]
    inner = width - 3          # 扣掉左边 `├` 和右边 `─▶` 占位
    line = ["─"] * inner
    if n == 1:
        line[1] = marks[0]
    else:
        step = max(1, (inner - 3) // max(n - 1, 1))
        for i in range(n):
            pos = min(1 + i * step, inner - 1)
            line[pos] = marks[i]
    return "├" + "".join(line) + "▶"


def _clean_insight(text: str) -> str:
    """去掉模型自己加的「补充：」前缀。

    ⚠️ 渲染模板已经写了「💡 **补充理解**：」，而 prompt 里又建议模型用「补充：」开头，
    两边一叠加就变成 `补充理解：补充：…` —— 实测 12 条全部如此。
    这里统一剥掉，模板负责加前缀，模型只负责内容。
    """
    t = (text or "").strip()
    for prefix in ("补充：", "补充:", "补充 ", "注：", "注:"):
        if t.startswith(prefix):
            t = t[len(prefix):].lstrip()
            break
    return t


def _timeline_md(s: Summary) -> list[str]:
    """关键节点列表（原"时间轴"，已去掉时间戳与跳转）。

    去掉时间戳后，highlights 退化为"转折点短语列表"，仅用于快速扫读视频脉络。
    """
    hs = getattr(s, "highlights", None) or []
    if not hs:
        return []
    out = ["## 关键节点", ""]
    for i, h in enumerate(hs, 1):
        out.append(f"{i}. {h.text}")
    out.append("")
    return out


def to_markdown(s: Summary) -> str:
    """渲染总结产物。

    结构（去掉时间戳后）：

        # 标题
        > 声明 + 一行元信息
        ## 速览              ← 一眼知道值不值得看
        ## 总结              ← 脉络，按句换行
        ## 关键节点           ← 转折点短语列表，快速扫读脉络
        ## 要点              ← **分段精炼陈述**，不是摘要
        ### 生成记录

    「要点」的定位：**它不是摘要，是分段精炼陈述**——
    去废话但不丢信息、可显式加补充理解，目标是"只读这一节就够，不必再看视频"。
    因此这里每条一个 `###` 小节（带主题标题），而不是一句话的列表项。
    """
    from .disclaimer import artifact_header

    # 防御性读取：渲染只依赖"有哪些字段"，不该因为字段缺失就崩掉
    duration_s = int(getattr(s, "video_duration", 0) or 0)
    tl_dr = getattr(s, "tl_dr", "") or ""
    transcript_chars = int(getattr(s, "transcript_chars", 0) or 0)

    label = "单次全文" if s.strategy is Strategy.SINGLE else "分片 map-reduce"
    duration = f"{duration_s // 60} 分钟 · " if duration_s else ""
    size = f" · 逐字稿 {transcript_chars} 字" if transcript_chars else ""

    lines = [
        f"# {s.title} — 总结与要点",
        "",
        artifact_header(f"AI 总结（{s.model}）", s.bvid, kind="总结"),
        "",
        f"> {duration}{len(s.key_points)} 条要点 · `{s.model}` · {label} · "
        f"¥{s.cost_cny:.4f}{size}　"
        f"（[原视频](https://www.bilibili.com/video/{s.bvid})）",
        "",
    ]

    if tl_dr:
        lines += ["## 速览", "", f"> {tl_dr}", ""]

    lines += ["## 总结", ""]
    for para in _split_sentences(s.overall):
        lines += [para, ""]

    lines += _timeline_md(s)

    lines += ["## 要点", ""]
    for i, p in enumerate(s.key_points, 1):
        cat = f"`{p.category}` " if p.category else ""
        lines.append(f"### {i}. {cat}{p.display_topic}")
        lines.append("")
        # 陈述部分放宽到 90 字一行：这一节是"要读的内容"，不是导航
        for para in _split_sentences(p.text, target=90):
            lines += [para, ""]
        if p.insight:
            lines += [f"> 💡 **补充理解**：{_clean_insight(p.insight)}", ""]

    if s.notes:
        lines += ["---", "", "### 生成记录", ""]
        lines += [f"- {n}" for n in s.notes]
        lines.append("")
    return "\n".join(lines)

