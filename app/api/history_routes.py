"""历史行程 API：归档列表、单条行程详情与删除。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.api.deps import require_user
from app.core import memory
from app.core.database import get_conn

router = APIRouter(prefix="/api/history", tags=["history"])


def _require_user(authorization: str | None) -> str:
    return require_user(authorization, missing="未登录", invalid="token 无效或已过期")


@router.get("")
def get_history(authorization: str | None = Header(default=None)):
    user_id = _require_user(authorization)
    with get_conn() as conn:
        items = memory.list_itineraries(user_id, conn)
    return items


@router.get("/{plan_id}")
def get_itinerary(plan_id: str, authorization: str | None = Header(default=None)):
    user_id = _require_user(authorization)
    with get_conn() as conn:
        # 验证该行程属于该用户
        row = conn.execute(
            "SELECT user_id FROM itineraries WHERE id=?", (plan_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "行程不存在")
        if row["user_id"] != user_id:
            raise HTTPException(403, "无权访问")
        data = memory.load_itinerary(plan_id, conn)
    return data


@router.delete("/{plan_id}")
def delete_itinerary(plan_id: str, authorization: str | None = Header(default=None)):
    """删除一条行程。不可恢复，且永远不被引用它的规划任务阻塞。

    BEGIN IMMEDIATE 让"查所有权 → 清空 Run 引用 → 删行程"三步处于同一事务，
    避免并发下清空引用后行程已被他人删除而留下空引用。
    """
    user_id = _require_user(authorization)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id FROM itineraries WHERE id=?", (plan_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "行程不存在")
        if row["user_id"] != user_id:
            raise HTTPException(403, "无权访问")
        if not memory.delete_itinerary(user_id, plan_id, conn):
            raise HTTPException(404, "行程不存在")
    return {"id": plan_id, "deleted": True}
