"""纯工具函数（无 LLM、无外部 API 调用）。"""

from __future__ import annotations

import logging
import math
import os
import re
import asyncio
import time
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

from app.core.amap_notify import emit_into_current_run
from app.core.amap_quota import AmapQuotaExceeded
from app.core.env import load_local_env
from app.core.async_resources import provider_slot
from app.providers.amap.poi import (
    parse_location,
    normalize_address,
    search_attraction_pois_async,
    poi_to_spot,
)


# ─── 环境变量 ─────────────────────────────────────────────────

def amap_key() -> str:
    load_local_env()
    key = os.getenv("AMAP_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 AMAP_API_KEY，请在 .env.local 中配置")
    return key


@contextmanager
def attach_run_warning_sink():
    """在当前上下文中装上高德配额预警的发送器。

    调用通道在预占额度首次越过阈值时需要一个出口把预警交给用户，但它既不知道
    当前是哪个 Run，也不该依赖运行时层。这里用 contextvar 桥接：装上发送器后，
    通道发出的预警会作为 `amap.quota_warning` 自定义事件挂到当前 Run 上。

    没有正在运行的 Run 时（直接调用节点、脚本）不装任何东西，通道退化为落日志。
    """
    try:
        from app.runtime.worker import current_emit
    except Exception:  # noqa: BLE001
        # 运行时层不可用不应影响规划本身：规划曾经完全不依赖它。
        yield
        return

    emit = current_emit()
    if emit is None:
        yield
        return

    def sink(kind: str, payload: dict[str, Any]) -> None:
        emit(kind, payload)

    with emit_into_current_run(sink):
        yield


# ─── 日期解析 ─────────────────────────────────────────────────

def parse_iso_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


# ─── 地理距离 ─────────────────────────────────────────────────

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: dict[str, float], b: dict[str, float]) -> float:
    """两点球面 haversine 距离（km）。"""
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lng"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lng"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _has_coords(loc: Any) -> bool:
    """location 是否为含数值经纬度的 dict。"""
    return (
        isinstance(loc, dict)
        and isinstance(loc.get("lat"), (int, float))
        and isinstance(loc.get("lng"), (int, float))
    )


def cluster_pois_by_location(
    pois: list[dict[str, Any]], k: int
) -> dict[str, int]:
    """按经纬度把候选景点聚成 k 个地理分区，返回 {景点名: 分区编号(0-based)}。

    用于给 Planner 提供「真实坐标距离」的地理分区软提示，替代坐标盲的行政区名
    （adname）——同一行政区的景点可能相距很远（如玄武湖与中山陵同属玄武区却
    相距约 10km）。

    - 无坐标的景点归入分区 -1（不参与聚类）。
    - k<=1 或有效景点过少时，全部归入分区 0。
    - 确定性：固定种子初始化（按经度排序等距取点），同一输入每次结果一致。
    """
    result: dict[str, int] = {}
    valid: list[tuple[str, dict[str, float]]] = []
    for s in pois:
        loc = s.get("location")
        if _has_coords(loc):
            valid.append((s["name"], loc))
        else:
            result[s["name"]] = -1

    n = len(valid)
    if n == 0:
        return result
    k = max(1, min(k, n))
    if k == 1:
        for name, _ in valid:
            result[name] = 0
        return result

    # 确定性初始化：按经度（再纬度）排序后等距取 k 个种子
    ordered = sorted(valid, key=lambda x: (x[1]["lng"], x[1]["lat"]))
    centroids = [
        {"lat": ordered[round(i * (n - 1) / (k - 1))][1]["lat"],
         "lng": ordered[round(i * (n - 1) / (k - 1))][1]["lng"]}
        for i in range(k)
    ]

    assign: dict[str, int] = {}
    for _ in range(20):
        new_assign = {
            name: min(range(k), key=lambda c: haversine_km(centroids[c], loc))
            for name, loc in valid
        }
        if new_assign == assign:
            break
        assign = new_assign
        for c in range(k):
            members = [loc for name, loc in valid if assign[name] == c]
            if members:
                centroids[c] = {
                    "lat": sum(m["lat"] for m in members) / len(members),
                    "lng": sum(m["lng"] for m in members) / len(members),
                }

    result.update(assign)
    return result


# ─── 偏好清洗 ─────────────────────────────────────────────────

# LLM 在用户未提供偏好时偶尔吐出的占位垃圾值（应等同于"无偏好"）
_JUNK_PREF = {"null", "none", "undefined", "n/a", "na", "无", "暂无", "没有", "不限", "无偏好"}


def clean_pref(v: str | None) -> str | None:
    """把偏好字段归一化：去空白；整串为占位垃圾值（如 'null'/'无'）时视为无偏好返回 None。

    只在『整串』等于垃圾 token 时清空，避免误伤 '无辣不欢' 这类正常偏好。
    """
    s = (v or "").strip()
    return None if (not s or s.lower() in _JUNK_PREF) else s


# ─── 候选景点池 ───────────────────────────────────────────────

async def fetch_city_spots_async(
    city: str, api_key: str, *, max_spots: int = 30
) -> list[dict[str, Any]]:
    """多关键词并发搜索 + 去重，返回最多 max_spots 个有坐标的候选景点。

    固定 3 个关键词、并发发出：一次规划消耗 3 次基础搜索服务额度
    （缓存命中则 0 次）。改动关键词数量会直接改变每次规划的额度开销。
    """
    keywords_list = [f"{city}必去景点", f"{city}热门景区", f"{city}博物馆"]
    pages = await asyncio.gather(
        *(
            search_attraction_pois_async(city, api_key, keywords=keyword)
            for keyword in keywords_list
        )
    )
    seen: set[str] = set()
    spots: list[dict[str, Any]] = []
    for raw_items in pages:
        for raw in raw_items:
            if len(spots) >= max_spots:
                return spots
            name = raw.get("name", "")
            if name in seen:
                continue
            spot = poi_to_spot(raw)
            if spot:
                seen.add(name)
                spots.append(spot)
    return spots


def filter_by_rating(
    spots: list[dict[str, Any]], min_rating: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只保留 rating ≥ min_rating 的景点；无评分视为不达标。"""
    kept = [s for s in spots if s.get("rating") is not None and s["rating"] >= min_rating]
    dropped = [s for s in spots if s not in kept]
    return kept, dropped


# ─── 格式化 ──────────────────────────────────────────────────

_CLUSTER_LABELS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"


def _spot_line(s: dict[str, Any]) -> str:
    loc = s.get("location") or {}
    rating = f"{s['rating']:.1f}" if s.get("rating") else "无"
    open_t = s.get("open_time") or "未知"
    area = s.get("adname") or "未知"
    if _has_coords(loc):
        coord = f"坐标 {loc['lng']:.4f},{loc['lat']:.4f}"
    else:
        coord = "坐标未知"
    return f"- {s['name']}（区域 {area}，评分 {rating}，开放 {open_t}，{coord}）"


def format_spots_for_llm(
    pois: list[dict[str, Any]],
    cluster_map: dict[str, int] | None = None,
) -> str:
    """候选景点池的紧凑清单（喂给 LLM）。

    传入 cluster_map（{景点名: 分区编号}，见 cluster_pois_by_location）时，按
    『地理分区』分组展示——同一分区的景点连续列出，引导 LLM 把同区景点排在同
    一天。不传则保持平铺（向后兼容）。
    """
    if not cluster_map:
        return "\n".join(_spot_line(s) for s in pois)

    groups: dict[int, list[dict[str, Any]]] = {}
    for s in pois:
        cid = cluster_map.get(s["name"], -1)
        groups.setdefault(cid, []).append(s)

    blocks = []
    # 有效分区按编号升序在前，无坐标(-1)放最后
    for cid in sorted(groups, key=lambda c: (c == -1, c)):
        if cid == -1:
            header = "📍其他（无坐标，地理分区未知）"
        else:
            label = _CLUSTER_LABELS[cid] if cid < len(_CLUSTER_LABELS) else f"#{cid + 1}"
            header = f"📍地理分区{label}"
        lines = "\n".join(_spot_line(s) for s in groups[cid])
        blocks.append(f"{header}\n{lines}")
    return "\n\n".join(blocks)


def spot_location_map(pois: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    return {s["name"]: s["location"] for s in pois}



_TIME_RANGE_RE = re.compile(r"(\d{1,2})[:：](\d{2})\s*[-~—至]\s*(\d{1,2})[:：](\d{2})")


def _to_minutes(hhmm: str) -> int | None:
    m = re.match(r"\s*(\d{1,2})[:：](\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


# ─── 抵达 / 返程时刻窗口 ──────────────────────────────────────
# 用户对抵达或返程时刻的表述可能不精确（如「傍晚抵达」）。系统保留原话用于展示，
# 另用下面的对照表把它换算成排程与核验可用的时刻——换算结果只表达
# 「不得早于/晚于何时安排」，不代表系统知道了用户的真实抵达或离开时刻。

_PERIOD_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("morning", ("上午", "早上", "早晨", "一早")),
    ("noon", ("中午", "正午", "晌午")),
    ("afternoon", ("下午", "午后")),
    ("evening", ("傍晚", "黄昏", "日落")),
    ("night", ("晚上", "晚间", "夜里", "夜晚")),
)

# 抵达：该时段的右端——用户说「傍晚到」通常指那个时段内落地，
# 取右端意味着系统假定他要到时段末尾才能开始活动，宁可排少不排错。
ARRIVAL_FLOOR_BY_PERIOD = {
    "morning": "12:00",    # 上午 09:00–12:00
    "noon": "14:00",       # 中午 12:00–14:00
    "afternoon": "17:00",  # 下午 14:00–17:00
    "evening": "19:00",    # 傍晚 17:00–19:00
    "night": "21:00",      # 晚上 19:00 之后
}

# 返程：该时段「最早可能离开」的时刻。方向与抵达相反——
# 返程还要再减去赶车缓冲，因此按更早的一端估算才安全。
DEPARTURE_EDGE_BY_PERIOD = {
    "morning": "09:00",
    "noon": "12:00",
    "afternoon": "14:00",
    "evening": "17:00",
    "night": "19:00",
}

# 抵达不早于此刻时，首日视为过晚抵达，不再安排任何景点
LATE_ARRIVAL_CUTOFF = "21:00"

# 末日景点必须在返程时刻之前留出的赶车缓冲（分钟）
DEPARTURE_BUFFER_MINUTES = 120

# 12 小时制补正：这些词出现时，小于 12 的钟点数按下午处理
_PM_WORDS = ("中午", "下午", "午后", "傍晚", "黄昏", "日落", "晚上", "晚间", "夜里", "夜晚")


def _parse_clock(text: str) -> str | None:
    """从文本里解析出具体时刻（HH:MM）：18:30 / 18：30 / 下午3点 / 傍晚6点。"""
    m = re.search(r"(\d{1,2})[:：](\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    m = re.search(r"(\d{1,2})\s*[点时]", text)
    if m:
        hour = int(m.group(1))
        if hour < 12 and any(word in text for word in _PM_WORDS):
            hour += 12
        if 0 <= hour <= 23:
            return f"{hour:02d}:00"
    return None


def _match_period(text: str) -> str | None:
    for key, words in _PERIOD_WORDS:
        if any(word in text for word in words):
            return key
    return None


def _resolve_time(value: str | None, table: dict[str, str]) -> str | None:
    """先取具体时刻，取不到再按时段词查表；都无法识别时返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    clock = _parse_clock(text)
    if clock:
        return clock
    period = _match_period(text)
    return table.get(period) if period else None


def arrival_floor(value: str | None) -> str | None:
    """抵达可用下界（HH:MM）：首日不得早于此刻开始安排活动。"""
    return _resolve_time(value, ARRIVAL_FLOOR_BY_PERIOD)


def departure_edge(value: str | None) -> str | None:
    """返程时刻的保守取值（HH:MM）：该时段最早可能离开的时刻。"""
    return _resolve_time(value, DEPARTURE_EDGE_BY_PERIOD)


def is_late_arrival(value: str | None) -> bool:
    """抵达下界是否晚于深夜阈值。"""
    minutes = _to_minutes(arrival_floor(value) or "")
    cutoff = _to_minutes(LATE_ARRIVAL_CUTOFF) or 0
    return minutes is not None and minutes > cutoff


def departure_deadline(value: str | None) -> str | None:
    """末日景点必须在此刻之前结束（返程时刻减去赶车缓冲）。"""
    minutes = _to_minutes(departure_edge(value) or "")
    if minutes is None:
        return None
    latest = max(0, minutes - DEPARTURE_BUFFER_MINUTES)
    return f"{latest // 60:02d}:{latest % 60:02d}"


def single_day_window(arrival: str | None, departure: str | None) -> tuple[str, str] | None:
    """单日行程的可用窗口；窗口不存在或过窄时返回 None。"""
    start = _to_minutes(arrival_floor(arrival) or "")
    end = _to_minutes(departure_deadline(departure) or "")
    if start is None or end is None or end <= start:
        return None
    return f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}"


# 首日规则
FIRST_DAY_EVENING_ONLY = "evening_only"   # 只排晚餐与一个夜间可玩点
FIRST_DAY_NO_SPOTS = "no_spots"           # 抵达过晚，首日不排景点
# 末日规则
LAST_DAY_BEFORE_DEADLINE = "before_deadline"


def time_window_plan(
    arrival: str | None, departure: str | None, days: int | None
) -> dict[str, Any] | None:
    """把抵达与返程表述换算成确定性的排程规则，供提示词与核验消费。

    规则由这里决定、提示词只负责措辞：首末两天的处理属于会静默出错的分支逻辑，
    放在可单测的纯函数里比散在 f-string 中可靠。

    返回 None 表示用户两处都没提供，调用方应保持与引入该能力之前完全一致的行为。
    """
    arrival = (arrival or "").strip()
    departure = (departure or "").strip()
    if not arrival and not departure:
        return None

    total_days = days if isinstance(days, int) and days > 0 else 0
    plan: dict[str, Any] = {
        "arrival": arrival or None,
        "arrival_floor": arrival_floor(arrival) if arrival else None,
        "departure": departure or None,
        "departure_edge": departure_edge(departure) if departure else None,
        "departure_deadline": departure_deadline(departure) if departure else None,
        "days": total_days,
        "first_day": None,
        "last_day": None,
        "single_day_window": None,
        "single_day_too_narrow": False,
        "single_day_unknown": False,
    }

    if total_days == 1:
        window = single_day_window(arrival, departure)
        if window:
            plan["single_day_window"] = window
        elif plan["arrival_floor"] or plan["departure_deadline"]:
            # 窗口真实存在但装不下任何活动（例如傍晚到、当晚就走）
            plan["single_day_too_narrow"] = True
        else:
            # 表述无法识别，无法证明窗口够用，按最保守处理
            plan["single_day_unknown"] = True
    elif total_days >= 2:
        if arrival:
            plan["first_day"] = (
                FIRST_DAY_NO_SPOTS if is_late_arrival(arrival) else FIRST_DAY_EVENING_ONLY
            )
        if departure:
            plan["last_day"] = LAST_DAY_BEFORE_DEADLINE
    return plan


def open_time_violations(route: list[dict[str, Any]], pois: list[dict[str, Any]]) -> list[str]:
    """检查每个景点游玩时段是否落在开放时间内。"""
    open_map = {s["name"]: (s.get("open_time") or "") for s in pois}
    bad: list[str] = []
    for day in route:
        for spot in day.get("spots", []):
            rng = _TIME_RANGE_RE.search(open_map.get(spot["name"], ""))
            if not rng:
                continue
            o_start = int(rng.group(1)) * 60 + int(rng.group(2))
            o_end = int(rng.group(3)) * 60 + int(rng.group(4))
            s_start = _to_minutes(spot.get("start_time", ""))
            s_end = _to_minutes(spot.get("end_time", ""))
            if s_start is None or s_end is None:
                continue
            if s_start < o_start or s_end > o_end:
                bad.append(
                    f"Day{day.get('day')} {spot['name']} 游玩 {spot.get('start_time')}-{spot.get('end_time')}"
                    f" 超出开放 {rng.group(0)}"
                )
    return bad


def unknown_spots(route: list[dict[str, Any]], pois: list[dict[str, Any]]) -> list[str]:
    """找出不在候选池的景点名。"""
    valid = {s["name"] for s in pois}
    bad = []
    for day in route:
        for spot in day.get("spots", []):
            if spot["name"] not in valid:
                bad.append(f"Day{day.get('day')} {spot['name']}")
    return bad


# ─── 时间线辅助 ───────────────────────────────────────────────

def last_spot_of_period(day: dict[str, Any], period: str) -> dict[str, Any] | None:
    """返回当天指定时段的最后一个景点（按 spots 列表顺序）。"""
    spots = [s for s in day.get("spots", []) if s.get("period") == period]
    if spots:
        return spots[-1]
    all_spots = day.get("spots", [])
    if not all_spots:
        return None
    return all_spots[0] if period == "morning" else all_spots[-1]


def dinner_anchor_spot(day: dict[str, Any]) -> dict[str, Any] | None:
    """晚餐搜索中心：优先取第一个 evening 景点，无则兜底取最后一个 afternoon 景点。"""
    evening_spots = [s for s in day.get("spots", []) if s.get("period") == "evening"]
    if evening_spots:
        return evening_spots[0]
    return last_spot_of_period(day, "afternoon")


# ─── 结构化 LLM 调用（含 None 重试守卫）────────────────────────

def _message_char_count(message: Any) -> int:
    """Estimate a LangChain message or legacy ``(role, content)`` pair."""
    if isinstance(message, tuple) and len(message) == 2:
        role, content = message
        return len(str(role)) + len(str(content))
    role = getattr(message, "type", None) or getattr(message, "role", None) or ""
    content = getattr(message, "content", "")
    return len(str(role)) + len(str(content))


def invoke_structured(llm: Any, messages: list[Any], *, retries: int = 3) -> Any:
    """调用结构化输出 LLM，对偶发返回 None 做重试。

    DeepSeek function_calling 模式偶尔返回 None；重试若干次，
    仍失败则抛出明确错误而非 AttributeError。

    诊断日志：
    - 每次调用打印耗时（帮助区分"网络慢"和"重试导致慢"）
    - 出现 None 时打印警告，标明是第几次重试
    """
    # 估算输入 token（粗略：字符数 / 2 ≈ token 数，仅供参考）
    total_chars = sum(_message_char_count(message) for message in messages)
    schema_name = getattr(getattr(llm, "schema", None), "__name__", None)
    # 从 llm 对象尝试取 schema 名（with_structured_output 绑定的 Pydantic 类）
    if schema_name is None:
        # langchain with_structured_output 把 schema 存在内部不同位置，尝试常见路径
        for attr in ("_schema", "schema_", "output_schema"):
            s = getattr(llm, attr, None)
            if s and hasattr(s, "__name__"):
                schema_name = s.__name__
                break
    label = schema_name or "unknown"

    for attempt in range(retries):
        t0 = time.perf_counter()
        result = llm.invoke(messages)
        elapsed = time.perf_counter() - t0

        if result is not None:
            if attempt > 0:
                logger.warning(
                    "[invoke_structured] %s 第 %d 次重试后成功，本次耗时 %.2fs，"
                    "输入约 %d chars",
                    label, attempt + 1, elapsed, total_chars,
                )
            else:
                logger.debug(
                    "[invoke_structured] %s 首次成功，耗时 %.2fs，输入约 %d chars",
                    label, elapsed, total_chars,
                )
            return result

        logger.warning(
            "[invoke_structured] %s 第 %d 次调用返回 None（耗时 %.2fs），准备重试…",
            label, attempt + 1, elapsed,
        )

    raise RuntimeError(f"结构化输出连续 {retries} 次返回 None，模型未产出有效结果")


async def ainvoke_structured(
    llm: Any,
    messages: list[Any],
    *,
    retries: int = 3,
) -> Any:
    """异步结构化 LLM 调用，保持与同步版本相同的 None 重试语义。"""
    total_chars = sum(_message_char_count(message) for message in messages)
    for attempt in range(retries):
        started = time.perf_counter()
        async with provider_slot("llm"):
            result = await llm.ainvoke(messages)
        elapsed = time.perf_counter() - started
        if result is not None:
            logger.debug(
                "[ainvoke_structured] attempt=%d elapsed=%.2fs chars=%d",
                attempt + 1,
                elapsed,
                total_chars,
            )
            return result
        logger.warning(
            "[ainvoke_structured] attempt=%d returned None elapsed=%.2fs",
            attempt + 1,
            elapsed,
        )
        if attempt + 1 < retries:
            await asyncio.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"结构化输出连续 {retries} 次返回 None，模型未产出有效结果")


# ─── 餐厅 POI 解析 ────────────────────────────────────────────

# ─── 天气工具 ─────────────────────────────────────────────────

async def fetch_weather_for_dates_async(
    destination: str,
    start_date: date,
    end_date: date,
    api_key: str,
) -> tuple[list[dict[str, Any]], str | None]:
    from app.providers.weather.amap import fetch_forecast_async

    try:
        all_forecasts = await fetch_forecast_async(destination, api_key)
    except AmapQuotaExceeded:
        # 额度用完不能降级成「按晴天规划」：那会把一次代价明确的失败
        # 变成一份没有天气依据的行程，用户还不知道为什么。让它冒泡，由
        # Run 带着可读原因失败。
        raise
    except Exception:
        all_forecasts = []
    if not all_forecasts:
        return [], "天气信息获取失败，按晴天规划路线"
    forecast_map = {item["date"]: item for item in all_forecasts}
    travel_dates = []
    current = start_date
    while current <= end_date:
        travel_dates.append(current.isoformat())
        current += timedelta(days=1)
    matched = [forecast_map[item] for item in travel_dates if item in forecast_map]
    missing = [item for item in travel_dates if item not in forecast_map]
    if not matched:
        return [], "旅游日期超出天气预报范围（高德预报约 4 天内），建议出行前关注天气预报"
    note = f"部分日期（{', '.join(missing)}）超出天气预报范围" if missing else None
    return matched, note


def format_weather_for_llm(forecast: list[dict[str, Any]]) -> str:
    """格式化天气信息供 LLM 读取，如：
    2024-06-05: 白天晴/夜间晴，气温22-32°C
    2024-06-06: 白天中雨/夜间小雨，气温18-24°C ⚠️ 雨雪天气
    """
    if not forecast:
        return ""
    lines = []
    for w in forecast:
        warning = " ⚠️ 雨雪天气，请优先安排室内景点" if w.get("is_bad") else ""
        lines.append(
            f"{w['date']}: 白天{w['day_weather']}/夜间{w['night_weather']}，"
            f"气温{w['night_temp']}-{w['day_temp']}°C{warning}"
        )
    return "\n".join(lines)


# ─── 餐厅 POI 解析 ────────────────────────────────────────────

def restaurant_to_dict(poi: dict[str, Any]) -> dict[str, Any] | None:
    """把高德周边搜索的餐饮 POI 整理成结构化餐厅信息。缺坐标返回 None。"""
    location = parse_location(poi.get("location"))
    if not location:
        return None

    biz_ext = poi.get("biz_ext") or {}
    if not isinstance(biz_ext, dict):
        biz_ext = {}

    cost_raw = str(biz_ext.get("cost", "")).strip()
    rating_raw = str(biz_ext.get("rating", "")).strip()
    try:
        rating: float | None = float(rating_raw) if rating_raw else None
    except ValueError:
        rating = None

    photos = poi.get("photos") or []
    photo: str | None = None
    if isinstance(photos, list) and photos and isinstance(photos[0], dict):
        photo = str(photos[0].get("url", "")).strip() or None

    open_time_r = (
        str(biz_ext.get("opentime2", "")).strip()
        or str(biz_ext.get("opentime", "")).strip()
        or None
    )
    tel_r = str(poi.get("tel") or "").strip() or None
    type_str = str(poi.get("type") or "")
    category = (type_str.split(";")[-1].strip() if ";" in type_str else type_str.strip()) or None

    return {
        "name": str(poi.get("name", "")),
        "cost": cost_raw or None,
        "rating": rating,
        "keytag": str(poi.get("type", "")),
        "location": location,
        "address": normalize_address(poi.get("address")),
        "photo": photo,
        "open_time": open_time_r,
        "tel": tel_r,
        "category": category,
    }
