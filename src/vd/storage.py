"""凭证的加密落盘。

方案 §5.3① / R11 的硬性要求：
  - 密钥与数据分离：密钥在 .env（VD_SECRET_KEY），密文在 data/credentials/
  - 按用户隔离：每个 B 站 UID 一个文件，互不可见
  - 支持吊销：delete_credentials()

一旦托管他人的 SESSDATA，它就是他人账号的登录态，泄露一次影响所有使用者，
所以这里不做「方便」的明文缓存，任何落盘都必须是密文。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

CRED_DIR = Path(__file__).resolve().parents[2] / "data" / "credentials"


class CredentialError(RuntimeError):
    """凭证读取失败 —— 宁可报错，不要静默降级成匿名请求（R1 的同源风险）。"""


def _load_secret() -> bytes:
    secret = os.getenv("VD_SECRET_KEY", "").strip()
    if not secret:
        raise CredentialError(
            "缺少 VD_SECRET_KEY。请复制 .env.example 为 .env 并生成一个密钥：\n"
            '  python -c "import secrets;print(secrets.token_urlsafe(48))"'
        )
    # token_urlsafe 的字符串不是合法 Fernet key，先做一次确定性派生
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _fernet() -> Fernet:
    return Fernet(_load_secret())


@dataclass
class Credentials:
    """一个 B 站账号的登录态。"""

    uid: str
    cookies: dict[str, str]
    uname: str = ""
    logged_in_at: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_owner(self) -> bool:
        owner = os.getenv("VD_OWNER_BILI_UID", "").strip()
        return bool(owner) and owner == self.uid

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def _path_for(uid: str) -> Path:
    safe = "".join(ch for ch in str(uid) if ch.isalnum() or ch in "-_")
    if not safe:
        raise CredentialError(f"非法 UID：{uid!r}")
    return CRED_DIR / f"{safe}.enc"


def save_credentials(creds: Credentials) -> Path:
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "uid": creds.uid,
            "uname": creds.uname,
            "cookies": creds.cookies,
            "logged_in_at": creds.logged_in_at,
            "extra": creds.extra,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    path = _path_for(creds.uid)
    path.write_bytes(_fernet().encrypt(payload))
    return path


def load_credentials(uid: str) -> Credentials:
    path = _path_for(uid)
    if not path.exists():
        raise CredentialError(f"未找到 UID={uid} 的登录凭证，请先扫码登录。")
    try:
        raw = _fernet().decrypt(path.read_bytes())
    except InvalidToken as exc:
        raise CredentialError(
            f"UID={uid} 的凭证无法解密。VD_SECRET_KEY 是否被换过？换过则需重新扫码登录。"
        ) from exc
    data = json.loads(raw)
    return Credentials(
        uid=data["uid"],
        uname=data.get("uname", ""),
        cookies=data["cookies"],
        logged_in_at=data.get("logged_in_at", ""),
        extra=data.get("extra", {}),
    )


def list_uids() -> list[str]:
    if not CRED_DIR.exists():
        return []
    return sorted(p.stem for p in CRED_DIR.glob("*.enc"))


def delete_credentials(uid: str) -> bool:
    """一键登出 / 吊销（方案 §5.3① 要求）。"""
    path = _path_for(uid)
    if path.exists():
        path.unlink()
        return True
    return False
