"""HTTP 层共用的身份校验。

登录态的唯一判据是：token 能解出 user_id，且该账号在库里存在、状态为 active。
把这个判断收在一处，是为了让「禁用账号」立刻生效——否则已签发的 token 会一直
有效到进程重启，管理员会以为禁用没起作用。
"""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException, Request

from app.core import users as users_repo
from app.core.auth import decode_token


def _load(authorization: str | None, missing: str, invalid: str) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail=missing)
    user_id = decode_token(authorization[7:])
    row = users_repo.get_user_by_id(user_id) if user_id else None
    if row is None or row["status"] != users_repo.STATUS_ACTIVE:
        raise HTTPException(status_code=401, detail=invalid)
    return row


def require_user(
    authorization: str | None,
    *,
    missing: str = "需要登录",
    invalid: str = "token 无效或已过期",
) -> str:
    """要求登录，返回 user_id。文案可定制，便于各路由保留既有提示。"""
    return _load(authorization, missing, invalid)["id"]


def require_admin(
    authorization: str | None,
    *,
    missing: str = "需要登录",
    invalid: str = "token 无效或已过期",
) -> str:
    row = _load(authorization, missing, invalid)
    if row["role"] != users_repo.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return row["id"]


def optional_user_id(request: Request) -> str | None:
    """可选登录场景：未登录（或账号被禁用）返回 None，而不是抛 401。"""
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    user_id = decode_token(authorization[7:])
    row = users_repo.get_user_by_id(user_id) if user_id else None
    if row is None or row["status"] != users_repo.STATUS_ACTIVE:
        return None
    return row["id"]


def require_user_from_request(
    request: Request,
    *,
    missing: str = "需要登录",
    invalid: str = "token 无效或已过期",
) -> str:
    return require_user(request.headers.get("Authorization"), missing=missing, invalid=invalid)
