"""高德 REST 接口的公共常量与失败分类。"""

from __future__ import annotations

from typing import Any

AMAP_TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
AMAP_AROUND_SEARCH_URL = "https://restapi.amap.com/v3/place/around"
AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
AMAP_WALKING_URL = "https://restapi.amap.com/v3/direction/walking"

CONFIG_ERROR = "amap_config_error"


# ─── 失败语义分类 ────────────────────────────────────────────
#
# 高德 v3 响应形如 {"status", "info", "infocode"}。两个字段都要看：有的
# infocode 没有对应的 info 名，反过来也有。
#
# 来源：https://lbs.amap.com/api/webservice/guide/tools/info

# 配额与权限类：继续请求没有意义，而且额度已经见底，再打只会更糟。
#
# DAILY_QUERY_OVER_LIMIT / USER_DAILY_QUERY_OVER_LIMIT 对应的高德日配额已于
# 2025-05-20 取消（公告：https://lbs.amap.com/news/service_amap），但官方
# 错误码表**至今没有月配额条目**。也就是说，月配额超限可能复用这两个历史码，
# 也可能返回一个未文档化的新码。历史码必须留在这里：留着能覆盖「复用」的情形；
# 而「新码」的情形由兜底语义处理（按未知错误上报，不重试放大，也不静默当作成功）。
#
# 名字与数字两种表示都要认：高德有时只给数字 infocode、有时只给名字 info。
# 同一个错误的两个表示若映射到不同类别，分类结果就会随高德返回哪个字段而漂移。
QUOTA_INFOS = {
    # 名字形式
    "DAILY_QUERY_OVER_LIMIT",
    "USER_DAILY_QUERY_OVER_LIMIT",
    "USER_ABROAD_DAILY_QUERY_OVER_LIMIT",
    "ABROAD_DAILY_QUERY_OVER_LIMIT",
    # 数字形式（官方错误码表 10003 / 10044 / 10045 / 10029）
    "10003",
    "10044",
    "10045",
    "10029",
}

# 密钥/服务不可用类：同样不可重试，但**与「额度用完」是两回事**——额度下个月
# 会回来，一把失效的 Key 不会。两者共用文案会让用户白等到下个月。
ACCESS_DENIED_INFOS = {
    "INVALID_USER_KEY",
    "SERVICE_NOT_AVAILABLE",
    "USER_KEY_RECYCLED",
    # 官方错误码表：10001 无效 Key、10002 服务未开通、10009 与绑定平台不符
    "10001",
    "10002",
    "10009",
}

# 限流类：降速后可以恢复，值得退避重试。
#
# 高德的 QPS 超限行为是「超出部分被拒绝，阈值内的请求正常返回」，不是整把
# Key 被封，所以退避重试是有意义的。
THROTTLE_INFOS = {
    "CUQPS_HAS_EXCEEDED_THE_LIMIT",
    "CKQPS_HAS_EXCEEDED_THE_LIMIT",
    "CQPS_HAS_EXCEEDED_THE_LIMIT",
    "QPS_HAS_EXCEEDED_THE_LIMIT",
    "ACCESS_TOO_FREQUENT",
    "GATEWAY_TIMEOUT",
    # 官方错误码表：10004 一分钟内超限、10014 / 10019 / 10020 / 10021 QPS 超限、
    # 10015 单机 QPS 限流（官方排查策略是「建议降低请求的 QPS」）
    "10004",
    "10014",
    "10015",
    "10019",
    "10020",
    "10021",
}

# 需要人工处理、且**重试与等待都无用**的一类。
#
# IP_QUERY_OVER_LIMIT：官方排查策略原文是「封停后无法自动恢复，需要提交工单
# 联系我们」。若与「额度用完」共用文案，用户会白等到下个月。
SPECIAL_INFOS: dict[str, str] = {
    "IP_QUERY_OVER_LIMIT": "amap_ip_restricted",
    # 官方错误码表：10010 IP 访问超限
    "10010": "amap_ip_restricted",
}

# 现有调用方（poi.py 的历史重试判定）使用的集合，保持向后兼容。
# 新的调用方应当改用 `classify_failure`。
AMAP_RATE_LIMIT_INFOS = {
    "CUQPS_HAS_EXCEEDED_THE_LIMIT",
    "USER_DAILY_QUERY_OVER_LIMIT",
    "USER_KEY_RECYCLED",
}

QUOTA = "quota"
ACCESS_DENIED = "access_denied"
THROTTLE = "throttle"
SPECIAL = "special"
OTHER = "other"


def classify_failure(data: dict[str, Any]) -> tuple[str, str]:
    """判定一次高德失败响应的语义类别。

    返回 `(类别, 原始错误标识)`；类别取值见本模块的五个常量。
    调用方只在 `status != "1"` 时才应当调用它。
    """
    infocode = str(data.get("infocode") or "").strip()
    info = str(data.get("info") or "").strip()
    candidates = [value for value in (infocode, info) if value]

    for candidate in candidates:
        if candidate in QUOTA_INFOS:
            return QUOTA, candidate
    for candidate in candidates:
        if candidate in ACCESS_DENIED_INFOS:
            return ACCESS_DENIED, candidate
    for candidate in candidates:
        if candidate in SPECIAL_INFOS:
            return SPECIAL, candidate
    for candidate in candidates:
        if candidate in THROTTLE_INFOS:
            return THROTTLE, candidate
    return OTHER, info or infocode or "未知错误"


def int_or_none(value: Any) -> int | None:
    """把高德返回的数字字符串转成整数，无法转换时返回 None。"""
    return int(value) if str(value).isdigit() else None
