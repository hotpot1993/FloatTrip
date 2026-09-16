"""端到端手工验证：真实走 poi.py / weather.amap.py → 通道 → 配额。

不是单元测试，而是一次可读的验收脚本：把 HTTP 层换成假响应，其余全部走生产代码。
运行：python scripts/verify_amap_quota.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.TemporaryDirectory()
DB = Path(tmp.name) / "app.db"

# 小限额便于观察：搜索 3 次、天气 1 次、LBS 不限
os.environ["AMAP_QUOTA_SEARCH_LIMIT"] = "3"
os.environ["AMAP_QUOTA_WEATHER_LIMIT"] = "1"
os.environ["AMAP_QUOTA_LBS_LIMIT"] = "0"
os.environ["AMAP_QUOTA_WARNING_RATIO"] = "0.8"

import app.core.database as database  # noqa: E402
from app.core.amap_notify import emit_into_current_run  # noqa: E402
from app.core.amap_quota import AmapQuotaExceeded, QuotaBucket  # noqa: E402
from app.core.amap_quota_store import QuotaStore  # noqa: E402
from app.core.async_resources import amap_capacity  # noqa: E402

database.configure_database(DB)
database.init_db(DB)

from app.planning.helpers import fetch_city_spots_async  # noqa: E402
from app.providers.amap import channel as channel  # noqa: E402
from app.providers.amap import poi as poi_mod  # noqa: E402
from app.providers.weather import amap as weather_mod  # noqa: E402

STORE = QuotaStore(DB)
HITS: list[str] = []
WARNINGS: list[dict] = []


async def fake_request_once(url: str, *, timeout: int) -> dict:
    """假的一次高德网络请求。

    `infocode: 10000` 不能省：真实高德成功响应会带它，而通道的失败分类靠
    infocode / info 判定；漏掉的话空字符串会被当成未知错误。
    """
    HITS.append(url)
    if "weatherInfo" in url:
        return {
            "status": "1",
            "infocode": "10000",
            "info": "OK",
            "forecasts": [
                {
                    "casts": [
                        {
                            "date": "2026-09-20",
                            "dayweather": "中雨",
                            "nightweather": "小雨",
                            "daytemp": "22",
                            "nighttemp": "16",
                        }
                    ]
                }
            ],
        }
    return {
        "status": "1",
        "infocode": "10000",
        "info": "OK",
        "pois": [{"name": "中山陵", "location": "118.85,32.06", "biz_ext": {"rating": "4.7"}}],
    }


def ok(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    return condition


async def main() -> int:
    failures = 0
    # 替身必须接在 channel 上：channel.py 用 `from app.core.http import
    # http_get_json_async` 绑定了函数对象，patch app.core.http 影响不到它。
    # 接 `_request_once` 正好是「一次真实网络请求」的边界，与通道自己的重试
    # 逻辑互不干扰。
    with mock.patch.object(channel, "_request_once", new=fake_request_once), \
         emit_into_current_run(lambda kind, payload: WARNINGS.append(payload)):

        print("\n1) 一次景点搜索（3 个关键词并发）")
        spots = await fetch_city_spots_async("南京", "k")
        used = STORE.snapshot(QuotaBucket.SEARCH).used
        failures += not ok("3 个关键词各发一次，恰好消耗 3 次额度", used == 3, f"used={used}")
        failures += not ok("解析出候选景点", len(spots) == 1, f"{len(spots)} 个")
        failures += not ok("发出 3 个真实请求", len(HITS) == 3, f"{len(HITS)} 个")

        print("\n2) 额度已满时再次搜索（硬拦截）")
        before = len(HITS)
        try:
            await fetch_city_spots_async("苏州", "k")
            failures += not ok("应当被拦截", False, "竟然成功了")
        except AmapQuotaExceeded as exc:
            failures += not ok("抛出 AmapQuotaExceeded", True)
            failures += not ok(
                "一次网络请求都没发出", len(HITS) == before, f"多发了 {len(HITS) - before} 个"
            )
            failures += not ok("文案含池名", "基础搜索服务" in exc.public_message)
            failures += not ok("文案含重置时刻", "重置" in exc.public_message)
            print(f"      文案：{exc.public_message}")

        print("\n3) 搜索池耗尽不影响天气池")
        try:
            forecast = await weather_mod.fetch_forecast_async("南京", "k")
            failures += not ok("天气照常返回", len(forecast) == 1)
        except Exception as exc:  # noqa: BLE001
            failures += not ok("天气不应被搜索池牵连", False, repr(exc))

        print("\n4) 天气池耗尽")
        try:
            await weather_mod.fetch_forecast_async("苏州", "k")
            failures += not ok("天气应当被拦截", False, "竟然成功了")
        except AmapQuotaExceeded as exc:
            failures += not ok("天气池被拦截", True)
            failures += not ok("文案含天气服务组名", "天气预报" in exc.public_message)
            print(f"      文案：{exc.public_message}")

        print("\n5) 阈值预警（每池各一次）")
        # 搜索池限额 3、比例 0.8 → 阈值 max(1, ceil(2.4)) = 3，第 3 次触发；
        # 天气池限额 1 → 阈值 1，第 1 次触发。两池各自预警一次，互不影响。
        search_warnings = [w for w in WARNINGS if w["bucket"] == "search"]
        weather_warnings = [w for w in WARNINGS if w["bucket"] == "weather"]
        failures += not ok(
            "搜索池恰好预警一次", len(search_warnings) == 1, f"{len(search_warnings)} 次"
        )
        failures += not ok(
            "天气池恰好预警一次", len(weather_warnings) == 1, f"{len(weather_warnings)} 次"
        )
        if search_warnings:
            failures += not ok("搜索池预警带用量", search_warnings[0]["used"] == 3)
            failures += not ok(
                "搜索池预警文案含服务组名",
                "基础搜索服务" in search_warnings[0]["message"],
            )

        print("\n6) 并发槽位已归还")
        failures += not ok("AMap 信号量空闲", amap_capacity._value == 8, f"value={amap_capacity._value}")

    print(f"\n实际发出的 HTTP 请求总数：{len(HITS)}")
    print(f"搜索池 used={STORE.snapshot(QuotaBucket.SEARCH).used}"
          f"  天气池 used={STORE.snapshot(QuotaBucket.WEATHER).used}"
          f"  LBS池 used={STORE.snapshot(QuotaBucket.LBS).used}")
    print("\n" + ("全部通过" if not failures else f"失败 {failures} 项"))
    return 1 if failures else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    tmp.cleanup()
    raise SystemExit(code)
