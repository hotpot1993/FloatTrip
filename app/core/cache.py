"""Redis 缓存层：高德 API 结果缓存，Redis 不可用时静默降级。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 全局 Redis 客户端（延迟初始化）
_redis_client = None
_redis_init_attempted = False


def _get_redis():
    """获取 Redis 客户端，使用连接池复用连接。Redis 不可用时返回 None。"""
    global _redis_client, _redis_init_attempted
    if _redis_init_attempted:
        return _redis_client
    _redis_init_attempted = True

    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        logger.info("未配置 REDIS_URL，缓存功能已禁用")
        return None

    try:
        import redis
        _redis_client = redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=1,
            retry_on_timeout=False,
        )
        # 测试连接
        _redis_client.ping()
        logger.info("Redis 缓存已连接：%s", redis_url.split("@")[-1] if "@" in redis_url else redis_url)
    except Exception as exc:
        logger.warning("Redis 连接失败，缓存功能已禁用：%s", exc)
        _redis_client = None

    return _redis_client


def get_cached(key: str) -> Any | None:
    """从缓存获取数据。Redis 不可用或未命中时返回 None。"""
    r = _get_redis()
    if r is None:
        return None
    try:
        raw = r.get(key)
        if raw is not None:
            return json.loads(raw)
    except Exception as exc:
        logger.debug("缓存读取失败 [%s]：%s", key, exc)
    return None


def set_cached(key: str, value: Any, ttl_seconds: int) -> None:
    """写入缓存。Redis 不可用时静默跳过。"""
    r = _get_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl_seconds, json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        logger.debug("缓存写入失败 [%s]：%s", key, exc)


# ─── 缓存键命名工具 ──────────────────────────────────────────
#
# 命名空间约定：`tripagent:{类别}:...`。**新类别必须另起一段前缀**，不能往
# 既有前缀后面续键——否则两类语义不同的结果会落进同一个键空间。
#
# 高德按「服务组」共享月配额，缓存命中不消耗额度，所以每一个能命中的缓存
# 都是一次真实的额度节省。

def weather_cache_key(city: str) -> str:
    """天气预报缓存键。格式：tripagent:weather:{city}"""
    return f"tripagent:weather:{city}"


def poi_cache_key(city: str, keyword: str) -> str:
    """POI 关键字搜索缓存键。格式：tripagent:poi:{city}:{keyword}

    注意：这个键只看城市与关键词，**不含 types / offset / radius**。调用方
    必须自己保证 `keyword` 带上足以区分结果集的上下文，否则不同参数的结果会
    互相污染。新增的周边搜索与步行路线因此另起了命名空间（见下）。
    """
    return f"tripagent:poi:{city}:{keyword}"


# 坐标保留位数：4 位小数约 11 米。周边搜索的半径以公里计，11 米的抖动不该
# 造成缓存未命中；再粗就会让两个不同路口共用一份结果。
COORD_PRECISION = 4


def _coord(value: float) -> str:
    return f"{float(value):.{COORD_PRECISION}f}"


def nearby_cache_key(
    location: dict[str, float],
    *,
    radius: int,
    types: str,
    keyword: str,
    offset: int,
) -> str:
    """周边搜索缓存键。

    格式：`tripagent:nearby:{lng},{lat}:r{radius}:o{offset}:t{types}:k{keyword}`

    刻意不放进 `tripagent:poi:` 命名空间：既有 `poi_cache_key` 不含 radius /
    types / offset，两者若共用一个前缀，围绕同一点的关键字搜索与餐饮周边搜索
    会在同一个键上互相覆盖。
    """
    return (
        f"tripagent:nearby:{_coord(location['lng'])},{_coord(location['lat'])}"
        f":r{radius}:o{offset}:t{types}:k{keyword}"
    )


def walking_cache_key(
    origin: dict[str, float], destination: dict[str, float]
) -> str:
    """步行路线缓存键。

    格式：`tripagent:walk:{olng},{olat}->{dlng},{dlat}`

    不做起终点排序归一：步行路线的几何形状是方向相关的（虽然距离对称），
    归一化会省下一半键空间，但代价是可能返回与请求方向不符的折线。
    """
    return (
        f"tripagent:walk:{_coord(origin['lng'])},{_coord(origin['lat'])}"
        f"->{_coord(destination['lng'])},{_coord(destination['lat'])}"
    )


# ─── TTL 常量 ──────────────────────────────────────────────────

WEATHER_TTL = 4 * 3600     # 天气缓存 4 小时
POI_TTL = 12 * 3600        # POI 关键字搜索缓存 12 小时
MANUAL_SEARCH_TTL = 12 * 3600  # 手动编辑的搜索代理缓存 12 小时（与 POI_TTL 同为半天）

# 周边搜索：餐厅是会被选进行程的对象，缓存太久会让用户看到已关门的店。
NEARBY_TTL = 2 * 3600

# 步行路线：同一对坐标的步行折线实质上不随时间变化，可以放长。
WALKING_TTL = 7 * 24 * 3600
