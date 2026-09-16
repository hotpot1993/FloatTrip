"""高德 REST 调用的唯一通道。

这是后端**唯一**允许发出高德请求的地方。它把四件事固定在同一处：

1. **失败分类** —— 配额类立即失败、限流类退避重试；
2. **额度预占** —— 每次真实请求发出**之前**占 1 次额度；
3. **并发约束** —— 共享 `provider_slot("amap")`；
4. **请求执行** —— 只发一次网络请求，重试由本通道驱动。

第 4 点是刻意的：`http_get_json_async` 的内部重试循环对调用方不可见，重试
发生时计数点看不到它，「重试也要占额」这条规格就无法成立。所以这里传
`max_attempts=1`，退避与占额都由本通道自己驱动。
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

from app.core import amap_quota_store
from app.core.amap_notify import notify_quota_warning
from app.core.amap_quota import (
    AmapQuotaExceeded,
    QuotaBucket,
    quota_warning_message,
)
from app.core.http import http_get_json_async, redact_url
from app.providers.amap.client import (
    ACCESS_DENIED,
    OTHER,
    QUOTA,
    SPECIAL,
    SPECIAL_INFOS,
    THROTTLE,
    classify_failure,
)

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.2

# 需要人工处理、等待无用的一类：文案必须与「额度用完」区分开。
_SPECIAL_MESSAGES: dict[str, str] = {
    "amap_ip_restricted": (
        "当前 IP 触发了高德访问限制，需要在高德控制台提交工单解封，"
        "请勿等待自动恢复。"
    ),
}


class AmapAuthorizationError(RuntimeError):
    """高德密钥不可用或服务未开通。重试与等待都无用。"""

    def __init__(self, detail: str):
        self.detail = detail
        self.public_code = "amap_access_denied"
        self.public_message = (
            f"高德密钥不可用或未开通该服务（{detail}），请检查 AMAP_API_KEY 配置。"
        )
        self.retryable = False
        super().__init__(f"高德授权失败：{detail}")


class AmapRequestError(RuntimeError):
    """其它高德失败。沿用变更前的失败语义。

    可选携带 `public_code` / `public_message`：需要人工处理（而非等待重置）的
    失败要有自己的文案，否则用户会按错误的处置方式白等。
    """

    def __init__(
        self,
        detail: str,
        *,
        public_code: str | None = None,
        public_message: str | None = None,
        retryable: bool = True,
    ):
        self.detail = detail
        if public_code is not None:
            self.public_code = public_code
            self.public_message = public_message or detail
            self.retryable = retryable
        super().__init__(detail)


def _special_error(identifier: str) -> RuntimeError:
    """构造「需要人工处理」类失败。

    `SPECIAL_INFOS` 的值就是要在用户面前露出的错误码，文案取自
    `_SPECIAL_MESSAGES`；映射缺失时退化为普通请求错误，而不是猜一个文案。
    """
    code = _SPECIAL_MESSAGES.get(SPECIAL_INFOS.get(identifier, ""))
    if code is None:
        return AmapRequestError(f"高德请求失败：{identifier}")
    return AmapRequestError(
        f"高德请求失败：{identifier}",
        public_code=SPECIAL_INFOS[identifier],
        public_message=code,
        retryable=False,
    )


def _quota_exceeded(
    bucket: QuotaBucket, store: amap_quota_store.QuotaStore | None = None
) -> AmapQuotaExceeded:
    """构造配额耗尽异常。

    本地闸门拒绝与高德返回配额错误走**同一个**异常类型与错误码，上层不需要
    知道失败来自本地还是远端。

    读快照要用调用方传进来的 store，否则会去读另一份数据库。
    """
    quota = store or amap_quota_store.QuotaStore()
    snapshot = quota.snapshot(bucket)
    return AmapQuotaExceeded(bucket, used=snapshot.used, limit=snapshot.limit)


def _announce_warning(snapshot) -> None:
    """把「首次越过配额阈值」报告给正在执行的 Run（没有 Run 时只落日志）。

    预警是旁路信息：它绝不能影响正在进行的请求，所以异常在 notify 内部被吞掉。
    """
    notify_quota_warning(
        {
            "bucket": snapshot.bucket.value,
            "used": snapshot.used,
            "limit": snapshot.limit,
            "remaining": snapshot.remaining,
            "message": quota_warning_message(
                snapshot.bucket, used=snapshot.used, limit=snapshot.limit
            ),
        }
    )


async def call_amap(
    url: str,
    params: dict[str, Any],
    bucket: QuotaBucket,
    *,
    timeout: int = 15,
    store: amap_quota_store.QuotaStore | None = None,
) -> dict[str, Any]:
    """向高德发出一次 REST 请求，成功时返回解析后的 JSON。

    配额不足时抛出 `AmapQuotaExceeded`，且**一次网络请求都不会发出**。
    """
    quota = store or amap_quota_store.QuotaStore()
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    last_error: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        # 先占额，再发请求。占额必须早于任何网络动作。
        reservation = quota.reserve(bucket)
        if reservation is None:
            raise _quota_exceeded(bucket, quota)
        snapshot, crossed_warning = reservation
        if crossed_warning:
            _announce_warning(snapshot)

        try:
            data = await _request_once(full_url, timeout=timeout)
        except _RetryableRequestError as exc:
            last_error = exc.error
        else:
            status = str(data.get("status") or "")
            if status == "1":
                return data

            category, identifier = classify_failure(data)
            if category == QUOTA:
                # 高德自己回的配额错误：本地计数显然低估了。仍按所属池构造
                # 同一个异常类型，上层无需区分失败来自本地闸门还是远端。
                raise _quota_exceeded(bucket, quota)
            if category == ACCESS_DENIED:
                raise AmapAuthorizationError(identifier)
            if category == SPECIAL:
                raise _special_error(identifier)
            if category == OTHER:
                # 未识别的失败：立即上报，不重试。月配额超限若返回未文档化的
                # 新码就会落到这里——不重试放大，也不被静默当作成功。
                raise AmapRequestError(f"高德请求失败：{identifier}")
            last_error = AmapRequestError(f"高德请求失败：{identifier}")

        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(BACKOFF_SECONDS * (attempt + 1))

    raise AmapRequestError(
        f"高德请求失败（已重试 {MAX_ATTEMPTS} 次）：{redact_url(full_url)}；原因：{last_error}"
    )


class _RetryableRequestError(Exception):
    """网络层失败：值得退避重试，且已经占过额。"""

    def __init__(self, error: Exception):
        self.error = error
        super().__init__(str(error))


async def _request_once(url: str, *, timeout: int) -> dict[str, Any]:
    """发出**一次**网络请求。

    `max_attempts=1` 关掉 `app.core.http` 的内部重试，使重试完全由本通道驱动，
    从而保证每次真实请求都被计一次额度。
    """
    try:
        return await http_get_json_async(url, timeout=timeout, max_attempts=1)
    except Exception as exc:  # noqa: BLE001
        raise _RetryableRequestError(exc) from exc
