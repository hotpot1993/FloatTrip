"""管理员后台接口：账号管理、单个账号数据清理与整库重置。

权限模型：所有接口都要求 Bearer token 对应的账号 role='admin'。管理员不能
禁用或删除自己——那会把后台锁死，而且没有第二个入口能救回来。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import require_admin
from app.core import users as users_repo
from app.core.thread_store import thread_store

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 重置整库需要原文确认短语：破坏性操作不该只靠一次点击。
RESET_CONFIRM_PHRASE = "RESET"
MIN_PASSWORD_LENGTH = 6


class StatusRequest(BaseModel):
    status: Literal["active", "disabled"]


class PasswordRequest(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)


class ResetRequest(BaseModel):
    confirm: str
    password: str


def _not_found(user_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail="账号不存在")


def _conflict(exc: users_repo.ActiveRunsExist) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"还有 {exc.count} 个进行中的任务，请等它结束或先取消后再操作",
    )


@router.get("/users")
def list_users(
    q: str | None = Query(default=None, max_length=100),
    authorization: str | None = Header(default=None),
):
    """账号列表（含数据量统计）与全局概览。"""
    admin_id = require_admin(authorization)
    return {
        "me": admin_id,
        "summary": users_repo.database_summary(),
        "users": users_repo.list_users(q),
    }


@router.get("/users/{user_id}")
def get_user(user_id: str, authorization: str | None = Header(default=None)):
    """某个账号的只读明细：行程、对话、长期记忆与任务记录。"""
    require_admin(authorization)
    try:
        return users_repo.user_detail(user_id)
    except users_repo.UserNotFound:
        raise _not_found(user_id)


@router.post("/users/{user_id}/status")
def set_user_status(
    user_id: str,
    body: StatusRequest,
    authorization: str | None = Header(default=None),
):
    """禁用 / 启用账号。禁用后该账号立刻无法登录，已签发的 token 同步失效。"""
    admin_id = require_admin(authorization)
    if user_id == admin_id and body.status == users_repo.STATUS_DISABLED:
        raise HTTPException(status_code=400, detail="不能禁用当前登录的管理员账号")
    try:
        return users_repo.set_status(user_id, body.status)
    except users_repo.UserNotFound:
        raise _not_found(user_id)


@router.post("/users/{user_id}/password")
def reset_user_password(
    user_id: str,
    body: PasswordRequest,
    authorization: str | None = Header(default=None),
):
    """管理员直接给账号设置新密码，无需旧密码（管理员也可以改自己的）。"""
    require_admin(authorization)
    try:
        return users_repo.set_password(user_id, body.password)
    except users_repo.UserNotFound:
        raise _not_found(user_id)


@router.post("/users/{user_id}/clear")
def clear_user_data(user_id: str, authorization: str | None = Header(default=None)):
    """清空某个账号的行程 / 对话 / 记忆 / 任务记录，账号保留。"""
    require_admin(authorization)
    try:
        return users_repo.clear_user_data(user_id)
    except users_repo.UserNotFound:
        raise _not_found(user_id)
    except users_repo.ActiveRunsExist as exc:
        raise _conflict(exc)


@router.delete("/users/{user_id}")
def delete_user(user_id: str, authorization: str | None = Header(default=None)):
    """删除账号及其全部数据。"""
    admin_id = require_admin(authorization)
    if user_id == admin_id:
        raise HTTPException(status_code=400, detail="不能删除当前登录的管理员账号")
    try:
        return users_repo.delete_user(user_id)
    except users_repo.UserNotFound:
        raise _not_found(user_id)
    except users_repo.ActiveRunsExist as exc:
        raise _conflict(exc)


@router.post("/reset")
def reset_database(body: ResetRequest, authorization: str | None = Header(default=None)):
    """重置整个数据库：清空业务数据，保留全部账号。

    要求同时给出确认短语与当前管理员密码——这一步不可撤销，口令是最后一道闸。
    """
    admin_id = require_admin(authorization)
    if body.confirm.strip().upper() != RESET_CONFIRM_PHRASE:
        raise HTTPException(status_code=400, detail=f"确认短语不正确，请输入 {RESET_CONFIRM_PHRASE}")
    if not users_repo.verify_password_for(admin_id, body.password):
        raise HTTPException(status_code=403, detail="管理员密码不正确")
    try:
        result = users_repo.reset_business_data()
    except users_repo.ActiveRunsExist as exc:
        raise _conflict(exc)
    # 续接用的待补信息存在进程内存里，指向的行程已经没了，一并丢掉。
    thread_store.clear()
    return result
