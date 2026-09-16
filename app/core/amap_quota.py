"""高德配额池的领域模型。

高德开放平台自 2025-05-20 起取消日配额，改为按「服务组」共享的月配额
（公告：https://lbs.amap.com/news/service_amap）。同一服务组内所有接口、
同一账号下所有 Key（含 JS API Key）合计消耗同一份额度。

来源：https://lbs.amap.com/upgrade#price —— 已认证个人开发者：

    基础搜索服务            5,000 / 月   （关键字查询、周边查询……）
    其它基础服务-天气预报    5,000 / 月   （天气查询）
    基础LBS服务           150,000 / 月   （驾车/骑行/步行路径规划……）

本模块只负责「池是什么、限额多少、现在是几月」。额度的持久化与原子预占
由 `app.core.amap_quota_store` 负责，请求发出与失败分类由
`app.providers.amap.channel` 负责。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from enum import StrEnum

# 高德的月配额按北京时间结算。这里刻意写死，不读容器 TZ：
# 容器迁移或改 TZ 时若跟随本地时区，会让同一个物理时刻归属到不同的月份，
# 结果是当月额度被重复发放（少算已用）或提前重置。
#
# 注意：官方从未公布月配额的重置时刻、是否自然月、以及时区。
# 这整段都是「按自然月推断」，不是已确认事实。
QUOTA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


class QuotaBucket(StrEnum):
    """一个配额池 = 高德的一个服务组。"""

    SEARCH = "search"
    WEATHER = "weather"
    LBS = "lbs"


# 池到官方服务组名的映射，用于生成用户可见的文案。
# 名字必须与高德定价页保持一致，否则用户去控制台对不上账。
BUCKET_SERVICE_GROUP: dict[QuotaBucket, str] = {
    QuotaBucket.SEARCH: "基础搜索服务",
    QuotaBucket.WEATHER: "天气预报",
    QuotaBucket.LBS: "基础LBS服务",
}

# 池到限额环境变量的映射。
BUCKET_LIMIT_ENV: dict[QuotaBucket, str] = {
    QuotaBucket.SEARCH: "AMAP_QUOTA_SEARCH_LIMIT",
    QuotaBucket.WEATHER: "AMAP_QUOTA_WEATHER_LIMIT",
    QuotaBucket.LBS: "AMAP_QUOTA_LBS_LIMIT",
}

# 官方公布的已认证个人开发者月配额，仅用于文档与注释。
# 代码里的默认限额要留安全边际，见 DEFAULT_LIMITS。
OFFICIAL_MONTHLY_QUOTA: dict[QuotaBucket, int] = {
    QuotaBucket.SEARCH: 5000,
    QuotaBucket.WEATHER: 5000,
    QuotaBucket.LBS: 150000,
}

# 默认限额取官方数值的 95%。
#
# 留边际的理由：官方未说明「被高德拒绝的请求是否计入配额消耗」。若计入，
# 本地计数会低估（我们没数被拒的那次）；若不计入，本地计数准确。边际覆盖前者。
#
# LBS 池默认 0（不限制）：该池 150,000/月 的主流消耗者是前端的 AMap.Driving，
# 服务端根本数不到；后端只有步行路线一个出口且额度充裕。设一个拦不住大头的
# 假上限，只会给用户虚假的安全感。
DEFAULT_LIMITS: dict[QuotaBucket, int] = {
    QuotaBucket.SEARCH: 4750,
    QuotaBucket.WEATHER: 4750,
    QuotaBucket.LBS: 0,
}

UNLIMITED = 0

DEFAULT_WARNING_RATIO = 0.8

WARNING_RATIO_ENV = "AMAP_QUOTA_WARNING_RATIO"


class QuotaConfigError(RuntimeError):
    """限额配置非法。启动阶段就该暴露，不允许静默降级。"""


class AmapQuotaExceeded(RuntimeError):
    """额度不足。

    同时携带 `public_code` / `public_message`，使
    `app/runtime/scheduler.py` 既有的
    `getattr(exc, "public_code", ...)` 读取路径直接可用。

    本地预占失败与高德返回配额错误使用**同一个**异常类型和错误码：
    上层不需要知道失败来自本地闸门还是远端响应。
    """

    def __init__(self, bucket: QuotaBucket, *, used: int, limit: int):
        self.bucket = bucket
        self.used = used
        self.limit = limit
        self.public_code = "amap_quota_exhausted"
        self.public_message = quota_exhausted_message(bucket)
        self.retryable = False
        super().__init__(f"{bucket.value} 配额已用尽（{used}/{limit}）")


def current_month(now: datetime | None = None) -> str:
    """返回当前配额月份，格式 YYYY-MM，按 QUOTA_TIMEZONE 计算。"""
    moment = now if now is not None else datetime.now(QUOTA_TIMEZONE)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=QUOTA_TIMEZONE)
    return moment.astimezone(QUOTA_TIMEZONE).strftime("%Y-%m")


def next_month_start(now: datetime | None = None) -> datetime:
    """返回下一个配额月份的开始时刻（用于文案里的重置时间）。"""
    moment = now if now is not None else datetime.now(QUOTA_TIMEZONE)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=QUOTA_TIMEZONE)
    local = moment.astimezone(QUOTA_TIMEZONE)
    year, month = local.year, local.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return datetime(year, month, 1, tzinfo=QUOTA_TIMEZONE)


def reset_hint(now: datetime | None = None) -> str:
    """重置时刻的用户可读文案。所有文案必须走这里，不许另处硬编码。"""
    return next_month_start(now).strftime("%Y-%m-%d 00:00")


def quota_exhausted_message(bucket: QuotaBucket, now: datetime | None = None) -> str:
    """额度耗尽时给用户看的话。"""
    group = BUCKET_SERVICE_GROUP[bucket]
    return (
        f"高德「{group}」的本月调用额度已用完，"
        f"预计 {reset_hint(now)} 重置，请届时再试。"
    )


def quota_warning_message(
    bucket: QuotaBucket, *, used: int, limit: int, now: datetime | None = None
) -> str:
    """配额预警文案。"""
    group = BUCKET_SERVICE_GROUP[bucket]
    return (
        f"高德「{group}」的本月调用额度已用 {used}/{limit}，"
        f"接近上限，预计 {reset_hint(now)} 重置。"
    )


# ─── 限额配置 ────────────────────────────────────────────────

def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise QuotaConfigError(
            f"环境变量 {name} 必须是整数，当前值为 {raw!r}"
        ) from exc


def _read_ratio(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise QuotaConfigError(
            f"环境变量 {name} 必须是 0–1 之间的小数，当前值为 {raw!r}"
        ) from exc


def load_limits() -> dict[QuotaBucket, int]:
    """读取三个池的限额。

    每次调用都重新读环境变量，与仓库既有的 `os.getenv` 风格一致，
    也让测试可以只用 monkeypatch 环境变量而无需重载模块。
    """
    return {
        bucket: _read_int(env_name, DEFAULT_LIMITS[bucket])
        for bucket, env_name in BUCKET_LIMIT_ENV.items()
    }


def load_warning_ratio() -> float:
    return _read_ratio(WARNING_RATIO_ENV, DEFAULT_WARNING_RATIO)


def validate_quota_config() -> None:
    """校验配额配置。非法值必须让启动失败，而不是被静默当作「不限制」。

    传 0 的意图是「不限制」，一个拼错的负数或非数字绝不能退化成同样的效果。
    """
    for bucket, limit in load_limits().items():
        if limit < 0:
            raise QuotaConfigError(
                f"{BUCKET_LIMIT_ENV[bucket]} 不能为负数（当前 {limit}）。"
                f"如需不限制请显式设为 0。"
            )
    ratio = load_warning_ratio()
    if not 0.0 < ratio <= 1.0:
        raise QuotaConfigError(
            f"{WARNING_RATIO_ENV} 必须落在 0（不含）到 1（含）之间，当前为 {ratio}"
        )


def is_unlimited(bucket: QuotaBucket, limit: int | None = None) -> bool:
    effective = load_limits()[bucket] if limit is None else limit
    return effective == UNLIMITED


def warning_threshold(bucket: QuotaBucket, limit: int | None = None) -> int | None:
    """预警阈值。不限制的池没有阈值，返回 None。"""
    effective = load_limits()[bucket] if limit is None else limit
    if effective == UNLIMITED:
        return None
    # 向上取整：限额很小时也要保证「达到阈值」是可实现的
    return max(1, int(effective * load_warning_ratio() + 0.999999))
