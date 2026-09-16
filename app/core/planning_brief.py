"""Shared PlanningBrief readiness rules.

The chat graph and persistence layer both use these pure helpers so a brief
cannot be presented as ready when the formal planning graph would immediately
interrupt for missing calendar dates.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def required_brief_fields(data: dict[str, Any]) -> list[str]:
    """Return missing or invalid fields required before formal planning."""
    missing: list[str] = []
    if not str(data.get("destination") or "").strip():
        missing.append("destination")

    start = _iso_date(data.get("start_date"))
    end = _iso_date(data.get("end_date"))
    if start is None:
        missing.append("start_date")
    if end is None:
        missing.append("end_date")
    if start is not None and end is not None and end < start:
        missing.append("date_range")
    return missing


def merged_brief_data(
    brief: dict[str, Any] | None,
    patch: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge the persisted brief data with fields extracted in this Chat turn."""
    data = dict((brief or {}).get("data") or {})
    data.update(
        {
            key: value
            for key, value in (patch or {}).items()
            if value is not None
        }
    )
    return data


# --- 字段提问 -----------------------------------------------------------------
#
# 「问什么」由服务端决定，语言模型不参与。清单固定为六项，因此提问必然在有
# 限轮次内结束：每一项要么被回答，要么被用户明确跳过（写入 declined_fields）。
#
# 字段组与单一字段的区分很重要：date_range 在需求单里对应 start_date 与
# end_date 两个键，preferences 对应一整个 trip_constraints 列表。结构化回答
# 接口按组校验，因此这里也按组判定「是否已处理」。

DECLINED_FIELDS_KEY = "declined_fields"

# 规划需求单上「耐用」字段的规范清单：只有列在这里的键才能穿过语言模型那条
# 写入路径（app/chat/executor.py 的合并过滤）并留在 brief 上。
#
# 同一个字段在这个仓库里要经过四处各自独立的集合——本清单、chat 侧
# PlanningBriefPatch、HTTP 侧 BriefPatch、以及规划状态 TravelPlanState。
# 新增字段时漏掉任何一处，就会得到「只在半数路径生效」的行为（见 docs.md
# 问题十四）。本清单放在这里，是为了让不再依赖 FastAPI 与语言模型的测试能够
# 直接断言这条不变量。
DURABLE_BRIEF_FIELDS = frozenset(
    {
        "destination",
        "start_date",
        "end_date",
        "arrival_time",
        "departure_time",
        "days",
        "budget",
        "trip_budget",
        "attraction_preference",
        "food_preference",
        "habit_preference",
        "trip_constraints",
        "excluded_memory_fact_ids",
        DECLINED_FIELDS_KEY,
    }
)

# 每个提问字段最终落在需求单的哪些键上。提问清单与存储形态必须一一对应，
# 否则「回答了却存不下」会表现为提问反复出现。
QUESTION_STORAGE_KEYS: dict[str, frozenset[str]] = {
    "destination": frozenset({"destination"}),
    "date_range": frozenset({"start_date", "end_date", "days"}),
    "arrival_time": frozenset({"arrival_time"}),
    "departure_time": frozenset({"departure_time"}),
    "trip_budget": frozenset({"trip_budget"}),
    "preferences": frozenset({"trip_constraints"}),
}

_BRIEF_QUESTIONS: tuple[dict[str, Any], ...] = (
    {
        "field": "destination",
        "label": "目的地",
        "question": "这趟想去哪里？",
        "kind": "text",
        "options": (),
        "optional": False,
        "hint": "城市或地区都可以，例如「云南」「京都」",
    },
    {
        "field": "date_range",
        "label": "出行日期",
        "question": "打算哪天出发、哪天回来？",
        "kind": "date_range",
        "options": (),
        "optional": False,
        "hint": "只知道玩几天也可以直接写，例如「11月7号去，玩5天」",
    },
    {
        "field": "arrival_time",
        "label": "抵达时刻",
        "question": "到达那天大概几点到？",
        "kind": "single_choice",
        "options": ("上午", "中午", "下午", "傍晚", "晚上"),
        "optional": True,
        "hint": "也可以直接写具体时间，例如 18:30",
    },
    {
        "field": "departure_time",
        "label": "返程时刻",
        "question": "最后一天大概几点离开？",
        "kind": "single_choice",
        "options": ("上午", "中午", "下午", "傍晚", "晚上"),
        "optional": True,
        "hint": "也可以直接写具体时间，例如 15:00",
    },
    {
        "field": "trip_budget",
        "label": "本次预算",
        "question": "这趟大概想花多少？",
        "kind": "single_choice",
        "options": ("经济", "舒适", "高端"),
        "optional": True,
        "hint": "也可以直接写数字，例如 6000 元",
    },
    {
        "field": "preferences",
        "label": "旅行偏好",
        "question": "有什么特别想安排，或者想避开的？",
        "kind": "multi_choice",
        "options": ("自然风光", "历史人文", "美食", "亲子", "摄影", "慢节奏放松"),
        "optional": True,
        "hint": "可以多选，也可以直接写，例如「不想爬太陡的山」",
    },
)

QUESTION_FIELDS: tuple[str, ...] = tuple(
    item["field"] for item in _BRIEF_QUESTIONS
)
QUESTION_FIELD_SET = frozenset(QUESTION_FIELDS)
OPTIONAL_QUESTION_FIELDS: tuple[str, ...] = tuple(
    item["field"] for item in _BRIEF_QUESTIONS if item["optional"]
)
OPTION_LISTS: dict[str, tuple[str, ...]] = {
    item["field"]: item["options"] for item in _BRIEF_QUESTIONS
}


def declined_fields(data: dict[str, Any] | None) -> list[str]:
    """Return the optional fields the user has explicitly skipped.

    Only optional fields can be skipped: a required field that somehow ends up
    in the stored list is ignored here, so a mistaken write can never make the
    brief permanently unanswerable.  The result is always in catalog order, so
    the same set of skips serializes identically no matter who wrote it.
    """
    raw = (data or {}).get(DECLINED_FIELDS_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    marked = {str(value or "").strip() for value in raw}
    return [field for field in OPTIONAL_QUESTION_FIELDS if field in marked]


def _date_range_answered(data: dict[str, Any]) -> bool:
    blocking = {"start_date", "end_date", "date_range"}
    return not blocking.intersection(required_brief_fields(data))


def field_answered(field: str, data: dict[str, Any] | None) -> bool:
    """Whether a question field already carries the user's answer."""
    payload = data or {}
    if field == "destination":
        return bool(str(payload.get("destination") or "").strip())
    if field == "date_range":
        return _date_range_answered(payload)
    if field == "preferences":
        return bool(payload.get("trip_constraints"))
    if field in {"arrival_time", "departure_time", "trip_budget"}:
        return bool(str(payload.get(field) or "").strip())
    return False


def questioned_fields(data: dict[str, Any] | None) -> list[str]:
    """Field ids that no longer need asking: answered or explicitly skipped."""
    payload = data or {}
    skipped = set(declined_fields(payload))
    return [
        field
        for field in QUESTION_FIELDS
        if field in skipped or field_answered(field, payload)
    ]


def pending_fields(data: dict[str, Any] | None) -> list[str]:
    """Field ids still waiting for an answer or an explicit skip."""
    handled = set(questioned_fields(data))
    return [field for field in QUESTION_FIELDS if field not in handled]


def next_question(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the single question the server is currently asking.

    The order is fixed, so the first field that is neither answered nor skipped
    wins.  Required fields keep being returned until they carry a usable value;
    optional fields stop being asked as soon as they are answered or skipped.
    Returns ``None`` when collection is complete.
    """
    payload = data or {}
    pending = pending_fields(payload)
    if not pending:
        return None
    target = pending[0]
    spec = next(item for item in _BRIEF_QUESTIONS if item["field"] == target)
    return {
        "field": spec["field"],
        "label": spec["label"],
        "question": spec["question"],
        "kind": spec["kind"],
        "options": list(spec["options"]),
        "optional": spec["optional"],
        "hint": spec["hint"],
        "remaining": len(pending),
        "total": len(QUESTION_FIELDS),
    }


def collection_complete(data: dict[str, Any] | None) -> bool:
    """Whether every question has been answered or explicitly skipped.

    This is deliberately independent of ``required_brief_fields``: a brief can
    be ready (required information complete, submittable) long before collection
    is complete.
    """
    return next_question(data) is None


def auto_decline_questions(data: dict[str, Any] | None) -> list[str]:
    """Mark every unanswered optional field as handled.

    Used when the user asks to start planning without answering the rest: the
    required fields still have to be answered, but the optional ones stop being
    asked.  Returns the field ids newly marked as skipped.
    """
    payload = data or {}
    existing = declined_fields(payload)
    added = [
        field
        for field in OPTIONAL_QUESTION_FIELDS
        if field not in existing and not field_answered(field, payload)
    ]
    return existing + added


class AnswerConflict(ValueError):
    """The answer targets a field the server is not currently asking about."""


class AnswerInvalid(ValueError):
    """The answer targets the right field but carries an unusable value."""


# 偏好标签到约束类别的映射。类别决定这条偏好会被哪个规划阶段消费，
# 因此不能一律塞进 other_travel_preference。
PREFERENCE_CATEGORIES: dict[str, str] = {
    "自然风光": "attraction_preference",
    "历史人文": "attraction_preference",
    "摄影": "attraction_preference",
    "美食": "food_preference",
    "亲子": "companion_context",
    "慢节奏放松": "travel_pace",
}

_MAX_TEXT = {"destination": 200, "arrival_time": 100, "departure_time": 100, "trip_budget": 500}


def _is_skip(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _clean_text(field: str, label: str, value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise AnswerInvalid(f"{label}不能为空。")
    limit = _MAX_TEXT.get(field, 200)
    if len(text) > limit:
        raise AnswerInvalid(f"{label}最多 {limit} 个字。")
    return text


def answer_patch(
    data: dict[str, Any] | None, field: str, value: Any
) -> dict[str, Any]:
    """Validate one typed answer and return the patch to write into the brief.

    This is the only place where a value can enter a PlanningBrief without
    passing through the dialogue agent (see ADR 0002).  It therefore refuses
    anything the server did not itself offer: the field must be the one the
    server is currently asking about, and a choice value must come from that
    question's option list.  ``None`` (or a blank string) means "skip", which
    only optional questions accept.
    """
    payload = data or {}
    question = next_question(payload)
    if question is None:
        raise AnswerConflict("所有问题都已经处理完了。")
    if field != question["field"]:
        raise AnswerConflict("这不是当前正在询问的问题。")

    label = question["label"]
    if _is_skip(value):
        if not question["optional"]:
            raise AnswerInvalid(f"{label}还没有填写，不能跳过。")
        # 落库前就归一化成清单顺序：否则同一个跳过集合会因为写入先后不同
        # 而产生不同的持久化内容，指纹与幂等比较都会因此抖动。
        combined = declined_fields(
            {DECLINED_FIELDS_KEY: [*declined_fields(payload), field]}
        )
        return {DECLINED_FIELDS_KEY: combined}

    if field == "destination":
        return {"destination": _clean_text(field, label, value)}

    if field == "date_range":
        if not isinstance(value, dict):
            raise AnswerInvalid("请同时给出开始日期和结束日期。")
        start = _iso_date(value.get("start_date"))
        end = _iso_date(value.get("end_date"))
        if start is None or end is None:
            raise AnswerInvalid("日期格式不正确，请选择具体的出发和返回日期。")
        if end < start:
            raise AnswerInvalid("结束日期不能早于开始日期。")
        return {"start_date": start.isoformat(), "end_date": end.isoformat()}

    if field in {"arrival_time", "departure_time", "trip_budget"}:
        return {field: _clean_text(field, label, value)}

    if field == "preferences":
        chosen = value if isinstance(value, (list, tuple)) else [value]
        tags = [str(item).strip() for item in chosen if str(item).strip()]
        allowed = OPTION_LISTS["preferences"]
        unknown = [tag for tag in tags if tag not in allowed]
        if unknown:
            raise AnswerInvalid("这些偏好不在可选范围内：" + "、".join(unknown))
        # 偏好题只在需求单里一条约束都没有时才会成为当前提问，因此这里的
        # existing 通常为空；保留合并是为了让「已答」的判定将来放宽时不会
        # 静默丢掉用户已经说过的其它约束。
        existing = list(payload.get("trip_constraints") or [])
        merged = existing + [
            {
                "category": PREFERENCE_CATEGORIES[tag],
                "value_text": tag,
                "polarity": "prefer",
                "source": "manual",
            }
            for tag in tags
        ]
        # 去重交给 normalize_brief_data：它按（类别、文本、极性）判重，
        # 因此重复回答同一标签不会产生第二条。
        return {"trip_constraints": merged}

    raise AnswerConflict("这不是当前正在询问的问题。")
