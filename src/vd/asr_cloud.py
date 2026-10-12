"""云 ASR —— 阿里云百炼 Paraformer（仅用于**没有站内字幕**的视频）。

## 为什么走云端而不是本地

实测（`scripts/measure_asr_memory.py`、`bench_asr_steady.py`）：
  · 本地 FunASR 峰值内存 **5.3 GB** → 服务器至少要 4核8G（450 元/年）
  · CPU 稳态 RTF ≈ 0.17（104 分钟视频约 18 分钟）
  · 而云端 **0.288 元/小时**，按实际用量付费，不为闲置买单

对"一个人偶尔用"的场景，云端明显更省。

## 一个必须绕开的坑：B 站 CDN 校验 Referer

实测（`scripts/probe_audio_fetch.py`）：
    裸请求 B 站音频链接            → HTTP 403
    带 Referer: bilibili.com      → HTTP 200
而阿里云 Paraformer **只接受公网可访问的 URL**（官方 FAQ 明确：
"不支持 Base64，不支持二进制流，也不支持本地文件"）。
所以让阿里云直接拉 B 站链接会得到 `FILE_403_FORBIDDEN`（实测过）。

因此流程必须是：
    下载音频 → 放到**我们自己的服务器**上临时托管 → 把那个 URL 交给 Paraformer

## 中转的实现

`AudioRelay` 把音频放到一个临时目录，并在 Web 层挂一个带随机 token 的路由。
token 是一次性的、结果取回后**立刻删除文件**——不给公网留可猜测的入口。

⚠️ 这要求服务器**有公网 IP**；本地开发时阿里云拉不到 `127.0.0.1`，
需要 `VD_PUBLIC_BASE_URL` 指向一个公网可达地址（或走内网穿透）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import os
import secrets
import shutil
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .bilibili import BiliClient
from .summarize import sanitize_no_proxy
from .transcript import Segment, Transcript, TranscriptSource

log = logging.getLogger(__name__)

BASE_API = "https://dashscope.aliyuncs.com/api/v1"
SUBMIT_PATH = "/services/audio/asr/transcription"

# 中转文件放这里（Web 层按 token 提供下载）
RELAY_DIR = Path(__file__).resolve().parents[2] / "data" / "relay"

# 结果 JSON 里 `sentences[].begin_time` 的单位是**毫秒**
MS = 1000.0


class AsrCloudError(RuntimeError):
    """云 ASR 的可预期失败（要展示给用户）。"""


class RelayUnavailable(AsrCloudError):
    """没有公网可达地址 —— 阿里云拉不到我们托管的音频。"""


@dataclass
class RelayTicket:
    """一次中转：本地文件 + 对外 URL。用完必须 release()。"""

    token: str
    path: Path
    public_url: str

    def release(self) -> None:
        """删除中转文件。**必须**在取回结果后调用（finally 里）。"""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            log.warning("清理中转文件失败 %s：%s", self.path, exc)


def public_base_url() -> str:
    """服务器对外的基地址（阿里云要能访问到）。

    本地开发时通常是空的 —— 那时云 ASR 不可用，应当**明确报错**，
    而不是提交一个阿里云拉不到的 `127.0.0.1` 链接然后拿到一个
    语焉不详的 `FILE_403_FORBIDDEN`（实测就是这样）。
    """
    return os.getenv("VD_PUBLIC_BASE_URL", "").strip().rstrip("/")


def relay_route_prefix() -> str:
    return "/relay"


def relay_ttl() -> int:
    """中转音频的最长存活时间（秒）。

    默认 2 小时，远大于实际需要（阿里云通常 1~3 分钟就取完了），
    这是**兜底**：万一进程异常退出、来不及清理，文件也不会长期挂在公网。

    想更保守就调小（如 600），代价是网络很慢时可能在中途过期。
    """
    return int(os.getenv("VD_RELAY_TTL_SECONDS", "7200"))


def publish(local_file: Path, ttl: int = 0) -> RelayTicket:
    """把本地音频放进中转目录，返回带随机 token 的对外 URL。

    token 用 `secrets` 生成：URL 会暴露在公网，必须不可猜测。
    `ttl=0` 表示用 `VD_RELAY_TTL_SECONDS` 的配置值。
    """
    base = public_base_url()
    if not base:
        raise RelayUnavailable(
            "未配置 VD_PUBLIC_BASE_URL —— 阿里云无法访问本机托管的音频。\n"
            "请把它设为服务器**公网可达**的地址（例如 https://your-host），"
            "或在本地开发时改用其它可达方式。"
        )
    if not ttl:
        ttl = relay_ttl()
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24).replace("-", "").replace("_", "")
    suffix = local_file.suffix or ".m4s"
    dest = RELAY_DIR / f"{token}{suffix}"
    # 用 move 而不是 copy：音频可能有几十 MB，复制一份纯属浪费
    try:
        shutil.move(str(local_file), str(dest))
    except OSError:
        shutil.copy2(local_file, dest)
    _remember(token, dest, ttl)
    return RelayTicket(token=token, path=dest,
                       public_url=f"{base}{relay_route_prefix()}/{token}")


# ---------------------------------------------------------------- token 注册表
#
# ## 为什么必须落盘（这是一个真实的架构 bug 的修复）
#
# 第一版把 token 存在**进程内字典**里。实测立刻暴露：
#
#     verify 脚本进程：publish() 写自己的内存
#     服务进程：        /relay 路由查自己的内存 → 空 → 403
#
# 阿里云因此拿到 `FILE_403_FORBIDDEN`（服务日志原文：
# "严格模式：无进行中的转写任务，拒绝中转请求"）。
#
# **发布 token 的进程和响应 /relay 的进程不是同一个** ——
# 生产环境里 worker 与服务同进程时侥幸能跑，但服务一重启就丢，
# 而且任何"离线脚本 + 服务"的组合都会坏。
#
# 所以改成 SQLite：跨进程共享、重启不丢、并发安全。

REGISTRY_DB = RELAY_DIR / "registry.sqlite3"

_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_token (
    token      TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relay_expires ON relay_token(expires_at);
"""


def _registry() -> sqlite3.Connection:
    """打开注册表连接。

    ⚠️ 两个坑都在这里：
      · 必须设 `row_factory = sqlite3.Row`，否则按列名取值会抛
        `TypeError: tuple indices must be integers`（踩过）；
      · 调用方用 `with closing(_registry()) as conn:` ——
        sqlite3 的 `with` 只管事务、不关连接（同 vd.quota 记过的坑）。
    """
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REGISTRY_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_REGISTRY_SCHEMA)
    return conn


def _remember(token: str, path: Path, ttl: int) -> None:
    now = time.time()
    with closing(_registry()) as conn:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO relay_token(token, path, expires_at,"
                " created_at) VALUES(?,?,?,?)",
                (token, str(path), now + ttl, now),
            )


def resolve(token: str) -> Path | None:
    """Web 层用：token → 文件路径（过期即失效）。

    ⚠️ 必须能**跨进程**读到 —— 见上面"为什么必须落盘"。
    """
    with closing(_registry()) as conn:
        row = conn.execute(
            "SELECT path, expires_at FROM relay_token WHERE token=?", (token,)
        ).fetchone()
    if not row:
        return None
    path, expires = Path(row[0]), float(row[1])
    if time.time() > expires or not path.exists():
        forget(token)
        return None
    return path


def forget(token: str) -> None:
    with closing(_registry()) as conn:
        with conn:
            conn.execute("DELETE FROM relay_token WHERE token=?", (token,))


def has_pending() -> bool:
    """当前是否有"正在等阿里云来取音频"的中转。

    供 `/relay` 的**严格模式**使用（`VD_RELAY_STRICT=1`）：
    没有进行中的转写任务时，中转路由一律 403 —— 这样随手扫描的请求
    拿不到任何东西。

    注意这不是安全边界：真正的防护是 token 不可猜测 + 一次性 + 短命。
    它只是把"可被访问的时间窗口"从"文件存在期间"收窄到"任务进行中"。
    """
    gc_tokens()
    with closing(_registry()) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM relay_token").fetchone()
    return bool(row and row["n"])


def gc_tokens() -> int:
    """清掉过期 token 及其文件。返回清理数量。"""
    now = time.time()
    with closing(_registry()) as conn:
        rows = conn.execute(
            "SELECT token, path FROM relay_token WHERE expires_at < ?", (now,)
        ).fetchall()
        with conn:
            conn.execute("DELETE FROM relay_token WHERE expires_at < ?", (now,))
    for r in rows:
        try:
            Path(r["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return len(rows)


# ---------------------------------------------------------------- 调用

def _headers() -> dict[str, str]:
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise AsrCloudError("未配置 DASHSCOPE_API_KEY，无法使用云 ASR")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }


@dataclass
class AsrCloudResult:
    transcript: Transcript
    # 阿里云返回的**实际计费口径**：被判为语音内容的时长（秒）
    speech_seconds: float
    # 音频总时长（秒），用于和视频时长交叉校验
    audio_seconds: float
    task_id: str
    raw: dict = field(default_factory=dict)


def transcribe_url(
    audio_url: str,
    *,
    bvid: str,
    title: str,
    info,
    poll_interval: float = 5.0,
    timeout: float = 1800.0,
    vocabulary_id: str = "",
) -> AsrCloudResult:
    """提交一个公网 URL 给 Paraformer，轮询到完成后取回结果。

    官方文档要点：
      · 提交需要 `X-DashScope-Async: enable`，否则提交不了
      · 结果 JSON 存在 `transcription_url`，**有效期 24 小时**，必须及时落盘
      · `sentences[].begin_time / end_time` 是**毫秒**
      · `words[]` 带词级时间戳与标点 —— 比本地 FunASR 还多一层
    """
    sanitize_no_proxy()
    model = os.getenv("DASHSCOPE_MODEL", "paraformer-v2").strip()
    params: dict = {
        "channel_id": [0],
        "language_hints": ["zh", "en"],
        # 让识别结果与音频对齐，时间戳才可用于跳转
        "timestamp_alignment_enabled": True,
        "disfluency_removal_enabled": False,
    }
    if vocabulary_id:
        params["vocabulary_id"] = vocabulary_id

    payload = {"model": model, "input": {"file_urls": [audio_url]},
               "parameters": params}

    with httpx.Client(timeout=60) as c:
        r = c.post(f"{BASE_API}{SUBMIT_PATH}", json=payload, headers=_headers())
    if r.status_code != 200:
        raise AsrCloudError(f"提交转写任务失败（HTTP {r.status_code}）："
                            f"{r.text[:300]}")
    task_id = ((r.json().get("output") or {}).get("task_id")) or ""
    if not task_id:
        raise AsrCloudError(f"提交成功但没拿到 task_id：{r.text[:300]}")
    log.info("Paraformer 任务已提交：%s", task_id)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        with httpx.Client(timeout=60) as c:
            q = c.get(f"{BASE_API}/tasks/{task_id}", headers=_headers())
        if q.status_code != 200:
            log.warning("查询任务失败（HTTP %s），继续重试", q.status_code)
            continue
        out = (q.json().get("output") or {})
        status = out.get("task_status")
        if status in ("PENDING", "RUNNING"):
            continue
        if status != "SUCCEEDED":
            raise AsrCloudError(f"转写任务失败：{status} {q.text[:300]}")

        for res in out.get("results") or []:
            if res.get("subtask_status") != "SUCCEEDED":
                code = res.get("code") or "?"
                msg = res.get("message") or ""
                # FILE_403_FORBIDDEN 几乎总是"音频 URL 外网拉不到"
                hint = ""
                if "403" in str(code) or "Download" in str(msg):
                    hint = ("\n常见原因：音频 URL 对阿里云不可达。"
                            "B 站 CDN 校验 Referer，必须走自托管中转"
                            "（检查 VD_PUBLIC_BASE_URL 是否为公网可达地址）。")
                raise AsrCloudError(f"子任务失败：{code} {msg}{hint}")
            turl = res.get("transcription_url")
            if not turl:
                continue
            with httpx.Client(timeout=180) as c:
                raw = c.get(turl).text
            data = json.loads(raw)
            return _to_result(data, bvid=bvid, title=title, info=info,
                              task_id=task_id)
        raise AsrCloudError("任务成功但没有可用的转写结果")

    raise AsrCloudError(f"转写超时（{timeout:.0f} 秒未完成），task_id={task_id}")


def _to_result(data: dict, *, bvid: str, title: str, info,
               task_id: str) -> AsrCloudResult:
    """把阿里云的结果 JSON 转成本项目的 Transcript。

    关键字段：
        properties.original_duration_in_milliseconds  音频总长
        transcripts[].content_duration_in_milliseconds **计费口径**（语音时长）
        transcripts[].sentences[]                     句级结果
            begin_time / end_time                     毫秒
            words[]                                   词级时间戳（可不用）
    """
    props = data.get("properties") or {}
    audio_ms = float(props.get("original_duration_in_milliseconds") or 0)
    tr = (data.get("transcripts") or [{}])[0]
    speech_ms = float(tr.get("content_duration_in_milliseconds") or 0)

    segments: list[Segment] = []
    for s in tr.get("sentences") or []:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        start = float(s.get("begin_time") or 0) / MS
        end = float(s.get("end_time") or 0) / MS
        # 防御：个别句子可能 end <= start，纠正为至少 0.01 秒，
        # 否则后续"锚点必须落在真实 segment 起始时间上"的校验会出怪结果
        if end <= start:
            end = start + 0.01
        segments.append(Segment(start=start, end=end, text=text))

    if not segments:
        raise AsrCloudError("云 ASR 返回的句子为空（可能是纯音乐/无人声）")

    full_text = "".join(s.text for s in segments)
    transcript = Transcript(
        bvid=bvid,
        title=title,
        source=TranscriptSource.ASR,
        segments=segments,
        info=info,
        notes=[f"逐字稿由阿里云 Paraformer 生成（task {task_id}）",
               f"语音时长 {speech_ms / MS:.0f} 秒 / 音频总长 {audio_ms / MS:.0f} 秒"],
    )
    return AsrCloudResult(
        transcript=transcript,
        speech_seconds=speech_ms / MS,
        audio_seconds=audio_ms / MS,
        task_id=task_id,
        raw=data,
    )


def transcribe_video(
    client: BiliClient, info, *, title: str = "", out_dir: Path,
    download=None, progress=None,
) -> AsrCloudResult:
    """完整流程：下载音频 → 中转 → Paraformer → 清理。

    `download` 留给调用方注入（便于测试）；默认用 vd.asr.download_audio。
    `progress(msg)` 用于向 Web 任务页上报当前阶段（下载/转写都很慢，
    没有进度用户会以为卡死）。
    """
    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    if download is None:
        from .asr import download_audio   # 复用已有的流式下载

        download = download_audio

    url, _duration = client.audio_url(info)
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / f"{info.bvid}.relay.m4s"
    say(f"下载音频用于云 ASR（{info.duration // 60} 分钟视频）")
    download(client, url, local)
    if not local.exists() or local.stat().st_size == 0:
        raise AsrCloudError("音频下载失败或为空")
    say(f"音频已下载（{local.stat().st_size / 1024 / 1024:.1f} MB），"
        f"上传中转中")

    ticket = publish(local)
    say("音频已中转，提交给阿里云转写（排队 + 转写通常 1~3 分钟）")
    try:
        return transcribe_url(ticket.public_url, bvid=info.bvid,
                              title=title or info.title, info=info)
    finally:
        # ⚠️ 必须清理：不给公网留下可访问的音频文件
        ticket.release()
        forget(ticket.token)
