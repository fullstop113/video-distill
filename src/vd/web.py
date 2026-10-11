"""P3 Web 层：FastAPI 应用。

## 与 CLI 的关系

同一个 `vd.pipeline`，只是把进度上报给了 HTTP 而不是终端。**没有第二套逻辑。**

## 关于"要不要扫码登录"

这里曾有一个自相矛盾的设计，值得记下来：

最初实现了"扫码登录 + 会话 Cookie"，同时又要求"提交任务必须有会话"。
但**本机磁盘上本来就有操作号凭据**（CLI 一直在用），于是出现荒诞局面：
首页显示提交表单（因为凭据在），提交时却要求会话（因为没扫码），
而页面上又**没有登录入口** —— 用户彻底卡死。

根因是我把"是否需要登录"和"凭据是否可用"混在一起了。正确做法是按**访问来源**决定：

| 访问来源 | 是否需要扫码 | 理由 |
| --- | --- | --- |
| 本机（localhost / 127.0.0.1 / ::1） | **不需要**，自动会话 | 能访问本机端口的人本来就能直接读 `data/`，登录是纯粹的仪式感 |
| 局域网 / 公网 | **需要** | 此时登录才是真正的访问控制 |

判定基于 **socket 的对端地址**（`request.client.host`），不是 Host 头 ——
Host 头是客户端可控的，用它做安全判定等于没做。

## 安全边界（不可被 Web 层绕过）

1. **取数只能用操作号凭据**（`vd.credentials` 白名单；Web 层不重新实现它）。
2. **配额在服务端判定**，不信前端传来的任何身份或用量。
3. **非本机访问必须登录**，且未受邀者不能提交任务。
4. **每次取数前重新读凭据**，不在内存里长期持有。
"""

from __future__ import annotations

import datetime as dt_mod
import html
import io
import logging
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from . import tasks
from .credentials import (
    CredentialPolicyError,
    admin_uids,
    load_operator_credentials,
    operator_uid,
    owner_uid,
)
from .disclaimer import DISCLAIMER_VERSION, full_text
from .login import QrState, is_logged_in, persist, poll_once, start_qr_login
from .quota import report
from .storage import Credentials

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"
SESSION_COOKIE = "vd_session"
SESSION_TTL = 14 * 24 * 3600        # 两周
QR_TTL = 175                        # B 站二维码实测约 182 秒

# 登出后写入 Cookie 的哨兵值：它让 `current_session()` **不再走本机自动放行**。
#
# 为什么需要它：本机访问是免登录的（凭据在磁盘上就自动放行），
# 于是"删掉 Cookie"这种常规登出**完全无效** —— 下一个请求立刻又被自动登录，
# 用户点了登出却发现自己还在里面。这就是"按了没作用"的原因。
#
# ⚠️ 已知局限（有意接受的取舍）：哨兵是**全局**的，不是按浏览器区分的。
# 因此本机 A 浏览器登出后，B 浏览器也会一起被登出（都要重新点"显示二维码"）。
# 对"单人本机工具"这是合理取舍 —— 用全局开关换掉一整套会话记账。
# 但如果日后 `--host 0.0.0.0` 对外提供服务，这里必须改成**按会话**记录，
# 否则一个人登出会让所有使用者一起掉线。
_SESSION_LOGGED_OUT = "__logged_out__"
_LOGGED_OUT: dict[str, float] = {}   # token -> 登出时刻

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


# ---------------------------------------------------------------- 会话

# token -> {"uid","uname","created","is_operator","local"}
_SESSIONS: dict[str, dict] = {}
# key -> {"png": bytes, "qr": QrSession, "created": float}
_QR_SESSIONS: dict[str, dict] = {}


def _gc() -> None:
    now = time.time()
    for k, v in list(_SESSIONS.items()):
        if now - v["created"] > SESSION_TTL:
            _SESSIONS.pop(k, None)
    for k, v in list(_QR_SESSIONS.items()):
        # 二维码本身就有时效，超过 2 倍窗口肯定没用了
        if now - v["created"] > QR_TTL * 2:
            _QR_SESSIONS.pop(k, None)
    for k, t in list(_LOGGED_OUT.items()):
        if now - t > SESSION_TTL:
            _LOGGED_OUT.pop(k, None)


def fmt_time(iso: str, with_seconds: bool = True) -> str:
    """把 ISO 时间串格式化成 `yyyy-MM-dd HH:mm:ss`。

    直接切片（原来的 `iso[5:16].replace('T',' ')`）是错的 —— 那样只得到
    `10-11 10:54`，缺年份与秒。用户要求统一成完整格式，所以收成一处，
    免得各页面各写一种切片。

    解析失败时**退回原串前 19 字符**，绝不抛异常：时间显示不该让整页 500。
    """
    s = (iso or "").strip()
    if not s:
        return ""
    try:
        dt = dt_mod.datetime.fromisoformat(s)
    except ValueError:
        return s[:19].replace("T", " ")
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def _client_host(request: Request) -> str:
    """对端地址。**不能用 Host 头** —— 那是客户端可控的。"""
    return (request.client.host if request.client else "") or ""


def is_local(request: Request) -> bool:
    return _client_host(request) in LOCAL_HOSTS


def trust_lan() -> bool:
    """是否把局域网来源也当作可信（默认关）。

    默认关的理由：把服务开在 `--host 0.0.0.0` 时，同一网络下任何人都能提交任务。
    需要局域网免登录时应显式打开，并清楚这意味着什么。
    """
    return os.getenv("VD_WEB_TRUST_LAN", "").strip().lower() in ("1", "true", "yes", "on")


def _session_of(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or token == _SESSION_LOGGED_OUT:
        return None
    return _SESSIONS.get(token)


def is_logged_out(request: Request) -> bool:
    """本次浏览器是否明确登出过。

    登出后不再自动放行，否则"登出"在本机等于没点 —— 会被下一次请求的
    自动登录立刻覆盖掉。
    """
    if request.cookies.get(SESSION_COOKIE, "") == _SESSION_LOGGED_OUT:
        return True
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token and token in _LOGGED_OUT)


def _issue_session(uid: str, uname: str = "", local: bool = False) -> str:
    token = secrets.token_urlsafe(24)
    _SESSIONS[token] = {
        "uid": str(uid), "uname": uname or str(uid),
        "created": time.time(), "is_operator": str(uid) == operator_uid(),
        "local": local,
    }
    return token


def current_session(request: Request) -> dict | None:
    """取当前会话。

    顺序：
      1. Cookie 里的会话（扫码登录得到的）；
      2. 若**明确登出过** → 直接返回 None，不走自动放行；
      3. 本机访问且磁盘上已有操作号凭据 → 自动放行，**不写内存字典**
         （只返回临时 dict；否则每个请求都会往 `_SESSIONS` 里塞一条，内存会涨）。
    """
    sess = _session_of(request)
    if sess:
        return sess
    if is_logged_out(request):
        # ⚠️ 这一条是修"登出无效"的关键：没有它，下一步的自动放行会让登出形同虚设
        return None
    if not is_local(request) and not trust_lan():
        return None
    try:
        creds = load_operator_credentials(required=False)
    except CredentialPolicyError:
        return None
    if creds is None:
        return None
    return {
        "uid": creds.uid, "uname": creds.uname, "local": True,
        "is_operator": True, "created": time.time(),
    }


def _login_for_test(uid: str, uname: str = "") -> str:
    """给测试用：直接造一个会话，不经过扫码。

    放在这里而不是测试文件里，是为了让测试用的与会话结构**同一个定义**，
    避免测试自己拼一个 dict、字段名改了测试还照样过。
    """
    return _issue_session(uid, uname)


# ---------------------------------------------------------------- 权限


def invited_uids() -> set[str]:
    raw = os.getenv("VD_INVITED_UIDS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def can_submit(uid: str) -> tuple[bool, str]:
    """谁能提交任务（方案 §5.3⑦ 邀请制）。

    规则：
      - 管理员集合内的账号：可以（服务器主人自己）
      - 显式列入 VD_INVITED_UIDS：可以
      - 其他：不可以
    """
    uid = str(uid)
    if uid in admin_uids():
        return True, "管理员"
    if uid in invited_uids():
        return True, "受邀使用者"
    return False, "未受邀"


# ---------------------------------------------------------------- 免责声明渲染


def _disclaimer_html() -> str:
    """把纯文本免责声明渲染成真正的 HTML 结构。

    ⚠️ 修过的两个坑，都是"逐行转换"想当然导致的：

    1. 最初只是把换行替换成 `<br>`，于是 `**不对外开放注册**` 这类 Markdown
       粗体标记原样显示给用户看 —— **给使用者看的法律文本不能是半成品标记**，
       那会直接削弱它的严肃性。
    2. 改成逐行转换后，仍然漏了**跨行的粗体**：
       `10. …**不得对外发布、转载、再分发，` / `    不得用于任何商业用途**。`
       的 `**` 跨越两行，逐行处理时两行各自只剩一个星号，谁也配不上对。

    所以这里先把**续行拼回上一条目**（缩进而上一行是编号条目），再做转换。
    最后还有一道兜底：转换结果里不允许再出现 `**`。
    """
    out: list[str] = []
    open_list = False

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            out.append("</ol>")
            open_list = False

    # ---- 第一步：把物理行合并成逻辑条目（处理续行） ----
    #
    # ⚠️ 这里的判定必须精确，否则会**把同级条目也拼进上一条**。
    # 踩过的坑：免责声明原文的编号条目本身就带 2 空格缩进（`  1. …`），
    # 而"缩进 = 续行"的宽松判定会把它们全部粘成一大段，结构彻底破坏。
    # 所以：缩进的**编号条目 / 标题 / 提示**都是新条目，只有"纯文本缩进行"才是续行。
    logical: list[str] = []
    for raw in full_text().splitlines():
        if not raw.strip():
            continue
        s = raw.strip()
        # ⚠️ 顺序很重要：提示块（⚠️ 开头）**先判**。
        # 踩过的坑：`⚠️ 本服务仅使用操作号凭据…` 里含有 `9.` 之类的数字，
        # 若先判"编号条目"就会把它当普通列表项，提示样式丢失。
        starts_new = bool(
            s.startswith(("⚠️", "【"))              # 提示块 / 标题
            or re.match(r"^\d+\.", s)              # 编号条目
            or re.match(r"^[一二三四五六七八九十]+、", s)   # 中文序号标题
        )
        is_continuation = (
            raw.startswith(("  ", "\t")) and logical and not starts_new
        )
        if is_continuation:
            logical[-1] = logical[-1].rstrip() + s
        else:
            logical.append(s)

    # ---- 第二步：把逻辑条目转成 HTML ----
    for line in logical:
        stripped = line.strip()

        if stripped.startswith("【") and stripped.endswith("】"):
            close_list()
            out.append(f"<h3>{html.escape(stripped[1:-1])}</h3>")
            continue

        if re.match(r"^[一二三四五六七八九十]+、", stripped):
            close_list()
            out.append(f"<h4>{_md(html.escape(stripped))}</h4>")
            continue

        m = re.match(r"^(\d+)\.\s*(.*)$", stripped)
        if m:
            if not open_list:
                out.append("<ol>")
                open_list = True
            out.append(f"<li>{_md(html.escape(m.group(2)))}</li>")
            continue

        close_list()
        if stripped.startswith("⚠️"):
            out.append(f'<div class="note">{_md(html.escape(stripped))}</div>')
        else:
            out.append(f"<p>{_md(html.escape(stripped))}</p>")

    close_list()
    rendered = "\n".join(out)
    if "**" in rendered:
        log.warning("免责声明渲染后仍残留 ** 标记，已强制清理：%d 处",
                    rendered.count("**"))
        rendered = rendered.replace("**", "")
    return rendered


def _md(text: str) -> str:
    """只处理 `**粗体**`，因为它是我们自己在 disclaimer.py 里用的唯一标记。"""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


# ---------------------------------------------------------------- 应用

app = FastAPI(title="video-distill", docs_url=None, redoc_url=None)
_worker = None


def set_worker(worker) -> None:
    global _worker
    _worker = worker


# ---------------- 样式与页面骨架 ----------------

CSS = """
:root { color-scheme: dark; --bg:#0f1115; --card:#181b21; --line:#272b33;
        --fg:#e8eaed; --mut:#9aa0a6; --acc:#4f8cff; --ok:#34d399;
        --warn:#fbbf24; --bad:#f87171; }
* { box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
       background:var(--bg); color:var(--fg); margin:0; padding:32px 18px 64px;
       line-height:1.65; -webkit-font-smoothing:antialiased; }
.wrap { max-width:880px; margin:0 auto; }
header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap;
         margin-bottom:6px; }
h1 { font-size:24px; margin:0; letter-spacing:-.3px; }
h2 { font-size:15px; margin:32px 0 12px; color:var(--mut);
     text-transform:uppercase; letter-spacing:.08em; font-weight:600; }
h3 { font-size:17px; margin:0 0 4px; }
h4 { font-size:14px; margin:18px 0 8px; color:#cfd3d9; }
p, li { font-size:14.5px; }
.sub { color:var(--mut); font-size:13.5px; margin:0 0 24px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px;
        padding:20px; margin-bottom:16px; }
/* 卡片统一不用 backdrop-filter：它会创建新层叠上下文，
   把时间轴的气泡压在下面一层（实测气泡明显发暗）。用不透明背景即可。 */
.card.soft { background:#15181d; }
/* 状态徽章等小标签。
   ⚠️ `white-space:nowrap` 是必需的：表格列一窄，"完成"两个字就被拆成两行
   （用户截图指出"失败/完成看着不齐"）。
   注意这和 `.tl-node .cap` 的坑**正好相反** —— 那边是继承了 nowrap 导致
   一长串文字不换行，要显式 normal；这边必须禁止换行。
   同一个属性、两种需求，都得显式写清楚，不能靠继承碰运气。 */
.pill { display:inline-block; padding:2px 10px; border-radius:999px;
        font-size:12px; background:#22262e; color:var(--mut);
        border:1px solid var(--line); white-space:nowrap; text-align:center;
        line-height:1.5; vertical-align:middle; }
.pill.ok { color:var(--ok); border-color:#1e4d3a; background:#12241d; }
.pill.warn { color:var(--warn); border-color:#4d3f1e; background:#241f12; }
.pill.bad { color:var(--bad); border-color:#4d2626; background:#241414; }
input[type=text] { background:#0d0f13; border:1px solid #333842; color:var(--fg);
       border-radius:10px; padding:12px 14px; font-size:15px; flex:1; min-width:180px;
       font-family:inherit; }
input[type=text]:focus { outline:none; border-color:var(--acc); }
button { background:var(--acc); color:#fff; border:0; border-radius:10px;
       padding:12px 20px; font-size:15px; font-weight:500; cursor:pointer;
       font-family:inherit; white-space:nowrap; }
button:hover { background:#3d7cf0; }
button.ghost { background:#22262e; color:var(--fg); border:1px solid var(--line); }
button.ghost:hover { background:#2b3038; }
.row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th,td { text-align:left; padding:11px 8px; border-bottom:1px solid var(--line); }
th { color:var(--mut); font-weight:500; font-size:12.5px;
     text-transform:uppercase; letter-spacing:.05em; }
tr:last-child td { border-bottom:0; }
.mut { color:var(--mut); }
.small { font-size:13px; }
.ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
code { background:#22262e; padding:2px 7px; border-radius:6px;
       font-size:13px; font-family:ui-monospace,Consolas,monospace; color:#cfd3d9; }
a { color:var(--acc); text-decoration:none; }
a:hover { text-decoration:underline; }
.bar { height:6px; background:#22262e; border-radius:999px; overflow:hidden; }
.bar > i { display:block; height:100%; background:var(--acc); border-radius:999px;
           transition:width .4s ease; }
.note { background:#241f12; border:1px solid #4d3f1e; color:#e5cf94;
        border-radius:10px; padding:12px 14px; font-size:13.5px; margin:12px 0; }
.legal { max-height:340px; overflow:auto; padding-right:10px; }
.legal h3 { font-size:16px; margin-bottom:12px; }
.legal ol { padding-left:22px; margin:0; }
.legal li { margin-bottom:10px; color:#c9ccd1; }
.legal h4 { color:#e8eaed; }
.legal strong { color:#fff; }
.qr { width:260px; height:260px; background:#fff; padding:12px;
      border-radius:14px; display:block; }
.qrbox { display:flex; gap:20px; align-items:flex-start; flex-wrap:wrap; }
.empty { text-align:center; padding:28px 16px; color:var(--mut); }
.kv { display:flex; gap:8px; font-size:13.5px; }
.kv b { font-weight:600; }
footer { margin-top:36px; color:var(--mut); font-size:13px; }
.steps { counter-reset:s; list-style:none; padding:0; margin:0; }
.steps li { counter-increment:s; position:relative; padding-left:32px;
            margin-bottom:12px; color:#c9ccd1; }
.steps li::before { content:counter(s); position:absolute; left:0; top:1px;
     width:21px; height:21px; border-radius:50%; background:#22262e;
     border:1px solid var(--line); color:var(--mut); font-size:12px;
     display:flex; align-items:center; justify-content:center; }

/* ============ 关键时间轴：横向箭头 + 气泡节点 ============ */
/* ⚠️ 这里的关键是 **气泡与滚动容器分层**。
   第一版把气泡放在 .tl-scroll（overflow-x:auto）里面，
   于是它被裁掉、还被卡片的 backdrop-filter 盖住 —— 截图里前两个气泡明显发暗。
   overflow 容器**无法**让子元素溢出显示，所以气泡必须挂在滚动容器之外：
     .tl-card(.tl-layer 定位层)
        ├ .tl-bubble-layer  ← 气泡层，不裁剪，跟随节点定位
        └ .tl-scroll        ← 只负责横向滚动轴线与节点
   如果不用分层，唯一的选择是 z-index 硬抬，但那样会出现"气泡盖住标题"的新问题。 */
.tl-card { position:relative; padding:24px 20px 16px; }
.tl-bubble-layer { position:absolute; inset:0; pointer-events:none; z-index:5; }
.tl-scroll { overflow-x:auto; padding:34px 2px 6px; scrollbar-width:thin; }
.tl-scroll::-webkit-scrollbar { height:7px; }
.tl-scroll::-webkit-scrollbar-thumb { background:#333842; border-radius:4px; }
.tl-axis { position:relative; display:flex; align-items:flex-start;
           min-width:max-content; }
.tl-axis::before { content:""; position:absolute; left:0; right:16px; top:7px;
                   height:2px; background:linear-gradient(90deg,#2b3038,#4f8cff); }
.tl-axis::after { content:""; position:absolute; right:4px; top:2px;
                  border-left:12px solid var(--acc);
                  border-top:6px solid transparent; border-bottom:6px solid transparent; }
.tl-node { position:relative; flex:0 0 auto; width:124px; text-align:center;
           background:none; border:0; padding:0 8px; color:inherit; font:inherit;
           cursor:pointer; box-sizing:border-box; }
.tl-node .dot { display:block; width:16px; height:16px; border-radius:50%;
                background:var(--bg); border:2px solid var(--acc);
                margin:0 auto;
                transition:transform .15s, background .15s, box-shadow .15s; }
/* ⚠️ 三个子元素都必须是 display:block，并且 **white-space:normal**。
   踩过的坑（查计算样式才定位到）：
     · 它们是 <span>，默认 inline → 必须显式 block 化；
     · `<button>` 的默认 white-space 是 pre，浏览器/框架 reset 还可能给 nowrap，
       两者都会被继承下来 —— 实测 `.cap` 的计算值是 `white-space: nowrap`，
       `scrollWidth 276 > width 116`，文字被压成一行溢出，
       `-webkit-line-clamp` 完全无从生效。
   结论：光写 line-clamp 不够，**必须显式恢复 white-space:normal**。 */
.tl-node .at { display:block; white-space:normal;
               font-size:11.5px; color:var(--mut); margin-top:8px;
               font-family:ui-monospace,Consolas,monospace; }
.tl-node .cap { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
                overflow:hidden; white-space:normal; width:100%;
                font-size:12px; color:#c9ccd1;
                margin-top:3px; line-height:1.35; transition:color .15s; }
/* 悬停反馈只加在**圆点与文字**上。
   第一版给整个 .tl-node 加了 background + border-radius，
   结果出现一大块蓝色背景（用户截图指出"覆盖面太大不好看"）。
   节点本身应保持无背景，让"轴上有节点"的形状不被破坏。 */
.tl-node:hover .dot, .tl-node:focus-visible .dot, .tl-node.active .dot {
  background:var(--acc); transform:scale(1.28);
  box-shadow:0 0 0 5px rgba(79,140,255,.16); }
.tl-node:hover .at, .tl-node.active .at { color:var(--acc); }
.tl-node:hover .cap, .tl-node:focus-visible .cap, .tl-node.active .cap { color:#fff; }
/* 气泡本体：由 JS 定位在气泡层内 */
.tl-bubble { position:absolute; width:288px; background:#1b1f26;
             border:1px solid #3a414c; border-radius:14px; padding:14px 16px;
             text-align:left; box-shadow:0 18px 44px rgba(0,0,0,.62);
             opacity:0; visibility:hidden; transform:translate(-50%,6px);
             transition:opacity .16s ease, transform .16s ease; }
.tl-bubble.show { opacity:1; visibility:visible; transform:translate(-50%,0); }
.tl-bubble .bt { font-size:12px; color:var(--acc); margin-bottom:7px;
                 font-family:ui-monospace,Consolas,monospace;
                 display:flex; align-items:center; gap:6px; }
.tl-bubble .bt b { color:var(--fg); font-family:inherit; font-size:12.5px; }
.tl-bubble .bx { font-size:13.5px; line-height:1.6; color:#e2e5ea; }
.tl-bubble .ba { display:flex; gap:8px; margin-top:11px; flex-wrap:wrap; }
.tl-bubble .ba a, .tl-bubble .ba span { font-size:12px; padding:4px 11px;
     border-radius:999px; border:1px solid var(--line); background:#12151a; }
.tl-bubble .ba a:hover { background:#22262e; text-decoration:none; }
.tl-hint { font-size:12.5px; color:var(--mut); margin-top:10px; }
.tl-hint kbd { background:#22262e; border:1px solid var(--line); border-radius:5px;
               padding:1px 6px; font-size:11.5px; font-family:inherit; }

/* ============ 要点：分段陈述 ============ */
.kp { border-left:2px solid var(--line); padding-left:18px; margin-bottom:30px;
      scroll-margin-top:24px; }
.kp:target, .kp.flash { border-left-color:var(--acc); }
.kp:target .kp-h, .kp.flash .kp-h { color:var(--acc); }
.kp-h { font-size:16.5px; font-weight:600; margin:0 0 10px; line-height:1.45; }
.kp-t { font-size:14.5px; color:#d5d8dd; margin:0 0 10px; line-height:1.75; }
.kp-insight { display:flex; gap:8px; align-items:flex-start;
              background:#16202b; border:1px solid #24405c; color:#bcd6f5;
              border-radius:10px; padding:9px 13px; font-size:13.5px;
              margin:10px 0 8px; line-height:1.6; }
.kp-insight .tag { flex:0 0 auto; color:#7fb0e8; font-size:12px;
                   border:1px solid #2c4d6e; border-radius:999px; padding:1px 8px;
                   margin-top:2px; }
.kp-src { font-size:12.5px; color:var(--mut); }
.kp-src a { font-family:ui-monospace,Consolas,monospace; }

/* ============ 任务列表：标题 / 筛选 / 分页 ============ */
.ttl { font-size:13px; color:#c9ccd1; margin-top:3px; line-height:1.4;
       max-width:420px; }
.chips { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
.chip { font-size:12.5px; padding:5px 13px; border-radius:999px;
        border:1px solid var(--line); background:#181b21; color:var(--mut); }
.chip:hover { background:#22262e; text-decoration:none; }
.chip.on { background:#1b2836; border-color:#2c4d6e; color:#8dbcf0; }
.pager { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
         margin-top:16px; padding-top:14px; border-top:1px solid var(--line);
         font-size:13px; }
.pager .cur { background:var(--acc); color:#fff; padding:4px 11px;
              border-radius:7px; font-weight:600; }
.pager a { padding:4px 10px; border-radius:7px; border:1px solid var(--line);
           background:#181b21; }
.pager a:hover { background:#22262e; text-decoration:none; }
.pager .mut { padding:4px 6px; }
/* 任务列表：状态列不许换行、列宽稳定、时间不折行。
   用户反馈"失败/完成看着不齐" —— 除了徽章被拆行，列宽随内容变化也会让
   整列看起来歪。所以给关键列定宽，时间列禁止折行。
   ⚠️ 详情列要留够宽度：失败原因较长时会折成好几行，把行高撑得参差不齐
   （实测 170px 时折成 4 行）。这里加宽到 200px，并配 .detail-msg 限高。 */
td { vertical-align:middle; }
.tasks-table { width:100%; }
.tasks-table th:nth-child(1), .tasks-table td:nth-child(1) {
  width:64px; text-align:center; }
.tasks-table th:nth-child(3), .tasks-table td:nth-child(3) {
  width:130px; white-space:nowrap; }
.tasks-table th:nth-child(4), .tasks-table td:nth-child(4) {
  width:168px; white-space:nowrap; }
.tasks-table th:nth-child(5), .tasks-table td:nth-child(5) { width:200px; }
.tasks-table td { padding:12px 10px; border-top:1px solid var(--line); }
.tasks-table th { padding:0 10px 10px; }
/* 详情列的文字：最多两行，超出打点，避免行高参差 */
.detail-msg { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
              overflow:hidden; white-space:normal; max-width:200px; }

/* 产物列表 */
.art-row { display:flex; align-items:center; gap:12px; padding:11px 0;
           border-bottom:1px solid var(--line); }
.art-row:last-child { border-bottom:0; }
.art-kind { flex:0 0 auto; font-size:11.5px; padding:2px 9px; border-radius:999px;
            border:1px solid #2c4d6e; background:#16202b; color:#8dbcf0; }
.art-kind.transcript { border-color:#3d4a2c; background:#1d2416; color:#a8cc7a; }
.art-main { flex:1; min-width:0; }
.art-name { font-size:13.5px; color:#e2e5ea; overflow:hidden;
            text-overflow:ellipsis; white-space:nowrap; }
.art-meta { font-size:12px; color:var(--mut); margin-top:2px; }
.art-meta code { font-size:11.5px; }

/* ============ 悬浮「回到底部」按钮 ============ */
/* 长文（实测产物 236~300 行）看完想下载要一直滚，所以给一个固定在右下角的按钮。
   只在结果页出现，且滚动超过一屏才显示 —— 页面短的时候它没有意义。

   ⚠️ 踩过的坑：只给按钮写了 `position:fixed`，**外层容器没写**。
   容器是 static，于是 `right/bottom` 对按钮失效、按钮回到文档流里，
   跟着内容一起滚走 —— 表现就是"悬浮按钮根本不悬浮"。
   实测用 Chrome 读计算样式才看出来：`position=static, right=auto, bottom=auto`。 */
.to-bottom { position:fixed; right:26px; bottom:26px; z-index:40;
             display:flex; flex-direction:column; gap:8px; align-items:flex-end;
             pointer-events:none; }
.to-bottom button { pointer-events:auto;
             background:var(--acc); color:#fff; border:0; border-radius:999px;
             padding:11px 18px; font-size:13.5px; font-weight:500; cursor:pointer;
             box-shadow:0 10px 26px rgba(0,0,0,.5); font-family:inherit;
             opacity:0; visibility:hidden; transform:translateY(8px);
             /* ⚠️ 只过渡 opacity 与 transform。
                把 visibility 也放进 transition 会引入两个问题：
                  1. `getComputedStyle` 在过渡进行中返回的是**起始值**（hidden），
                     于是自动化验证会误判成"样式没生效"（我为此绕了很久）；
                  2. 过渡期元素已参与命中测试，语义含混。
                可见性应当**立即翻转**，只让位移与透明度做动效。 */
             transition:opacity .18s, transform .18s; }
.to-bottom button.show { opacity:1; visibility:visible; transform:translateY(0); }
.to-bottom button:hover { background:#3d7cf0; }
.to-bottom button.ghost { background:#22262e; border:1px solid var(--line); }
"""

PAGE_HEAD = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>video-distill</title><style>{CSS}</style></head>
<body><div class="wrap">
"""

PAGE_FOOT = """</div></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE_HEAD + body + PAGE_FOOT, status_code=status)


def _nav(sess: dict | None, current: str = "") -> str:
    """页脚导航。

    `current` 用于**去掉指向当前页面的链接** —— 用户反馈"首页这个按钮没有用"：
    在首页上再放一个"首页"链接确实是死链接，点了毫无变化。
    """
    items: list[tuple[str, str]] = [
        ("首页", "/"),
        ("我的产物", "/artifacts"),
        ("服务状态", "/status"),
        ("配额", "/quota"),
    ]
    parts = [f'<a href="{href}">{label}</a>' for label, href in items if href != current]
    if sess:
        parts.append(
            '<form method="post" action="/logout" style="display:inline">'
            '<button class="ghost" style="padding:5px 12px;font-size:13px">登出</button>'
            "</form>"
        )
    return '<footer>' + " · ".join(parts) + "</footer>"


# ---------------- 登录卡片 ----------------


def _login_card(reason: str = "") -> str:
    note = f'<div class="note">{html.escape(reason)}</div>' if reason else ""
    return f"""
    <div class="card" id="login-card">
      {note}
      <h3>需要扫码登录</h3>
      <p class="mut small" style="margin:6px 0 18px">
        本服务通过浏览器访问时必须登录。请用 <strong>B 站手机客户端</strong> 扫描下方二维码。
      </p>
      <div id="qrbox" class="empty">点击下方按钮显示二维码</div>
      <div class="row" style="margin-top:16px">
        <button onclick="startLogin()" id="qrbtn">显示二维码</button>
        <span class="mut small">二维码约 3 分钟内有效</span>
      </div>
    </div>
    <script>
    let pollTimer = null;
    async function startLogin() {{
      const box = document.getElementById('qrbox');
      const btn = document.getElementById('qrbtn');
      btn.disabled = true;
      box.className = 'empty';
      box.textContent = '正在获取二维码…';
      try {{
        const r = await fetch('/login/start', {{method:'POST'}});
        const d = await r.json();
        if (!d.ok) {{ box.innerHTML = '<span class="bad">'+d.error+'</span>'; btn.disabled = false; return; }}
        box.className = 'qrbox';
        box.innerHTML =
          '<div><img class="qr" src="/login/qr.png?key='+encodeURIComponent(d.key)
          +'&t='+Date.now()+'" alt="登录二维码">'
          + '<div class="mut small" id="qrtip" style="margin-top:10px;text-align:center"></div></div>'
          + '<div style="flex:1;min-width:220px"><ol class="steps">'
          + '<li>打开 B 站手机客户端</li>'
          + '<li>点右上角「扫一扫」</li>'
          + '<li>扫描左侧二维码</li>'
          + '<li>在手机上确认登录</li></ol></div>';
        countdown(d.expires_in);
        poll(d.key);
      }} catch (e) {{
        box.innerHTML = '<span class="bad">请求失败：'+e+'</span>';
      }}
      btn.disabled = false;
    }}
    function countdown(sec) {{
      let left = sec;
      const el = () => document.getElementById('qrtip');
      const tick = () => {{
        if (!el()) return;
        if (left <= 0) {{ el().innerHTML = '<span class="warn">二维码已过期，请重新点击按钮</span>'; return; }}
        el().textContent = '剩余 ' + left + ' 秒';
        left -= 1;
        setTimeout(tick, 1000);
      }};
      tick();
    }}
    async function poll(key) {{
      clearTimeout(pollTimer);
      try {{
        const r = await fetch('/login/poll?key='+encodeURIComponent(key));
        const d = await r.json();
        if (d.status === 'success') {{
          document.getElementById('qrbox').innerHTML =
            '<div class="empty"><span class="ok" style="font-size:16px">✓ 登录成功</span>'
            + '<div class="mut small" style="margin-top:8px">'+(d.uname||d.uid)
            + (d.note ? '<br><span class="warn">'+d.note+'</span>' : '')
            + '</div><div class="mut small" style="margin-top:8px">正在跳转…</div></div>';
          setTimeout(()=>location.href='/', 1000);
          return;
        }}
        if (d.status === 'expired' || d.status === 'missing') {{
          document.getElementById('qrbox').innerHTML =
            '<div class="empty"><span class="warn">二维码已失效</span>'
            + '<div class="mut small" style="margin-top:8px">请重新点击「显示二维码」</div></div>';
          return;
        }}
        if (d.status === 'scanned') {{
          const tip = document.getElementById('qrtip');
          if (tip) tip.innerHTML = '<span class="ok">已扫码，请在手机上确认</span>';
        }}
        pollTimer = setTimeout(()=>poll(key), 1800);
      }} catch (e) {{ pollTimer = setTimeout(()=>poll(key), 2500); }}
    }}
    </script>
    """


# ---------------- 路由 ----------------


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "worker_running": bool(_worker and _worker.is_running),
        "worker_current": (_worker.current_task if _worker else ""),
        "queues": tasks.stats(),
    }


@app.get("/artifacts", response_class=HTMLResponse)
def artifacts_page(request: Request, page: int = 1, kind: str = "", q: str = "",
                   all_versions: int = 0):
    """我的产物：跨任务汇总所有已生成的文件。

    ## 为什么是**独立页签**而不是首页加筛选

    用户问"筛选 / 新页签 / tab 切换哪个好"。选独立页签，理由不只是整齐：

      · **生命周期不同**：任务记录是"某次运行"，会越堆越多；
        产物是**交付物**，要沉淀下来反复取用。混在一起，产物会被
        "失败的、排队中的、昨天试的"淹掉。
      · **问题不同**：首页回答"我要蒸馏什么"，这一页回答"我蒸馏过什么、能不能找到"。
      · **独立 URL**：能收藏、能直接翻页筛选；tab 切换做不到（刷新回默认 tab）。

    同时保留首页的轻量筛选（只看失败等），两者各管各的。

    ## 关于"旧版本"

    默认**同一视频同一类型只显示最新一份**。实测首次打开时 12 个产物里有 5 份是
    同一个视频的重复总结 —— 不合并的话列表会被历史版本刷屏。
    但不偷偷丢：显示「另有 N 个旧版本」，并可一键展开（`all_versions=1`）。
    """
    sess = current_session(request)
    if not sess:
        return RedirectResponse("/?need_login=1", status_code=303)

    all_items = tasks.all_artifacts()
    kinds = sorted({i["kind"] for i in all_items if i["kind"]})
    items = [i for i in all_items if not kind or i["kind"] == kind]
    kw = q.strip().lower()
    if kw:
        items = [
            i for i in items
            if kw in (i["title"] or "").lower() or kw in i["bvid"].lower()
        ]

    # 合并同一 (bvid, kind) 的旧版本，保留最新一条（all_artifacts 已按时间倒序）
    hidden_versions = 0
    if not all_versions:
        seen: dict[tuple[str, str], dict] = {}
        merged: list[dict] = []
        for i in items:
            key = (i["bvid"], i["kind"])
            if key in seen:
                seen[key]["older"] = seen[key].get("older", 0) + 1
                hidden_versions += 1
                continue
            seen[key] = i
            merged.append(i)
        items = merged

    total_size = sum(i["size"] for i in items)
    per_page = 15
    pages = max(1, (len(items) + per_page - 1) // per_page)
    page = min(max(page, 1), pages)   # 同样要收敛后再渲染分页条
    chunk = items[(page - 1) * per_page : page * per_page]

    kind_label = {"summary": "总结与要点", "transcript": "逐字稿"}
    chips = [("", f"全部 {len(all_items)}")]
    chips += [(k, f"{kind_label.get(k, k)} {sum(1 for i in all_items if i['kind'] == k)}")
              for k in kinds]
    chip_html = "".join(
        f'<a class="chip{" on" if kind == val else ""}" '
        f'href="/artifacts?kind={val}&page=1">{html.escape(label)}</a>'
        for val, label in chips
    )

    if not chunk:
        body_items = '<div class="empty">没有匹配的产物。</div>'
    else:
        rows = []
        for i in chunk:
            label = kind_label.get(i["kind"], i["kind"] or "文件")
            size_kb = f"{i['size'] / 1024:.1f} KB" if i["size"] else "—"
            extra = []
            if i.get("points"):
                extra.append(f"{i['points']} 要点")
            if i.get("chars"):
                extra.append(f"{i['chars']} 字")
            if i.get("cost_cny"):
                extra.append(f"¥{i['cost_cny']}")
            older = i.get("older") or 0
            if older:
                extra.append(f"另有 {older} 个旧版本")
            meta = " · ".join(extra)
            rows.append(
                f'<div class="art-row">'
                f'<span class="art-kind {html.escape(i["kind"])}">{html.escape(label)}</span>'
                f'<div class="art-main">'
                f'<div class="art-name">{html.escape(i["title"][:64])}</div>'
                f'<div class="art-meta"><code>{html.escape(i["bvid"])}</code>'
                f'{"　" + meta if meta else ""}　{size_kb}　'
                f'{fmt_time(i["finished_at"])}</div>'
                f'</div>'
                f'<a class="pill" href="/files/{html.escape(i["name"])}" download>↓ 下载</a>'
                f'<a class="pill" href="/tasks/{html.escape(i["task_id"])}">查看</a>'
                f'</div>'
            )
        body_items = "".join(rows)

    toggle_q = (f"kind={kind}&q={html.escape(q)}&"
                f"all_versions={0 if all_versions else 1}")
    toggle_label = ("显示全部版本" if not all_versions else "只显示最新版本")
    note = (f"已合并 {hidden_versions} 个重复版本" if hidden_versions else "")

    body = f"""
    <header><h1>我的产物</h1>
      <span class="pill">{len(items)} 个文件 · {total_size / 1024:.0f} KB</span>
    </header>
    <p class="sub">任务记录是「某次运行」，这里是沉淀下来的交付物</p>

    <div class="chips">{chip_html}</div>
    <div class="card">
      <form method="get" action="/artifacts">
        <div class="row">
          <input type="text" name="q" value="{html.escape(q)}"
                 placeholder="按标题或 BV 号搜索" autocomplete="off">
          <button type="submit">搜索</button>
          {'<a class="pill" href="/artifacts">清除</a>' if kw else ''}
          <a class="pill" href="/artifacts?{toggle_q}">{toggle_label}</a>
        </div>
        {f'<div class="mut small" style="margin-top:8px">{note}</div>' if note else ''}
      </form>
    </div>

    <div class="card">{body_items}
      {_pager(page, pages, len(items), "/artifacts",
              extra=f"kind={kind}&q={q}&all_versions={all_versions}")}
    </div>
    {_nav(sess, current="/artifacts")}
    """
    return _page(body)


@app.get("/status", response_class=HTMLResponse)
def status_page():
    """服务状态页（JSON 接口仍是 /healthz）。"""
    st = tasks.stats()
    cur = _worker.current_task if _worker else ""
    running = bool(_worker and _worker.is_running)
    rows = "".join(
        f"<tr><td class='mut'>{k}</td><td>{v}</td></tr>"
        for k, v in (
            ("排队中", st["queued"]), ("进行中", st["running"]),
            ("已完成", st["done"]), ("失败", st["failed"]), ("总计", st["total"]),
        )
    )
    env_rows = "".join(
        f"<tr><td class='mut'>{k}</td><td>{v}</td></tr>"
        for k, v in (
            ("邀请名单", ", ".join(sorted(invited_uids())) or "<span class='mut'>（空）</span>"),
            ("DeepSeek 模型", f"<code>{os.getenv('DEEPSEEK_MODEL','deepseek-flash')}</code>"),
        )
    )
    try:
        creds = load_operator_credentials(required=False)
        cred_txt = (
            f"<span class='pill ok'>就绪</span> <code>{cred_uid(creds)}</code>"
            if creds else "<span class='pill warn'>未登录</span>"
        )
    except CredentialPolicyError:
        cred_txt = "<span class='pill warn'>未登录</span>"
    body = f"""
    <header><h1>服务状态</h1></header>
    <p class="sub">工作线程串行消费任务（避免并发请求触发风控）</p>

    <div class="card">
      <div class="row">
        <span class="pill {'ok' if running else 'bad'}">{'运行中' if running else '已停止'}</span>
        <span class="mut small">当前任务：<code>{cur or '空闲'}</code></span>
      </div>
    </div>

    <h2>队列</h2>
    <div class="card"><table>{rows}</table></div>

    <h2>配置</h2>
    <div class="card">
      <table>{env_rows}</table>
      <div style="margin-top:14px" class="kv"><span class="mut">登录状态：</span>{cred_txt}</div>
    </div>

    <h2>接口</h2>
    <div class="card small mut">
      <code>GET /healthz</code> 健康检查（JSON）·
      <code>GET /quota</code> 配额（JSON）·
      <code>GET /tasks/&lt;id&gt;/json</code> 任务详情（JSON）
    </div>
    {_nav(None, current="/status")}
    """
    return _page(body)


def cred_uid(creds: Credentials) -> str:
    return creds.uname or creds.uid


@app.get("/", response_class=HTMLResponse)
def index(request: Request, page: int = 1, status: str = ""):
    _gc()
    try:
        creds = load_operator_credentials(required=False)
    except CredentialPolicyError:
        return _page(
            "<header><h1>video-distill</h1></header>"
            '<div class="note">服务凭据尚未就绪，请联系管理员或扫码登录。</div>'
            + _nav(None, current="/")
        )

    sess = current_session(request)

    # 没有凭据 → 必须登录；有凭据但非本机且无会话 → 也要登录
    if creds is None or sess is None:
        reason = ""
        if creds is None and operator_uid():
            reason = "尚未登录，请扫码登录。"
        elif creds is not None and sess is None:
            reason = "你正在从其它设备访问本服务，需要先登录。"
        return _page(
            """
            <header><h1>video-distill</h1></header>
            <p class="sub">把 B 站视频蒸馏成可读、可检索的文字资产</p>
            """
            + _login_card(reason)
            + '<h2>使用须知</h2><div class="card legal">'
            + _disclaimer_html() + "</div>"
            + _nav(None, current="/")
        )

    allowed, role = can_submit(sess["uid"])
    q = report(creds.uid)
    local = is_local(request)
    body = f"""
    <header><h1>video-distill</h1>
      <span class="pill {'ok' if local else 'warn'}">{'本机访问' if local else '远程访问'}</span>
    </header>
    <p class="sub">把 B 站视频蒸馏成可读、可检索的文字资产</p>

    <div class="card">
      <div class="kv"><span class="mut">当前账号</span>
        <b>{html.escape(creds.uname or creds.uid)}</b>
        <code>{creds.uid}</code>
        <span class="pill">{role}</span></div>
      <div class="kv" style="margin-top:8px"><span class="mut">配额</span>
        <span>{html.escape(q.describe())}</span></div>
    </div>
    """

    if not allowed:
        body += f"""
        <div class="card">
          <div class="bad">当前账号未受邀，不能提交任务。</div>
          <p class="mut small" style="margin:10px 0 0">
            本服务为邀请制（见下方使用须知）。若你是服务器主人，请把该 UID 加入
            <code>.env</code> 的 <code>VD_INVITED_UIDS</code>。
          </p>
        </div>
        """
    else:
        body += """
        <div class="card">
          <form method="post" action="/tasks">
            <div class="row">
              <input type="text" name="bvid" placeholder="粘贴 BV 号，例如 BV1NXH661EpG"
                     required autocomplete="off" spellcheck="false">
              <button type="submit">开始蒸馏</button>
            </div>
          </form>
        </div>
        """

    body += _task_table(sess["uid"] if not local else creds.uid, page=page, status=status)
    body += '<h2>使用须知</h2><div class="card legal">' + _disclaimer_html() + "</div>"
    body += _nav(sess, current="/")
    return _page(body)


def _pager(page: int, pages: int, total: int, base: str, extra: str = "") -> str:
    """分页导航。

    只渲染「上一页 / 页码 / 下一页」，页码多时用省略号收窄 ——
    产物多了以后几十个页码全铺开会把页面撑爆。
    """
    if pages <= 1:
        return f'<div class="pager"><span class="mut small">共 {total} 条</span></div>'
    q = f"&{extra}" if extra else ""

    def link(p: int, label: str) -> str:
        return f'<a href="{base}?page={p}{q}">{label}</a>'

    nums: list[str] = []
    window = 2
    show = sorted({1, pages, *range(max(1, page - window), min(pages, page + window) + 1)})
    prev = 0
    for n in show:
        if prev and n - prev > 1:
            nums.append('<span class="mut">…</span>')
        nums.append(f'<span class="cur">{n}</span>' if n == page else link(n, str(n)))
        prev = n

    parts = [link(page - 1, "← 上一页") if page > 1 else '<span class="mut">← 上一页</span>']
    parts.extend(nums)
    parts.append(link(page + 1, "下一页 →") if page < pages else '<span class="mut">下一页 →</span>')
    return (
        '<div class="pager">' + " ".join(parts)
        + f'<span class="mut small" style="margin-left:auto">'
          f'共 {total} 条 · 第 {page}/{pages} 页</span></div>'
    )


def _task_table(uid: str, *, page: int = 1, status: str = "", limit: int = 10) -> str:
    """我的任务列表：带**视频标题**、分页与状态筛选。

    用户反馈三点：列表里只有 BV 号看不清是哪个视频；
    条目多了要分页；还得能筛选（翻页找一条失败记录很烦）。
    """
    # page_tasks 会收敛越界页码，**必须用它的返回值**渲染分页条，
    # 否则会出现"数据是第 3 页、页码写着第 99/3 页"（踩过）
    rows, total, pages, page = tasks.page_tasks(
        limit=limit, page=page, requester=uid, status=status
    )
    tabs = [("", "全部"), ("done", "已完成"), ("failed", "失败"),
            ("running", "进行中"), ("queued", "排队中")]
    chips = "".join(
        f'<a class="chip{" on" if status == val else ""}" href="/?status={val}&page=1">{label}</a>'
        for val, label in tabs
    )
    head = f'<h2>我的任务</h2><div class="chips">{chips}</div>'

    if not rows:
        empty = "这个筛选下还没有任务。" if status else "还没有任务。粘贴一个 BV 号开始吧。"
        return head + f'<div class="card"><div class="empty">{empty}</div></div>'

    tr = []
    for t in rows:
        r = t.result or {}
        if t.status is tasks.TaskStatus.DONE:
            badge = '<span class="pill ok">完成</span>'
            detail = (
                f"<a href='/tasks/{t.id}'>查看</a>"
                f"<div class='mut small'>{r.get('points',0)} 要点 · "
                f"{r.get('chars',0)} 字 · ¥{r.get('cost_cny',0)}</div>"
            )
        elif t.status is tasks.TaskStatus.FAILED:
            badge = '<span class="pill bad">失败</span>'
            # 失败原因可能是多行长文，列表里只取第一行并限高两行显示
            first = (t.error or "").splitlines()[0] if t.error else ""
            detail = (f"<a href='/tasks/{t.id}'>详情</a>"
                      f"<div class='mut small detail-msg' title='{html.escape(t.error[:300])}'>"
                      f"{html.escape(first)}</div>")
        elif t.status is tasks.TaskStatus.RUNNING:
            badge = '<span class="pill warn">进行中</span>'
            detail = (f"<a href='/tasks/{t.id}'>进度</a>"
                      f"<div class='mut small'>{html.escape(t.stage_label)}</div>")
        else:
            pos = tasks.queue_position(t.id)
            badge = f'<span class="pill">排队 #{pos}</span>'
            detail = f"<a href='/tasks/{t.id}'>进度</a>"
        title = t.display_title
        title_html = (
            f'<div class="ttl">{html.escape(title[:46])}{"…" if len(title) > 46 else ""}</div>'
            if title else '<div class="ttl mut">（旧任务，无标题）</div>'
        )
        up = t.display_up_name
        up_html = (
            f'<div class="up-name">{html.escape(up[:20])}{"…" if len(up) > 20 else ""}</div>'
            if up else '<div class="up-name mut">—</div>'
        )
        tr.append(
            f"<tr><td>{badge}</td>"
            f"<td><code>{html.escape(t.bvid)}</code>{title_html}</td>"
            f"<td class='up-col'>{up_html}</td>"
            f"<td class='mut small'>{fmt_time(t.created_at)}</td>"
            f"<td>{detail}</td></tr>"
        )
    return (
        head
        + "<div class='card'><table class='tasks-table'>"
        "<tr><th>状态</th><th>视频</th><th>UP主</th><th>提交时间</th><th></th></tr>"
        + "".join(tr)
        + "</table>"
        + _pager(page, pages, total, "/", extra=f"status={status}")
        + "</div>"
    )


# ---------------- 登录 ----------------


@app.post("/login/start")
def login_start():
    _gc()
    try:
        qr = start_qr_login()
    except Exception as exc:  # noqa: BLE001
        log.exception("生成二维码失败")
        return JSONResponse({"ok": False, "error": f"获取二维码失败：{exc}"}, status_code=502)
    try:
        import qrcode

        buf = io.BytesIO()
        qrcode.make(qr.url).save(buf, format="PNG")
        png = buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.exception("渲染二维码失败")
        return JSONResponse({"ok": False, "error": f"渲染二维码失败：{exc}"}, status_code=500)

    key = secrets.token_urlsafe(12)
    # 图片在**生成时就渲染好**：轮询接口会消耗会话状态，不能等到取图时再渲染
    _QR_SESSIONS[key] = {"png": png, "qr": qr, "created": time.time()}
    return {"ok": True, "key": key, "expires_in": QR_TTL}


@app.get("/login/qr.png")
def login_qr_png(key: str = ""):
    """返回预渲染好的 PNG。

    ⚠️ 修过的坑：前端最初传 `?k=`，后端读的是 `key`，参数名不匹配导致**永远 404**，
    二维码从来没显示出来过。现在前端用 `key`，并有测试覆盖这条路径。
    """
    entry = _QR_SESSIONS.get(key)
    if not entry:
        raise HTTPException(404, "二维码不存在或已过期")
    return Response(
        content=entry["png"],
        media_type="image/png",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/login/poll")
def login_poll(key: str = ""):
    entry = _QR_SESSIONS.get(key)
    if not entry:
        return {"status": "missing"}
    qr = entry["qr"]
    try:
        state, message = poll_once(qr)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}

    if state is QrState.PENDING:
        return {"status": "pending", "message": message}
    if state is QrState.SCANNED:
        return {"status": "scanned", "message": message}
    if state is QrState.EXPIRED:
        _QR_SESSIONS.pop(key, None)
        return {"status": "expired"}
    if state is QrState.SUCCESS:
        if not is_logged_in(qr):
            return {"status": "error", "error": "登录成功但未拿到 SESSDATA"}
        try:
            creds = persist(qr)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"保存凭据失败：{exc}"}
        _QR_SESSIONS.pop(key, None)

        token = _issue_session(creds.uid, creds.uname, local=False)
        resp = JSONResponse({
            "status": "success", "uid": creds.uid, "uname": creds.uname,
            "is_operator": creds.uid == operator_uid(), "note": "",
        })
        resp.set_cookie(SESSION_COOKIE, token, httponly=True,
                        samesite="lax", max_age=SESSION_TTL)
        return resp
    return {"status": "unknown", "raw": state.value}


@app.post("/logout")
def logout(request: Request):
    """登出。

    ⚠️ 这里**不能**只是删 Cookie。本机访问是免登录的（凭据在磁盘上就自动放行），
    删掉 Cookie 后下一个请求立刻又被自动登录，用户按了登出却还在里面 ——
    这就是"按了没作用"的原因。

    所以：把会话标记为已登出，并写入哨兵 Cookie，`current_session()` 见到它
    就不再自动放行。Cookie 不删除（否则哨兵也没了），只在登录成功时覆盖。
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    if token and token != _SESSION_LOGGED_OUT:
        _SESSIONS.pop(token, None)
    _LOGGED_OUT[token or _SESSION_LOGGED_OUT] = time.time()
    # 强制该浏览器在本次会话内保持登出状态
    _LOGGED_OUT[_SESSION_LOGGED_OUT] = time.time()

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, _SESSION_LOGGED_OUT,
        httponly=True, samesite="lax", max_age=SESSION_TTL,
    )
    return resp


# ---------------- 任务 ----------------


@app.post("/tasks")
def create_task(request: Request, bvid: str = Form(...)):
    sess = current_session(request)
    if not sess:
        # 浏览器流程要给页面而不是 JSON 401，否则用户看到一坨 JSON 无从下手
        return RedirectResponse("/?need_login=1", status_code=303)

    try:
        creds = load_operator_credentials(required=False)
    except CredentialPolicyError:
        raise HTTPException(409, "服务凭据尚未就绪，请联系管理员。") from None
    if creds is None:
        return RedirectResponse("/?need_login=1", status_code=303)

    allowed, _ = can_submit(sess["uid"])
    if not allowed:
        raise HTTPException(403, "当前账号未受邀，不能提交任务")

    bvid = bvid.strip()
    if not bvid:
        raise HTTPException(400, "缺少 BV 号")
    m = re.search(r"BV[0-9A-Za-z]{10}", bvid)
    if not m:
        raise HTTPException(400, f"没看出这是 BV 号：{bvid[:40]}")
    bvid = m.group(0)  # 允许直接粘贴整条链接

    existing = tasks.find_reusable(bvid)
    if existing:
        return RedirectResponse(f"/tasks/{existing.id}", status_code=303)

    task = tasks.create(bvid, requester=sess["uid"])
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(task_id: str, request: Request):
    sess = current_session(request)
    if not sess:
        return RedirectResponse("/?need_login=1", status_code=303)
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.requester and t.requester != sess["uid"] and sess["uid"] not in admin_uids():
        raise HTTPException(403, "这不是你的任务")

    r = t.result or {}
    if t.status is tasks.TaskStatus.DONE:
        detail = _render_done(t, r)
    elif t.status is tasks.TaskStatus.FAILED:
        detail = f"""
        <h2>失败</h2>
        <div class="card"><div class="bad" style="margin-bottom:10px">任务未能完成</div>
          <pre style="white-space:pre-wrap;background:#0d0f13;border:1px solid var(--line);
            border-radius:10px;padding:14px;font-size:13px;color:#c9ccd1;margin:0"
          >{html.escape(t.error)}</pre>
        </div>
        """
    else:
        detail = f"""
        <h2>进度</h2>
        <div class="card">
          <div class="bar"><i style="width:{t.progress}%"></i></div>
          <div class="row" style="margin-top:14px;justify-content:space-between">
            <b>{html.escape(t.stage_label)}</b>
            <span class="mut small">{t.progress}%</span>
          </div>
          <div class="mut small" style="margin-top:6px">{html.escape(t.message or '…')}</div>
          <div class="mut small" style="margin-top:14px">
            {('队列第 ' + str(tasks.queue_position(t.id)) + ' 位 · ') if t.status is tasks.TaskStatus.QUEUED else ''}
            本页每 2 秒自动刷新
          </div>
        </div>
        <script>setTimeout(()=>location.reload(), 2000);</script>
        """

    body = f"""
    <header><h1>{html.escape(t.bvid)}</h1></header>
    <p class="sub">任务 <code>{t.id}</code> ·
      {html.escape(fmt_time(t.created_at))}</p>
    {detail}
    <footer><a href="/">← 返回首页</a></footer>
    """
    # 只有"已完成"的长文才需要悬浮导航：进度页与失败页都很短
    if t.status is tasks.TaskStatus.DONE:
        body += _to_bottom_html()
    return _page(body)


def _sec(label: str) -> int:
    parts = [int(x) for x in str(label).split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else 0


def _paragraphs(text: str) -> str:
    """把长段落按句渲染成多个 <p>，与产物的 Markdown 排版保持一致。

    直接复用 `summarize._split_sentences`，避免网页与文件两种排版各写一套、
    然后慢慢漂移。
    """
    from .summarize import _split_sentences

    return "".join(f"<p style='margin:0 0 12px'>{html.escape(x)}</p>"
                   for x in _split_sentences(text))


def _timeline_html(bvid: str, highlights: list[dict]) -> str:
    """横向箭头时间轴 + 可冒泡、可点击跳转的节点。

    交互（按用户要求）：
      · **点击节点（圆点或文字都可）→ 平滑滚动到下方对应要点**，不是跳视频
      · 悬停/聚焦 → 弹气泡显示要点信息，气泡里也有「跳到要点」
      · 跳原视频的动作**下移**到要点段落里的时间戳

    实现要点见 CSS 注释：气泡挂在 `.tl-bubble-layer`（**不在滚动容器内**），
    位置由 JS 按节点的实际屏幕坐标计算 —— 这是解决遮挡的关键。
    """
    if not highlights:
        return ""
    nodes, bubbles = [], []
    for i, h in enumerate(highlights, 1):
        idx = h.get("point_index", -1)
        linked = isinstance(idx, int) and idx >= 0
        target = f"kp-{idx + 1}" if linked else ""
        detail = (h.get("detail") or "").strip()
        jump = (
            f'<a href="#{target}" data-goto="{target}">↓ 跳到要点 {idx + 1}</a>'
            if linked else '<span class="mut">未关联要点</span>'
        )
        nodes.append(
            f'<button class="tl-node" type="button" data-bubble="tl-b-{i}"'
            f' data-goto="{target}" aria-label="跳到要点 {idx + 1 if linked else 0}">'
            f'<span class="dot"></span>'
            f'<span class="at">{html.escape(h.get("at", ""))}</span>'
            f'<span class="cap">{html.escape(h.get("text", ""))}</span>'
            f"</button>"
        )
        body = f'<div class="bx">{html.escape(detail or h.get("text", ""))}</div>' if detail else ""
        bubbles.append(
            f'<div class="tl-bubble" id="tl-b-{i}">'
            f'<div class="bt">{html.escape(h.get("at", ""))}<b>节点 {i}</b></div>'
            f'<div class="bx">{html.escape(h.get("text", ""))}</div>'
            f"{body}"
            f'<div class="ba">{jump}</div>'
            f"</div>"
        )
    return (
        '<h2>关键时间轴 <span class="mut small">'
        "点节点直接滚到下方要点 · 悬停看详情</span></h2>"
        '<div class="card tl-card">'
        f'<div class="tl-bubble-layer">{"".join(bubbles)}</div>'
        f'<div class="tl-scroll"><div class="tl-axis">{"".join(nodes)}</div></div>'
        '<div class="tl-hint">跳原视频请点下方要点段落里的时间戳；'
        "也可以用 <kbd>Tab</kbd> 逐个聚焦节点。</div>"
        "</div>"
        + _TIMELINE_JS
    )


_TIMELINE_JS = """
<script>
(function () {
  document.querySelectorAll('.tl-card').forEach(function (card) {
    const layer = card.querySelector('.tl-bubble-layer');
    const scroll = card.querySelector('.tl-scroll');
    const nodes = [...card.querySelectorAll('.tl-node')];
    if (!layer || !nodes.length) return;

    function place(node) {
      const b = document.getElementById(node.dataset.bubble);
      if (!b) return;
      const nr = node.getBoundingClientRect();
      const cr = card.getBoundingClientRect();
      b.style.left = (nr.left - cr.left + nr.width / 2) + 'px';
      b.style.top = (nr.top - cr.top - b.offsetHeight - 10) + 'px';
    }
    function hideAll() {
      layer.querySelectorAll('.tl-bubble').forEach(x => x.classList.remove('show'));
      nodes.forEach(n => n.classList.remove('active'));
    }
    function show(node) {
      hideAll();
      const b = document.getElementById(node.dataset.bubble);
      if (!b) return;
      b.classList.add('show');
      node.classList.add('active');
      place(node);
      // 贴边时贴着卡片内沿，避免气泡被视口切掉
      const br = b.getBoundingClientRect();
      const cr = card.getBoundingClientRect();
      if (br.left < cr.left + 6) {
        b.style.left = (parseFloat(b.style.left) + (cr.left + 6 - br.left)) + 'px';
      } else if (br.right > cr.right - 6) {
        b.style.left = (parseFloat(b.style.left) - (br.right - (cr.right - 6))) + 'px';
      }
    }

    // 悬停弹泡
    nodes.forEach(function (n) {
      n.addEventListener('mouseenter', () => show(n));
      n.addEventListener('focus', () => show(n));
      n.addEventListener('mouseleave', hideAll);
      n.addEventListener('blur', hideAll);
    });
    // 横向滚动时气泡跟随
    if (scroll) scroll.addEventListener('scroll', () => {
      const act = card.querySelector('.tl-node.active');
      if (act) place(act);
    });
    window.addEventListener('resize', hideAll);

    // 点击节点（圆点或文字都算）→ 平滑滚到对应要点
    function go(node) {
      const id = node.dataset.goto;
      if (!id) return;
      const target = document.getElementById(id);
      if (!target) return;
      hideAll();
      target.scrollIntoView({behavior: 'smooth', block: 'start'});
      target.classList.add('flash');
      setTimeout(() => target.classList.remove('flash'), 1600);
    }
    nodes.forEach(n => n.addEventListener('click', () => go(n)));
    // 键盘：Enter / 空格（button 默认已处理，这里补一层保险）
    nodes.forEach(n => n.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(n); }
    }));
    // 气泡内的「跳到要点」链接同样是平滑滚动
    layer.querySelectorAll('a[data-goto]').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        const t = document.getElementById(a.dataset.goto);
        if (!t) return;
        hideAll();
        t.scrollIntoView({behavior: 'smooth', block: 'start'});
        t.classList.add('flash');
        setTimeout(() => t.classList.remove('flash'), 1600);
      });
    });
  });
})();
</script>
"""


def _to_bottom_html() -> str:
    """悬浮「回到底部 / 回到顶部」按钮 + 脚本。

    用户反馈："文稿总结页面右侧加一个悬浮按钮，点击快速滑到最下面，
    避免文章过长，让用户可以快速到下载的部分。"

    实测产物 236~300 行，滚到下载区要很久，所以按钮默认显示「⬇ 到下载」。
    同时给一个「⬆ 顶部」——滚到底之后想回去也一样费劲。
    两个按钮都是**滚动超过一屏才出现**：页面短的时候它们只是干扰。
    """
    return """
<div class="to-bottom">
  <button type="button" class="ghost" id="to-top" aria-label="回到顶部">⬆ 顶部</button>
  <button type="button" id="to-bottom" aria-label="跳到页面最下方">⬇ 到下载</button>
</div>
<script>
(function () {
  const top = document.getElementById('to-top');
  const bottom = document.getElementById('to-bottom');
  if (!top || !bottom) return;
  const THRESHOLD = window.innerHeight * 0.6;

  function sync() {
    const y = window.scrollY || document.documentElement.scrollTop;
    const nearBottom = y + window.innerHeight >=
                       document.documentElement.scrollHeight - 120;
    bottom.classList.toggle('show', y > THRESHOLD && !nearBottom);
    top.classList.toggle('show', y > THRESHOLD);
  }
  // 平滑滚动：优先用 scrollTo({behavior:'smooth'})，
  // 不支持时退回瞬时跳转（不要静默什么都不做）
  function scrollToEnd() {
    const h = document.documentElement.scrollHeight;
    if ('scrollBehavior' in document.documentElement.style) {
      window.scrollTo({top: h, behavior: 'smooth'});
    } else {
      window.scrollTo(0, h);
    }
  }
  function scrollToTop() {
    if ('scrollBehavior' in document.documentElement.style) {
      window.scrollTo({top: 0, behavior: 'smooth'});
    } else {
      window.scrollTo(0, 0);
    }
  }
  bottom.addEventListener('click', function (e) {
    e.preventDefault();
    scrollToEnd();
  });
  top.addEventListener('click', function (e) {
    e.preventDefault();
    scrollToTop();
  });
  window.addEventListener('scroll', sync, {passive: true});
  window.addEventListener('resize', sync);
  sync();
})();
</script>
"""

def _render_done(t, r: dict) -> str:
    """已完成任务的详情渲染：速览 / 总结 / 时间轴 / 分段要点。"""
    from .summarize import _trim_insight

    kps = r.get("key_points") or []
    blocks = []
    for i, p in enumerate(kps, 1):
        cat = (f'<span class="pill">{html.escape(p.get("category",""))}</span> '
               if p.get("category") else "")
        topic = html.escape(p.get("topic") or f"要点 {i}")
        body = _paragraphs(p.get("text", "")).replace(
            "<p style='margin:0 0 12px'", "<p class='kp-t'"
        )
        insight = (
            f'<div class="kp-insight"><span class="tag">一句话</span>'
            f'<span>{html.escape(_trim_insight(p["insight"]))}</span></div>'
            if p.get("insight") else ""
        )
        anchors = " ".join(
            f'<a href="https://www.bilibili.com/video/{t.bvid}?t={_sec(a)}" '
            f'target="_blank" rel="noreferrer">▶ {html.escape(a)}</a>'
            for a in (p.get("anchors") or [])
        )
        src = (f'<div class="kp-src">跳原视频：{anchors}</div>' if anchors
               else '<div class="kp-src warn">⚠️ 无可靠出处</div>')
        blocks.append(
            f'<div class="kp" id="kp-{i}">'
            f'<h3 class="kp-h">{i}. {cat}{topic}</h3>'
            f'{body}{insight}{src}</div>'
        )

    tldr = (
        f'<h2>速览</h2><div class="card" style="font-size:15.5px">'
        f'{html.escape(r.get("tl_dr",""))}</div>'
        if r.get("tl_dr") else ""
    )
    return f"""
    {tldr}
    <h2>总结</h2>
    <div class="card">{_paragraphs(r.get('overall',''))}</div>
    {_timeline_html(t.bvid, r.get('highlights') or [])}
    <h2>要点 <span class="mut small">
      {len(kps)} 条分段陈述 · 点时间戳跳原视频</span></h2>
    <div class="card">{''.join(blocks)}</div>
    <h2>下载</h2>
    <div class="card row">
      <a class="pill" href="/files/{r.get('summary_file','')}">↓ 总结与要点</a>
      <a class="pill" href="/files/{r.get('transcript_file','')}">↓ 逐字稿</a>
    </div>
    <div class="card small mut">
      路线：{html.escape(r.get('transcript_source',''))} ·
      逐字稿 {r.get('chars',0)} 字 / {r.get('segments',0)} 段 ·
      成本 ¥{r.get('cost_cny',0)} · 策略 {html.escape(r.get('strategy',''))}
    </div>
    """


@app.get("/tasks/{task_id}/json")
def task_json(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    d = t.to_public()
    d["queue_position"] = tasks.queue_position(task_id)
    return d


@app.get("/files/{name}")
def download(name: str):
    safe = Path(name).name
    if not safe.endswith(".md") or safe != name:
        raise HTTPException(400, "非法文件名")
    path = OUTPUT_DIR / safe
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=safe)


@app.get("/quota", response_class=HTMLResponse)
def quota_page(request: Request):
    try:
        creds = load_operator_credentials(required=False)
    except CredentialPolicyError:
        return _page(
            "<header><h1>配额</h1></header>"
            '<div class="note">服务凭据尚未就绪，请联系管理员或扫码登录。</div>' + _nav(None, current="/quota")
        )
    if creds is None:
        return _page(
            "<header><h1>配额</h1></header>"
            '<div class="card"><div class="empty">尚未登录，请先扫码登录。</div></div>'
            + _nav(None, current="/quota")
        )

    st = report(creds.uid)
    rule = st.rule
    if rule.unlimited:
        summary = "<span class='pill ok'>不限</span>"
        bars = "<p class='mut small'>管理员账号不受每日配额限制。</p>"
    else:
        vpct = min(100, int(st.used_videos / max(rule.daily_videos, 1) * 100))
        spct = min(100, int(st.used_seconds / max(rule.daily_seconds, 1) * 100))
        summary = f"<span class='pill warn'>受限</span>"
        bars = f"""
        <div style="margin-top:16px">
          <div class="row" style="justify-content:space-between">
            <span class="small">视频数</span>
            <span class="mut small">{st.used_videos} / {rule.daily_videos}</span></div>
          <div class="bar" style="margin-top:6px"><i style="width:{vpct}%"></i></div>
        </div>
        <div style="margin-top:16px">
          <div class="row" style="justify-content:space-between">
            <span class="small">当日累计时长</span>
            <span class="mut small">{st.used_seconds // 60} / {rule.daily_seconds // 60} 分钟</span></div>
          <div class="bar" style="margin-top:6px"><i style="width:{spct}%"></i></div>
        </div>
        """
    body = f"""
    <header><h1>配额</h1></header>
    <p class="sub">按<strong>登录的 B 站账号</strong>计数，换浏览器或清 Cookie 都无法绕过</p>
    <div class="card">
      <div class="kv"><span class="mut">账号</span>
        <b>{html.escape(creds.uname or creds.uid)}</b><code>{creds.uid}</code>{summary}</div>
      {bars}
    </div>
    <div class="card small mut">
      判定发生在<strong>取字幕之前</strong>：配额不足时不会消耗带宽。<br>
      只有真正产出产物的任务才计入额度，失败的不会占用。
    </div>
    {_nav(None, current="/quota")}
    """
    return _page(body)


@app.get("/apikey-check")
def apikey_check():
    """不暴露 key 本身，只回答"配置是否就绪"。"""
    key = os.getenv("DEEPSEEK_API_KEY", "")
    return {
        "configured": bool(key),
        "hint": (key[:6] + "…" + key[-4:]) if len(key) > 12 else "",
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        "disclaimer_version": DISCLAIMER_VERSION,
    }
