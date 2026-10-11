"""B 站扫码登录（方案 §4.1 实测的四种状态）。

状态机：
    86101 未扫码      → 继续轮询
    86090 已扫码待确认 → 提示用户在手机上确认
    0     登录成功     → 响应头 Set-Cookie 携带 SESSDATA / bili_jct / DedeUserID
    86038 二维码失效   → 必须重新生成二维码

不手工复制 Cookie，凭证由 save_credentials() 加密落盘（R11）。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from enum import Enum

import requests

from .bilibili import REFERER, UA, BiliError
from .storage import Credentials, save_credentials

log = logging.getLogger(__name__)

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"


class QrState(str, Enum):
    PENDING = "86101"        # 未扫码
    SCANNED = "86090"        # 已扫码待确认
    SUCCESS = "0"            # 登录成功
    EXPIRED = "86038"        # 二维码失效


@dataclass
class QrSession:
    url: str
    qrcode_key: str
    session: requests.Session


def start_qr_login() -> QrSession:
    """① 生成二维码。返回的 url 交给前端渲染成二维码。"""
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": REFERER})
    resp = session.get(QR_GENERATE, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise BiliError(f"生成二维码失败：{data}", code=data.get("code", -1))
    payload = data["data"]
    return QrSession(
        url=payload["url"], qrcode_key=payload["qrcode_key"], session=session
    )


def poll_once(qr: QrSession) -> tuple[QrState, str]:
    """② 轮询一次，返回 (状态, 提示语)。"""
    resp = qr.session.get(
        QR_POLL, params={"qrcode_key": qr.qrcode_key}, timeout=15
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise BiliError(f"轮询失败：{body}", code=body.get("code", -1))
    data = body["data"]
    state = QrState(str(data.get("code")))
    return state, data.get("message", "")


def poll_until_done(qr: QrSession, expire_seconds: int = 180, on_state=None) -> bool:
    """轮询到成功或失效。返回是否登录成功。"""
    deadline = time.monotonic() + expire_seconds
    last: QrState | None = None
    while time.monotonic() < deadline:
        state, message = poll_once(qr)
        if state != last:
            log.info("扫码状态：%s %s", state.value, message)
            if on_state:
                on_state(state, message)
            last = state
        if state is QrState.SUCCESS:
            return True
        if state is QrState.EXPIRED:
            return False
        time.sleep(2)
    return False


def is_logged_in(qr: QrSession) -> bool:
    """登录成功的判据：会话里拿到了 SESSDATA。"""
    return bool(qr.session.cookies.get("SESSDATA"))


def persist(qr: QrSession, uname: str = "") -> Credentials:
    """③ 从 Set-Cookie 取出凭证并加密落盘，与用户身份（UID）绑定。"""
    if not is_logged_in(qr):
        raise BiliError("尚未登录成功，无法保存凭证。")
    cookies = {
        name: qr.session.cookies.get(name, "")
        for name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")
        if qr.session.cookies.get(name)
    }
    uid = cookies.get("DedeUserID", "")
    if not uid:
        raise BiliError("登录成功但未拿到 DedeUserID，凭证不完整。")
    creds = Credentials(
        uid=uid,
        uname=uname,
        cookies=cookies,
        logged_in_at=dt.datetime.now().isoformat(timespec="seconds"),
    )
    path = save_credentials(creds)
    log.info("凭证已加密保存：%s", path)
    return creds


def qr_ascii(url: str) -> str:
    """把二维码渲染成终端 ASCII 图，方便 P0 阶段在本机直接扫。"""
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    # 用半块字符压缩纵向高度：一个字符代表上下两格
    lines = []
    for y in range(0, len(matrix), 2):
        row = ""
        for x in range(len(matrix[0])):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < len(matrix) else False
            row += {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}[
                (top, bottom)
            ]
        lines.append(row)
    return "\n".join(lines)
