"""历史行程 API 测试：归档列表、详情与删除的连带边界。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离 SQLite 到临时目录，并提供挂载了历史行程路由的应用。

    优先使用完整应用 app.main（含中间件、CORS 与全部路由）。当环境缺少
    langgraph 一类重型依赖时，退化为只挂载 history_router 的最小应用——
    本文件只断言历史行程的对外契约，两种装配下语义一致。
    """
    import app.core.database as database

    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "test.db")
    database.init_db()

    try:
        from app.main import app
    except ModuleNotFoundError:
        from fastapi import FastAPI

        from app.api.history_routes import router

        app = FastAPI()
        app.include_router(router)
    return TestClient(app)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """只隔离数据库，不导入 app.main（供持久层测试使用）。"""
    import app.core.database as database

    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database


def make_auth():
    """造一个用户 id + Bearer 头。"""
    from app.core.auth import create_token
    from app.core.database import get_conn

    uid = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
            (uid, f"test-{uid}", "test", "2026-01-01T00:00:00Z"),
        )
    return uid, {"Authorization": "Bearer " + create_token(uid)}


def make_itinerary(user_id: str, *, destination: str = "南京", parent_id: str | None = None) -> str:
    """直接落一条行程，返回 plan_id。"""
    from app.core.database import get_conn
    from app.core.memory import save_itinerary

    plan = {
        "destination": destination,
        "start_date": "2026-11-07",
        "end_date": "2026-11-10",
        "days": [{"day": 1, "timeline": []}],
    }
    with get_conn() as conn:
        return save_itinerary(user_id, plan, f"{destination}，2026-11-07至2026-11-10", conn, parent_id=parent_id)


def make_run(user_id: str, *, result_itinerary_id: str | None, conversation_id: str | None = None) -> str:
    """落一条 Run，可指定它引用的结果行程。"""
    from app.core.database import get_conn

    run_id = str(uuid.uuid4())
    now = "2026-01-01T00:00:00Z"
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO runs(id,user_id,conversation_id,kind,status,concurrency_key,
               request_snapshot_json,disconnect_policy,result_itinerary_id,created_at,queued_at,updated_at)
               VALUES(?,?,?,'travel_plan','succeeded',?,'{}','continue',?,?,?,?)""",
            (run_id, user_id, conversation_id, f"plan:{user_id}", result_itinerary_id, now, now, now),
        )
        conn.execute(
            """INSERT INTO run_events(id,run_id,sequence,kind,payload_json,durable,created_at)
               VALUES(?,?,1,'planning.itinerary_created','{}',1,?)""",
            (str(uuid.uuid4()), run_id, now),
        )
    return run_id


def make_conversation_with_message(user_id: str, *, related_itinerary_id: str | None) -> tuple[str, str]:
    """落一条对话与一条指向行程的消息，返回 (conversation_id, message_id)。"""
    from app.core.database import get_conn

    conversation_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    now = "2026-01-01T00:00:00Z"
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO conversations(id,user_id,title,status,created_at,updated_at)
               VALUES(?,?,'测试对话','active',?,?)""",
            (conversation_id, user_id, now, now),
        )
        conn.execute(
            """INSERT INTO messages(id,conversation_id,user_id,role,content,sequence,
               related_itinerary_id,created_at)
               VALUES(?,?,?,'assistant','这是你的行程',1,?,?)""",
            (message_id, conversation_id, user_id, related_itinerary_id, now),
        )
    return conversation_id, message_id


class TestDeleteItineraryPersistence:
    """持久层测试：不经过 HTTP，直接验证删除与引用置空的原子行为。"""

    def _user(self) -> str:
        from app.core.database import get_conn

        uid = str(uuid.uuid4())
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                (uid, f"u-{uid}", "test", "2026-01-01T00:00:00Z"),
            )
        return uid

    def test_不先清空引用则外键阻止删除(self, db):
        """记录这个删除操作存在的根本原因：直接删会被外键挡住。"""
        import sqlite3

        from app.core.database import get_conn

        uid = self._user()
        plan_id = make_itinerary(uid)
        make_run(uid, result_itinerary_id=plan_id)

        with pytest.raises(sqlite3.IntegrityError):
            with get_conn() as conn:
                conn.execute("DELETE FROM itineraries WHERE id=?", (plan_id,))

        with get_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM itineraries WHERE id=?", (plan_id,)
            ).fetchone()["n"] == 1

    def test_删除同时清空引用(self, db):
        from app.core.database import get_conn
        from app.core.memory import delete_itinerary

        uid = self._user()
        plan_id = make_itinerary(uid)
        run_id = make_run(uid, result_itinerary_id=plan_id)

        with get_conn() as conn:
            assert delete_itinerary(uid, plan_id, conn) is True

        with get_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM itineraries WHERE id=?", (plan_id,)
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT result_itinerary_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()["result_itinerary_id"] is None

    def test_非拥有者删除返回False且不产生副作用(self, db):
        from app.core.database import get_conn
        from app.core.memory import delete_itinerary

        owner = self._user()
        intruder = self._user()
        plan_id = make_itinerary(owner)
        run_id = make_run(owner, result_itinerary_id=plan_id)

        with get_conn() as conn:
            assert delete_itinerary(intruder, plan_id, conn) is False

        with get_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM itineraries WHERE id=?", (plan_id,)
            ).fetchone()["n"] == 1
            assert conn.execute(
                "SELECT result_itinerary_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()["result_itinerary_id"] == plan_id, "越权尝试不得清空引用"

    def test_删除不存在的行程返回False(self, db):
        from app.core.database import get_conn
        from app.core.memory import delete_itinerary

        uid = self._user()
        with get_conn() as conn:
            assert delete_itinerary(uid, str(uuid.uuid4()), conn) is False

    def test_删除后修改版仍可加载且parent_id悬空(self, db):
        from app.core.database import get_conn
        from app.core.memory import delete_itinerary, load_itinerary

        uid = self._user()
        root_id = make_itinerary(uid)
        child_id = make_itinerary(uid, parent_id=root_id)

        with get_conn() as conn:
            assert delete_itinerary(uid, root_id, conn) is True

        with get_conn() as conn:
            child = load_itinerary(child_id, conn)
        assert child is not None
        assert child["parent_id"] == root_id
        assert child["version"] == 2

    def test_删除只影响目标行程(self, db):
        from app.core.database import get_conn
        from app.core.memory import delete_itinerary, list_itineraries

        uid = self._user()
        kept_id = make_itinerary(uid, destination="苏州")
        removed_id = make_itinerary(uid, destination="南京")

        with get_conn() as conn:
            assert delete_itinerary(uid, removed_id, conn) is True

        with get_conn() as conn:
            remaining = [item["id"] for item in list_itineraries(uid, conn)]
        assert remaining == [kept_id]


class TestDeleteItinerary:
    def test_未登录返回401(self, client):
        r = client.delete(f"/api/history/{uuid.uuid4()}")
        assert r.status_code == 401

    def test_删除自己的行程后该行消失(self, client):
        uid, headers = make_auth()
        plan_id = make_itinerary(uid)

        r = client.delete(f"/api/history/{plan_id}", headers=headers)

        assert r.status_code == 200
        assert r.json() == {"id": plan_id, "deleted": True}
        assert client.get(f"/api/history/{plan_id}", headers=headers).status_code == 404
        assert client.get("/api/history", headers=headers).json() == []

    def test_重复删除返回404(self, client):
        uid, headers = make_auth()
        plan_id = make_itinerary(uid)

        assert client.delete(f"/api/history/{plan_id}", headers=headers).status_code == 200
        second = client.delete(f"/api/history/{plan_id}", headers=headers)

        assert second.status_code == 404
        assert second.json()["detail"] == "行程不存在"

    def test_删除不存在的行程返回404(self, client):
        _, headers = make_auth()

        r = client.delete(f"/api/history/{uuid.uuid4()}", headers=headers)

        assert r.status_code == 404
        assert r.json()["detail"] == "行程不存在"

    def test_删除他人的行程返回403且不被删除(self, client):
        owner, _ = make_auth()
        plan_id = make_itinerary(owner)
        _, intruder_headers = make_auth()

        r = client.delete(f"/api/history/{plan_id}", headers=intruder_headers)

        assert r.status_code == 403
        assert r.json()["detail"] == "无权访问"
        # 行程仍然存在：用拥有者身份确认
        from app.core.auth import create_token

        owner_headers = {"Authorization": "Bearer " + create_token(owner)}
        assert client.get(f"/api/history/{plan_id}", headers=owner_headers).status_code == 200


class TestDeleteWithRunReference:
    def test_被Run引用时删除成功且引用被置空(self, client):
        uid, headers = make_auth()
        plan_id = make_itinerary(uid)
        run_id = make_run(uid, result_itinerary_id=plan_id)

        r = client.delete(f"/api/history/{plan_id}", headers=headers)

        assert r.status_code == 200
        from app.core.database import get_conn

        with get_conn() as conn:
            run = conn.execute(
                "SELECT result_itinerary_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            events = conn.execute(
                "SELECT COUNT(*) AS n FROM run_events WHERE run_id=?", (run_id,)
            ).fetchone()
        assert run is not None, "Run 记录必须保留"
        assert run["result_itinerary_id"] is None, "指向被删行程的引用必须被置空"
        assert events["n"] == 1, "Run 事件必须保留"

    def test_只清空指向该行程的引用(self, client):
        uid, headers = make_auth()
        kept_id = make_itinerary(uid, destination="苏州")
        removed_id = make_itinerary(uid, destination="南京")
        kept_run = make_run(uid, result_itinerary_id=kept_id)
        make_run(uid, result_itinerary_id=removed_id)

        assert client.delete(f"/api/history/{removed_id}", headers=headers).status_code == 200

        from app.core.database import get_conn

        with get_conn() as conn:
            run = conn.execute(
                "SELECT result_itinerary_id FROM runs WHERE id=?", (kept_run,)
            ).fetchone()
        assert run["result_itinerary_id"] == kept_id


class TestDeleteBoundaries:
    def test_删掉原版后修改版仍可读取(self, client):
        uid, headers = make_auth()
        root_id = make_itinerary(uid, destination="南京")
        child_id = make_itinerary(uid, destination="南京", parent_id=root_id)

        assert client.delete(f"/api/history/{root_id}", headers=headers).status_code == 200

        detail = client.get(f"/api/history/{child_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["parent_id"] == root_id, "悬空的 parent_id 被容忍而不是被清理"

        listed = client.get("/api/history", headers=headers).json()
        assert [item["id"] for item in listed] == [child_id]

    def test_对话与消息不被级联删除(self, client):
        uid, headers = make_auth()
        plan_id = make_itinerary(uid)
        conversation_id, message_id = make_conversation_with_message(uid, related_itinerary_id=plan_id)

        assert client.delete(f"/api/history/{plan_id}", headers=headers).status_code == 200

        from app.core.database import get_conn

        with get_conn() as conn:
            message = conn.execute(
                "SELECT related_itinerary_id FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            conversation = conn.execute(
                "SELECT id FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        assert conversation is not None, "对话必须保留"
        assert message is not None, "消息必须保留"
        assert message["related_itinerary_id"] == plan_id, "消息里的行程引用作为历史事实保留"

    def test_删除不影响长期记忆(self, client):
        uid, headers = make_auth()
        plan_id = make_itinerary(uid)

        from app.core.database import get_conn

        with get_conn() as conn:
            conn.execute(
                """INSERT INTO memory_facts(id,user_id,category,value_text,normalized_value,
                   polarity,scope_type,scope_key,status,source_kind,sensitivity,
                   evidence_sequences_json,confidence,fingerprint,created_at,updated_at)
                   VALUES(?,?,'food_preference','不吃辣','不吃辣','avoid','global','{}',
                   'active','explicit_chat','normal','[]',1.0,?,?,?)""",
                (str(uuid.uuid4()), uid, "fp-1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

        assert client.delete(f"/api/history/{plan_id}", headers=headers).status_code == 200

        with get_conn() as conn:
            facts = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_facts WHERE user_id=?", (uid,)
            ).fetchone()
        assert facts["n"] == 1
