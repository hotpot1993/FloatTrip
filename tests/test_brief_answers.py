"""结构化回答的取值校验测试（纯逻辑，不依赖数据库或 FastAPI）。

这个函数是唯一一处「值可以不经对话理解直接进入需求单」的入口（见 ADR 0002），
所以它必须自己挡住所有服务端没提供过的输入。
"""

from __future__ import annotations

import pytest

from app.core.planning_brief import (
    DECLINED_FIELDS_KEY,
    AnswerConflict,
    AnswerInvalid,
    answer_patch,
    next_question,
)
from app.core.planning_constraints import normalize_brief_data


BASE = {"destination": "丽江", "start_date": "2026-11-07", "end_date": "2026-11-11"}


class TestTargeting:
    def test_rejects_a_field_the_server_is_not_asking_about(self):
        with pytest.raises(AnswerConflict):
            answer_patch(BASE, "trip_budget", "6000")

    def test_rejects_an_unknown_field(self):
        with pytest.raises(AnswerConflict):
            answer_patch(BASE, "itinerary_id", "x")

    def test_rejects_answers_after_collection_is_complete(self):
        data = {
            **BASE,
            DECLINED_FIELDS_KEY: ["arrival_time", "departure_time", "trip_budget", "preferences"],
        }
        assert next_question(data) is None
        with pytest.raises(AnswerConflict):
            answer_patch(data, "arrival_time", "傍晚")

    def test_accepts_the_field_that_is_currently_asked(self):
        assert answer_patch(BASE, "arrival_time", "傍晚") == {"arrival_time": "傍晚"}


class TestRequiredFields:
    def test_destination_is_trimmed(self):
        assert answer_patch({}, "destination", "  丽江  ") == {"destination": "丽江"}

    def test_blank_destination_is_not_a_skip(self):
        with pytest.raises(AnswerInvalid):
            answer_patch({}, "destination", "   ")

    def test_destination_length_is_bounded(self):
        with pytest.raises(AnswerInvalid):
            answer_patch({}, "destination", "丽" * 201)

    def test_required_field_cannot_be_skipped(self):
        for blank in (None, "", "   "):
            with pytest.raises(AnswerInvalid):
                answer_patch({}, "destination", blank)

    def test_date_range_normalizes_to_iso(self):
        patch = answer_patch({"destination": "丽江"}, "date_range", {
            "start_date": "2026-11-07", "end_date": "2026-11-11",
        })
        assert patch == {"start_date": "2026-11-07", "end_date": "2026-11-11"}

    @pytest.mark.parametrize(
        "value",
        [
            "2026-11-07",
            {"start_date": "2026-11-07"},
            {"start_date": "下个月", "end_date": "2026-11-11"},
            {"start_date": None, "end_date": None},
        ],
    )
    def test_date_range_requires_both_parseable_dates(self, value):
        with pytest.raises(AnswerInvalid):
            answer_patch({"destination": "丽江"}, "date_range", value)

    def test_reversed_dates_are_rejected(self):
        with pytest.raises(AnswerInvalid):
            answer_patch({"destination": "丽江"}, "date_range", {
                "start_date": "2026-11-11", "end_date": "2026-11-07",
            })


class TestOptionalFields:
    def test_skip_records_the_field_and_nothing_else(self):
        patch = answer_patch(BASE, "arrival_time", None)
        assert patch == {DECLINED_FIELDS_KEY: ["arrival_time"]}

    def test_skip_is_cumulative_and_in_catalog_order(self):
        data = {**BASE, DECLINED_FIELDS_KEY: ["departure_time"]}
        patch = answer_patch(data, "arrival_time", None)
        # 抵达排在返程之前，因此结果按清单顺序而不是写入顺序。
        assert patch == {DECLINED_FIELDS_KEY: ["arrival_time", "departure_time"]}

    def test_time_accepts_a_period_word(self):
        assert answer_patch(BASE, "arrival_time", "傍晚") == {"arrival_time": "傍晚"}

    def test_time_accepts_a_clock_value(self):
        assert answer_patch(BASE, "arrival_time", "18:30") == {"arrival_time": "18:30"}

    def test_time_length_is_bounded(self):
        with pytest.raises(AnswerInvalid):
            answer_patch(BASE, "arrival_time", "傍" * 101)

    def test_budget_accepts_free_text_but_is_bounded(self):
        assert answer_patch({**BASE, DECLINED_FIELDS_KEY: ["arrival_time", "departure_time"]},
                            "trip_budget", "6000 元左右") == {"trip_budget": "6000 元左右"}
        with pytest.raises(AnswerInvalid):
            answer_patch({**BASE, DECLINED_FIELDS_KEY: ["arrival_time", "departure_time"]},
                         "trip_budget", "6" * 501)


class TestPreferences:
    def _data(self):
        # 抵达、返程、预算都跳过，当前待问就是偏好。
        return {
            **BASE,
            DECLINED_FIELDS_KEY: ["arrival_time", "departure_time", "trip_budget"],
        }

    def test_tags_map_to_their_own_constraint_categories(self):
        patch = answer_patch(self._data(), "preferences", ["自然风光", "美食", "慢节奏放松"])
        categories = [item["category"] for item in patch["trip_constraints"]]
        assert categories == ["attraction_preference", "food_preference", "travel_pace"]
        assert all(item["polarity"] == "prefer" for item in patch["trip_constraints"])

    def test_rejects_tags_outside_the_offered_options(self):
        with pytest.raises(AnswerInvalid):
            answer_patch(self._data(), "preferences", ["自然风光", "滑雪"])

    def test_keeps_existing_constraints(self):
        # choices 之外的既有约束必须原样留在合并结果里。
        patch = answer_patch(self._data(), "preferences", ["美食"])
        normalized = normalize_brief_data(patch)
        assert [item["value_text"] for item in normalized["trip_constraints"]] == ["美食"]

    def test_any_existing_constraint_retires_the_preference_question(self):
        # 用户已经用别的方式表达过偏好（例如在对话里说「不吃海鲜」）时，
        # 偏好题就算已答，不会再被问一次。
        data = {
            **self._data(),
            "trip_constraints": [
                {"id": "c1", "category": "dietary_requirement", "value_text": "不吃海鲜",
                 "polarity": "require", "source": "conversation"},
            ],
        }
        assert next_question(data) is None
        with pytest.raises(AnswerConflict):
            answer_patch(data, "preferences", ["美食"])

    def test_repeat_answer_is_rejected_rather_than_duplicated(self):
        # 「只能回答当前提问」这条守卫顺带让重复提交成为不可能，
        # 因此不需要额外的去重分支。
        base = self._data()
        once = normalize_brief_data({**base, **answer_patch(base, "preferences", ["美食"])})
        assert next_question(once) is None
        with pytest.raises(AnswerConflict):
            answer_patch(once, "preferences", ["美食"])
        assert [item["value_text"] for item in normalize_brief_data(once)["trip_constraints"]] == ["美食"]
