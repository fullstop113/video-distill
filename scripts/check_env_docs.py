"""检查 .env.example 与代码实际读取的环境变量是否一致。

## 为什么需要它

文档和代码会漂移。实测发现：`VD_PUBLIC_BASE_URL` 是云 ASR 的**必需前置**
（不配就没法用），却**根本没写进 `.env.example`** —— 别人照文档配完，
一跑无字幕视频就报"未配置"，而他按文档检查过一遍，会以为是自己填错了。

这类漂移靠"记得更新文档"是防不住的，只能靠检查。

## 用法

    python scripts/check_env_docs.py          # 报告差异，有缺失则退出码 1
    python scripts/check_env_docs.py -v       # 附带每个变量的读取位置
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ENV_EXAMPLE = ROOT / ".env.example"

# 这些是"代码里读了但不必写进 .env.example"的
# —— 属于外部环境/测试注入/操作系统自带，不是用户要配的东西。
IGNORE = {
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy",
    "PYTHONIOENCODING", "TZ",
    # 操作系统自带的临时目录变量：脚本里读它们只是为了把大文件
    # 放到系统临时目录，用户不需要也无法"配置"它们。
    "TEMP", "TMP", "TMPDIR",
}


def collect_code_vars() -> dict[str, set[str]]:
    """扫描源码，找出所有被读取的环境变量名。"""
    found: dict[str, set[str]] = {}
    patterns = [
        re.compile(r"""getenv\(\s*["']([A-Z][A-Z0-9_]+)["']"""),
        re.compile(r"""environ(?:\.get)?[\(\[]\s*["']([A-Z][A-Z0-9_]+)["']"""),
    ]
    files = list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for pat in patterns:
            for m in pat.finditer(text):
                found.setdefault(m.group(1), set()).add(path.name)
    return {k: v for k, v in found.items() if k not in IGNORE}


def collect_example_vars() -> set[str]:
    """找出 .env.example 里出现的变量名（含被注释掉的行）。"""
    if not ENV_EXAMPLE.exists():
        return set()
    out = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]+)=", line)
        if m:
            out.add(m.group(1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="显示每个变量的读取位置")
    args = ap.parse_args()

    code = collect_code_vars()
    example = collect_example_vars()
    missing = sorted(set(code) - example)
    extra = sorted(example - set(code))

    print("=" * 68)
    print(f"代码读取 {len(code)} 个环境变量，.env.example 记录了 {len(example)} 个")
    print("=" * 68)

    ok = True

    if missing:
        ok = False
        print()
        print(f"❌ 代码用到但 .env.example 没写（{len(missing)} 个）")
        print("   别人照文档配完会漏掉它们，然后遇到莫名其妙的报错：")
        for k in missing:
            where = ", ".join(sorted(code[k]))
            print(f"     · {k:32s}" + (f"  ← {where}" if args.verbose else ""))
    else:
        print()
        print("✅ 代码读取的变量全部已文档化")

    if extra:
        print()
        print(f"⚠️  .env.example 写了但代码没读（{len(extra)} 个）—— 可能是改名后的残留：")
        for k in extra:
            print(f"     · {k}")

    print()
    print("=" * 68)
    print("一致 ✅" if ok else "存在缺失 ❌  请补进 .env.example")
    print("=" * 68)
    if not ok:
        print("\n补的时候记得写清：这个变量**干什么用**、**不填会怎样**、"
              "**怎么生成**。")
        print("反面教材是原来的 VD_SECRET_KEY：只写了一行生成命令，"
              "没说它是什么、丢了会怎样。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
