"""热词修正：字幕与 ASR **共用一份**的专有名词后处理表。

## 为什么必须共用

P0 实测（docs/p0-findings.md §7.3）发现两条路线的错误属于**同一类**：

| 路线 | 实测错误 |
| --- | --- |
| B 站 AI 字幕 | `DeepSeek` → `DPCARNE` / `DPC` |
| 本地 FunASR | `花鼓灯` → `花舞灯`，`红油` → `犯油` |

既然短板相同，修正表就不能各做一套，否则同一类错误要在两处重复维护、
而且很容易只修好一条路线。

## 设计要点

1. **数据驱动**：规则放在 `data/hotwords.json`，运行时可改，不必动代码。
2. **按上下文限定**：`DeepSeek Harness` 与单独的 `DPC` 风险不同，
   规则可声明 `context` 正则，只在匹配的上下文里替换，避免误伤。
3. **记录命中**：每次修正都记下命中了哪条规则，便于回流成新的规则。
4. **权威源优先**：`authoritative` 规则（如官方名称）优先于猜测性规则。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "hotwords.json"


@dataclass
class Correction:
    """一条修正规则。"""

    wrong: str
    right: str
    context: str = ""          # 可选的正则；只在匹配的上下文里替换
    note: str = ""
    authoritative: bool = False  # 来自官方/确定来源

    def compile(self) -> re.Pattern | None:
        return re.compile(self.context) if self.context else None


@dataclass
class CorrectionHit:
    wrong: str
    right: str
    count: int
    note: str = ""


@dataclass
class CorrectionResult:
    text: str
    hits: list[CorrectionHit] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(h.count for h in self.hits)


# 内置兜底规则。文件缺失时不至于完全没有修正能力。
# 这些条目都来自 P0/P1 的真实实测错误，不是凭空写的。
BUILTIN: list[Correction] = [
    # —— DeepSeek 系列：B 站 AI 字幕在同一篇里就吐出了 3 种错法 ——
    Correction("DPCCARNE", "DeepSeek", note="B站AI字幕实测（P1 同一篇内出现）", authoritative=True),
    Correction("DPC harness", "DeepSeek Harness", note="B站AI字幕实测", authoritative=True),
    Correction("DPC Harness", "DeepSeek Harness", note="大小写变体", authoritative=True),
    Correction("DPCARNE", "DeepSeek", note="B站AI字幕实测（P0 §7.3）", authoritative=True),
    # 裸 "DPC" 风险高（可能是别的缩写），限定在 B 站/工具语境里才替换
    Correction("DPCK", "DeepSeek", context=r"账号|登录|注册", note="B站AI字幕实测"),
    Correction("DPC", "DeepSeek", context=r"官方|账号|客户端|桌面端|安装", note="限定语境，避免误伤其它 DPC 缩写"),
    # —— FunASR 系列 ——
    Correction("花舞灯", "花鼓灯", note="FunASR 实测误识别（P0 §7.3）"),
    Correction("狗熊多草", "狗熊哆嗦", note="FunASR 实测误识别"),
    Correction("犯油", "红油", context=r"包子|烧心|吃完", note="FunASR 实测误识别，仅餐饮上下文"),
]


def load_corrections(path: Path | None = None) -> list[Correction]:
    """载入规则。文件里的规则在前，内置兜底在后（后者只补漏）。"""
    path = path or DEFAULT_PATH
    rules: list[Correction] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("corrections", []):
                rules.append(
                    Correction(
                        wrong=item["wrong"],
                        right=item["right"],
                        context=item.get("context", ""),
                        note=item.get("note", ""),
                        authoritative=bool(item.get("authoritative", False)),
                    )
                )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"热词文件格式错误 {path}：{exc}") from exc

    seen = {(r.wrong, r.context) for r in rules}
    for r in BUILTIN:
        if (r.wrong, r.context) not in seen:
            rules.append(r)

    # 权威规则先执行，长的 wrong 先执行（避免 "DPC" 把 "DPC harness" 拆坏）
    rules.sort(key=lambda r: (not r.authoritative, -len(r.wrong)))
    return rules


def apply_corrections(
    text: str, rules: list[Correction] | None = None, window: int = 12
) -> CorrectionResult:
    """对逐字稿应用修正，并返回命中统计。

    ## 带上下文规则的正确语义（这里踩过一次坑）

    第一版实现是"在匹配到的片段**内部**做替换"，但上下文正则匹配到的往往只是
    一个触发词（如 `包子`），而误识别词（如 `犯油`）在它**外面** —— 结果永远替换不到。
    正确的语义是"**上下文窗口**"：触发词前后 `window` 个字符范围内才做替换。
    这样既能避免在全篇误伤，又能真正命中目标。
    """
    rules = rules if rules is not None else load_corrections()
    hits: dict[tuple[str, str], int] = {}

    def _record(wrong: str, right: str, n: int) -> None:
        if n:
            key = (wrong, right)
            hits[key] = hits.get(key, 0) + n

    for rule in rules:
        ctx = rule.compile()
        if ctx is None:
            n = text.count(rule.wrong)
            if n:
                text = text.replace(rule.wrong, rule.right)
                _record(rule.wrong, rule.right, n)
            continue

        # 收集所有触发点，合并成不重叠的替换区间
        spans = [(m.start(), m.end()) for m in ctx.finditer(text)]
        if not spans:
            continue
        intervals: list[list[int]] = []
        for s, e in spans:
            lo, hi = max(0, s - window), min(len(text), e + window)
            if intervals and lo <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], hi)
            else:
                intervals.append([lo, hi])

        pieces: list[str] = []
        cursor = 0
        applied = 0
        for lo, hi in intervals:
            pieces.append(text[cursor:lo])
            chunk = text[lo:hi]
            applied += chunk.count(rule.wrong)
            pieces.append(chunk.replace(rule.wrong, rule.right))
            cursor = hi
        pieces.append(text[cursor:])
        text = "".join(pieces)
        _record(rule.wrong, rule.right, applied)

    notes = {r.wrong: r.note for r in rules}
    return CorrectionResult(
        text=text,
        hits=[
            CorrectionHit(wrong=w, right=r, count=c, note=notes.get(w, ""))
            for (w, r), c in sorted(hits.items(), key=lambda kv: -kv[1])
        ],
    )


def ensure_default_file(path: Path | None = None) -> Path:
    """把内置规则写成一份可编辑的初始文件（幂等）。"""
    path = path or DEFAULT_PATH
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "_comment": (
            "热词修正表：字幕与 ASR 共用。wrong 为误识别结果，right 为正确写法；"
            "context 可选，是限定替换范围的正则；authoritative=true 表示来源权威。"
        ),
        "corrections": [
            {
                "wrong": c.wrong,
                "right": c.right,
                "context": c.context,
                "note": c.note,
                "authoritative": c.authoritative,
            }
            for c in BUILTIN
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
