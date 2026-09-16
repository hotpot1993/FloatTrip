"""抵达与返程时刻窗口测试：表述解析、规则判定与两条状态链路。

覆盖三层：
1. helpers 的纯函数（表述 → 可用下界 / 规则）；
2. 新增字段能否沿快照透传进 TravelPlanState；
3. 修改任务能否从父行程的 planner checkpoint 继承这两个字段
   ——这条路走的是逐字段白名单，漏一处就会静默丢字段。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


# ─── 表述解析 ────────────────────────────────────────────────

class TestArrivalFloor:
    """抵达取时段右端：宁可假定用户要到时段末尾才能开始活动，排少不排错。"""

    @pytest.mark.parametrize("text,expected", [
        ("上午", "12:00"),
        ("中午", "14:00"),
        ("下午", "17:00"),
        ("傍晚", "19:00"),
        ("晚上", "21:00"),
        ("11月7日傍晚抵达", "19:00"),
    ])
    def test_时段词按右端换算(self, text, expected):
        from app.planning.helpers import arrival_floor

        assert arrival_floor(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("18:30", "18:30"),
        ("18：30", "18:30"),
        ("傍晚6点", "18:00"),
        ("下午3点", "15:00"),
        ("上午9点", "09:00"),
        ("晚上8点半", "20:00"),
    ])
    def test_具体时刻原样解析(self, text, expected):
        """说了钟点就用钟点，不再套时段的右端——「下午3点」是 15:00，不是 17:00。"""
        from app.planning.helpers import arrival_floor

        assert arrival_floor(text) == expected

    @pytest.mark.parametrize("text", ["", None, "   ", "还没定", "看情况"])
    def test_无法识别时返回None(self, text):
        from app.planning.helpers import arrival_floor

        assert arrival_floor(text) is None


class TestDepartureEdge:
    """返程方向与抵达相反：按该时段最早可能离开估算才安全。"""

    @pytest.mark.parametrize("text,expected", [
        ("上午", "09:00"),
        ("中午", "12:00"),
        ("下午", "14:00"),
        ("傍晚", "17:00"),
        ("晚上", "19:00"),
        ("18:30", "18:30"),
    ])
    def test_取时段起点(self, text, expected):
        from app.planning.helpers import departure_edge

        assert departure_edge(text) == expected


class TestDeadline:
    @pytest.mark.parametrize("text,expected", [
        ("18:30", "16:30"),
        ("15:00", "13:00"),
        ("上午", "07:00"),
    ])
    def test_返程时刻减去赶车缓冲(self, text, expected):
        from app.planning.helpers import departure_deadline

        assert departure_deadline(text) == expected

    def test_无法识别时返回None(self):
        from app.planning.helpers import departure_deadline

        assert departure_deadline("还没定") is None

    @pytest.mark.parametrize("text,expected", [
        ("22:00", True),
        ("21:30", True),
        # 「晚上」的右端正好落在阈值上：不算过晚，首日仍可排晚餐与夜间可玩点
        ("21:00", False),
        ("晚上", False),
        ("傍晚", False),
        ("", False),
    ])
    def test_深夜抵达判定(self, text, expected):
        from app.planning.helpers import is_late_arrival

        assert is_late_arrival(text) is expected


# ─── 规则判定 ────────────────────────────────────────────────

class TestTimeWindowPlan:
    def test_两处都没提供时返回None(self):
        from app.planning.helpers import time_window_plan

        assert time_window_plan(None, None, 4) is None
        assert time_window_plan("", "  ", 4) is None

    def test_傍晚抵达时首日只排晚间(self):
        from app.planning.helpers import FIRST_DAY_EVENING_ONLY, time_window_plan

        plan = time_window_plan("11月7日傍晚抵达", None, 4)

        # 傍晚的右端是 19:00：首日直到 19:00 才认为用户能开始活动
        assert plan["arrival_floor"] == "19:00"
        assert plan["first_day"] == FIRST_DAY_EVENING_ONLY
        assert plan["last_day"] is None

    def test_晚上抵达仍可排晚间活动(self):
        """「晚上」的右端正好是 21:00，等于阈值但不越界，因此不算过晚抵达。"""
        from app.planning.helpers import FIRST_DAY_EVENING_ONLY, time_window_plan

        plan = time_window_plan("晚上到", None, 3)

        assert plan["arrival_floor"] == "21:00"
        assert plan["first_day"] == FIRST_DAY_EVENING_ONLY

    def test_深夜抵达时首日不排景点(self):
        from app.planning.helpers import FIRST_DAY_NO_SPOTS, time_window_plan

        plan = time_window_plan("23:30 落地", None, 3)

        assert plan["first_day"] == FIRST_DAY_NO_SPOTS

    def test_给出返程时刻时末日受截止时刻约束(self):
        from app.planning.helpers import LAST_DAY_BEFORE_DEADLINE, time_window_plan

        plan = time_window_plan(None, "18:30 的高铁", 4)

        assert plan["departure_deadline"] == "16:30"
        assert plan["last_day"] == LAST_DAY_BEFORE_DEADLINE
        assert plan["first_day"] is None

    def test_单日行程给出可用窗口(self):
        from app.planning.helpers import time_window_plan

        plan = time_window_plan("09:00", "20:00", 1)

        assert plan["single_day_window"] == ("09:00", "18:00")
        assert plan["first_day"] is None and plan["last_day"] is None

    def test_单日行程窗口过窄(self):
        from app.planning.helpers import time_window_plan

        # 傍晚到、当晚就走：抵达下界 19:00，而返程截止被缓冲推到 17:00，装不下任何活动
        plan = time_window_plan("傍晚", "晚上", 1)

        assert plan["single_day_window"] is None
        assert plan["single_day_too_narrow"] is True

    def test_单日行程时刻无法识别时按最保守处理(self):
        from app.planning.helpers import time_window_plan

        plan = time_window_plan("看情况", "再说", 1)

        assert plan["single_day_unknown"] is True
        assert plan["single_day_too_narrow"] is False

    def test_天数未知时不套用首末规则(self):
        from app.planning.helpers import time_window_plan

        plan = time_window_plan("傍晚", "15:00", 0)

        assert plan["first_day"] is None and plan["last_day"] is None
        assert plan["arrival_floor"] == "19:00"


# ─── 快照透传 ────────────────────────────────────────────────

class TestSnapshotToState:
    def test_抵达与返程时刻进入图状态(self):
        from app.planning.runtime_worker import snapshot_to_state

        state = snapshot_to_state({
            "destination": "南京",
            "start_date": "2026-11-07",
            "end_date": "2026-11-10",
            "arrival_time": "傍晚",
            "departure_time": "18:30",
            "days": 4,
        })

        assert state.travel_arrival_time == "傍晚"
        assert state.travel_departure_time == "18:30"

    def test_旧快照缺少字段时取默认空值(self):
        from app.planning.runtime_worker import snapshot_to_state

        state = snapshot_to_state({
            "destination": "南京",
            "start_date": "2026-11-07",
            "end_date": "2026-11-10",
            "days": 4,
        })

        assert state.travel_arrival_time is None
        assert state.travel_departure_time is None


class TestBriefReadiness:
    def test_缺少抵达时刻不影响需求单可提交(self):
        from app.core.planning_brief import required_brief_fields

        assert required_brief_fields({
            "destination": "南京",
            "start_date": "2026-11-07",
            "end_date": "2026-11-10",
        }) == []


# ─── 修改任务继承 ────────────────────────────────────────────

class TestRevisionInheritance:
    """修改任务的状态由父行程 planner checkpoint 逐字段重建，
    不是从请求快照继承——所以这两个字段必须在白名单里显式接上。"""

    @pytest.fixture()
    def db(self, tmp_path, monkeypatch):
        import app.core.database as database

        monkeypatch.setattr(database, "_DB_PATH", tmp_path / "test.db")
        database.init_db()
        return database

    def _user(self) -> str:
        from app.core.database import get_conn

        uid = str(uuid.uuid4())
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                (uid, f"u-{uid}", "test", "2026-01-01T00:00:00Z"),
            )
        return uid

    def _itinerary(self, uid: str, planner_state: dict) -> str:
        from app.core.database import get_conn
        from app.core.memory import save_itinerary

        plan = {"destination": "南京", "start_date": "2026-11-07", "end_date": "2026-11-10", "days": []}
        with get_conn() as conn:
            return save_itinerary(
                uid, plan, "南京", conn, planner_state=planner_state
            )

    def test_修改任务继承抵达与返程时刻(self, db):
        from app.planning.runtime_worker import revision_snapshot_to_state

        uid = self._user()
        plan_id = self._itinerary(uid, {
            "query": "南京",
            "destination": "南京",
            "travel_start_date": "2026-11-07",
            "travel_end_date": "2026-11-10",
            "days": 4,
            "travel_arrival_time": "傍晚",
            "travel_departure_time": "18:30",
        })

        state = asyncio.run(revision_snapshot_to_state({
            "user_id": uid,
            "request_snapshot": {
                "related_itinerary_id": plan_id,
                "modification_notes": "第三天松一点",
            },
        }))

        assert state.travel_arrival_time == "傍晚"
        assert state.travel_departure_time == "18:30"
        assert state.days == 4

    def test_旧行程的checkpoint没有该字段时取空(self, db):
        from app.planning.runtime_worker import revision_snapshot_to_state

        uid = self._user()
        plan_id = self._itinerary(uid, {
            "query": "南京",
            "destination": "南京",
            "travel_start_date": "2026-11-07",
            "travel_end_date": "2026-11-10",
            "days": 4,
        })

        state = asyncio.run(revision_snapshot_to_state({
            "user_id": uid,
            "request_snapshot": {"related_itinerary_id": plan_id},
        }))

        assert state.travel_arrival_time is None
        assert state.travel_departure_time is None

    def test_缺少planner_checkpoint时报错而不是静默降级(self, db):
        from app.planning.runtime_worker import revision_snapshot_to_state

        uid = self._user()
        plan_id = self._itinerary(uid, {})

        with pytest.raises(ValueError, match="planner checkpoint"):
            asyncio.run(revision_snapshot_to_state({
                "user_id": uid,
                "request_snapshot": {"related_itinerary_id": plan_id},
            }))
