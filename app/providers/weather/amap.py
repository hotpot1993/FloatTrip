"""高德天气预报客户端。

调用高德天气预报接口（extensions=all），返回未来约 4 天逐日天气。
复用项目已有的 AMAP_API_KEY，无需额外申请。

请求经 `app.providers.amap.channel` 发出，归入 `weather` 配额池。高德按服务组
共享月配额，天气预报是独立的一组（个人认证 5,000/月），与基础搜索服务分开计。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.core.amap_quota import QuotaBucket
from app.core.cache import WEATHER_TTL, get_cached, set_cached, weather_cache_key
from app.providers.amap.channel import call_amap
from app.providers.amap.client import AMAP_WEATHER_URL

BAD_WEATHER_KEYWORDS = {"雨", "雪", "冰雹", "雾", "沙尘暴", "霾"}


def _is_bad(weather: str) -> bool:
    """判断天气描述是否为不宜户外出行的天气。"""
    return any(kw in weather for kw in BAD_WEATHER_KEYWORDS)


def _parse_forecasts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """把高德响应整理成逐日天气列表。

    格式：
        {
            "date":          "YYYY-MM-DD",
            "day_weather":   str,   # 白天天气，如"晴""小雨"
            "night_weather": str,   # 夜间天气
            "day_temp":      str,   # 白天最高温（°C）
            "night_temp":    str,   # 夜间最低温（°C）
            "is_bad":        bool,  # 是否为雨/雪/雾等不宜户外天气
        }

    结构异常或无数据时返回空列表（由调用方降级处理）。
    """
    forecasts = data.get("forecasts") or []
    if not forecasts or not isinstance(forecasts, list):
        return []
    casts = forecasts[0].get("casts") or []
    result: list[dict[str, Any]] = []
    for cast in casts:
        day = str(cast.get("date", "")).strip()
        if not day:
            continue
        day_weather = str(cast.get("dayweather", "")).strip()
        night_weather = str(cast.get("nightweather", "")).strip()
        result.append(
            {
                "date": day,
                "day_weather": day_weather,
                "night_weather": night_weather,
                "day_temp": str(cast.get("daytemp", "")).strip(),
                "night_temp": str(cast.get("nighttemp", "")).strip(),
                "is_bad": _is_bad(day_weather) or _is_bad(night_weather),
            }
        )
    return result


async def fetch_forecast_async(city: str, api_key: str) -> list[dict[str, Any]]:
    """取某城市未来约 4 天的逐日预报。

    接口异常或无数据时返回空列表（由调用方降级处理）。**配额耗尽不在此列**：
    那会以 `AmapQuotaExceeded` 抛出，让调用方把「额度用完」与「天气拿不到」
    区分开——前者用户需要知道，后者只需要一句降级说明。
    """
    cache_key = weather_cache_key(city)
    cached = await asyncio.to_thread(get_cached, cache_key)
    if cached is not None:
        return cached

    params: dict[str, Any] = {
        "key": api_key,
        "city": city,
        "extensions": "all",
        "output": "json",
    }
    data = await call_amap(AMAP_WEATHER_URL, params, QuotaBucket.WEATHER)
    result = _parse_forecasts(data)
    if result:
        await asyncio.to_thread(set_cached, cache_key, result, WEATHER_TTL)
    return result
