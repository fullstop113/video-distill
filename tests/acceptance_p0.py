"""P0 验收：逐条核对目标里的每一项，打印证据。

设计原则：**不重复实现**被测逻辑，而是调用真实代码路径并断言其行为，
再把运行环境里除脚本本身之外的一切当作黑盒来检查（子进程、真实接口、磁盘文件）。

用法：
    python tests/acceptance_p0.py          # 离线部分 + 真实接口检查
    python tests/acceptance_p0.py --offline  # 只跑离线部分（不联网）
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
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

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001
        FAIL.append(f"{name} —— {type(exc).__name__}: {exc}")
        print(f"  ❌ {name}\n       {type(exc).__name__}: {exc}")
        return
    if detail is False:
        FAIL.append(f"{name} —— 返回 False")
        print(f"  ❌ {name}")
        return
    PASS.append(name)
    print(f"  ✅ {name}" + (f"\n       {detail}" if isinstance(detail, str) else ""))


def skip(name: str, why: str) -> None:
    SKIP.append(f"{name} —— {why}")
    print(f"  ⏭️  {name}（{why}）")


# ==================== 目标项 1：项目专属 venv ====================


def t_venv_isolated():
    assert sys.prefix != sys.base_prefix, "不在 venv 中"
    assert ".venv" in sys.executable, sys.executable
    py = subprocess.run(
        [sys.executable, "-c", "import sys;print(sys.version.split()[0], sys.prefix)"],
        capture_output=True, text=True, timeout=60,
    )
    assert py.returncode == 0, py.stderr
    return f"解释器 {py.stdout.strip()}"


def t_global_python_untouched():
    """目标要求"项目专属"，所以要证明全局 Python 没被污染。"""
    candidates = [
        Path(r"D:\softWare\python313\python.exe"),
        Path(sys.base_prefix) / "python.exe",
    ]
    exe = next((p for p in candidates if p.exists()), None)
    if exe is None:
        return "找不到全局解释器，跳过（不影响隔离性结论）"
    out = subprocess.run(
        [str(exe), "-c", "import torch;print(torch.__version__)"],
        capture_output=True, text=True, timeout=180,
    )
    ver = (out.stdout or "").strip() or (out.stderr or "").strip().splitlines()[-1:]
    ver = ver if isinstance(ver, str) else " ".join(ver)
    assert "+cu124" not in ver, f"全局解释器竟被装了 CUDA torch：{ver}"
    return f"全局 {exe} 里 torch 仍是 {ver[:40] or '未安装'}（未被本项目污染）"


def t_cuda_torch():
    import torch

    assert torch.cuda.is_available(), "cuda_avail=False"
    assert "+cu" in torch.__version__, torch.__version__
    a = torch.randn(1024, 1024, device="cuda")
    b = torch.randn(1024, 1024, device="cuda")
    torch.cuda.synchronize()
    _ = a @ b
    torch.cuda.synchronize()
    props = torch.cuda.get_device_properties(0)
    return (
        f"torch {torch.__version__} / CUDA {torch.version.cuda} / "
        f"{props.name} {props.total_memory / 1024**3:.1f}GB / GPU 算子实测通过"
    )


def t_imageio_ffmpeg():
    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    assert Path(exe).exists(), exe
    out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return f"{Path(exe).name} 可执行：{out.stdout.splitlines()[0][:52]}"


def t_reproducible_install():
    """可复现性：下载器脚本存在，requirements 记录了实测正确的镜像与版本组合。"""
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "mirror.sjtu.edu.cn" in req, "requirements 未记录实测最快的镜像"
    assert "2.6.0" in req, "requirements 未记录唯一可用的版本组合"
    assert "no-deps" in req, "requirements 未记录防止 CPU 版覆盖的 --no-deps"
    fetched = ROOT / "scripts" / "fetch_wheel.ps1"
    assert fetched.exists(), "缺少可续传下载器"
    body = fetched.read_text(encoding="utf-8")
    # 断言的是**实际传给 curl 的参数**，不是注释里提到的名字：
    # 文件头注释里会引用这个坏参数作为反面教材，不该因此判失败。
    in_args = body.split("$curlArgs = @(", 1)[-1].split(")", 1)[0]
    assert "--speed-limit" not in in_args, f"下载器仍在传速度下限开关：{in_args}"
    assert "-C" in in_args, "下载器未启用续传"
    # 真实踩过的坑：走 301 时只取第一个 content-length 会恒为 0
    assert "Matches(" in body, "Content-Length 解析仍只取首个匹配（301 下会失效）"
    return "requirements + fetch_wheel.ps1 可复现；curl 参数无速度下限；Content-Length 解析已修正"


# ==================== 目标项 2：扫码登录 ====================


def t_credentials_privacy():
    """凭证必须真的加密，且不落在版本库里。"""
    storage = importlib.import_module("vd.storage")
    uids = storage.list_uids()
    assert uids, "没有已保存的凭证"
    path = storage.CRED_DIR / f"{uids[0]}.enc"
    raw = path.read_bytes()
    for needle in (b"SESSDATA", b"bili_jct", b"DedeUserID"):
        assert needle not in raw, f"密文里出现明文 {needle!r}"
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gi and "data/" in gi, ".gitignore 未排除凭证目录"
    return f"{len(uids)} 份凭证，磁盘上无明文；.env 与 data/ 已 gitignore"


def t_login_flow_implemented():
    login = importlib.import_module("vd.login")
    states = {s.value for s in login.QrState}
    assert states == {"86101", "86090", "0", "86038"}, states
    return "四态齐全：86101 未扫码 / 86090 待确认 / 0 成功 / 86038 失效"


def t_real_session_alive():
    """真实接口校验：凭证仍然有效，且服务端确认是登录态。"""
    from vd.bilibili import BiliClient
    from vd.storage import list_uids, load_credentials

    creds = load_credentials(list_uids()[0])
    nav = BiliClient(creds).nav()
    assert nav.get("isLogin") is True, nav
    return f"UID={creds.uid} uname={nav.get('uname')} isLogin=True"


def t_owner_identity_resolved():
    """管理员身份解析：大号与小号都在管理员集合内，都不限配额。

    注意这里**不再断言**"磁盘上的凭据是主人账号"——按大号/小号分离的设计，
    磁盘上只应该有操作号（小号）的凭据，大号凭据必须不在。
    """
    from vd.credentials import admin_uids, operator_uid, owner_uid
    from vd.quota import QuotaRule

    owner, op = owner_uid(), operator_uid()
    assert owner, "VD_OWNER_BILI_UID 未配置"
    assert op, "VD_OPERATOR_BILI_UID 未配置"
    admins = admin_uids()
    assert owner in admins, f"大号 {owner} 不在管理员集合 {sorted(admins)}"
    assert op in admins, f"操作号 {op} 不在管理员集合，会被按访客限流"
    for uid in (owner, op):
        rule = QuotaRule.for_uid(uid)
        assert rule.unlimited, f"管理员 {uid} 竟未获得不限配额"
    assert not QuotaRule.for_uid("999999999").unlimited, "非管理员竟不限配额"
    return f"管理员={sorted(admins)} 全部不限配额；非管理员仍受 3 次/3 小时约束"


def t_operator_separation():
    """大号/小号分离：管理员集合内的账号都不限配额，但取数只允许操作号。"""
    from vd.credentials import (
        CredentialPolicyError,
        admin_uids,
        load_operator_credentials,
        operator_uid,
        owner_uid,
    )
    from vd.storage import list_uids

    owner, op = owner_uid(), operator_uid()
    assert owner, "未配置管理员大号"
    assert op, "未配置操作号"
    assert op != owner, "操作号与管理员大号相同 → 大号会暴露给抓取自动化"
    assert op in admin_uids(), "操作号应为管理员，否则会被按访客限流"

    creds = load_operator_credentials()
    assert creds.uid == op, f"取数拿到的是 UID={creds.uid}，应为操作号 {op}"

    # 最关键的一条：大号凭据不得在磁盘上
    on_disk = set(list_uids())
    assert owner not in on_disk, (
        f"管理员大号 {owner} 的凭据竟还在磁盘上（{sorted(on_disk)}）→ "
        "隔离失效，请用 python scripts/login.py --logout 清理后重新用小号登录"
    )
    return (
        f"操作号 UID={op} 凭据就绪；磁盘仅有 {sorted(on_disk)}；"
        f"大号 UID={owner} 凭据不在磁盘、不参与取数"
    )


def t_scripts_do_not_pick_first_credential():
    """静态检查：取数脚本必须走白名单，不能出现"取第一个凭证"。

    例外：`login.py` 是登录/管理工具，它**需要**列出全部已保存凭据
    （包括非操作号的），好让使用者发现并清理不该留在机器上的凭据。
    它不取数，因此不适用本条。
    """
    exempt = {"login.py"}
    offenders = []
    for path in (ROOT / "scripts").glob("*.py"):
        if path.name in exempt:
            continue
        text = path.read_text(encoding="utf-8")
        if "load_credentials(" in text and "load_operator_credentials" not in text:
            offenders.append(path.name)
    assert not offenders, f"这些脚本仍在直接取任意凭证：{offenders}"
    return f"全部取数脚本均经 vd.credentials 白名单（{sorted(exempt)} 为管理工具，已豁免）"


# ==================== 目标项 3：取数探针与 R1 ====================


def t_r1_live_guard():
    """真实接口上验证：匿名下拉字幕必须报「无法判定」，不能报「无字幕」。"""
    from vd.bilibili import BiliClient, NoSubtitleError, NotLoggedInError

    client = BiliClient(None)
    assert not client.is_logged_in
    data = client._get_json(
        "https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
    )["data"]
    info = client.video_info((data.get("list") or [{}])[0]["bvid"])
    try:
        client.subtitle_tracks(info)
    except NotLoggedInError:
        return "匿名请求被正确判为 NotLoggedInError（R1 守住）"
    except NoSubtitleError:
        raise AssertionError("匿名被误判为「确认无字幕」—— R1 陷阱复现！")
    raise AssertionError("匿名竟拿到了字幕轨道，登录态判定逻辑需复核")


def t_logged_in_route_works():
    """登录态下真实跑通一次字幕取数，并校验逐字稿长度下限。"""
    from vd.bilibili import BiliClient
    from vd.storage import list_uids, load_credentials

    creds = load_credentials(list_uids()[0])
    client = BiliClient(creds)
    data = client._get_json(
        "https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
    )["data"]
    checked = 0
    for item in (data.get("list") or [])[:8]:
        info = client.video_info(item["bvid"])
        try:
            tracks = client.subtitle_tracks(info)
        except Exception:  # noqa: BLE001
            continue
        cues = client.fetch_cues(tracks[0])
        text = "".join(c.get("content", "") for c in cues)
        assert len(text) >= 80, f"字幕正文过短：{len(text)} 字"
        checked += 1
        if checked >= 2:
            break
    assert checked >= 1, "8 个视频里没有任何一个能取到有效字幕（接口可能已变）"
    return f"登录态下成功取得 {checked} 条有效字幕，长度下限校验通过"


def t_probe_scripts_exist():
    expected = {
        "raw_probe.py", "transcript_probe.py", "hit_rate.py",
        "asr_probe.py", "asr_quality.py", "check_env.py", "login.py", "login_qr.py",
    }
    have = {p.name for p in (ROOT / "scripts").glob("*.py")} | {"fetch_wheel.ps1"}
    missing = expected - have
    assert not missing, f"缺少探针：{missing}"
    return f"{len(expected)} 个探针脚本齐全"


# ==================== 目标项 4：字幕命中率已实测 ====================


def t_hit_rate_measured():
    path = ROOT / "data" / "hit_rate_result.json"
    assert path.exists(), "未找到命中率实测结果"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["total"] >= 20, f"样本数只有 {data['total']}"
    assert data["counts"].get("not_logged_in", 0) == 0, "存在无法判定样本，结果不可信"
    # 逐条复核 outcome 与字符数自洽
    for r in data["results"]:
        if r["outcome"] == "ok":
            assert r["char_count"] >= 20, f"{r['bvid']} 标记命中但字数不足"
            assert r["cue_count"] > 0, f"{r['bvid']} 标记命中但无字幕条"
    return (
        f"样本 {data['total']}，命中 {data['ok']}，命中率 {data['hit_rate']}%，"
        f"无法判定 {data['counts'].get('not_logged_in', 0)}，风控 {data['counts'].get('risk', 0)}"
    )


def t_hit_rate_conclusion_recorded():
    doc = (ROOT / "docs" / "p0-findings.md").read_text(encoding="utf-8")
    assert "60%" in doc, "文档未记录命中率结论"
    assert "AI 字幕" in doc and "人工字幕 0" in doc, "未记录「全是 AI 字幕」这一发现"
    return "p0-findings.md 已记录命中率、AI/人工构成与策略结论"


def t_asr_rtf_measured():
    path = ROOT / "data" / "asr_quality_result.json"
    assert path.exists(), "未找到 ASR 实测结果"
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["rtf"] < 1, f"RTF={d['rtf']} 未跑在 GPU 上？"
    assert d["chars"] > 100, f"转写字数过少：{d['chars']}"
    return (
        f"{d['audio_seconds']}s 音频转写 {d['asr_seconds']}s，RTF={d['rtf']}，"
        f"{d['chars']} 字，device={d['device']}"
    )


# ==================== 目标项 5：配额设计落地 ====================


def t_quota_rule_matches_spec():
    """规格：主人不限；他人每日 3 次且累计 ≤ 3 小时。"""
    from vd.quota import QuotaRule

    guest = QuotaRule.for_uid("999999")
    assert not guest.unlimited
    assert guest.daily_videos == 3, guest
    assert guest.daily_seconds == 10800, guest
    owner = QuotaRule.for_uid(os.environ["VD_OWNER_BILI_UID"])
    assert owner.unlimited
    return f"访客 {guest.daily_videos} 次/{guest.daily_seconds}s；主人 unlimited={owner.unlimited}"


def t_quota_two_independent_gates():
    """两道闸门必须独立：否则「累计 ≤3h」永远不会先于「3 次」触发。"""
    import tempfile

    quota = importlib.import_module("vd.quota")
    with tempfile.TemporaryDirectory() as td:
        orig = quota.DB_PATH
        quota.DB_PATH = Path(td) / "q.sqlite3"
        try:
            uid = "gate-test"
            # 第 1 个视频就吃满 3 小时
            assert quota.check(uid, planned_seconds=10800).allowed
            quota.commit(uid, "BV1", 10800)
            st = quota.check(uid, planned_seconds=60)
            assert not st.allowed, "时长已满却仍放行"
            assert st.used_videos == 1, f"此时次数才 1，必须是时长闸门挡住，实际 {st.used_videos}"
        finally:
            quota.DB_PATH = orig
    return "3 小时被单个视频占满时，第 2 个即被拒（次数才 1）→ 时长闸门独立生效"


def t_quota_is_per_bili_uid():
    """换浏览器/清 Cookie 不能绕过：配额绑在 B 站 UID 上。"""
    import tempfile

    quota = importlib.import_module("vd.quota")
    with tempfile.TemporaryDirectory() as td:
        orig = quota.DB_PATH
        quota.DB_PATH = Path(td) / "q.sqlite3"
        try:
            for i in range(3):
                quota.commit("A", f"BV{i}", 60)
            assert not quota.check("A", 60).allowed, "A 用完 3 次后仍放行"
            assert quota.check("B", 60).allowed, "另一个账号被连坐"
        finally:
            quota.DB_PATH = orig
    return "A 用满被拒，B 不受影响（无连坐）"


def t_quota_enforced_before_audio():
    """配额判定必须发生在取音频之前 —— 用代码顺序证明，不靠叙述。"""
    src = (ROOT / "scripts" / "transcript_probe.py").read_text(encoding="utf-8")
    i_quota = src.index("check(creds.uid")
    i_audio = src.find("audio_url(")
    assert i_audio == -1 or i_quota < i_audio, "配额判定晚于音频获取"
    # 也必须早于字幕/ASR 的实际取数
    i_tracks = src.index("subtitle_tracks(")
    assert i_quota < i_tracks, "配额判定晚于字幕取数"
    return "transcript_probe.py 中 check() 先于 subtitle_tracks()/audio_url()"


def t_transcript_no_empty_output():
    """宁报错不产空文件：产物必须存在且非空，且带时间戳锚点。"""
    md = ROOT / "output" / "BV1NXH661EpG.md"
    assert md.exists(), "没有已生成的逐字稿产物"
    text = md.read_text(encoding="utf-8")
    assert len(text) > 500, f"产物过短：{len(text)} 字"
    assert "## 逐字稿" in text
    stamps = text.count("**[0")
    assert stamps > 50, f"时间戳锚点过少：{stamps}"
    return f"产物 {len(text)} 字，{stamps} 个时间戳锚点"


# ==================== 目标项 6：代码骨架可运行 ====================


def t_tests_green():
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--no-header"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=300,
    )
    assert out.returncode == 0, out.stdout[-800:]
    tail = [ln for ln in out.stdout.strip().splitlines() if ln.strip()][-1]
    return f"pytest：{tail}"


def t_modules_importable():
    from vd import bilibili, login, quota, storage  # noqa: F401

    return "vd.bilibili / vd.login / vd.quota / vd.storage 均可导入"


def t_no_secret_in_repo_files():
    """凭证与密钥不得出现在任何会被提交的文件里。"""
    bad = []
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "sk-" in text and "sk-xxx" not in text:
            bad.append(str(path.relative_to(ROOT)))
    assert not bad, f"这些文件疑似含明文 key：{bad}"
    env = (ROOT / ".env").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-" in env
    return ".env 含真实 key（已 gitignore），src/ 与 scripts/ 中无明文 key"


def main() -> int:
    global SKIP
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("P0 目标验收（逐条核对）")
    print("=" * 70)

    print("\n【1】项目专属 venv 与依赖")
    check("运行在项目专属 venv 中", t_venv_isolated)
    check("全局 Python 未被污染", t_global_python_untouched)
    check("CUDA 版 torch 可用（R2）", t_cuda_torch)
    check("imageio-ffmpeg 自带 ffmpeg 可执行（R6）", t_imageio_ffmpeg)
    check("安装过程可复现", t_reproducible_install)

    print("\n【2】B 站扫码登录与凭证")
    check("扫码登录四态已实现", t_login_flow_implemented)
    check("凭证加密落盘、无明文（R11）", t_credentials_privacy)
    if args.offline:
        skip("真实会话仍有效", "offline 模式")
        skip("主人身份解析正确", "offline 模式")
        skip("大号/小号分离生效", "offline 模式")
    else:
        check("真实会话仍有效", t_real_session_alive)
        check("主人身份解析正确（配额不限）", t_owner_identity_resolved)
        check("大号/小号分离生效", t_operator_separation)
    check("取数脚本均走凭据白名单", t_scripts_do_not_pick_first_credential)

    print("\n【3】取数探针与 R1 陷阱防护")
    check("探针脚本齐全", t_probe_scripts_exist)
    if args.offline:
        skip("匿名 R1 守卫（真实接口）", "offline 模式")
        skip("登录态字幕取数（真实接口）", "offline 模式")
    else:
        check("匿名 R1 守卫（真实接口）", t_r1_live_guard)
        check("登录态字幕取数（真实接口）", t_logged_in_route_works)

    print("\n【4】字幕命中率已实测")
    check("命中率结果可复核", t_hit_rate_measured)
    check("命中率结论已入档", t_hit_rate_conclusion_recorded)
    check("ASR 兜底可用性已实测", t_asr_rtf_measured)

    print("\n【5】配额设计已落地")
    check("配额规格与要求一致", t_quota_rule_matches_spec)
    check("两道闸门相互独立", t_quota_two_independent_gates)
    check("按 B 站账号计数、无连坐", t_quota_is_per_bili_uid)
    check("配额判定早于音频获取", t_quota_enforced_before_audio)

    print("\n【6】代码骨架可运行")
    check("模块可导入", t_modules_importable)
    check("回归测试全绿", t_tests_green)
    check("产物非空且带时间戳锚点", t_transcript_no_empty_output)
    check("无明文密钥进入代码库", t_no_secret_in_repo_files)

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)}   失败 {len(FAIL)}   跳过 {len(SKIP)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print(f"  - {f}")
    if SKIP:
        print("\n跳过项：")
        for s in SKIP:
            print(f"  - {s}")
    print("=" * 70)
    print("VERDICT " + ("PASS" if not FAIL else "FAIL"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
