from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.chat.models import DialogueDecision, DialogueTarget
from app.chat.service import ChatService
from app.core.database import get_conn, init_db
from app.runtime.manager import RunManager
from app.runtime.models import RunKind, RunStatus
from app.runtime.repositories import ConversationRepository


class DialogueActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "dialogue.db"
        init_db(self.db_path)
        with get_conn(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                [
                    ("owner", "owner", "hash", "2026-01-01"),
                    ("other", "other", "hash", "2026-01-01"),
                ],
            )
        self.manager = RunManager(self.db_path)
        self.conversations = ConversationRepository(self.db_path)
        self.conversation = self.conversations.create("owner", "测试")
        self.service = ChatService(self.manager, self.db_path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _chat_run(self, text="帮我规划南京旅行"):
        _message, run = await self.service.submit_message(
            "owner", self.conversation["id"], text
        )
        return run

    async def test_brief_patch_normalizes_days_and_persists_assistant_reply(self):
        run = await self._chat_run()
        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="create_plan",
                reply="我先整理南京的三日行程。",
                brief_patch={
                    "destination": "南京",
                    "start_date": "2026-07-24",
                    "end_date": "2026-07-26",
                    "days": 2,
                },
            ),
        )
        brief = self.service.briefs.active_for_conversation("owner", self.conversation["id"])
        self.assertEqual(brief["status"], "ready")
        self.assertEqual(brief["data"]["days"], 3)
        messages = self.conversations.messages("owner", self.conversation["id"])
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["content"], "我先整理南京的三日行程。")

    async def test_invalid_date_has_no_brief_or_follow_up_run_side_effect(self):
        run = await self._chat_run()
        before = self.manager.runs.list("owner", conversation_id=self.conversation["id"])
        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="create_plan",
                reply="已记录。",
                brief_patch={"destination": "南京", "start_date": "bad-date"},
            ),
        )
        self.assertIsNone(self.service.briefs.active_for_conversation("owner", self.conversation["id"]))
        after = self.manager.runs.list("owner", conversation_id=self.conversation["id"])
        self.assertEqual([item["id"] for item in after], [item["id"] for item in before])
        messages = self.conversations.messages("owner", self.conversation["id"])
        self.assertIn("日期范围", messages[-1]["content"])

    async def test_planning_intent_without_any_field_still_creates_a_brief(self):
        """用户只说「我想出去玩」时也要开单并开始提问。

        否则最需要被逐项问清楚的场景永远进不了提问流程——这正是本次改动
        要修的那个缺口。
        """
        run = await self._chat_run("我想出去玩")
        result = await self.service.actions.execute(
            run,
            DialogueDecision(intent="create_plan", reply="那我们先把想法理一理。"),
        )
        brief = self.service.briefs.get("owner", result["brief_id"])
        self.assertEqual(brief["status"], "collecting")
        self.assertEqual(brief["question"]["field"], "destination")
        self.assertFalse(brief["collected"])

    async def test_confirmation_ends_the_questions_without_creating_a_run(self):
        """「开始规划」表达的是「别再问了」，不是「立刻建任务」。"""
        run = await self._chat_run()
        await self.service.apply_brief_patch(
            run,
            {"destination": "南京", "start_date": "2026-07-24", "end_date": "2026-07-26"},
        )
        before = self.service.briefs.active_for_conversation(
            "owner", self.conversation["id"]
        )
        self.assertEqual(before["question"]["field"], "arrival_time")

        result = await self.service.actions.execute(
            run, DialogueDecision(intent="confirm_plan", reply="现在就规划吧。")
        )

        self.assertNotIn("created_run_id", result)
        brief = self.service.briefs.get("owner", result["brief_id"])
        self.assertEqual(brief["status"], "ready")
        self.assertEqual(brief["declined_fields"], [
            "arrival_time", "departure_time", "trip_budget", "preferences",
        ])
        self.assertTrue(brief["collected"])
        self.assertIsNone(brief["question"])
        runs = self.manager.runs.list("owner", conversation_id=self.conversation["id"])
        self.assertEqual(
            [item for item in runs if item["kind"] == RunKind.TRAVEL_PLAN.value], []
        )
        messages = self.conversations.messages("owner", self.conversation["id"])
        self.assertIn("确认", messages[-1]["content"])

    async def test_required_gap_keeps_being_asked_after_a_start_request(self):
        """必填不齐时「别问了」不成立：可选停问，必填继续问。"""
        run = await self._chat_run("直接开始规划吧")
        result = await self.service.actions.execute(
            run,
            DialogueDecision(intent="create_plan", reply="好。", brief_patch={"days": 3}),
        )
        await self.service.actions.execute(
            run, DialogueDecision(intent="confirm_plan", reply="别问了，开始吧。")
        )
        brief = self.service.briefs.get("owner", result["brief_id"])
        self.assertEqual(brief["declined_fields"], [
            "arrival_time", "departure_time", "trip_budget", "preferences",
        ])
        self.assertEqual(brief["question"]["field"], "destination")
        self.assertFalse(brief["collected"])
        runs = self.manager.runs.list("owner", conversation_id=self.conversation["id"])
        self.assertEqual(
            [item for item in runs if item["kind"] == RunKind.TRAVEL_PLAN.value], []
        )

    async def test_duplicate_confirmation_after_submission_returns_the_same_run(self):
        run = await self._chat_run()
        brief = await self.service.apply_brief_patch(
            run,
            {"destination": "南京", "start_date": "2026-07-24", "end_date": "2026-07-26"},
        )
        _brief, planning_run = await self.service.submit_brief("owner", brief["id"])
        first = await self.service.actions.execute(
            run, DialogueDecision(intent="confirm_plan", reply="开始。")
        )
        second = await self.service.actions.execute(
            run, DialogueDecision(intent="confirm_plan", reply="再确认一次。")
        )
        self.assertEqual(first["created_run_id"], planning_run["id"])
        self.assertEqual(second["created_run_id"], planning_run["id"])
        runs = self.manager.runs.list("owner", conversation_id=self.conversation["id"])
        self.assertEqual(
            len([item for item in runs if item["kind"] == RunKind.TRAVEL_PLAN.value]), 1
        )

    async def test_skipped_field_survives_a_later_brief_patch(self):
        """跳过记录必须活着穿过语言模型那条写入路径。

        回归测试针对的是 docs.md 问题十四记录过的失败模式：新字段只在半数
        路径生效。这里的症状会是「用户跳过抵达时刻，之后随便补充一句，抵达
        时刻被重新问一遍」。
        """
        run = await self._chat_run()
        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="create_plan", reply="好的。",
                brief_patch={
                    "destination": "南京",
                    "start_date": "2026-07-24",
                    "end_date": "2026-07-26",
                },
            ),
        )
        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="update_brief", reply="记下了。",
                brief_patch={"declined_fields": ["arrival_time"]},
            ),
        )
        brief = self.service.briefs.active_for_conversation(
            "owner", self.conversation["id"]
        )
        self.assertEqual(brief["declined_fields"], ["arrival_time"])
        self.assertEqual(brief["question"]["field"], "departure_time")

        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="update_brief", reply="好。", brief_patch={"trip_budget": "6000"},
            ),
        )
        brief = self.service.briefs.active_for_conversation(
            "owner", self.conversation["id"]
        )
        self.assertEqual(brief["data"]["trip_budget"], "6000")
        self.assertEqual(brief["declined_fields"], ["arrival_time"])
        self.assertEqual(brief["question"]["field"], "departure_time")

    async def test_model_cannot_skip_a_required_field(self):
        """语言模型写错跳过列表时不能让需求单永远问不完。"""
        run = await self._chat_run()
        result = await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="create_plan", reply="好的。",
                brief_patch={"declined_fields": ["destination", "date_range"]},
            ),
        )
        brief = self.service.briefs.get("owner", result["brief_id"])
        self.assertEqual(brief["declined_fields"], [])
        self.assertEqual(brief["question"]["field"], "destination")

    async def test_control_requires_structured_action_and_valid_target_state(self):
        target = self.manager.create(
            user_id="owner", kind=RunKind.TRAVEL_PLAN,
            conversation_id=self.conversation["id"], request_snapshot={"destination": "南京"},
        )
        run = await self._chat_run("把任务停掉")
        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="run_control", reply="好的。", target=DialogueTarget(run_id=target["id"]),
                run_action="cancel", requires_confirmation=True,
            ),
        )
        self.assertEqual(self.manager.runs.get_internal(target["id"])["status"], RunStatus.QUEUED.value)
        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="run_control", reply="已停止。", target=DialogueTarget(run_id=target["id"]),
                run_action="cancel",
            ),
        )
        self.assertEqual(self.manager.runs.get_internal(target["id"])["status"], RunStatus.CANCELLED.value)

    async def test_free_text_reply_cannot_confirm_or_control_anything(self):
        target = self.manager.create(
            user_id="owner", kind=RunKind.TRAVEL_PLAN,
            conversation_id=self.conversation["id"], request_snapshot={"destination": "南京"},
        )
        run = await self._chat_run("确认并停止")
        await self.service.actions.execute(
            run,
            DialogueDecision(
                intent="general_chat",
                reply="我已经确认并停止了任务。",
            ),
        )
        self.assertEqual(self.manager.runs.get_internal(target["id"])["status"], RunStatus.QUEUED.value)
        self.assertIsNone(self.service.briefs.active_for_conversation("owner", self.conversation["id"]))

    async def test_chat_context_is_bounded_and_excludes_other_user_resources(self):
        for index in range(15):
            self.conversations.add_message(
                "owner", self.conversation["id"], "assistant", f"历史 {index}"
            )
        _message, run = await self.service.submit_message(
            "owner", self.conversation["id"], "当前消息"
        )
        other = self.conversations.create("other", "隔离")
        self.manager.create(
            user_id="other", kind=RunKind.TRAVEL_PLAN,
            conversation_id=other["id"], request_snapshot={"destination": "不应出现"},
        )
        context = (await self.service.chat_input(run))["dialogue_context"]
        self.assertEqual(context["today"].count("-"), 2)
        self.assertEqual(context["timezone"], "Asia/Shanghai")
        self.assertEqual(context["current_message"], "当前消息")
        self.assertLessEqual(len(context["history"]), 15)
        self.assertEqual(context["history"][-1]["content"], "历史 14")
        self.assertNotIn("不应出现", str(context))
