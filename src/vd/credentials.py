"""取数用哪个凭据 —— 大号/小号分离的强制约束。

## 为什么需要这个模块

B 站用户协议 §2.7 给的处罚权是**按账号**的：谁被用来发自动化请求，风险就落在谁头上。
所以本项目要区分两个角色：

| 角色 | 环境变量 | 是否在服务器上存凭据 | 拿什么好处 |
| --- | --- | --- | --- |
| **管理员（大号）** | `VD_OWNER_BILI_UID` | **绝不** | 配额不限 |
| **操作号（小号）** | `VD_OPERATOR_BILI_UID` | **只存它** | 承担抓取相关风险 |

**大号只用于识别"这是管理员"，永远不参与取数。** 这样即使服务触发风控、
即使凭据泄露、即使 B 站处罚，受影响的只有小号。

## 防御的是哪种错误

最容易犯的错是"取第一个凭证"——它在大号登录后就会静默地开始用大号抓取，
而且**没有任何报错**。所以这里不是"优先选小号"，而是**白名单**：
不在白名单里的凭据，一律拒绝用于取数。
"""

from __future__ import annotations

import logging
import os

from .storage import Credentials, list_uids, load_credentials

log = logging.getLogger(__name__)


class CredentialPolicyError(RuntimeError):
    """凭据使用违反大号/小号分离策略。"""


def owner_uid() -> str:
    """管理员（大号）UID —— 只决定配额，不参与取数。"""
    return os.getenv("VD_OWNER_BILI_UID", "").strip()


def operator_uid() -> str:
    """操作号（小号）UID —— 唯一允许用于取数的账号。"""
    return os.getenv("VD_OPERATOR_BILI_UID", "").strip()


def admin_uids() -> set[str]:
    """全部管理员 UID。配额与提交权限据此判定，与"用哪个号取数"无关。

    ## 操作号是否算管理员：由 `VD_OPERATOR_IS_ADMIN` 控制

    这里曾经有个设计错误：无条件把操作号并入管理员集合。
    后果是**没法让一个非管理员账号专门负责取数**——
    一旦设成操作号，它就自动获得了提交权限与不限配额。

    "用谁的凭据取数"和"谁能提交任务/不受配额限制"**是两件独立的事**，
    所以给一个显式开关，默认 `true`（单人使用时最省事），
    需要严格分离时设 `VD_OPERATOR_IS_ADMIN=false`。
    """
    raw = os.getenv("VD_ADMIN_UIDS", "")
    uids = {u.strip() for u in raw.split(",") if u.strip()}
    if owner_uid():
        uids.add(owner_uid())
    op = operator_uid()
    if op and os.getenv("VD_OPERATOR_IS_ADMIN", "true").strip().lower() not in (
        "0", "false", "no", "off",
    ):
        uids.add(op)
    return uids


def load_operator_credentials(required: bool = True) -> Credentials | None:
    """载入**唯一允许用于取数**的凭据。

    规则：
      1. 必须配置 `VD_OPERATOR_BILI_UID`；
      2. 该 UID 的凭据必须已登录并落盘；
      3. 若操作号 == 管理员大号，直接拒绝并提示（等于没做隔离）。

    Args:
        required: True 时缺配置/缺凭据就抛异常（脚本用）；
                  False 时返回 None（Web 层用它渲染"请先登录"页面）。
    """
    op = operator_uid()
    owner = owner_uid()

    if not op:
        if required:
            raise CredentialPolicyError(
                "未配置 VD_OPERATOR_BILI_UID。\n"
                "本项目规定：取数只能用操作号（小号）的凭据，大号凭据不得用于取数。\n"
                "请先在 .env 里设置 VD_OPERATOR_BILI_UID=<小号 UID>，"
                "再运行 `python scripts/login.py` 用小号扫码登录。"
            )
        return None

    if owner and op == owner:
        raise CredentialPolicyError(
            f"VD_OPERATOR_BILI_UID 与 VD_OWNER_BILI_UID 相同（都是 {op}）。\n"
            "这等于把大号暴露给抓取自动化，与「大号只做管理员」的约定冲突。\n"
            "请用小号 UID 作为 VD_OPERATOR_BILI_UID；"
            "若你确实只有这一个账号，请显式接受风险后再继续。"
        )

    try:
        creds = load_credentials(op)
    except Exception as exc:  # noqa: BLE001
        if required:
            raise CredentialPolicyError(
                f"操作号 UID={op} 尚未登录，或凭据无法解密：{exc}\n"
                "请运行 `python scripts/login.py`（显示二维码）"
                "或 `python scripts/login_qr.py`（输出 PNG 二维码）用小号扫码登录。"
            ) from exc
        return None

    others = [u for u in list_uids() if u != op]
    if others:
        log.warning(
            "检测到非操作号的凭据已落盘：%s。它们**不会被用于取数**"
            "（白名单只放行 UID=%s）。建议用 `python scripts/login.py --logout` 清理。",
            others, op,
        )
    return creds


def assert_allowed(uid: str) -> None:
    """断言某个 UID 允许用于取数 —— 供 Web 层在每次取数前调用。"""
    op = operator_uid()
    if not op:
        raise CredentialPolicyError("未配置 VD_OPERATOR_BILI_UID，拒绝取数。")
    if str(uid) != op:
        raise CredentialPolicyError(
            f"UID={uid} 不是操作号（操作号={op}），拒绝用它取数。"
        )
