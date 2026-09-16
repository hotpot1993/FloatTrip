from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.auth import create_token
from app.core.database import configure_database, get_conn, get_db_path, init_db


class ApplyAllMemoryLlm:
    async def ainvoke(self, messages):
        text = messages[-1].content
        payload = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
        return {
            "decisions": [
                {
                    "fact_id": fact["id"], "decision": "apply",
                    "application_level": (
                        "hard" if fact["category"] in {"dietary_requirement", "accessibility_need"}
                        else "preference"
                    ),
                    "reason_code": "supports_current_trip",
                }
                for fact in payload["frozen_active_facts"]
            ]
        }


class RuntimeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.app = app

    def setUp(self):
        self.original_path = get_db_path()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "api.db"
        configure_database(self.db_path)
        init_db()
        with get_conn() as conn:
            conn.executemany(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                [
                    ("owner", "owner", "hash", "2026-01-01"),
                    ("other", "other", "hash", "2026-01-01"),
                ],
            )
        self.client = TestClient(self.app)
        self.owner_headers = {"Authorization": f"Bearer {create_token('owner')}"}
        self.other_headers = {"Authorization": f"Bearer {create_token('other')}"}

    def tearDown(self):
        self.client.close()
        configure_database(self.original_path)
        self.tmp.cleanup()

    def test_conversation_message_and_cross_user_isolation(self):
        conversation = self.client.post(
            "/api/conversations",
            headers=self.owner_headers,
            json={"title": "云南"},
        ).json()
        with patch("app.api.runtime_routes.scheduler.notify"):
            response = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                headers=self.owner_headers,
                json={"content": "帮我规划云南五日游"},
            )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["message"]["sequence"], 1)
        self.assertEqual(body["run"]["status"], "queued")
        self.assertEqual(
            self.client.get(
                f"/api/conversations/{conversation['id']}",
                headers=self.other_headers,
            ).status_code,
            404,
        )

    def test_conversation_view_endpoint_clears_unread_and_checks_owner(self):
        from app.runtime.models import RunKind, RunStatus
        from app.runtime.repositories import RunRepository

        conversation = self.client.post(
            "/api/conversations", headers=self.owner_headers, json={"title": "看结果"}
        ).json()
        runs = RunRepository()
        with get_conn() as conn:
            run = runs.insert(
                conn,
                run_id="api-attention-run",
                user_id="owner",
                kind=RunKind.TRAVEL_PLAN,
                concurrency_key="plan:api-attention-run",
                request_snapshot={},
                conversation_id=conversation["id"],
            )
        runs.transition(run["id"], RunStatus.RUNNING)
        runs.transition(run["id"], RunStatus.SUCCEEDED)

        listed = self.client.get(
            "/api/conversations", headers=self.owner_headers
        ).json()
        self.assertTrue(listed[0]["has_unread_completed"])
        forbidden = self.client.post(
            f"/api/conversations/{conversation['id']}/view",
            headers=self.other_headers,
        )
        self.assertEqual(forbidden.status_code, 404)
        viewed = self.client.post(
            f"/api/conversations/{conversation['id']}/view",
            headers=self.owner_headers,
        )
        self.assertEqual(viewed.status_code, 200)
        self.assertFalse(viewed.json()["has_unread_completed"])

    def test_brief_submission_is_idempotent_and_creates_one_run(self):
        conversation = self.client.post(
            "/api/conversations", headers=self.owner_headers, json={}
        ).json()
        from app.runtime.repositories import PlanningBriefRepository

        brief = PlanningBriefRepository().upsert_active(
            "owner",
            conversation["id"],
            {
                "destination": "云南",
                "days": 5,
                "start_date": "2026-10-01",
                "end_date": "2026-10-05",
            },
        )
        with patch("app.api.runtime_routes.scheduler.notify"):
            first = self.client.post(
                f"/api/planning-briefs/{brief['id']}/submit",
                headers=self.owner_headers,
            )
            second = self.client.post(
                f"/api/planning-briefs/{brief['id']}/submit",
                headers=self.owner_headers,
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["run"]["id"], second.json()["run"]["id"])
        runs = self.client.get("/api/runs", headers=self.owner_headers).json()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["request_snapshot"]["destination"], "云南")

    def test_brief_auto_fills_memory_allows_trip_only_exclusion_and_freezes_constraints(self):
        from app.core.travel_memory import MemoryRepository
        from app.runtime.container import chat_service
        from app.runtime.repositories import ConversationRepository, PlanningBriefRepository

        fact = MemoryRepository().create(
            "owner", category="dietary_requirement", value_text="需要避开花生",
            polarity="require", status="active", sensitivity="protected",
        )
        conversation = ConversationRepository().create("owner")
        ConversationRepository().add_message(
            "owner", conversation["id"], "user", "东京三日游"
        )
        brief = PlanningBriefRepository().upsert_active(
            "owner", conversation["id"],
            {"destination": "东京", "start_date": "2026-09-01", "end_date": "2026-09-03"},
        )
        previous_llm = chat_service.planning_memory._llm
        chat_service.planning_memory._llm = ApplyAllMemoryLlm()
        try:
            updated = self.client.patch(
                f"/api/planning-briefs/{brief['id']}", headers=self.owner_headers,
                json={
                    "trip_budget": "本次约 5000 元",
                    "trip_constraints": [{
                        "id": "trip-slow", "category": "travel_pace",
                        "value_text": "每天最多三个景点", "polarity": "require",
                    }],
                },
            )
            self.assertEqual(updated.status_code, 200)
            body = updated.json()
            self.assertEqual(body["memory_context"]["applied_facts"][0]["fact_id"], fact["id"])
            self.assertEqual(body["data"]["trip_budget"], "本次约 5000 元")

            revision = MemoryRepository().revision("owner")
            excluded = self.client.patch(
                f"/api/planning-briefs/{brief['id']}", headers=self.owner_headers,
                json={"excluded_memory_fact_ids": [fact["id"]]},
            )
            self.assertEqual(excluded.status_code, 200)
            self.assertEqual(excluded.json()["memory_context"]["applied_facts"], [])
            self.assertEqual(MemoryRepository().revision("owner"), revision)

            with patch("app.api.runtime_routes.scheduler.notify"):
                submitted = self.client.post(
                    f"/api/planning-briefs/{brief['id']}/submit",
                    headers=self.owner_headers,
                )
            self.assertEqual(submitted.status_code, 202)
            snapshot = submitted.json()["run"]["request_snapshot"]
            self.assertEqual(snapshot["trip_budget"], "本次约 5000 元")
            self.assertEqual(snapshot["memory_context"]["revision"], revision)
            self.assertEqual(
                [item["value_text"] for item in snapshot["effective_constraints"]],
                ["每天最多三个景点"],
            )
        finally:
            chat_service.planning_memory._llm = previous_llm

    def test_cancel_retry_event_history_and_completed_replay(self):
        with patch("app.api.runtime_routes.scheduler.notify"):
            run = self.client.post(
                "/api/runs",
                headers=self.owner_headers,
                json={
                    "kind": "travel_plan",
                    "request": {
                        "destination": "成都",
                        "days": 2,
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-02",
                    },
                },
            ).json()
        cancelled = self.client.post(
            f"/api/runs/{run['id']}/cancel", headers=self.owner_headers
        )
        self.assertEqual(cancelled.json()["status"], "cancelled")
        again = self.client.post(
            f"/api/runs/{run['id']}/cancel", headers=self.owner_headers
        )
        self.assertEqual(again.status_code, 200)
        with patch("app.api.runtime_routes.scheduler.notify"):
            retried = self.client.post(
                f"/api/runs/{run['id']}/retry", headers=self.owner_headers
            )
        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json()["retry_of_run_id"], run["id"])
        events = self.client.get(
            f"/api/runs/{run['id']}/events?after_seq=0",
            headers=self.owner_headers,
        ).json()
        self.assertEqual([event["kind"] for event in events], ["custom", "end"])
        with self.client.stream(
            "GET",
            f"/api/runs/{run['id']}/stream",
            headers=self.owner_headers,
        ) as response:
            text = "".join(response.iter_text())
        self.assertIn("event: custom", text)
        self.assertIn("event: end", text)

    def test_direct_start_requires_complete_request(self):
        response = self.client.post(
            "/api/runs",
            headers=self.owner_headers,
            json={"kind": "travel_plan", "request": {"destination": "北京"}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("start_date", str(response.json()))
        self.assertIn("end_date", str(response.json()))

    def test_archive_is_read_only_idempotent_and_reports_memory_finalization(self):
        conversation = self.client.post(
            "/api/conversations", headers=self.owner_headers, json={}
        ).json()
        with patch("app.api.runtime_routes.scheduler.notify"):
            sent = self.client.post(
                f"/api/conversations/{conversation['id']}/messages",
                headers=self.owner_headers,
                json={"content": "我喜欢逛博物馆"},
            )
        self.assertEqual(sent.status_code, 202)
        archived = self.client.post(
            f"/api/conversations/{conversation['id']}/archive",
            headers=self.owner_headers,
        )
        repeated = self.client.post(
            f"/api/conversations/{conversation['id']}/archive",
            headers=self.owner_headers,
        )
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.json()["status"], "archived")
        self.assertEqual(archived.json()["finalization_status"], "pending")
        self.assertEqual(repeated.json()["archived_at"], archived.json()["archived_at"])
        blocked = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            headers=self.owner_headers,
            json={"content": "再补一句"},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"], "conversation_archived")
        compression = self.client.post(
            f"/api/conversations/{conversation['id']}/compress",
            headers=self.owner_headers,
        )
        self.assertEqual(compression.status_code, 409)
        self.assertEqual(compression.json()["detail"], "conversation_archived")

    def test_manual_compression_keeps_six_turns_and_is_safe_to_repeat(self):
        from app.runtime.container import chat_service
        from app.runtime.repositories import ConversationRepository

        class SummaryLlm:
            def __init__(self):
                self.calls = []

            async def ainvoke(self, messages):
                self.calls.append(messages)
                return {
                    "topics": ["测试旅行"],
                    "confirmed_constraints": [],
                    "negative_constraints": [],
                    "preferences_and_corrections": [],
                    "open_questions": [],
                    "decisions": [],
                    "source_sequence_range": {
                        "from_sequence": 1, "through_sequence": 2,
                    },
                }

        conversations = ConversationRepository()
        conversation = conversations.create("owner")
        for turn in range(7):
            conversations.add_message(
                "owner", conversation["id"], "user", f"第 {turn} 轮问题"
            )
            conversations.add_message(
                "owner", conversation["id"], "assistant", f"第 {turn} 轮回答"
            )
        memory = chat_service.memory_context
        previous_llm = memory.summary_llm
        previous_turns = memory.recent_turns
        llm = SummaryLlm()
        memory.summary_llm = llm
        memory.recent_turns = 6
        try:
            first = self.client.post(
                f"/api/conversations/{conversation['id']}/compress",
                headers=self.owner_headers,
            )
            second = self.client.post(
                f"/api/conversations/{conversation['id']}/compress",
                headers=self.owner_headers,
            )
        finally:
            memory.summary_llm = previous_llm
            memory.recent_turns = previous_turns
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["compressed"])
        self.assertEqual(first.json()["summarized_through_sequence"], 2)
        self.assertEqual(first.json()["recent_turns_kept"], 6)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["compressed"])
        self.assertEqual(second.json()["reason"], "not_enough_complete_turns")
        self.assertEqual(len(llm.calls), 1)
        with get_conn() as conn:
            job = conn.execute(
                "SELECT kind,from_sequence,through_sequence FROM memory_extraction_jobs "
                "WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()
        self.assertEqual((job["kind"], job["from_sequence"], job["through_sequence"]), ("pre_summary", 1, 2))

    def test_profile_fact_crud_and_candidate_approval(self):
        created = self.client.post(
            "/api/memories",
            headers=self.owner_headers,
            json={
                "category": "food_preference", "value_text": "喜欢清淡",
                "polarity": "prefer", "scope_type": "global", "scope_key": {},
            },
        )
        self.assertEqual(created.status_code, 201)
        first = created.json()
        profile = self.client.get("/api/profile", headers=self.owner_headers).json()
        self.assertEqual(profile["active_facts"][0]["id"], first["id"])
        self.assertGreaterEqual(profile["revision"], 1)

        edited = self.client.patch(
            f"/api/memories/{first['id']}",
            headers=self.owner_headers,
            json={
                "value_text": "去日本时喜欢清淡料理",
                "scope_type": "destination", "scope_key": {"destination": "日本"},
            },
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["supersedes_id"], first["id"])
        self.assertEqual(edited.json()["scope_key"], {"destination": "日本"})
        forgotten = self.client.delete(
            f"/api/memories/{edited.json()['id']}", headers=self.owner_headers
        )
        self.assertEqual(forgotten.json()["status"], "deleted")
        self.assertEqual(
            self.client.get("/api/profile", headers=self.owner_headers).json()["active_facts"],
            [],
        )

    def test_conversation_plan_keeps_frozen_memory_while_independent_plan_uses_latest(self):
        from app.core.travel_memory import MemoryRepository
        from app.runtime.repositories import ConversationRepository

        facts = MemoryRepository()
        old = facts.create(
            "owner", category="travel_pace", value_text="偏爱慢节奏", polarity="prefer"
        )
        conversation = ConversationRepository().create("owner")
        ConversationRepository().add_message(
            "owner", conversation["id"], "user", "先聊聊苏州"
        )
        facts.replace("owner", old["id"], value_text="偏爱紧凑行程")

        request = {
            "destination": "苏州", "days": 2,
            "start_date": "2026-09-01", "end_date": "2026-09-02",
        }
        from app.runtime.container import chat_service
        previous_llm = chat_service.planning_memory._llm
        chat_service.planning_memory._llm = ApplyAllMemoryLlm()
        try:
            with patch("app.api.runtime_routes.scheduler.notify"):
                from_conversation = self.client.post(
                    "/api/runs", headers=self.owner_headers,
                    json={
                        "kind": "travel_plan", "conversation_id": conversation["id"],
                        "request": request,
                    },
                )
                independent = self.client.post(
                    "/api/runs", headers=self.owner_headers,
                    json={"kind": "travel_plan", "request": request},
                )
        finally:
            chat_service.planning_memory._llm = previous_llm
        frozen_values = {
            fact["value_text"]
            for fact in from_conversation.json()["request_snapshot"]["memory_profile_snapshot"]
        }
        latest_values = {
            fact["value_text"]
            for fact in independent.json()["request_snapshot"]["memory_profile_snapshot"]
        }
        self.assertEqual(frozen_values, {"偏爱慢节奏"})
        self.assertEqual(latest_values, {"偏爱紧凑行程"})

    def test_message_budget_and_manual_pii_guard_return_actionable_422(self):
        conversation = self.client.post(
            "/api/conversations", headers=self.owner_headers, json={}
        ).json()
        too_long = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            headers=self.owner_headers,
            json={"content": "旅" * 1201},
        )
        self.assertEqual(too_long.status_code, 422)
        self.assertIn("拆成几条", too_long.json()["detail"])
        empty = self.client.post(
            f"/api/conversations/{conversation['id']}/messages",
            headers=self.owner_headers,
            json={"content": "   "},
        )
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()["detail"], "消息不能为空。")

        pii = self.client.post(
            "/api/memories", headers=self.owner_headers,
            json={
                "category": "other_travel_preference",
                "value_text": "我的邮箱 test@example.com",
                "polarity": "fact", "scope_type": "global", "scope_key": {},
            },
        )
        self.assertEqual(pii.status_code, 422)
        self.assertEqual(
            self.client.get("/api/profile", headers=self.owner_headers).json()["active_facts"],
            [],
        )

    def test_typed_answers_drive_the_question_sequence(self):
        """候选项回答不经过语言模型：它只能答当前那一题，答完立刻推进。"""
        from app.runtime.repositories import ConversationRepository, PlanningBriefRepository

        conversation = ConversationRepository().create("owner")
        brief = PlanningBriefRepository().upsert_active("owner", conversation["id"], {})
        self.assertEqual(brief["question"]["field"], "destination")
        self.assertFalse(brief["collected"])
        answers = f"/api/planning-briefs/{brief['id']}/answers"

        def answer(field, value, headers=None):
            return self.client.post(
                answers,
                headers=headers or self.owner_headers,
                json={"field": field, "value": value},
            )

        # 必填不能跳过，也不能答非所问。
        self.assertEqual(answer("destination", None).status_code, 422)
        self.assertEqual(answer("trip_budget", "6000").status_code, 409)
        self.assertEqual(
            answer("destination", "丽江", headers=self.other_headers).status_code, 404
        )

        first = answer("destination", "  丽江  ")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["data"]["destination"], "丽江")
        self.assertEqual(first.json()["question"]["field"], "date_range")
        self.assertEqual(first.json()["question"]["remaining"], 5)

        # 非法日期被拒，且不改变需求单。
        self.assertEqual(
            answer("date_range", {"start_date": "2026-11-11", "end_date": "2026-11-07"}).status_code,
            422,
        )
        dates = answer("date_range", {"start_date": "2026-11-07", "end_date": "2026-11-11"})
        self.assertEqual(dates.status_code, 200)
        self.assertEqual(dates.json()["status"], "ready")
        # 必填齐了但提问还没走完：摘要不该出现。
        self.assertFalse(dates.json()["collected"])

        for field in ("arrival_time", "departure_time", "trip_budget"):
            skipped = answer(field, None)
            self.assertEqual(skipped.status_code, 200)
            self.assertIn(field, skipped.json()["declined_fields"])
        self.assertEqual(dates.json()["question"]["field"], "arrival_time")

        last = answer("preferences", ["自然风光", "美食"])
        self.assertEqual(last.status_code, 200)
        body = last.json()
        self.assertTrue(body["collected"])
        self.assertIsNone(body["question"])
        self.assertEqual(
            [item["value_text"] for item in body["data"]["trip_constraints"]],
            ["自然风光", "美食"],
        )

        # 收集完成之后不再接受回答。
        self.assertEqual(answer("arrival_time", "傍晚").status_code, 409)

    def test_answers_are_rejected_once_the_brief_is_submitted(self):
        from app.runtime.repositories import ConversationRepository, PlanningBriefRepository

        conversation = ConversationRepository().create("owner")
        brief = PlanningBriefRepository().upsert_active(
            "owner", conversation["id"],
            {"destination": "云南", "start_date": "2026-10-01", "end_date": "2026-10-05"},
        )
        with patch("app.api.runtime_routes.scheduler.notify"):
            submitted = self.client.post(
                f"/api/planning-briefs/{brief['id']}/submit", headers=self.owner_headers
            )
        self.assertEqual(submitted.status_code, 202)
        response = self.client.post(
            f"/api/planning-briefs/{brief['id']}/answers",
            headers=self.owner_headers,
            json={"field": "arrival_time", "value": "傍晚"},
        )
        self.assertEqual(response.status_code, 409)
        # 关键：被拒绝的回答不能悄悄新建第二条 brief（部分唯一索引只允许一条）。
        self.assertEqual(
            self.client.get(
                f"/api/conversations/{conversation['id']}/planning-brief",
                headers=self.owner_headers,
            ).json(),
            None,
        )

    def test_brief_projection_exposes_the_current_question(self):
        from app.runtime.repositories import ConversationRepository, PlanningBriefRepository

        conversation = ConversationRepository().create("owner")
        PlanningBriefRepository().upsert_active(
            "owner", conversation["id"], {"destination": "云南"}
        )
        body = self.client.get(
            f"/api/conversations/{conversation['id']}/planning-brief",
            headers=self.owner_headers,
        ).json()
        self.assertEqual(body["question"]["field"], "date_range")
        self.assertEqual(body["question"]["total"], 6)
        self.assertEqual(body["answered_fields"], ["destination"])
        self.assertEqual(body["declined_fields"], [])
        self.assertFalse(body["collected"])
        self.assertEqual(body["missing_fields"], ["start_date", "end_date"])
