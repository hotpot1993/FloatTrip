"""高德调用通道的契约测试。

覆盖本次变更最关键的三条不变式：

1. 额度不足时**一次网络请求都不发出**；
2. 每次真实请求（含重试）各占一次额度；
3. 失败按语义分类：配额类不重试、限流类重试、需要人工处理的不引导等待。

本文件只依赖 app.core 与 app.providers，可在本机直接运行。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.amap_quota import QUOTA_TIMEZONE, AmapQuotaExceeded, QuotaBucket
from app.core.amap_quota_store import QuotaStore
from app.core.database import init_db
from app.providers.amap import channel
from app.providers.amap.channel import AmapAuthorizationError, AmapRequestError
from app.providers.amap.client import (
    ACCESS_DENIED,
    OTHER,
    QUOTA,
    SPECIAL,
    THROTTLE,
    classify_failure,
)

LIMIT_ENVS = (
    "AMAP_QUOTA_SEARCH_LIMIT",
    "AMAP_QUOTA_WEATHER_LIMIT",
    "AMAP_QUOTA_LBS_LIMIT",
)


def ok(payload=None):
    return {"status": "1", "pois": payload or []}


def fail(info, infocode=""):
    data = {"status": "0", "info": info}
    if infocode:
        data["infocode"] = infocode
    return data


class ChannelTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "quota.db"
        init_db(self.db_path)

        patcher = mock.patch.dict(os.environ, {}, clear=False)
        self.env = patcher.start()
        self.addCleanup(patcher.stop)
        for name in LIMIT_ENVS:
            self.env.pop(name, None)
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "3"
        self.env["AMAP_QUOTA_WEATHER_LIMIT"] = "3"

        # 限额通过环境变量现读，所以 store 必须在环境变量设好之后再建
        self.store = QuotaStore(self.db_path)

        # 退避不该拖慢测试，但它必须真的被 await 过
        sleep = mock.patch.object(channel.asyncio, "sleep", new=_no_sleep)
        sleep.start()
        self.addCleanup(sleep.stop)

        self.calls: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_request(self, responses):
        """把一个响应序列接成 `_request_once` 的替身，并记录每次真实请求。"""
        queue = list(responses)

        async def _fake(url, *, timeout):
            self.calls.append(url)
            item = queue.pop(0) if queue else queue_default()
            if isinstance(item, Exception):
                raise channel._RetryableRequestError(item)
            return item

        def queue_default():
            raise AssertionError("响应序列已用尽，出现了计划外的请求")

        return _fake

    async def run_call(self, responses, bucket=QuotaBucket.SEARCH, **kwargs):
        with mock.patch.object(
            channel, "_request_once", new=self.fake_request(responses)
        ):
            return await channel.call_amap(
                "https://restapi.amap.com/v3/place/text",
                {"key": "k", "keywords": "景点"},
                bucket,
                store=self.store,
                **kwargs,
            )


async def _no_sleep(_seconds):
    return None


def run(coro):
    return asyncio.run(coro)


class GateBeforeNetworkTestCase(ChannelTestCase):
    """额度不足时不得发出请求——这是硬拦截与事后记账的全部差别。"""

    def test_额度耗尽后不再发出任何请求(self):
        for _ in range(3):
            run(self.run_call([ok()]))
        self.assertEqual(len(self.calls), 3, "限额 3，应当正好发出 3 次请求")

        with self.assertRaises(AmapQuotaExceeded):
            run(self.run_call([]))  # 空响应序列：任何请求都会炸
        self.assertEqual(
            len(self.calls), 3, "额度已满时不得再发出第 4 次请求"
        )

    def test_配额耗尽的异常带池名与重置时刻(self):
        for _ in range(3):
            run(self.run_call([ok()]))
        with self.assertRaises(AmapQuotaExceeded) as ctx:
            run(self.run_call([]))
        exc = ctx.exception
        self.assertEqual(exc.bucket, QuotaBucket.SEARCH)
        self.assertEqual(exc.public_code, "amap_quota_exhausted")
        self.assertIn("基础搜索服务", exc.public_message)
        self.assertIn("重置", exc.public_message)

    def test_余额不足时不部分发出(self):
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "1"
        run(self.run_call([ok()]))
        with self.assertRaises(AmapQuotaExceeded):
            run(self.run_call([]))
        self.assertEqual(len(self.calls), 1)

    def test_池之间互不牵连(self):
        self.env["AMAP_QUOTA_WEATHER_LIMIT"] = "1"
        run(self.run_call([ok()], bucket=QuotaBucket.WEATHER))
        with self.assertRaises(AmapQuotaExceeded):
            run(self.run_call([], bucket=QuotaBucket.WEATHER))
        # 天气满了，搜索照常
        run(self.run_call([ok()], bucket=QuotaBucket.SEARCH))
        self.assertEqual(len(self.calls), 2)


class RetryAccountingTestCase(ChannelTestCase):
    """重试必须逐次占额：高德按实际收到的请求扣配额。"""

    def test_限流重试每次都占额(self):
        result = run(
            self.run_call(
                [
                    fail("CUQPS_HAS_EXCEEDED_THE_LIMIT"),
                    fail("CUQPS_HAS_EXCEEDED_THE_LIMIT"),
                    ok(),
                ]
            )
        )
        self.assertEqual(result["status"], "1")
        self.assertEqual(len(self.calls), 3, "失败两次 + 成功一次 = 3 次真实请求")
        self.assertEqual(
            self.store.snapshot(QuotaBucket.SEARCH).used,
            3,
            "已用量必须等于真实请求数 3，而不是逻辑调用数 1",
        )

    def test_网络失败重试也占额(self):
        with self.assertRaises(AmapRequestError):
            run(self.run_call([RuntimeError("boom")] * 3))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 3)

    def test_重试途中额度耗尽则停止重试(self):
        self.env["AMAP_QUOTA_SEARCH_LIMIT"] = "2"
        with self.assertRaises(AmapQuotaExceeded):
            run(
                self.run_call(
                    [
                        fail("CUQPS_HAS_EXCEEDED_THE_LIMIT"),
                        fail("CUQPS_HAS_EXCEEDED_THE_LIMIT"),
                        fail("CUQPS_HAS_EXCEEDED_THE_LIMIT"),
                    ]
                )
            )
        self.assertEqual(
            len(self.calls), 2, "第 3 次重试应因额度不足而被拦下，不得发出"
        )

    def test_成功一次只占一次额(self):
        run(self.run_call([ok()]))
        self.assertEqual(self.store.snapshot(QuotaBucket.SEARCH).used, 1)


class FailureClassificationTestCase(ChannelTestCase):
    def test_配额失败立即失败不重试(self):
        with self.assertRaises(AmapQuotaExceeded):
            run(
                self.run_call(
                    [
                        fail("USER_DAILY_QUERY_OVER_LIMIT"),
                        ok(),  # 不该被取用
                    ]
                )
            )
        self.assertEqual(len(self.calls), 1, "配额类失败不得重试")

    def test_配额失败按infocode识别(self):
        with self.assertRaises(AmapQuotaExceeded):
            run(self.run_call([{"status": "0", "info": "未知", "infocode": "10044"}]))
        self.assertEqual(len(self.calls), 1)

    def test_未文档化的新码按未知错误上报且不重试(self):
        """月配额超限的返回码官方未文档化。落到这里时不得重试放大。"""
        with self.assertRaises(AmapRequestError) as ctx:
            run(self.run_call([fail("SOMETHING_NEW", "10099"), ok()]))
        self.assertEqual(len(self.calls), 1, "未识别的失败不得重试")
        self.assertNotIsInstance(ctx.exception, AmapQuotaExceeded)

    def test_密钥失效不重试且给出配置指引(self):
        with self.assertRaises(AmapAuthorizationError) as ctx:
            run(self.run_call([fail("INVALID_USER_KEY"), ok()]))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(ctx.exception.public_code, "amap_access_denied")
        self.assertIn("AMAP_API_KEY", ctx.exception.public_message)

    def test_IP受限文案引导人工处理而不是等待(self):
        with self.assertRaises(AmapRequestError) as ctx:
            run(self.run_call([fail("IP_QUERY_OVER_LIMIT"), ok()]))
        exc = ctx.exception
        self.assertEqual(len(self.calls), 1, "IP 封停不会自动恢复，重试无意义")
        self.assertEqual(getattr(exc, "public_code", None), "amap_ip_restricted")
        self.assertIn("工单", getattr(exc, "public_message", ""))
        self.assertNotIn("重置", getattr(exc, "public_message", ""))

    def test_限流类才重试(self):
        result = run(
            self.run_call([fail("ACCESS_TOO_FREQUENT"), ok()])
        )
        self.assertEqual(result["status"], "1")
        self.assertEqual(len(self.calls), 2)


class ClassificationUnitTestCase(unittest.TestCase):
    """分类函数本身：infocode 与 info 都要看。"""

    def test_按info识别(self):
        self.assertEqual(classify_failure({"info": "INVALID_USER_KEY"})[0], ACCESS_DENIED)
        self.assertEqual(classify_failure({"info": "USER_DAILY_QUERY_OVER_LIMIT"})[0], QUOTA)
        self.assertEqual(classify_failure({"info": "ACCESS_TOO_FREQUENT"})[0], THROTTLE)
        self.assertEqual(classify_failure({"info": "IP_QUERY_OVER_LIMIT"})[0], SPECIAL)

    def test_按infocode识别(self):
        """高德有时只给数字码，同一个错误的两个表示必须映射到同一类别。"""
        self.assertEqual(classify_failure({"info": "随便", "infocode": "10044"})[0], QUOTA)
        self.assertEqual(classify_failure({"info": "", "infocode": "10003"})[0], QUOTA)
        self.assertEqual(classify_failure({"info": "", "infocode": "10001"})[0], ACCESS_DENIED)
        self.assertEqual(classify_failure({"info": "", "infocode": "10010"})[0], SPECIAL)
        self.assertEqual(
            classify_failure({"info": "", "infocode": "CUQPS_HAS_EXCEEDED_THE_LIMIT"})[0],
            THROTTLE,
        )

    def test_名字与数字映射到同一类别(self):
        pairs = [
            ("USER_DAILY_QUERY_OVER_LIMIT", "10044"),
            ("INVALID_USER_KEY", "10001"),
            ("IP_QUERY_OVER_LIMIT", "10010"),
            ("ACCESS_TOO_FREQUENT", "10004"),
        ]
        for name, code in pairs:
            with self.subTest(name=name, code=code):
                self.assertEqual(
                    classify_failure({"info": name})[0],
                    classify_failure({"info": "", "infocode": code})[0],
                )

    def test_未知错误分类为other(self):
        category, identifier = classify_failure({"info": "WHATEVER"})
        self.assertEqual(category, OTHER)
        self.assertEqual(identifier, "WHATEVER")

    def test_空响应不炸(self):
        self.assertEqual(classify_failure({})[0], OTHER)


if __name__ == "__main__":
    unittest.main()
