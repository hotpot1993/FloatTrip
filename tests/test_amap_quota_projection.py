"""配额预警的传递路径测试。

预警要从调用通道一路走到用户面前，中间必须解耦：通道属于 providers 层，
不知道当前是哪个 Run。这里钉住那段桥接的行为。

不依赖 langgraph / langchain_core，可在本机直接运行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.amap_notify import emit_into_current_run, notify_quota_warning
from app.core.amap_quota import QuotaBucket, current_month
from app.core.amap_quota_store import QuotaStore
from app.core.database import init_db
from app.providers.amap import channel


class NotifySinkTestCase(unittest.TestCase):
    def test_装有发送器时转交事件(self):
        seen = []
        with emit_into_current_run(lambda kind, payload: seen.append((kind, payload))):
            notify_quota_warning({"bucket": "search", "used": 8, "limit": 10})
        self.assertEqual(len(seen), 1)
        kind, payload = seen[0]
        self.assertEqual(kind, "amap.quota_warning")
        self.assertEqual(payload["used"], 8)

    def test_退出上下文后不再转发(self):
        seen = []
        with emit_into_current_run(lambda kind, payload: seen.append(kind)):
            pass
        notify_quota_warning({"bucket": "search"})
        self.assertEqual(seen, [], "上下文退出后不得再往那个 Run 发事件")

    def test_没有发送器时只落日志不抛异常(self):
        with self.assertLogs("app.core.amap_notify", level=logging.WARNING) as captured:
            notify_quota_warning({"bucket": "search", "used": 1, "limit": 2})
        self.assertTrue(any("配额预警" in line for line in captured.output))

    def test_发送器抛异常不影响调用方(self):
        """预警是旁路信息，不能因为它出问题就弄挂正在跑的规划。"""
        def boom(kind, payload):
            raise RuntimeError("事件总线挂了")

        with emit_into_current_run(boom):
            with self.assertLogs("app.core.amap_notify", level=logging.WARNING):
                notify_quota_warning({"bucket": "search"})

    def test_上下文按任务隔离(self):
        """contextvar 必须跟随 asyncio 任务，否则并发 Run 会互相串台。"""
        seen = []

        async def worker(tag):
            with emit_into_current_run(lambda kind, payload: seen.append(tag)):
                await asyncio.sleep(0)
                notify_quota_warning({"bucket": "search"})

        async def main():
            await asyncio.gather(worker("a"), worker("b"))

        asyncio.run(main())
        self.assertEqual(sorted(seen), ["a", "b"])


class ChannelWarningTestCase(unittest.TestCase):
    """通道在首次越过阈值时才发预警，且预警不改变请求结果。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "quota.db"
        init_db(self.db_path)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        self.env = patcher.start()
        self.addCleanup(patcher.stop)
        for name in (
            "AMAP_QUOTA_SEARCH_LIMIT",
            "AMAP_QUOTA_WEATHER_LIMIT",
            "AMAP_QUOTA_LBS_LIMIT",
            "AMAP_QUOTA_WARNING_RATIO",
        ):
            self.env.pop(name, None)
        # 限额 10、比例 0.8 → 阈值 8
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "10"
        self.env["AMAP_QUOTA_WARNING_RATIO"] = "0.8"
        self.store = QuotaStore(self.db_path)
        sleep = mock.patch.object(channel.asyncio, "sleep", new=_no_sleep)
        sleep.start()
        self.addCleanup(sleep.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, warnings):
        async def once():
            async def fake_request(url, *, timeout):
                return {"status": "1", "pois": []}

            with mock.patch.object(channel, "_request_once", new=fake_request):
                await channel.call_amap(
                    "https://restapi.amap.com/v3/place/text",
                    {"key": "k"},
                    QuotaBucket.SEARCH,
                    store=self.store,
                )

        asyncio.run(once())

    def test_越过阈值时发一次预警(self):
        seen = []
        with emit_into_current_run(lambda kind, payload: seen.append(payload)):
            for _ in range(10):
                self._call(seen)

        self.assertEqual(len(seen), 1, "限额 10、阈值 8，预警应当恰好一次")
        payload = seen[0]
        self.assertEqual(payload["bucket"], "search")
        self.assertEqual(payload["used"], 8)
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(payload["remaining"], 2)
        self.assertIn("基础搜索服务", payload["message"])
        self.assertIn("重置", payload["message"])

    def test_不限制的池不产生预警(self):
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "0"
        seen = []
        with emit_into_current_run(lambda kind, payload: seen.append(payload)):
            for _ in range(20):
                self._call(seen)
        self.assertEqual(seen, [])

    def test_预警不改写用量(self):
        with emit_into_current_run(lambda kind, payload: None):
            for _ in range(10):
                self._call(None)
        snapshot = self.store.snapshot(QuotaBucket.SEARCH)
        self.assertEqual(snapshot.used, 10)
        self.assertTrue(snapshot.warning_sent)
        self.assertEqual(snapshot.month, current_month())

    def test_发送器抛异常时请求仍然成功(self):
        def boom(kind, payload):
            raise RuntimeError("挂")

        with emit_into_current_run(boom):
            with self.assertLogs("app.core.amap_notify", level=logging.WARNING):
                for _ in range(10):
                    self._call(None)
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 10)


async def _no_sleep(_seconds):
    return None


if __name__ == "__main__":
    unittest.main()
