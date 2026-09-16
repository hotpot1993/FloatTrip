"""把高德配额预警送到正在执行的 Run 上。

调用通道（`app.providers.amap.channel`）在预占额度首次越过阈值时需要告诉用户，
但它既不知道当前是哪个 Run，也不该依赖运行时层。这里用一个 contextvar 解耦：

* 规划/对话的节点在执行期间用 `emit_into_current_run()` 装上一个发送器；
* 通道调用 `notify_quota_warning()`，有发送器就发事件，没有就只落日志。

contextvar 天然跟随 asyncio 任务与 `asyncio.to_thread` 的上下文传播，所以并发的
多个 Run 不会互相串台。手动编辑路由这类没有 Run 的调用方走到这里就只落日志——
它们本来也没有可以承载提示的地方。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# 发送器签名：(事件类型, 事件负载) -> None
WarningSink = Callable[[str, dict], None]

_sink: ContextVar[WarningSink | None] = ContextVar("amap_warning_sink", default=None)

QUOTA_WARNING_KIND = "amap.quota_warning"

# 该事件类型必须同时加进 app/runtime/models.py 的 PlanningBriefEvent 白名单，
# 否则 `GraphRuntimeWorker._validated_custom` 会把它丢掉——那是图来源自定义
# 事件的唯一入口，extra="forbid"。


def notify_quota_warning(payload: dict) -> None:
    """报告一次配额预警。没有发送器时退化为日志，绝不让调用方失败。"""
    sink = _sink.get()
    if sink is None:
        logger.warning("高德配额预警：%s", payload)
        return
    try:
        sink(QUOTA_WARNING_KIND, payload)
    except Exception as exc:  # noqa: BLE001
        # 预警是尽力而为的旁路信息，不能因为它出问题就弄挂正在跑的规划。
        logger.warning("高德配额预警发送失败：%s", exc)


@contextlib.contextmanager
def emit_into_current_run(sink: WarningSink) -> Iterator[None]:
    """在执行期间把预警发送器绑定到当前上下文。"""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)
