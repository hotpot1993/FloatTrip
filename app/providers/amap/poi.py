"""高德地点搜索与 POI 解析。

所有请求经 `app.providers.amap.channel` 发出——那是后端唯一允许发高德请求的
地方，负责失败分类、额度预占与并发约束。本模块只负责参数拼装、缓存与结果解析。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.amap_quota import QuotaBucket
from app.core.cache import (
    NEARBY_TTL,
    POI_TTL,
    get_cached,
    nearby_cache_key,
    poi_cache_key,
    set_cached,
)
from app.providers.amap.channel import call_amap
from app.providers.amap.client import (
    AMAP_AROUND_SEARCH_URL,
    AMAP_TEXT_SEARCH_URL,
)

ATTRACTION_TYPE = "风景名胜"


# ─── 周边搜索 ────────────────────────────────────────────────

async def search_around_pois_async(
    location: dict[str, float],
    api_key: str,
    *,
    types: str = "",
    keyword: str = "",
    radius: int = 1000,
    offset: int = 6,
) -> list[dict[str, Any]]:
    """调用高德周边搜索，围绕坐标查找餐饮、景点等 POI。

    餐饮搜索传 types='餐饮服务'，按分类搜索覆盖所有餐馆；
    有 types 时不发 keywords（两者语义不同，混用结果偏少）。

    结果按坐标 + 半径 + 类型 + 关键词 + 条数缓存：规划里每一天的午晚餐各发一次
    周边搜索，跨行程复用同一锚点时能直接省下真实额度。
    """
    cache_key = nearby_cache_key(
        location, radius=radius, types=types, keyword=keyword, offset=offset
    )
    cached = await asyncio.to_thread(get_cached, cache_key)
    if cached is not None:
        # 命中缓存不占额、不发请求——配额层根本不需要知道缓存的存在。
        return cached

    params: dict[str, Any] = {
        "key": api_key,
        "location": f"{location['lng']},{location['lat']}",
        "radius": str(radius),
        "offset": str(offset),
        "page": "1",
        "extensions": "all",
        "output": "json",
    }
    if types:
        params["types"] = types
    elif keyword:
        params["keywords"] = keyword

    data = await call_amap(AMAP_AROUND_SEARCH_URL, params, QuotaBucket.SEARCH)
    pois = data.get("pois", [])
    result = pois if isinstance(pois, list) else []
    # 失败响应不会走到这里（通道会抛异常），所以写进缓存的一定是成功结果。
    if result:
        await asyncio.to_thread(set_cached, cache_key, result, NEARBY_TTL)
    return result


# ─── 景点关键字搜索 ──────────────────────────────────────────

async def search_attraction_pois_async(
    city: str,
    api_key: str,
    *,
    keywords: str = "景点",
    offset: int = 25,
    page: int = 1,
) -> list[dict[str, Any]]:
    """用高德关键字搜索 API 返回景点 POI 列表，类型固定为风景名胜。"""
    # 缓存逻辑：仅缓存 page=1 的请求
    cache_key = poi_cache_key(city, keywords) if page == 1 else None
    if cache_key is not None:
        cached = await asyncio.to_thread(get_cached, cache_key)
        if cached is not None:
            return cached

    params: dict[str, Any] = {
        "key": api_key,
        "keywords": keywords,
        "types": ATTRACTION_TYPE,
        "city": city,
        "citylimit": "true",
        "offset": str(offset),
        "page": str(page),
        "extensions": "all",
        "output": "json",
    }
    data = await call_amap(AMAP_TEXT_SEARCH_URL, params, QuotaBucket.SEARCH)
    pois = data.get("pois", [])
    result = pois if isinstance(pois, list) else []
    if page == 1 and result and cache_key is not None:
        await asyncio.to_thread(set_cached, cache_key, result, POI_TTL)
    return result


async def search_city_pois_async(
    city: str,
    api_key: str,
    *,
    keywords: str,
    types: str,
    offset: int = 8,
) -> list[dict[str, Any]]:
    """通用城市关键字搜索（手动编辑换点用）。

    不做缓存：由调用方决定缓存粒度（`/api/poi/search` 会按 kind 与关键词缓存）。
    """
    params: dict[str, Any] = {
        "key": api_key,
        "keywords": keywords,
        "types": types,
        "city": city,
        "citylimit": "true",
        "offset": str(offset),
        "page": "1",
        "extensions": "all",
        "output": "json",
    }
    data = await call_amap(AMAP_TEXT_SEARCH_URL, params, QuotaBucket.SEARCH)
    pois = data.get("pois", [])
    return pois if isinstance(pois, list) else []


# ─── POI 解析 ────────────────────────────────────────────────

def parse_location(value: Any) -> dict[str, float] | None:
    """把高德 "lng,lat" 字符串解析成结构化坐标，失败返回 None。"""
    if not isinstance(value, str) or "," not in value:
        return None
    lng_text, lat_text = value.split(",", 1)
    try:
        return {"lng": float(lng_text), "lat": float(lat_text)}
    except ValueError:
        return None


def normalize_address(value: Any) -> str:
    """把高德可能返回的字符串或数组地址统一成字符串。"""
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item)
    return str(value or "")


def poi_to_spot(poi: dict[str, Any]) -> dict[str, Any] | None:
    """把高德景点 POI 原始字段整理成规划用结构。缺坐标返回 None。"""
    loc_str = poi.get("location", "")
    if not loc_str or "," not in loc_str:
        return None
    lng, lat = loc_str.split(",", 1)
    try:
        location = {"lng": float(lng), "lat": float(lat)}
    except ValueError:
        return None

    biz_ext = poi.get("biz_ext") or {}
    rating_raw = biz_ext.get("rating", "") if isinstance(biz_ext, dict) else ""
    try:
        rating: float | None = float(rating_raw) if rating_raw else None
    except ValueError:
        rating = None

    open_time: str | None = (
        str(biz_ext["opentime2"]).strip() if isinstance(biz_ext, dict) and biz_ext.get("opentime2") else None
    ) or (
        str(biz_ext["opentime"]).strip() if isinstance(biz_ext, dict) and biz_ext.get("opentime") else None
    ) or None

    photos = poi.get("photos") or []
    first_photo: str | None = None
    if isinstance(photos, list) and photos and isinstance(photos[0], dict):
        first_photo = str(photos[0].get("url", "")).strip() or None

    cost_raw = str(biz_ext.get("cost", "")).strip() if isinstance(biz_ext, dict) else ""

    return {
        "name": poi.get("name", ""),
        "rating": rating,
        "open_time": open_time,
        "location": location,
        "photo": first_photo,
        "adname": str(poi.get("adname") or ""),
        "address": normalize_address(poi.get("address", "")),
        "tel": str(poi.get("tel") or "").strip() or None,
        "cost": cost_raw or None,
    }
