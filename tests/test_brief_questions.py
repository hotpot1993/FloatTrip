"""服务端字段提问清单与收敛判定的单元测试。

这些测试不依赖数据库、FastAPI 或任何语言模型：提问的「问什么、什么时候问完」
必须是可以离线推理的确定行为，否则它就会退回到由模型每次临时决定的老问题。
"""

from __future__ import annotations

import pytest

from app.core.planning_brief import (
    DECLINED_FIELDS_KEY,
    DURABLE_BRIEF_FIELDS,
    OPTIONAL_QUESTION_FIELDS,
    QUESTION_FIELDS,
    QUESTION_STORAGE_KEYS,
    auto_decline_questions,
    collection_complete,
    declined_fields,
    field_answered,
    next_question,
    pending_fields,
    questioned_fields,
    required_brief_fields,
)
from app.core.planning_constraints import normalize_brief_data


COMPLETE = {
    "destination": "丽江",
    "start_date": "2026-11-07",
    "end_date": "2026-11-11",
    "arrival_time": "傍晚",
    "departure_time": "15:00",
    "trip_budget": "6000",
    "trip_constraints": [
        {
            "id": "c1",
            "category": "attraction_preference",
            "value_text": "自然风光",
            "polarity": "prefer",
        }
    ],
}

REQUIRED_ANSWERS = {
    "destination": {"destination": "丽江"},
    "date_range": {"start_date": "2026-11-07", "end_date": "2026-11-11"},
}


def advance(data: dict, question: dict) -> None:
    """回答必填、跳过可选，把提问推进一项。"""
    field = question["field"]
    if question["optional"]:
        data[DECLINED_FIELDS_KEY] = [*declined_fields(data), field]
    else:
        data.update(REQUIRED_ANSWERS[field])


class TestQuestionCatalog:
    def test_fixed_order(self):
        assert QUESTION_FIELDS == (
            "destination",
            "date_range",
            "arrival_time",
            "departure_time",
            "trip_budget",
            "preferences",
        )

    def test_only_destination_and_dates_are_required(self):
        assert OPTIONAL_QUESTION_FIELDS == (
            "arrival_time",
            "departure_time",
            "trip_budget",
            "preferences",
        )

    def test_every_question_is_serializable(self):
        # 提问会原样进入 HTTP 响应与 SSE 载荷，因此不能包含元组之类的
        # 非 JSON 类型，也不能缺字段。
        data: dict = {}
        for field in QUESTION_FIELDS:
            question = next_question(data)
            assert question is not None
            assert question["field"] == field
            assert isinstance(question["options"], list)
            assert isinstance(question["question"], str) and question["question"]
            assert isinstance(question["label"], str) and question["label"]
            assert question["kind"] in {
                "text", "date_range", "single_choice", "multi_choice",
            }
            assert question["total"] == len(QUESTION_FIELDS)
            advance(data, question)


class TestNextQuestion:
    def test_empty_brief_starts_with_destination(self):
        question = next_question({})
        assert question is not None
        assert question["field"] == "destination"
        assert question["optional"] is False
        assert question["remaining"] == 6

    def test_asks_in_order(self):
        data: dict = {}
        seen = []
        for _ in range(len(QUESTION_FIELDS) + 1):
            question = next_question(data)
            if question is None:
                break
            seen.append(question["field"])
            advance(data, question)
        assert tuple(seen) == QUESTION_FIELDS
        assert next_question(data) is None

    def test_required_fields_cannot_be_skipped(self):
        # 即便有人把必填字段写进跳过列表（例如语言模型写错），它们仍会被问。
        data = {DECLINED_FIELDS_KEY: ["destination", "date_range"]}
        assert declined_fields(data) == []
        question = next_question(data)
        assert question is not None and question["field"] == "destination"

    def test_destination_alone_moves_to_dates(self):
        question = next_question({"destination": "云南"})
        assert question is not None
        assert question["field"] == "date_range"
        assert question["remaining"] == 5

    def test_days_without_dates_still_asks_for_dates(self):
        question = next_question({"destination": "云南", "days": 5})
        assert question is not None
        assert question["field"] == "date_range"

    def test_reversed_dates_keep_asking_for_dates(self):
        question = next_question(
            {"destination": "云南", "start_date": "2026-11-11", "end_date": "2026-11-07"}
        )
        assert question is not None
        assert question["field"] == "date_range"

    def test_unparseable_dates_keep_asking_for_dates(self):
        question = next_question(
            {"destination": "云南", "start_date": "下个月", "end_date": "2026-11-07"}
        )
        assert question is not None
        assert question["field"] == "date_range"

    def test_required_fields_are_asked_before_optional_gaps(self):
        # 用户先跳过了抵达时刻，之后目的地被清空：必填必须重新排到最前。
        data = {
            "start_date": "2026-11-07",
            "end_date": "2026-11-11",
            DECLINED_FIELDS_KEY: ["arrival_time"],
        }
        question = next_question(data)
        assert question is not None
        assert question["field"] == "destination"

    def test_skipped_optional_field_is_not_asked_again(self):
        question = next_question(
            {
                "destination": "云南",
                "start_date": "2026-11-07",
                "end_date": "2026-11-11",
                DECLINED_FIELDS_KEY: ["arrival_time"],
            }
        )
        assert question is not None
        assert question["field"] == "departure_time"

    def test_preferences_are_answered_by_constraints(self):
        question = next_question(
            {
                "destination": "云南",
                "start_date": "2026-11-07",
                "end_date": "2026-11-11",
                DECLINED_FIELDS_KEY: ["arrival_time", "departure_time", "trip_budget"],
                "trip_constraints": [{"id": "c1", "category": "food_preference", "value_text": "爱吃辣"}],
            }
        )
        assert question is None

    def test_complete_brief_stops_asking(self):
        assert next_question(COMPLETE) is None

    def test_empty_preference_list_does_not_count_as_answered(self):
        # normalize_brief_data 总把 trip_constraints 设成列表（可能是空的），
        # 空列表必须继续算作「没回答」，否则偏好永远问不到。
        data = normalize_brief_data({"destination": "云南"})
        assert data["trip_constraints"] == []
        assert field_answered("preferences", data) is False


class TestDeclinedFields:
    def test_ignores_unknown_ids_and_duplicates(self):
        assert declined_fields(
            {DECLINED_FIELDS_KEY: ["nope", "arrival_time", "arrival_time", None, 7]}
        ) == ["arrival_time"]

    def test_tolerates_wrong_types(self):
        assert declined_fields({DECLINED_FIELDS_KEY: "arrival_time"}) == []
        assert declined_fields({DECLINED_FIELDS_KEY: None}) == []
        assert declined_fields({}) == []


class TestCollectionComplete:
    def test_ready_does_not_imply_collected(self):
        data = {"destination": "丽江", "start_date": "2026-11-07", "end_date": "2026-11-11"}
        assert required_brief_fields(data) == []
        assert collection_complete(data) is False

    def test_collected_implies_ready(self):
        assert required_brief_fields(COMPLETE) == []
        assert collection_complete(COMPLETE) is True

    def test_all_optional_skipped_but_destination_missing_is_not_complete(self):
        data = {DECLINED_FIELDS_KEY: list(OPTIONAL_QUESTION_FIELDS)}
        assert collection_complete(data) is False
        question = next_question(data)
        assert question is not None and question["field"] == "destination"

    def test_questioned_and_pending_partition_the_catalog(self):
        data = {
            "destination": "丽江",
            DECLINED_FIELDS_KEY: ["arrival_time"],
        }
        handled = set(questioned_fields(data))
        waiting = set(pending_fields(data))
        assert handled | waiting == set(QUESTION_FIELDS)
        assert handled & waiting == set()


class TestAutoDeclineQuestions:
    def test_marks_every_unanswered_optional_field(self):
        data = {"destination": "丽江", "start_date": "2026-11-07", "end_date": "2026-11-11"}
        assert auto_decline_questions(data) == list(OPTIONAL_QUESTION_FIELDS)
        # 必填已齐、可选全跳过之后，收集就算完成。
        after = {**data, DECLINED_FIELDS_KEY: auto_decline_questions(data)}
        assert collection_complete(after) is True

    def test_keeps_already_answered_optional_fields(self):
        data = {
            "destination": "丽江",
            "start_date": "2026-11-07",
            "end_date": "2026-11-11",
            "trip_budget": "6000",
            DECLINED_FIELDS_KEY: ["arrival_time"],
        }
        result = auto_decline_questions(data)
        assert "arrival_time" in result
        assert "trip_budget" not in result
        assert "departure_time" in result

    def test_does_not_decline_required_fields(self):
        data = {"destination": "丽江"}
        result = auto_decline_questions(data)
        assert "destination" not in result
        assert "date_range" not in result

    def test_required_gap_survives_auto_decline(self):
        # 用户说「别问了直接开始」但目的地还空着：可选全跳过，必填仍要问。
        data = {DECLINED_FIELDS_KEY: auto_decline_questions({})}
        question = next_question(data)
        assert question is not None and question["field"] == "destination"
        assert collection_complete(data) is False


class TestOptionalFieldIsolation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("arrival_time", "18:30"),
            ("departure_time", "15:00"),
            ("trip_budget", "6000"),
        ],
    )
    def test_single_value_answers_only_its_own_field(self, field, value):
        data = {"destination": "丽江", field: value}
        assert field_answered(field, data) is True
        for other in OPTIONAL_QUESTION_FIELDS:
            if other != field:
                assert field_answered(other, data) is False

    def test_blank_string_is_not_an_answer(self):
        data = {"destination": "  ", "arrival_time": "", "trip_budget": "   "}
        assert field_answered("destination", data) is False
        assert field_answered("arrival_time", data) is False
        assert field_answered("trip_budget", data) is False


class TestLegacyPreferenceMigration:
    def test_legacy_string_projects_into_preferences(self):
        data = normalize_brief_data({"destination": "云南", "food_preference": "爱吃辣"})
        assert field_answered("preferences", data) is True
        assert any(item["value_text"] == "爱吃辣" for item in data["trip_constraints"])

    def test_legacy_key_is_migrated_away_not_re_derived(self):
        once = normalize_brief_data({"destination": "云南", "food_preference": "爱吃辣"})
        assert "food_preference" not in once
        # 迁移之后再走一遍归一化，不会冒出新的一条。
        twice = normalize_brief_data(once)
        assert [item["value_text"] for item in twice["trip_constraints"]] == ["爱吃辣"]

    def test_editing_a_derived_constraint_does_not_duplicate_it(self):
        """回归：用户改掉由旧字段派生出来的那条偏好，不能变成两条。

        这正是 `docs.md` 里记录过的失败模式——同一条事实在同一份数据里
        有两个来源，而其中一个来源每次读取都会被重新生成。
        """
        brief = normalize_brief_data({"destination": "云南", "attraction_preference": "喜欢古镇"})
        edited = normalize_brief_data({
            **brief,
            "trip_constraints": [
                {**item, "value_text": "喜欢安静的古镇"} for item in brief["trip_constraints"]
            ],
        })
        assert [item["value_text"] for item in edited["trip_constraints"]] == ["喜欢安静的古镇"]

    def test_deleting_a_derived_constraint_sticks(self):
        brief = normalize_brief_data({"destination": "云南", "habit_preference": "慢节奏"})
        assert brief["trip_constraints"]
        cleared = normalize_brief_data({**brief, "trip_constraints": []})
        assert cleared["trip_constraints"] == []

    def test_blank_legacy_key_is_dropped_without_adding_anything(self):
        data = normalize_brief_data({"destination": "云南", "attraction_preference": "   "})
        assert "attraction_preference" not in data
        assert data["trip_constraints"] == []

    def test_migrated_constraint_keeps_its_deterministic_id(self):
        first = normalize_brief_data({"destination": "云南", "food_preference": "爱吃辣"})
        rebuilt = normalize_brief_data({"destination": "云南", "food_preference": "爱吃辣"})
        assert first["trip_constraints"][0]["id"] == rebuilt["trip_constraints"][0]["id"]


class TestDurableFieldCoverage:
    """提问清单、存储键与「耐用字段」白名单必须彼此对齐。

    这三者分处不同模块：提问在 planning_brief.py，存储形态在 brief.data，
    白名单决定字段能否穿过语言模型那条写入路径。任何一处漏掉，症状都是
    「用户明明回答了，提问却反复出现」——很难从界面反推原因，所以在这里
    用断言把它钉死。
    """

    def test_declined_fields_survives_the_model_write_path(self):
        assert DECLINED_FIELDS_KEY in DURABLE_BRIEF_FIELDS

    def test_every_question_field_has_a_storage_mapping(self):
        assert set(QUESTION_STORAGE_KEYS) == set(QUESTION_FIELDS)

    def test_every_storage_key_is_durable(self):
        for field, keys in QUESTION_STORAGE_KEYS.items():
            missing = keys - DURABLE_BRIEF_FIELDS
            assert not missing, f"提问字段 {field} 的 {sorted(missing)} 不在耐用字段白名单里"
