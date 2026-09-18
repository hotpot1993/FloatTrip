"""用户账号与管理员领域操作。

这个模块是「账号」这件事的唯一真相来源：账号有哪些字段、状态与角色的取值、
哪些数据属于某个用户、管理员能对账号做什么，全部收在这里。路由层只负责把
HTTP 语义翻译成这里的调用，不自己拼 SQL——否则「哪些表算业务数据」会在多处
漂移，删库和删用户迟早会不一致。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.auth import hash_password, verify_password
from app.core.database import get_conn

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_USER = "user"

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

# 这些状态的任务正被调度器或用户推进：清库会把它们脚下的数据抽走，
# 所以先拒绝，由管理员决定是等待还是先去取消。
ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_user")

# 业务数据表：整库重置与「清空单个用户」共用同一份清单。
# 元组顺序就是删除顺序，必须先删引用方——SQLite 的外键是即时校验的，
# conversations 一删，指向它的 briefs/messages 就会把删除挡回来。
_BUSINESS_TABLES = (
    "runs",
    "memory_extraction_jobs",
    "conversation_memory_states",
    "messages",
    "planning_briefs",
    "conversations",
    "memory_facts",
    "user_memory_states",
    "itineraries",
    "pending_modifications",
    "user_profiles",
)

# 自引用列：一批行一起删时「被引用者」可能先消失，所以删前先断开引用。
_SELF_REFERENCES = (
    ("runs", "retry_of_run_id"),
    ("memory_facts", "supersedes_id"),
)

# 规划图的中间态存在独立的检查点库里，属于业务数据的一部分。
_CHECKPOINT_TABLES = ("writes", "checkpoints", "checkpoint_writes", "checkpoint_blobs")


class UserNotFound(Exception):
    """账号不存在。"""


class ActiveRunsExist(Exception):
    """还有进行中的任务，破坏性操作被拒绝。"""

    def __init__(self, count: int) -> None:
        super().__init__(f"还有 {count} 个进行中的任务")
        self.count = count


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── 账号读取 ─────────────────────────────────────────────────


def get_user_by_id(user_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_username(username: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def is_admin(user_id: str) -> bool:
    row = get_user_by_id(user_id)
    return bool(row) and row["role"] == ROLE_ADMIN


def verify_password_for(user_id: str, password: str) -> bool:
    """校验某个账号的密码；账号不存在一律返回 False。"""
    row = get_user_by_id(user_id)
    if row is None:
        return False
    return verify_password(password, row["password_hash"])


def touch_login(user_id: str) -> None:
    """记录最近登录时间。失败不影响登录本身。"""
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), user_id))


# ─── 管理员账号初始化 ─────────────────────────────────────────


def ensure_admin_account(username: str | None = None, password: str | None = None) -> dict:
    """启动时保证存在一个管理员账号。

    用户名与密码优先取环境变量；都没配置时退回 admin/admin123，让全新部署
    不必先读文档就能进后台，同时把「正在使用默认口令」写进启动日志。
    已存在的账号只补管理员角色，不改密码——否则管理员在后台改过的口令会在
    下次重启时被环境变量悄悄改回去。
    """
    resolved_username = (username or os.getenv("ADMIN_USERNAME") or DEFAULT_ADMIN_USERNAME).strip()
    resolved_password = password or os.getenv("ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD
    if not resolved_username:
        raise ValueError("管理员用户名不能为空")
    if not resolved_password:
        raise ValueError("管理员密码不能为空")

    using_default = resolved_password == DEFAULT_ADMIN_PASSWORD
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, role FROM users WHERE username=?", (resolved_username,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,role,status,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    resolved_username,
                    hash_password(resolved_password),
                    ROLE_ADMIN,
                    STATUS_ACTIVE,
                    _now(),
                ),
            )
            result = {
                "username": resolved_username,
                "created": True,
                "promoted": False,
                "using_default_password": using_default,
            }
        else:
            promoted = row["role"] != ROLE_ADMIN
            if promoted:
                conn.execute(
                    "UPDATE users SET role=? WHERE id=?", (ROLE_ADMIN, row["id"])
                )
            result = {
                "username": resolved_username,
                "created": False,
                "promoted": promoted,
                "using_default_password": False,
            }

    if result["created"]:
        logger.warning(
            "已创建管理员账号 %s%s",
            resolved_username,
            "（当前为默认密码 admin123，请登录后立即在后台重置）" if using_default else "",
        )
    elif result["promoted"]:
        logger.warning("已把既有账号 %s 提升为管理员", resolved_username)
    else:
        logger.info("管理员账号 %s 已存在，未改动其密码", resolved_username)
    return result


# ─── 账号列表与明细 ───────────────────────────────────────────


def list_users(query: str | None = None) -> list[dict]:
    """账号列表 + 每个账号的数据量。管理员排在最前，便于一眼找到自己。"""
    sql = """
        SELECT u.id, u.username, u.role, u.status, u.created_at, u.last_login_at,
               (SELECT COUNT(*) FROM itineraries  i WHERE i.user_id=u.id) AS itinerary_count,
               (SELECT COUNT(*) FROM conversations c WHERE c.user_id=u.id) AS conversation_count,
               (SELECT COUNT(*) FROM memory_facts  m
                 WHERE m.user_id=u.id AND m.status<>'deleted')            AS memory_fact_count,
               (SELECT COUNT(*) FROM runs          r WHERE r.user_id=u.id) AS run_count
        FROM users u
    """
    params: tuple = ()
    keyword = (query or "").strip()
    if keyword:
        sql += " WHERE u.username LIKE ?"
        params = (f"%{keyword}%",)
    sql += " ORDER BY (u.role='admin') DESC, u.created_at DESC, u.id DESC"
    with get_conn() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def database_summary() -> dict:
    """后台首页的全局概览。"""
    with get_conn() as conn:
        def scalar(sql: str, params: tuple = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0])

        placeholders = ",".join("?" * len(ACTIVE_RUN_STATUSES))
        return {
            "user_count": scalar("SELECT COUNT(*) FROM users"),
            "admin_count": scalar("SELECT COUNT(*) FROM users WHERE role=?", (ROLE_ADMIN,)),
            "disabled_count": scalar("SELECT COUNT(*) FROM users WHERE status=?", (STATUS_DISABLED,)),
            "itinerary_count": scalar("SELECT COUNT(*) FROM itineraries"),
            "conversation_count": scalar("SELECT COUNT(*) FROM conversations"),
            "message_count": scalar("SELECT COUNT(*) FROM messages"),
            "memory_fact_count": scalar("SELECT COUNT(*) FROM memory_facts WHERE status<>'deleted'"),
            "run_count": scalar("SELECT COUNT(*) FROM runs"),
            "active_run_count": scalar(
                f"SELECT COUNT(*) FROM runs WHERE status IN ({placeholders})",
                ACTIVE_RUN_STATUSES,
            ),
        }


def user_detail(user_id: str) -> dict:
    """只读明细：账号信息 + 行程 / 对话 / 记忆 / 任务清单。

    只取必要的列并设上限——后台是给人看的，不需要把整份 plan_json 拉进浏览器。
    """
    with get_conn() as conn:
        user = conn.execute(
            "SELECT id, username, role, status, created_at, last_login_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if user is None:
            raise UserNotFound(user_id)
        itineraries = [
            dict(row)
            for row in conn.execute(
                "SELECT id, destination, start_date, end_date, version, created_at "
                "FROM itineraries WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        ]
        conversations = [
            dict(row)
            for row in conn.execute(
                "SELECT id, title, status, created_at, updated_at FROM conversations "
                "WHERE user_id=? ORDER BY updated_at DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        ]
        memory_facts = [
            dict(row)
            for row in conn.execute(
                "SELECT id, category, value_text, polarity, status, updated_at "
                "FROM memory_facts WHERE user_id=? AND status<>'deleted' "
                "ORDER BY updated_at DESC LIMIT 100",
                (user_id,),
            ).fetchall()
        ]
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT id, kind, status, created_at FROM runs "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
                (user_id,),
            ).fetchall()
        ]
    return {
        "user": dict(user),
        "itineraries": itineraries,
        "conversations": conversations,
        "memory_facts": memory_facts,
        "runs": runs,
    }


# ─── 账号变更 ─────────────────────────────────────────────────


def set_status(user_id: str, status: str) -> dict:
    if status not in (STATUS_ACTIVE, STATUS_DISABLED):
        raise ValueError("状态只能是 active 或 disabled")
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound(user_id)
        conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    return {"id": user_id, "username": row["username"], "status": status}


def set_password(user_id: str, password: str) -> dict:
    if not password:
        raise ValueError("密码不能为空")
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound(user_id)
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(password), user_id),
        )
    return {"id": user_id, "username": row["username"]}


def count_active_runs(user_id: str | None = None) -> int:
    placeholders = ",".join("?" * len(ACTIVE_RUN_STATUSES))
    sql = f"SELECT COUNT(*) FROM runs WHERE status IN ({placeholders})"
    params: tuple = tuple(ACTIVE_RUN_STATUSES)
    if user_id is not None:
        sql += " AND user_id=?"
        params += (user_id,)
    with get_conn() as conn:
        return int(conn.execute(sql, params).fetchone()[0])


# ─── 数据清理 ─────────────────────────────────────────────────


def _scope(user_id: str | None) -> tuple[str, tuple]:
    return (" WHERE user_id=?", (user_id,)) if user_id is not None else ("", ())


def _delete(conn: sqlite3.Connection, table: str, user_id: str | None) -> int:
    where, params = _scope(user_id)
    return conn.execute(f"DELETE FROM {table}{where}", params).rowcount


def _delete_run_events(conn: sqlite3.Connection, user_id: str | None) -> int:
    """run_events 没有 user_id，只能顺着 runs 找归属。"""
    if user_id is None:
        return conn.execute("DELETE FROM run_events").rowcount
    return conn.execute(
        "DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE user_id=?)",
        (user_id,),
    ).rowcount


def _wipe_business_data(conn: sqlite3.Connection, user_id: str | None) -> dict[str, int]:
    """删除业务数据，保留 users 表。

    user_id 为 None 表示整库重置；否则只清该用户。返回每张表被删的行数，
    让后台能把「到底删掉了什么」显示出来，而不是只说一句成功。
    """
    where, params = _scope(user_id)
    counts: dict[str, int] = {}
    for table, column in _SELF_REFERENCES:
        conn.execute(f"UPDATE {table} SET {column}=NULL{where}", params)
    counts["run_events"] = _delete_run_events(conn, user_id)
    for table in _BUSINESS_TABLES:
        counts[table] = _delete(conn, table, user_id)
    if user_id is None:
        # 高德用量是全局计数，不属于任何账号，只有整库重置才清。
        counts["amap_quota_usage"] = conn.execute(
            "DELETE FROM amap_quota_usage"
        ).rowcount
    return counts


def _guard_active_runs(user_id: str | None) -> None:
    active = count_active_runs(user_id)
    if active:
        raise ActiveRunsExist(active)


def clear_user_data(user_id: str) -> dict:
    """清空某个账号的行程 / 对话 / 记忆 / 任务记录，保留账号本身。"""
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound(user_id)
    _guard_active_runs(user_id)
    with get_conn() as conn:
        counts = _wipe_business_data(conn, user_id)
    return {"id": user_id, "username": row["username"], "deleted": counts}


def delete_user(user_id: str) -> dict:
    """删除账号及其全部数据。"""
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise UserNotFound(user_id)
    _guard_active_runs(user_id)
    with get_conn() as conn:
        counts = _wipe_business_data(conn, user_id)
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"id": user_id, "username": row["username"], "deleted": counts}


def clear_planning_checkpoints() -> dict:
    """清空规划图的检查点库。

    重置只清业务数据，但检查点里存着规划图的中间态——留着它们等于把已删行程
    的半成品继续留在磁盘上。运行中的图持有自己的连接，所以这里单独开一条连接
    并容忍失败：检查点清不掉，不该让整个重置失败。
    """
    path = Path(os.getenv("RUNTIME_CHECKPOINT_DB", "data/langgraph-checkpoints.db"))
    if not path.exists():
        return {"status": "skipped", "detail": "检查点库不存在"}
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in _CHECKPOINT_TABLES:
                if table in existing:
                    counts[table] = conn.execute(f"DELETE FROM {table}").rowcount
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:  # 检查点被占用不该阻塞重置
        logger.warning("清空检查点库失败：%s", exc)
        return {"status": "failed", "detail": str(exc)}
    return {"status": "cleared", "tables": counts}


def reset_business_data() -> dict:
    """重置整个数据库：清空业务数据，保留全部账号（含管理员与普通用户）。"""
    _guard_active_runs(None)
    with get_conn() as conn:
        counts = _wipe_business_data(conn, None)
        remaining = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    return {
        "deleted": counts,
        "remaining_user_count": remaining,
        "checkpoints": clear_planning_checkpoints(),
    }
