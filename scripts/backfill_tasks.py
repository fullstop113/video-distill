"""一次性回填：给已有任务记录补上标题与产物清单。

## 为什么需要它

`title` / `artifacts` 是后加的列。旧行虽然通过 ALTER 有了这两列，
但值为空 —— 列表页会显示"（旧任务，无标题）"，"我的产物"页也列不出东西。

数据其实**都在**：`result` JSON 里有 `title`，产物文件在 output 目录里，
只是当时没有冗余存这两列。所以这是纯回填，不需要重跑任何任务。

用法：
    python scripts/backfill_tasks.py            # 只看将要改什么
    python scripts/backfill_tasks.py --apply    # 真正写入
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from vd import tasks  # noqa: E402

OUT_DIR = ROOT / "output"
KINDS = {"summary": "summary", "transcript": "transcript"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    args = ap.parse_args()

    rows: list[tasks.Task] = []
    # 分页拉全量：这个脚本不追求性能，但要求不漏
    page = 1
    while True:
        chunk, total, pages = tasks.page_tasks(limit=200, page=page)
        rows.extend(chunk)
        if page >= pages:
            break
        page += 1

    print(f"共 {len(rows)} 条任务记录\n")
    fixed_title = fixed_art = missing_file = 0
    for t in rows:
        title = t.title or (t.result or {}).get("title") or ""
        arts: list[dict] = list(t.artifacts or [])
        if not arts and t.status is tasks.TaskStatus.DONE:
            # 从 result 里取文件名，再到 output 目录确认存在并取大小
            r = t.result or {}
            for key, kind in KINDS.items():
                name = r.get(f"{key}_file") or ""
                if not name:
                    continue
                path = OUT_DIR / name
                if path.exists():
                    arts.append({"name": name, "kind": kind,
                                 "size": path.stat().st_size})
                else:
                    missing_file += 1
                    print(f"  ⚠️ {t.bvid} 记录里的 {name} 已不在 output 目录")

        need_title = bool(title) and not t.title
        need_art = bool(arts) and not t.artifacts
        if need_title or need_art:
            flag = "写入" if args.apply else "待写"
            print(f"  [{flag}] {t.id} {t.bvid} title={title[:32]!r} "
                  f"产物={len(arts)}")
            if args.apply:
                if need_title:
                    tasks.set_title(t.id, title)
                    fixed_title += 1
                if need_art:
                    tasks.set_artifacts(t.id, arts)
                    fixed_art += 1

    print(f"\n标题回填 {fixed_title} 条，产物回填 {fixed_art} 条，"
          f"缺失文件 {missing_file} 个")
    if not args.apply:
        print("（这是预览模式，加 --apply 才真正写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
