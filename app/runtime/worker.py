"""LangGraph v2 stream consumer that exposes only the public protocol."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.runtime.manager import RunManager
from app.runtime.models import CUSTOM_EVENT_TYPES, RunStatus
from langgraph.types import Command

InputBuilder = Callable[[dict[str, Any]], dict[str, Any] | Any]
Finalizer = Callable[
    [dict[str, Any], dict[str, Any], str],
    Awaitable[dict[str, Any] | None],
]

# 事件发射器：(事件类型, 事件负载) -> None
EventEmitter = Callable[[str, dict], None]

logger = logging.getLogger(__name__)

_custom_adapter = TypeAdapter(CUSTOM_EVENT_TYPES)

# 当前正在执行的 Run 的事件发射器。
#
# 存在的理由：高德调用通道需要在额度越过阈值时把预警挂到当前 Run 上，但它不能
# 依赖运行时层（通道属于 providers 层，被 core 层的规划代码调用）。用 contextvar
# 桥接，规划节点通过 `app.planning.helpers.attach_run_warning_sink` 取用。
_current_emit: ContextVar[EventEmitter | None] = ContextVar(
    "run_event_emitter", default=None
)


def current_emit() -> EventEmitter | None:
    """取当前 Run 的事件发射器；不在 Run 内执行时返回 None。"""
    return _current_emit.get()


class GraphRuntimeWorker:
    """Execute a compiled graph with `astream(..., version="v2")`.

    Raw updates are consumed only for final state and interrupt detection. They are
    never published to clients.
    """

    def __init__(
        self,
        manager: RunManager,
        graph: Any,
        input_builder: InputBuilder,
        *,
        stream_messages: bool,
        visible_nodes: set[str] | None = None,
        visible_tags: set[str] | None = None,
        finalizer: Finalizer | None = None,
    ):
        self.manager = manager
        self.graph = graph
        self.input_builder = input_builder
        self.stream_messages = stream_messages
        self.visible_nodes = visible_nodes or {"respond"}
        self.visible_tags = visible_tags or {"user-visible"}
        self.finalizer = finalizer

    async def __call__(
        self, run: dict[str, Any], cancel_event: asyncio.Event
    ) -> dict[str, Any] | None:
        graph_input = self.input_builder(run)
        if inspect.isawaitable(graph_input):
            graph_input = await graph_input
        return await self._execute(run, cancel_event, graph_input)

    async def resume(
        self,
        run: dict[str, Any],
        cancel_event: asyncio.Event,
        value: Any,
    ) -> dict[str, Any] | None:
        return await self._execute(run, cancel_event, Command(resume=value))

    async def _execute(
        self,
        run: dict[str, Any],
        cancel_event: asyncio.Event,
        graph_input: Any,
    ) -> dict[str, Any] | None:
        token = _current_emit.set(self._make_emitter(run))
        try:
            return await self._run_stream(run, cancel_event, graph_input)
        finally:
            _current_emit.reset(token)

    def _make_emitter(self, run: dict[str, Any]) -> EventEmitter:
        """造一个同步的事件发射器，供规划节点在通道回调里使用。

        通道的预警回调是同步的（它运行在 await 之间），所以这里把发布排成任务。
        事件不参与失败判定：发布失败只落日志，不影响正在跑的规划。
        """
        loop = asyncio.get_running_loop()

        def emit(kind: str, payload: dict[str, Any]) -> None:
            async def _publish() -> None:
                try:
                    await self.manager.publish(run["id"], "custom", payload, durable=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("自定义事件发布失败 [%s]：%s", kind, exc)

            loop.create_task(_publish())

        return emit

    async def _run_stream(
        self,
        run: dict[str, Any],
        cancel_event: asyncio.Event,
        graph_input: Any,
    ) -> dict[str, Any] | None:
        stream_modes = ["custom", "updates", "values"]
        if self.stream_messages:
            stream_modes.insert(0, "messages")
        config = {
            "configurable": {"thread_id": run["id"]},
            "recursion_limit": int(run["request_snapshot"].get("recursion_limit", 30)),
        }
        final_updates: dict[str, Any] = {}
        latest_values: dict[str, Any] = {}
        message_parts: list[str] = []
        async for part in self.graph.astream(
            graph_input,
            config=config,
            stream_mode=stream_modes,
            version="v2",
        ):
            if cancel_event.is_set():
                raise asyncio.CancelledError
            part_type = part.get("type")
            if part_type == "messages" and self.stream_messages:
                message, metadata = part.get("data", (None, {}))
                if not self._message_is_public(metadata):
                    continue
                text = self._message_text(message)
                if text:
                    message_parts.append(text)
                    await self.manager.publish(
                        run["id"],
                        "messages",
                        {
                            "message_id": f"assistant:{run['id']}",
                            "delta": text,
                        },
                        durable=False,
                    )
            elif part_type == "custom":
                payload = self._validated_custom(part.get("data"))
                if payload is not None:
                    await self.manager.publish(
                        run["id"], "custom", payload, durable=True
                    )
            elif part_type == "updates":
                data = part.get("data") or {}
                interrupts = data.get("__interrupt__") or ()
                if interrupts:
                    await self._handle_interrupt(run["id"], interrupts[0])
                    return None
                for node_name, update in data.items():
                    if not node_name.startswith("__") and isinstance(update, dict):
                        final_updates.update(update)
            elif part_type == "values":
                data = part.get("data") or {}
                interrupts = (
                    part.get("interrupts")
                    or (data.get("__interrupt__") if isinstance(data, dict) else ())
                    or ()
                )
                if interrupts:
                    await self._handle_interrupt(run["id"], interrupts[0])
                    return None
                if isinstance(data, dict):
                    latest_values = {
                        key: value
                        for key, value in data.items()
                        if not key.startswith("__")
                    }

        assistant_text = "".join(message_parts)
        if self.finalizer:
            return await self.finalizer(
                run,
                {**final_updates, **latest_values},
                assistant_text,
            )
        return None

    async def _handle_interrupt(self, run_id: str, interrupt_value: Any) -> None:
        value = getattr(interrupt_value, "value", None) or {}
        if not isinstance(value, dict):
            value = {"question": str(value)}
        interaction_id = str(
            value.get("interaction_id")
            or getattr(interrupt_value, "id", "")
        )
        safe_payload = {
            "kind": "run.waiting_user",
            "interaction_id": interaction_id,
            "question": str(value.get("question") or "请补充所需信息"),
            "input_schema": value.get("input_schema") or {},
        }
        await self.manager.publish(
            run_id, "custom", safe_payload, durable=True
        )
        await self.manager.transition(
            run_id,
            RunStatus.WAITING_USER,
            outstanding_interaction_id=interaction_id,
        )

    def _message_is_public(self, metadata: dict[str, Any]) -> bool:
        node = metadata.get("langgraph_node")
        tags = set(metadata.get("tags") or ())
        return node in self.visible_nodes or bool(tags & self.visible_tags)

    @staticmethod
    def _message_text(message: Any) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return ""

    @staticmethod
    def _validated_custom(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        try:
            return _custom_adapter.validate_python(payload).model_dump()
        except ValidationError:
            return None
