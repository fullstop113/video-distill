"""安全地更新 .env 里的某个 Key —— **不打印、不回显、不进任何日志**。

## 为什么需要这个脚本

"把新 Key 写进 .env"看起来一行命令就够，但直接写命令有三个坑：

1. **别把 Key 写进命令行参数** —— 它会进 PowerShell 历史、
   可能进进程列表（`Get-Process` 能看到命令行）。所以本脚本
   **从 stdin 读**，不接受参数。
2. **别用 shell 重定向拼字符串** —— PowerShell 的引号转义很容易把
   Key 写坏（我这个会话里已经被引号转义坑过好几次了），
   而且写坏了你很难发现（API 报"无效 Key"，你会以为是 Key 本身的问题）。
3. **别让它出现在终端回显里** —— 如果你旁边有人，或者你在录屏。

## 用法

    python scripts/set_key.py DASHSCOPE_API_KEY

然后它会提示你粘贴，你粘贴后按回车。**屏上不会显示任何字符**。

写法上刻意做了这些保证：
  · 只改 `.env`，不碰其它文件；
  · 改之前备份为 `.env.bak`（只在首次创建，不覆盖已有备份）；
  · 写完立刻回读校验，确认真的写进去了且长度正确；
  · 全程不打印 Key 的任何字符，只打印长度与前缀类别。
"""

from __future__ import annotations

import getpass
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ENV = ROOT / ".env"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        print("用法：python scripts/set_key.py <变量名>")
        print("例如：python scripts/set_key.py DASHSCOPE_API_KEY")
        return 2

    name = sys.argv[1].strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        print(f"❌ 变量名不合法：{name!r}")
        return 2

    if not ENV.exists():
        print(f"❌ 找不到 {ENV}（应先把 .env.example 复制为 .env）")
        return 2

    print(f"即将更新 {ENV} 里的 {name}")
    print("请粘贴新值后按回车（**输入不会显示在屏幕上**）：")
    try:
        # getpass 不回显 —— 这是关键，避免旁人看到 / 避免录进终端记录
        value = getpass.getpass("> ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，未做任何修改。")
        return 1

    if not value:
        print("❌ 输入为空，未做任何修改。")
        return 1

    # 基本形态检查：只提示，不阻断（不同服务格式不同）
    warn = ""
    if name.endswith("_API_KEY") and not value.startswith("sk-"):
        warn = "  ⚠️ 这个变量通常以 sk- 开头，请确认没粘贴错"

    lines = ENV.read_text(encoding="utf-8").splitlines()
    hit = False
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(name)}\s*=", line):
            lines[i] = f"{name}={value}"
            hit = True
            break
    if not hit:
        lines.append(f"{name}={value}")

    # 备份：只备份一次，不覆盖已有备份（避免把好版本盖掉）
    backup = ENV.with_suffix(".env.bak")
    if not backup.exists():
        shutil.copy2(ENV, backup)
        print(f"已备份原文件到 {backup.name}")

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 回读校验：确认真的写进去了，且不是被截断/转义坏掉
    check = ""
    for line in ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"\s*{re.escape(name)}\s*=\s*(.*)$", line)
        if m:
            check = m.group(1)
            break

    if check != value:
        print("❌ 回读校验失败：写入的值与输入不一致（可能被转义破坏）")
        print(f"   输入长度 {len(value)}，读回长度 {len(check)}")
        print(f"   备份在 {backup.name}，可用它恢复")
        return 1

    print(f"✅ 已更新 {name}")
    print(f"   长度 {len(value)}，写入与回读一致")
    if warn:
        print(warn)
    print()
    print("下一步：验证这个 Key 真的能用 ——")
    if "DASHSCOPE" in name:
        print("    python scripts/verify_dashscope.py BV1NXH661EpG")
    else:
        print("    python scripts/check_env.py")
    print()
    print("注意：本脚本只改了本机 .env。控制台上那个旧 Key 需要你自己吊销。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
