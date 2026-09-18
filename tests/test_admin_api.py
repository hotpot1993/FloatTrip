"""管理员后台 API 测试：账号管理、单个账号数据清理与整库重置。

这些用例锁住的是「管理员能力」的对外契约，重点是三条不可退让的线：
普通账号进不来、被禁用账号当场失效、破坏性操作不能误伤账号本身。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """隔离 SQLite，并准备好一个管理员账号 root。"""
    import app.core.database as database

    # 默认检查点库是仓库里的真实文件，测试必须把它挪到临时目录，
    # 否则「重置整库」的用例会顺手清掉开发者本机的规划检查点。
    monkeypatch.setenv("RUNTIME_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "admin.db")
    database.init_db()

    from app.core import users as users_repo

    users_repo.ensure_admin_account(ADMIN_USERNAME, ADMIN_PASSWORD)
    return database


@pytest.fixture()
def client(db):
    try:
        from app.main import app
    except ModuleNotFoundError:
        from fastapi import FastAPI

        from app.api.admin_routes import router as admin_router
        from app.api.auth_routes import router as auth_router
        from app.api.history_routes import router as history_router

        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(admin_router)
        app.include_router(history_router)
    return TestClient(app)


ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-pw-123"


# ─── 造数据 ───────────────────────────────────────────────────


def make_user(username: str, *, password: str = "pw-123456", role: str = "user", status: str = "active") -> str:
    from app.core.auth import hash_password
    from app.core.database import get_conn

    user_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, username, hash_password(password), role, status, "2026-01-01T00:00:00Z"),
        )
    return user_id


def headers_for(user_id: str) -> dict:
    from app.core.auth import create_token

    return {"Authorization": "Bearer " + create_token(user_id)}


def login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def admin_headers(client) -> dict:
    response = login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def make_itinerary(user_id: str, destination: str = "南京") -> str:
    from app.core.database import get_conn
    from app.core.memory import save_itinerary

    plan = {"destination": destination, "start_date": "2026-11-07", "end_date": "2026-11-09",
            "days": [{"day": 1, "timeline": []}]}
    with get_conn() as conn:
        return save_itinerary(user_id, plan, f"{destination} 三日", conn)


def make_conversation(user_id: str) -> str:
    from app.core.database import get_conn

    conversation_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversations(id,user_id,title,status,created_at,updated_at) "
            "VALUES(?,?,'测试对话','active','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (conversation_id, user_id),
        )
    return conversation_id


def make_memory_fact(user_id: str, *, fingerprint: str | None = None) -> str:
    from app.core.database import get_conn

    fact_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO memory_facts(id,user_id,category,value_text,normalized_value,
               polarity,scope_type,scope_key,status,source_kind,sensitivity,
               evidence_sequences_json,confidence,fingerprint,created_at,updated_at)
               VALUES(?,?,'food_preference','不吃辣','不吃辣','avoid','global','{}',
               'active','explicit_chat','normal','[]',1.0,?,?,?)""",
            (fact_id, user_id, fingerprint or f"fp-{fact_id}", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
    return fact_id


def make_active_run(user_id: str, status: str = "running") -> str:
    from app.core.database import get_conn

    run_id = str(uuid.uuid4())
    now = "2026-01-01T00:00:00Z"
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO runs(id,user_id,kind,status,concurrency_key,request_snapshot_json,
               disconnect_policy,created_at,queued_at,updated_at)
               VALUES(?,?,'travel_plan',?,?,'{}','continue',?,?,?)""",
            (run_id, user_id, status, f"plan:{user_id}", now, now, now),
        )
    return run_id


def table_count(table: str, where: str = "", params: tuple = ()) -> int:
    from app.core.database import get_conn

    with get_conn() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0])


# ─── 权限边界 ─────────────────────────────────────────────────


class TestAdminPermission:
    def test_未登录访问后台返回401(self, client):
        assert client.get("/api/admin/users").status_code == 401

    def test_普通用户访问后台返回403(self, client):
        user_id = make_user("normal")
        assert client.get("/api/admin/users", headers=headers_for(user_id)).status_code == 403

    def test_登录接口返回角色(self, client):
        make_user("normal")
        assert login(client, "normal", "pw-123456").json()["role"] == "user"
        assert login(client, ADMIN_USERNAME, ADMIN_PASSWORD).json()["role"] == "admin"

    def test_当前账号接口返回角色(self, client):
        headers = admin_headers(client)
        body = client.get("/api/auth/me", headers=headers).json()
        assert body["username"] == ADMIN_USERNAME
        assert body["role"] == "admin"

    def test_禁用后已签发token立即失效(self, client):
        user_id = make_user("normal")
        headers = headers_for(user_id)
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        assert client.post(
            f"/api/admin/users/{user_id}/status",
            json={"status": "disabled"}, headers=admin_headers(client),
        ).status_code == 200

        assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert client.get("/api/history", headers=headers).status_code == 401

    def test_被禁用账号无法登录且启用后恢复(self, client):
        user_id = make_user("normal")
        admin = admin_headers(client)

        client.post(f"/api/admin/users/{user_id}/status", json={"status": "disabled"}, headers=admin)
        blocked = login(client, "normal", "pw-123456")
        assert blocked.status_code == 403
        assert "禁用" in blocked.json()["detail"]

        client.post(f"/api/admin/users/{user_id}/status", json={"status": "active"}, headers=admin)
        assert login(client, "normal", "pw-123456").status_code == 200

    def test_管理员不能禁用或删除自己(self, client):
        headers = admin_headers(client)
        me_id = client.get("/api/auth/me", headers=headers).json()["user_id"]

        blocked = client.post(f"/api/admin/users/{me_id}/status", json={"status": "disabled"}, headers=headers)
        assert blocked.status_code == 400
        assert client.delete(f"/api/admin/users/{me_id}", headers=headers).status_code == 400

    def test_不能删除账号后仍然用旧token(self, client):
        user_id = make_user("normal")
        headers = headers_for(user_id)
        assert client.delete(f"/api/admin/users/{user_id}", headers=admin_headers(client)).status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 401


# ─── 账号列表与明细 ───────────────────────────────────────────


class TestUserList:
    def test_列表带数据量统计且管理员优先(self, client):
        user_id = make_user("alice")
        make_itinerary(user_id)
        make_conversation(user_id)
        make_memory_fact(user_id)

        body = client.get("/api/admin/users", headers=admin_headers(client)).json()

        assert body["users"][0]["username"] == ADMIN_USERNAME
        alice = next(item for item in body["users"] if item["username"] == "alice")
        assert (alice["itinerary_count"], alice["conversation_count"], alice["memory_fact_count"]) == (1, 1, 1)
        assert body["summary"]["user_count"] == 2
        assert body["summary"]["admin_count"] == 1

    def test_按用户名搜索(self, client):
        make_user("alice")
        make_user("bob")

        names = [u["username"] for u in
                 client.get("/api/admin/users?q=ali", headers=admin_headers(client)).json()["users"]]
        assert names == ["alice"]

    def test_明细返回行程对话与记忆(self, client):
        user_id = make_user("alice")
        make_itinerary(user_id, "苏州")
        make_conversation(user_id)
        make_memory_fact(user_id)

        detail = client.get(f"/api/admin/users/{user_id}", headers=admin_headers(client)).json()

        assert detail["user"]["username"] == "alice"
        assert [item["destination"] for item in detail["itineraries"]] == ["苏州"]
        assert len(detail["conversations"]) == 1
        assert [fact["value_text"] for fact in detail["memory_facts"]] == ["不吃辣"]

    def test_明细不存在的账号返回404(self, client):
        assert client.get(
            f"/api/admin/users/{uuid.uuid4()}", headers=admin_headers(client)
        ).status_code == 404


# ─── 账号变更 ─────────────────────────────────────────────────


class TestUserMutation:
    def test_重置密码后旧密码失效(self, client):
        user_id = make_user("alice")
        headers = admin_headers(client)

        assert client.post(f"/api/admin/users/{user_id}/password",
                           json={"password": "new-pw-999"}, headers=headers).status_code == 200

        assert login(client, "alice", "pw-123456").status_code == 401
        assert login(client, "alice", "new-pw-999").status_code == 200

    def test_密码过短被拒绝(self, client):
        user_id = make_user("alice")
        response = client.post(f"/api/admin/users/{user_id}/password",
                               json={"password": "123"}, headers=admin_headers(client))
        assert response.status_code == 422
        assert login(client, "alice", "pw-123456").status_code == 200

    def test_清单数据清空但账号保留(self, client):
        user_id = make_user("alice")
        make_itinerary(user_id)
        make_conversation(user_id)
        make_memory_fact(user_id)

        body = client.post(f"/api/admin/users/{user_id}/clear", headers=admin_headers(client)).json()

        assert body["deleted"]["itineraries"] == 1
        assert table_count("users", " WHERE id=?", (user_id,)) == 1
        assert table_count("itineraries", " WHERE user_id=?", (user_id,)) == 0
        assert table_count("conversations", " WHERE user_id=?", (user_id,)) == 0
        assert table_count("memory_facts", " WHERE user_id=?", (user_id,)) == 0

    def test_删除账号连带清空数据(self, client):
        user_id = make_user("alice")
        make_itinerary(user_id)
        make_memory_fact(user_id)

        assert client.delete(f"/api/admin/users/{user_id}", headers=admin_headers(client)).status_code == 200

        assert table_count("users", " WHERE id=?", (user_id,)) == 0
        assert table_count("itineraries", " WHERE user_id=?", (user_id,)) == 0
        assert table_count("memory_facts", " WHERE user_id=?", (user_id,)) == 0

    def test_删除只影响目标账号(self, client):
        kept = make_user("kept")
        removed = make_user("removed")
        make_itinerary(kept)
        make_itinerary(removed)

        client.delete(f"/api/admin/users/{removed}", headers=admin_headers(client))

        assert table_count("itineraries", " WHERE user_id=?", (kept,)) == 1
        assert table_count("users", " WHERE id=?", (kept,)) == 1

    def test_有进行中任务时拒绝清空(self, client):
        user_id = make_user("alice")
        make_itinerary(user_id)
        make_active_run(user_id)

        response = client.post(f"/api/admin/users/{user_id}/clear", headers=admin_headers(client))

        assert response.status_code == 409
        assert "进行中" in response.json()["detail"]
        assert table_count("itineraries", " WHERE user_id=?", (user_id,)) == 1

    def test_操作不存在的账号返回404(self, client):
        missing = str(uuid.uuid4())
        headers = admin_headers(client)
        assert client.post(f"/api/admin/users/{missing}/status",
                           json={"status": "disabled"}, headers=headers).status_code == 404
        assert client.post(f"/api/admin/users/{missing}/clear", headers=headers).status_code == 404


# ─── 整库重置 ─────────────────────────────────────────────────


def write_checkpoint_db(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE checkpoints(thread_id TEXT, checkpoint_id TEXT)")
        conn.execute("CREATE TABLE writes(thread_id TEXT, checkpoint_id TEXT)")
        conn.execute("INSERT INTO checkpoints VALUES('t1','c1')")
        conn.execute("INSERT INTO writes VALUES('t1','c1')")
        conn.commit()
    finally:
        conn.close()


class TestDatabaseReset:
    def test_未登录或普通用户不能重置(self, client):
        user_id = make_user("normal")
        body = {"confirm": "RESET", "password": ADMIN_PASSWORD}
        assert client.post("/api/admin/reset", json=body).status_code == 401
        assert client.post("/api/admin/reset", json=body, headers=headers_for(user_id)).status_code == 403

    def test_确认短语错误被拒绝(self, client):
        response = client.post("/api/admin/reset", json={"confirm": "reset database", "password": ADMIN_PASSWORD},
                               headers=admin_headers(client))
        assert response.status_code == 400
        assert "RESET" in response.json()["detail"]

    def test_管理员密码错误被拒绝(self, client):
        response = client.post("/api/admin/reset", json={"confirm": "RESET", "password": "wrong-pw"},
                               headers=admin_headers(client))
        assert response.status_code == 403

    def test_清空业务数据但保留全部账号(self, client, db, tmp_path):
        alice = make_user("alice")
        bob = make_user("bob")
        make_itinerary(alice)
        make_conversation(alice)
        make_memory_fact(alice)
        make_itinerary(bob)

        from app.core.database import get_conn

        with get_conn() as conn:
            conn.execute("INSERT INTO amap_quota_usage(bucket,month,updated_at) VALUES('lbs','2026-01',?)",
                         ("2026-01-01T00:00:00Z",))

        checkpoint_path = tmp_path / "checkpoints.db"
        write_checkpoint_db(checkpoint_path)

        body = client.post("/api/admin/reset", json={"confirm": "reset", "password": ADMIN_PASSWORD},
                           headers=admin_headers(client)).json()

        assert body["deleted"]["itineraries"] == 2
        assert body["deleted"]["memory_facts"] == 1
        assert body["remaining_user_count"] == 3, "账号全部保留：管理员 + alice + bob"
        assert body["checkpoints"]["status"] == "cleared"
        assert table_count("itineraries") == 0
        assert table_count("conversations") == 0
        assert table_count("memory_facts") == 0
        assert table_count("amap_quota_usage") == 0
        assert table_count("users") == 3

        # 清完之后管理员仍能登录并进入后台
        assert login(client, ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200
        assert client.get("/api/admin/users", headers=admin_headers(client)).status_code == 200

        conn = sqlite3.connect(str(checkpoint_path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
        finally:
            conn.close()

    def test_有进行中任务时拒绝重置(self, client):
        alice = make_user("alice")
        make_itinerary(alice)
        make_active_run(alice, status="queued")

        response = client.post("/api/admin/reset", json={"confirm": "RESET", "password": ADMIN_PASSWORD},
                               headers=admin_headers(client))

        assert response.status_code == 409
        assert table_count("itineraries") == 1

    def test_重置后普通用户仍可登录(self, client):
        make_user("alice")
        make_itinerary(make_user("bob"))

        client.post("/api/admin/reset", json={"confirm": "RESET", "password": ADMIN_PASSWORD},
                    headers=admin_headers(client))

        assert login(client, "alice", "pw-123456").status_code == 200


# ─── 管理员账号初始化 ─────────────────────────────────────────


class TestEnsureAdminAccount:
    def test_未配置环境变量时创建默认管理员(self, db, monkeypatch):
        from app.core import users as users_repo

        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        import app.core.database as database

        monkeypatch.setattr(database, "_DB_PATH", database.get_db_path().parent / "default-admin.db")
        database.init_db()

        result = users_repo.ensure_admin_account()

        assert result == {"username": "admin", "created": True, "promoted": False,
                          "using_default_password": True}
        row = users_repo.get_user_by_username("admin")
        assert row["role"] == "admin"
        assert users_repo.verify_password_for(row["id"], "admin123")

    def test_环境变量决定用户名与密码(self, db, monkeypatch):
        from app.core import users as users_repo

        monkeypatch.setenv("ADMIN_USERNAME", "boss")
        monkeypatch.setenv("ADMIN_PASSWORD", "boss-pw-777")

        result = users_repo.ensure_admin_account()
        row = users_repo.get_user_by_username("boss")

        assert result["created"] is True
        assert result["using_default_password"] is False
        assert users_repo.verify_password_for(row["id"], "boss-pw-777")

    def test_既有账号只补角色不改密码(self, db):
        from app.core import users as users_repo

        user_id = make_user("veteran", password="my-own-pw")

        result = users_repo.ensure_admin_account("veteran", "another-pw")

        assert result["created"] is False and result["promoted"] is True
        assert users_repo.get_user_by_id(user_id)["role"] == "admin"
        assert users_repo.verify_password_for(user_id, "my-own-pw"), "已存在的密码不该被环境变量覆盖"

    def test_重复调用是幂等的(self, db):
        from app.core import users as users_repo

        users_repo.ensure_admin_account(ADMIN_USERNAME, ADMIN_PASSWORD)
        again = users_repo.ensure_admin_account(ADMIN_USERNAME, ADMIN_PASSWORD)

        assert again["created"] is False and again["promoted"] is False
        assert table_count("users", " WHERE username=?", (ADMIN_USERNAME,)) == 1
