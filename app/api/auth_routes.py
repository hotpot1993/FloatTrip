"""用户注册 / 登录接口。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.api.deps import require_user
from app.core import users as users_repo
from app.core.auth import create_token, hash_password, verify_password
from app.core.database import get_conn

router = APIRouter(prefix="/api/auth", tags=["auth"])


class AuthRequest(BaseModel):
    username: str
    password: str


def _public_user(row) -> dict:
    """对外只暴露账号的公开字段，不带上密码哈希。"""
    return {
        "user_id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "status": row["status"],
    }


@router.post("/register")
def register(req: AuthRequest):
    if not req.username.strip() or not req.password:
        raise HTTPException(400, "用户名和密码不能为空")
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role, status, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    user_id,
                    req.username.strip(),
                    hash_password(req.password),
                    users_repo.ROLE_USER,
                    users_repo.STATUS_ACTIVE,
                    now,
                ),
            )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "用户名已存在")
        raise HTTPException(500, "注册失败")
    return {
        "user_id": user_id,
        "token": create_token(user_id),
        "username": req.username.strip(),
        "role": users_repo.ROLE_USER,
    }


@router.post("/login")
def login(req: AuthRequest):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash, role, status FROM users WHERE username=?",
            (req.username.strip(),),
        ).fetchone()
    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    if row["status"] != users_repo.STATUS_ACTIVE:
        raise HTTPException(403, "账号已被禁用，请联系管理员")
    users_repo.touch_login(row["id"])
    return {
        "user_id": row["id"],
        "token": create_token(row["id"]),
        "username": req.username.strip(),
        "role": row["role"],
    }


@router.get("/me")
def me(authorization: str | None = Header(default=None)):
    """当前登录账号。前端据此决定是否显示后台入口。"""
    user_id = require_user(authorization, missing="未登录", invalid="登录已过期，请重新登录")
    row = users_repo.get_user_by_id(user_id)
    if row is None:  # 校验与读取之间账号被删掉
        raise HTTPException(401, "登录已过期，请重新登录")
    return _public_user(row)
