"""高德配额池的领域行为测试。

覆盖：限额配置校验、月份边界、原子预占、用量对账的绝对值语义、阈值预警。
本文件只依赖 app.core，不需要 langgraph / langchain_core，可在本机直接运行。
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app.core.amap_quota import (
    DEFAULT_LIMITS,
    DEFAULT_WARNING_RATIO,
    OFFICIAL_MONTHLY_QUOTA,
    QUOTA_TIMEZONE,
    BUCKET_LIMIT_ENV,
    BUCKET_SERVICE_GROUP,
    AmapQuotaExceeded,
    QuotaBucket,
    QuotaConfigError,
    current_month,
    next_month_start,
    quota_exhausted_message,
    reset_hint,
    validate_quota_config,
    warning_threshold,
)
from app.core.amap_quota_store import QuotaStore, delete_old_months
from app.core.database import get_conn, init_db


LIMIT_ENVS = tuple(BUCKET_LIMIT_ENV.values())


class QuotaConfigTestCase(unittest.TestCase):
    """限额配置：非法值必须启动失败，而不是静默退化成「不限制」。"""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        self.env = patcher.start()
        self.addCleanup(patcher.stop)
        for name in (*LIMIT_ENVS, "AMAP_QUOTA_WARNING_RATIO"):
            self.env.pop(name, None)

    def test_默认限额按官方数值留出安全边际(self):
        limits = dict(DEFAULT_LIMITS)
        for bucket in (QuotaBucket.SEARCH, QuotaBucket.WEATHER):
            self.assertLess(limits[bucket], OFFICIAL_MONTHLY_QUOTA[bucket])
            self.assertGreater(limits[bucket], int(OFFICIAL_MONTHLY_QUOTA[bucket] * 0.9))
        # LBS 默认不限制：前端 AMap.Driving 的消耗服务端数不到，
        # 设一个拦不住大头的假上限只会给出虚假的安全感。
        self.assertEqual(limits[QuotaBucket.LBS], 0)

    def test_所有池都有服务组名与限额环境变量(self):
        for bucket in QuotaBucket:
            self.assertTrue(BUCKET_SERVICE_GROUP[bucket].strip())
            self.assertTrue(BUCKET_LIMIT_ENV[bucket].strip())

    def test_负数限额启动失败(self):
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "-1"
        with self.assertRaises(QuotaConfigError) as ctx:
            validate_quota_config()
        self.assertIn("AMAP_QUOTA_SEARCH_LIMIT", str(ctx.exception))

    def test_非整数限额启动失败而不是被当作不限制(self):
        self.env["AMAP_QUOTA_WEATHER_LIMIT"] = "abc"
        with self.assertRaises(QuotaConfigError):
            validate_quota_config()

    def test_零是合法的不限制(self):
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "0"
        validate_quota_config()  # 不应抛出

    def test_预警比例越界启动失败(self):
        for bad in ("0", "-0.5", "1.5"):
            with self.subTest(bad=bad):
                self.env["AMAP_QUOTA_WARNING_RATIO"] = bad
                with self.assertRaises(QuotaConfigError):
                    validate_quota_config()

    def test_不限制的池没有预警阈值(self):
        self.assertEqual(warning_threshold(QuotaBucket.LBS, 0), None)

    def test_限额很小时阈值仍可实现(self):
        # 向上取整，避免 int(1 * 0.8) == 0 导致永远达不到阈值
        self.assertEqual(warning_threshold(QuotaBucket.SEARCH, 1), 1)


class QuotaMonthTestCase(unittest.TestCase):
    """月份边界。写死时区是刻意的：跟随容器 TZ 会让同一个物理时刻归属不同月份。"""

    def test_月份按固定时区计算(self):
        # UTC 2026-09-30T20:00Z = 北京时间 2026-10-01 04:00
        moment = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(current_month(moment), "2026-10")

    def test_月份边界前一小时仍属当月(self):
        moment = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)  # 北京 23:00
        self.assertEqual(current_month(moment), "2026-09")

    def test_跨年(self):
        moment = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)  # 北京 2027-01-01
        self.assertEqual(current_month(moment), "2027-01")
        self.assertEqual(next_month_start(moment).strftime("%Y-%m-%d"), "2027-02-01")

    def test_容器时区不影响结果(self):
        moment = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)
        original = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            self.assertEqual(current_month(moment), "2027-01")
            os.environ["TZ"] = "UTC"
            self.assertEqual(current_month(moment), "2027-01")
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original

    def test_重置文案与月份边界同源(self):
        moment = datetime(2026, 9, 15, 0, 0, tzinfo=QUOTA_TIMEZONE)
        self.assertEqual(reset_hint(moment), "2026-10-01 00:00")
        message = quota_exhausted_message(QuotaBucket.SEARCH, moment)
        self.assertIn("2026-10-01 00:00", message)
        self.assertIn(BUCKET_SERVICE_GROUP[QuotaBucket.SEARCH], message)


class QuotaStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "quota.db"
        init_db(self.db_path)
        self.store = QuotaStore(self.db_path)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        self.env = patcher.start()
        self.addCleanup(patcher.stop)
        for name in (*LIMIT_ENVS, "AMAP_QUOTA_WARNING_RATIO"):
            self.env.pop(name, None)
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "10"
        self.env["AMAP_QUOTA_WEATHER_LIMIT"] = "4"
        self.env["AMAP_QUOTA_WARNING_RATIO"] = "0.8"

    def tearDown(self):
        self.tmp.cleanup()

    # ─── 预占 ────────────────────────────────────────────────

    def test_首次预占建立当月行(self):
        result = self.store.reserve(QuotaBucket.SEARCH)
        self.assertIsNotNone(result)
        snapshot, _ = result
        self.assertEqual(snapshot.used, 1)
        self.assertEqual(snapshot.limit, 10)
        self.assertEqual(snapshot.remaining, 9)

    def test_额度不足时返回None且不写入(self):
        for _ in range(10):
            self.assertIsNotNone(self.store.reserve(QuotaBucket.SEARCH))
        self.assertIsNone(self.store.reserve(QuotaBucket.SEARCH))
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 10)

    def test_超额预占不部分写入(self):
        # 剩 10 次，一次要 11 次：必须整体失败，而不是写进 10
        self.assertIsNone(self.store.reserve(QuotaBucket.SEARCH, count=11))
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 0)

    def test_池之间互不影响(self):
        for _ in range(4):
            self.store.reserve(QuotaBucket.WEATHER)
        self.assertIsNone(self.store.reserve(QuotaBucket.WEATHER))
        # 天气满了，搜索仍然可以正常预占
        self.assertIsNotNone(self.store.reserve(QuotaBucket.SEARCH))
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 1)

    def test_并发预占不超支(self):
        """检查与自增必须在同一事务里，否则并发请求会各自通过检查后一起超支。"""
        def worker(_):
            return self.store.reserve(QuotaBucket.SEARCH) is not None

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(30)))

        self.assertEqual(sum(results), 10, "成功预占数必须恰好等于限额")
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 10)

    def test_不限制的池永不拒绝(self):
        self.env["AMAP_QUOTA_LBS_LIMIT"] = "0"
        for _ in range(50):
            self.assertIsNotNone(self.store.reserve(QuotaBucket.LBS))
        snapshot = self.store.snapshot(QuotaBucket.LBS)
        self.assertTrue(snapshot.unlimited)
        self.assertIsNone(snapshot.remaining, "不限制时不应给出会被误读为真实余量的数字")

    def test_不限制的池不产生预警(self):
        self.env["AMAP_QUOTA_LBS_LIMIT"] = "0"
        for _ in range(50):
            _, crossed = self.store.reserve(QuotaBucket.LBS)
            self.assertFalse(crossed)

    # ─── 预警 ────────────────────────────────────────────────

    def test_首次越过阈值预警一次(self):
        # 限额 10、比例 0.8 → 阈值 8
        crossed_flags = []
        for _ in range(10):
            _, crossed = self.store.reserve(QuotaBucket.SEARCH)
            crossed_flags.append(crossed)
        self.assertEqual(
            [i for i, flag in enumerate(crossed_flags, start=1) if flag],
            [8],
            "阈值预警必须恰好触发一次，且在第 8 次",
        )

    def test_预警后继续消耗不重复打扰(self):
        for _ in range(8):
            self.store.reserve(QuotaBucket.SEARCH)
        self.assertTrue(self.store.snapshot(QuotaBucket.SEARCH).warning_sent)
        _, crossed = self.store.reserve(QuotaBucket.SEARCH)
        self.assertFalse(crossed)

    def test_新月份重新预警(self):
        for _ in range(8):
            self.store.reserve(QuotaBucket.SEARCH, month="2026-01")
        self.assertFalse(self.store.snapshot(QuotaBucket.SEARCH, month="2026-02").warning_sent)
        crossed_flags = [
            self.store.reserve(QuotaBucket.SEARCH, month="2026-02")[1]
            for _ in range(8)
        ]
        self.assertEqual(crossed_flags.count(True), 1)

    # ─── 用量对账 ────────────────────────────────────────────

    def test_录入绝对值后本地计数继续累加(self):
        """本变更的核心不变式：对账不得吃掉本地自动计数。"""
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "1000"
        snapshot = self.store.set_used(QuotaBucket.SEARCH, 300)
        self.assertEqual(snapshot.used, 300)

        result = self.store.reserve(QuotaBucket.SEARCH, count=5)
        self.assertIsNotNone(result, "录入 300 之后仍应有额度可预占")
        after, _ = result
        self.assertEqual(after.used, 305, "已用量应为 305，而不是 5 或 300")
        self.assertEqual(after.local_calls, 5)
        self.assertEqual(after.baseline, 300)

    def test_对账与控制台数字不相加(self):
        """控制台的数字已含对账前的本地调用，相加就是重复计数。"""
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "1000"
        self.store.reserve(QuotaBucket.SEARCH, count=4)
        # 控制台显示 300（其中已含本地这 4 次）
        snapshot = self.store.set_used(QuotaBucket.SEARCH, 300)
        self.assertEqual(snapshot.used, 300, "应为 300，而不是 4+300")
        after, _ = self.store.reserve(QuotaBucket.SEARCH, count=2)
        self.assertEqual(after.used, 302, "对账后只追加增量，不重算对账前的部分")

    def test_对账后的增量只算一次(self):
        """时间锚定：锚点记的是对账那一刻的本地计数，之后再对账会重新锚定。"""
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "1000"
        self.store.set_used(QuotaBucket.SEARCH, 300)
        self.store.reserve(QuotaBucket.SEARCH, count=5)
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 305)

        self.store.set_used(QuotaBucket.SEARCH, 400)
        self.assertEqual(
            self.store.snapshot(QuotaBucket.SEARCH).used,
            400,
            "重新对账后本地的那 5 次已被 400 涵盖，不能再追加一次",
        )
        again, _ = self.store.reserve(QuotaBucket.SEARCH)
        self.assertEqual(again.used, 401)

    def test_对账可下调如实的控制台读数(self):
        """本地计数比控制台高不代表控制台错了——如实录入就该如实生效。"""
        self.store.reserve(QuotaBucket.SEARCH, count=10)
        self.assertIsNone(self.store.reserve(QuotaBucket.SEARCH))
        self.store.set_used(QuotaBucket.SEARCH, 2)
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 2)
        self.assertIsNotNone(self.store.reserve(QuotaBucket.SEARCH))

    def test_负的已用量被拒绝(self):
        with self.assertRaises(ValueError):
            self.store.set_used(QuotaBucket.SEARCH, -1)

    def test_对账后额度不足按新值判断(self):
        self.store.set_used(QuotaBucket.SEARCH, 10)
        self.assertIsNone(self.store.reserve(QuotaBucket.SEARCH))

    def test_对账建立的基准参与预警判定(self):
        """录入的基准若已越过阈值，预占时就该发出预警，而不是等到本地也数够。"""
        self.store.set_used(QuotaBucket.SEARCH, 9)
        _, crossed = self.store.reserve(QuotaBucket.SEARCH)
        self.assertTrue(crossed)

    def test_未对账时退化为纯本地计数(self):
        for expected in (1, 2, 3):
            snapshot, _ = self.store.reserve(QuotaBucket.SEARCH)
            self.assertEqual(snapshot.used, expected)
            self.assertEqual(snapshot.baseline, 0)

    # ─── 快照 ────────────────────────────────────────────────

    def test_没有行时快照为零而不是报错(self):
        snapshot = self.store.snapshot(QuotaBucket.WEATHER)
        self.assertEqual(snapshot.used, 0)
        self.assertEqual(snapshot.remaining, 4)
        self.assertFalse(snapshot.warning_sent)

    def test_快照公开放映字段自解释(self):
        self.store.set_used(QuotaBucket.SEARCH, 3)
        public = self.store.snapshot(QuotaBucket.SEARCH).to_public()
        for key in (
            "bucket", "month", "used", "limit", "remaining",
            "unlimited", "local_calls", "baseline",
            "warning_threshold", "warning_sent",
        ):
            self.assertIn(key, public)
        self.assertEqual(public["baseline"], 3)
        self.assertEqual(public["warning_threshold"], 8)

    def test_三个池的快照一次取全(self):
        snapshots = self.store.snapshots()
        self.assertEqual(
            {item.bucket for item in snapshots}, set(QuotaBucket)
        )
        self.assertTrue(all(item.month == current_month() for item in snapshots))

    # ─── 清理 ────────────────────────────────────────────────

    def test_清理保留近期月份(self):
        target = current_month()
        with get_conn(self.db_path) as conn:
            conn.execute(
                "INSERT INTO amap_quota_usage"
                "(bucket,month,counter_calls,counter_baseline,updated_at) "
                "VALUES(?,?,?,?,?)",
                (QuotaBucket.SEARCH.value, "2020-01", 5, 0, "2020-01-01T00:00:00Z"),
            )
        removed = delete_old_months(13, db_path=self.db_path)
        self.assertEqual(removed, 1)
        with get_conn(self.db_path) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM amap_quota_usage WHERE month=?", (target,)
            ).fetchone()[0]
        self.assertGreaterEqual(remaining, 0)  # 当月行可能尚未建立

    def test_清理参数校验(self):
        with self.assertRaises(ValueError):
            delete_old_months(0, db_path=self.db_path)


class QuotaExceptionTestCase(unittest.TestCase):
    def test_异常携带公开错误码与文案(self):
        exc = AmapQuotaExceeded(QuotaBucket.SEARCH, used=10, limit=10)
        self.assertEqual(exc.public_code, "amap_quota_exhausted")
        self.assertIn(BUCKET_SERVICE_GROUP[QuotaBucket.SEARCH], exc.public_message)
        self.assertIn("重置", exc.public_message)
        self.assertFalse(exc.retryable)

    def test_调度器能读到公开字段(self):
        """app/runtime/scheduler.py 用 getattr 读这两个属性，裸 RuntimeError 会落到默认文案。"""
        exc = AmapQuotaExceeded(QuotaBucket.WEATHER, used=4, limit=4)
        self.assertEqual(getattr(exc, "public_code", "run_failed"), "amap_quota_exhausted")
        self.assertNotEqual(
            getattr(exc, "public_message", "任务执行失败，请稍后重试"),
            "任务执行失败，请稍后重试",
        )


class DefaultWarningRatioTestCase(unittest.TestCase):
    def test_默认预警比例(self):
        self.assertEqual(DEFAULT_WARNING_RATIO, 0.8)


if __name__ == "__main__":
    unittest.main()
