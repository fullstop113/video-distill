"""P0 验收：环境自检。

每一项都对应方案里的一个风险编号，逐条打印证据而不是「应该没问题」。
退出码非 0 表示有硬阻塞项。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台默认 GBK，输不出 emoji / 部分符号 —— 强制 UTF-8，避免脚本
# 因为"打不出中文提示"而崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

HARD_FAIL = 0
SOFT_WARN = 0


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def warn(msg: str) -> None:
    global SOFT_WARN
    SOFT_WARN += 1
    print(f"  ⚠️  {msg}")


def bad(msg: str) -> None:
    global HARD_FAIL
    HARD_FAIL += 1
    print(f"  ❌ {msg}")


def main() -> int:
    import os

    print("=" * 66)
    print("video-distill 环境自检 (P0)")
    print("=" * 66)

    print("\n[1] Python 与解释器")
    print(f"  Python {platform.python_version()} @ {sys.executable}")
    if sys.version_info[:2] != (3, 13):
        warn(f"当前为 {sys.version_info[:2]}，方案按 3.13 验证过；其它版本也能跑。")
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        ok("运行在项目专属 venv 中（未污染全局 Python）")
    else:
        bad("不在 venv 中！全局环境已有 torch，会互相污染。请用 .venv\\Scripts\\python.exe")

    print("\n[2] R2 —— CUDA 版 torch（决定 ASR 是否可用）")
    try:
        import torch

        print(f"  torch {torch.__version__}")
        if torch.cuda.is_available():
            dev = torch.cuda.get_device_name(0)
            total = torch.cuda.get_device_properties(0).total_memory / 1024**3
            ok(f"CUDA 可用：{dev}（{total:.1f} GB）")
            # 真正跑一次算子才算验收通过
            t = torch.randn(512, 512, device="cuda")
            ok(f"GPU 算子实测通过（matmul 结果均值 {float((t @ t).mean()):.4f}）")
        else:
            bad(
                "torch 是 CPU 版或 CUDA 不可用 → 1 小时视频要转 2-3 小时，"
                "对多人 Web 服务不可接受（方案 §3.3）"
            )
    except ImportError:
        bad("未安装 torch")

    print("\n[3] torchaudio（FunASR 依赖）")
    try:
        import torchaudio

        ok(f"torchaudio {torchaudio.__version__}")
    except ImportError:
        bad("未安装 torchaudio")

    print("\n[4] R6 —— ffmpeg 可达性")
    exe = shutil.which("ffmpeg")
    if exe:
        ok(f"PATH 中的 ffmpeg: {exe}")
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        out = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=20)
        first = (out.stdout or "").splitlines()[0] if out.stdout else "?"
        ok(f"imageio-ffmpeg 自带二进制：{p}")
        ok(f"可执行：{first[:60]}")
    except ImportError:
        bad("未安装 imageio-ffmpeg（GitHub 不通，ffmpeg 必须走这个包）")
    except Exception as exc:  # noqa: BLE001
        bad(f"imageio-ffmpeg 装了但执行失败：{exc}")

    print("\n[5] 凭证与密钥（R11）")
    sec = os.getenv("VD_SECRET_KEY", "")
    if len(sec) >= 32:
        ok("VD_SECRET_KEY 已设置（密钥与数据分离成立）")
    else:
        bad("VD_SECRET_KEY 缺失或过短 → 无法加密保存 B 站凭证")
    if os.getenv("DEEPSEEK_API_KEY"):
        ok("DEEPSEEK_API_KEY 已设置（P2 用）")
    else:
        warn("DEEPSEEK_API_KEY 未设置（P0/P1 不需要，P2 之前必须补上）")

    print("\n[5b] 大号/小号分离（账号风险隔离）")
    from vd.credentials import (
        CredentialPolicyError,
        admin_uids,
        load_operator_credentials,
        operator_uid,
        owner_uid,
    )

    o, p = owner_uid(), operator_uid()
    print(f"  管理员（大号，凭据不落盘）VD_OWNER_BILI_UID = {o or '(未配置)'}")
    print(f"  操作号（小号，唯一可取数）VD_OPERATOR_BILI_UID = {p or '(未配置)'}")
    print(f"  管理员集合 VD_ADMIN_UIDS = {sorted(admin_uids()) or '(空)'}")
    if p and o and p == o:
        bad("操作号与管理员大号是同一个 UID → 大号会直接暴露给抓取自动化，隔离失效")
    elif p:
        ok("操作号与管理员大号已分离")
    else:
        warn("未配置操作号 → 无法取数（这是有意设计：宁可拒绝，不要静默用大号抓取）")

    try:
        from vd.storage import list_uids, load_credentials

        uids = list_uids()
        if not uids:
            warn("尚无 B 站凭证，运行 scripts/login.py 扫码登录（请用小号）")
        for u in uids:
            c = load_credentials(u)
            tag = "操作号" if u == p else "⚠️ 非操作号（不会被用于取数）"
            ok(f"已保存凭据可解密：UID={u} uname={c.uname or '(未补全)'} → {tag}")
        op_creds = load_operator_credentials(required=False)
        if op_creds is not None:
            ok(f"操作号凭据就绪：UID={op_creds.uid} uname={op_creds.uname}")
    except CredentialPolicyError as exc:
        warn(f"操作号凭据未就绪：{str(exc).splitlines()[0]}")
    except Exception as exc:  # noqa: BLE001
        bad(f"凭证模块异常：{type(exc).__name__}: {exc}")

    print("\n[6] R1 —— 字幕静默失败防护（回归测试）")
    try:
        from vd.bilibili import BiliClient, NoSubtitleError, NotLoggedInError, VideoInfo

        client = BiliClient(None)
        # 热门榜里挑一个真实存在的视频，验证匿名请求不会伪装成「无字幕」
        data = client._get_json(
            "https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
        )["data"]
        item = (data.get("list") or [{}])[0]
        if item.get("bvid"):
            info = client.video_info(item["bvid"])
            try:
                client.subtitle_tracks(info)
                ok("匿名居然拿到了字幕轨道（出乎预期，需复核登录态判定逻辑）")
            except NotLoggedInError:
                ok("匿名请求被正确判定为 not_logged_in，未误报为「无字幕」")
            except NoSubtitleError:
                bad("匿名被误判为「确认无字幕」→ R1 陷阱复现，判定逻辑有 bug")
            except Exception as exc:  # noqa: BLE001
                warn(f"匿名探测返回 {type(exc).__name__}: {exc}")
        else:
            warn("热门榜为空，跳过该回归测试")
    except Exception as exc:  # noqa: BLE001
        warn(f"匿名回归测试未执行：{type(exc).__name__}: {exc}")

    print("\n" + "=" * 66)
    print(f"硬阻塞项：{HARD_FAIL}    警告项：{SOFT_WARN}")
    print("=" * 66)
    return 1 if HARD_FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
