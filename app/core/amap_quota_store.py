"""高德配额用量的持久化与原子预占。

设计约束（见 `docs/adr/0003-amap-quota-pools-and-estimated-usage.md`）：

1. 计数落在 SQLite，**不挂到 Redis 缓存层**。Redis 是可选依赖，未配置
   `REDIS_URL` 时整个缓存层静默失效——配额保护若随之失效，就恰好会在最需要
   它的环境里没有保护。
2. **不做内存缓冲、不批量刷盘。** 崩溃丢写会低估用量，而本特性的全部价值
   就在于不低估。允许的失效方向只有「高估」，所以每次预占都是一次同步写。
3. 检查与自增必须在同一个 `BEGIN IMMEDIATE` 事务里完成，否则并发请求会各自
   通过检查后一起超支。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.amap_quota import (
    QuotaBucket,
    UNLIMITED,
    current_month,
    is_unlimited,
    warning_threshold,
)
from app.core.database import get_conn, get_db_path


@dataclass(frozen=True)
class QuotaSnapshot:
    """某个池在某个配额月的用量快照。"""

    bucket: QuotaBucket
    month: str
    used: int
    limit: int
    local_calls: int
    baseline: int
    warning_sent: bool

    @property
    def unlimited(self) -> bool:
        return self.limit == UNLIMITED

    @property
    def remaining(self) -> int | None:
        """剩余量。不限制的池返回 None，而不是一个会被误读成真实余量的数字。"""
        if self.unlimited:
            return None
        return max(0, self.limit - self.used)

    @property
    def ratio(self) -> float:
        if self.unlimited or self.limit <= 0:
            return 0.0
        return self.used / self.limit

    def to_public(self) -> dict[str, object]:
        """给 API 用的投影。字段名要能自解释「这是估算」。"""
        return {
            "bucket": self.bucket.value,
            "month": self.month,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "unlimited": self.unlimited,
            "local_calls": self.local_calls,
            "baseline": self.baseline,
            "warning_threshold": warning_threshold(self.bucket, self.limit),
            "warning_sent": self.warning_sent,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _combine(calls: int, baseline: int, calls_at_reconcile: int) -> int:
    """把「控制台基准」与「本地计数」合成已用量。

    **不能取 max，也不能相加**，理由是这两个数字不在同一时间点上：

    * `baseline` —— 用户对账那一刻从控制台读到的账号月内已用量。它覆盖了
      **截至那一刻**的全部消耗（含本服务的调用），但对之后的本地调用一无所知。
    * `calls` / `calls_at_reconcile` —— 本服务自部署以来的累计调用数，以及对账
      那一刻的同一数字。两者之差就是**对账之后**新发生的本地调用。

    所以正确式子是「基准 + 对账之后的本地调用」。取 max 会漏掉对账后的增量，
    相加则会把对账前的本地调用重复计一次。

    这个式子在「从未对账」（基准与锚点均为 0）时退化成纯本地计数，符合预期。
    """
    return baseline + (calls - calls_at_reconcile)


class QuotaStore:
    """按（池，月份）维护用量的仓储。

    每次操作一条短连接，沿用 `app/runtime/repositories.py` 的既有风格。
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else get_db_path()

    # ─── 读 ──────────────────────────────────────────────────

    def snapshot(
        self, bucket: QuotaBucket, *, month: str | None = None
    ) -> QuotaSnapshot:
        target = month or current_month()
        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT counter_calls, counter_baseline, counter_at_reconcile, warning_sent_at "
                "FROM amap_quota_usage WHERE bucket=? AND month=?",
                (bucket.value, target),
            ).fetchone()
        return self._to_snapshot(bucket, target, row)

    def snapshots(self, *, month: str | None = None) -> list[QuotaSnapshot]:
        target = month or current_month()
        return [self.snapshot(bucket, month=target) for bucket in QuotaBucket]

    @staticmethod
    def _to_snapshot(
        bucket: QuotaBucket, month: str, row: sqlite3.Row | tuple | None
    ) -> QuotaSnapshot:
        limit = _limit_of(bucket)
        if row is None:
            calls, baseline, anchor, warned = 0, 0, 0, None
        else:
            calls, baseline, anchor, warned = (
                int(row[0]),
                int(row[1]),
                int(row[2]),
                row[3],
            )
        return QuotaSnapshot(
            bucket=bucket,
            month=month,
            used=_combine(calls, baseline, anchor),
            limit=limit,
            local_calls=calls,
            baseline=baseline,
            warning_sent=warned is not None,
        )

    # ─── 写 ──────────────────────────────────────────────────

    def reserve(
        self, bucket: QuotaBucket, *, count: int = 1, month: str | None = None
    ) -> tuple[QuotaSnapshot, bool] | None:
        """原子预占 `count` 次额度。

        返回 `(快照, 本次是否首次越过预警阈值)`；额度不足时返回 None 且**不写入**。

        调用方必须在真正发出 HTTP 请求之前调用它。它是「硬拦截」的全部依据：
        预占失败意味着一次请求都不该发出去。
        """
        if count <= 0:
            raise ValueError("count must be positive")
        target = month or current_month()
        limit = _limit_of(bucket)
        now = _now_iso()

        with get_conn(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO amap_quota_usage "
                "(bucket, month, counter_calls, counter_baseline, counter_at_reconcile, "
                " warning_sent_at, updated_at) "
                "VALUES (?, ?, 0, 0, 0, NULL, ?) "
                "ON CONFLICT(bucket, month) DO NOTHING",
                (bucket.value, target, now),
            )
            row = conn.execute(
                "SELECT counter_calls, counter_baseline, counter_at_reconcile, warning_sent_at "
                "FROM amap_quota_usage WHERE bucket=? AND month=?",
                (bucket.value, target),
            ).fetchone()
            calls, baseline, anchor = int(row[0]), int(row[1]), int(row[2])
            warned_at = row[3]
            used_before = _combine(calls, baseline, anchor)

            if limit != UNLIMITED and used_before + count > limit:
                return None

            calls_after = calls + count
            used_after = _combine(calls_after, baseline, anchor)
            threshold = warning_threshold(bucket, limit)
            crossed = (
                threshold is not None
                and warned_at is None
                and used_after >= threshold
            )
            conn.execute(
                "UPDATE amap_quota_usage "
                "SET counter_calls=?, warning_sent_at=?, updated_at=? "
                "WHERE bucket=? AND month=?",
                (
                    calls_after,
                    now if crossed else warned_at,
                    now,
                    bucket.value,
                    target,
                ),
            )

        snapshot = QuotaSnapshot(
            bucket=bucket,
            month=target,
            used=used_after,
            limit=limit,
            local_calls=calls_after,
            baseline=baseline,
            warning_sent=crossed or warned_at is not None,
        )
        return snapshot, crossed

    def set_used(
        self, bucket: QuotaBucket, used: int, *, month: str | None = None
    ) -> QuotaSnapshot:
        """用量对账：录入从控制台读到的账号月内已用量。

        **绝对值**语义，不是增量：控制台显示「本月已用 300」，就录入 300。

        实现上把基准设为 `used`，并记下**此刻的本地计数**作为锚点：

        * 基准覆盖截至此刻的全部消耗，其中已含本服务此前的调用；
        * 锚点之后的本地调用才是需要追加的部分。

        因此对账**既不重复计数、也不吃掉本地计数**，且可反复进行——每次对账都
        重新锚定。`counter_calls` 全程只增不改，是纯累计量。
        """
        if used < 0:
            raise ValueError("used must not be negative")
        target = month or current_month()
        now = _now_iso()

        with get_conn(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # DO UPDATE 而不是 DO NOTHING：行不存在时要真正建出来，
            # 行已存在时只刷新 updated_at，绝不碰 counter_calls。
            conn.execute(
                "INSERT INTO amap_quota_usage "
                "(bucket, month, counter_calls, counter_baseline, counter_at_reconcile, "
                " warning_sent_at, updated_at) "
                "VALUES (?, ?, 0, 0, 0, NULL, ?) "
                "ON CONFLICT(bucket, month) DO UPDATE SET updated_at=excluded.updated_at",
                (bucket.value, target, now),
            )
            row = conn.execute(
                "SELECT counter_calls FROM amap_quota_usage WHERE bucket=? AND month=?",
                (bucket.value, target),
            ).fetchone()
            calls = int(row[0])
            conn.execute(
                "UPDATE amap_quota_usage "
                "SET counter_baseline=?, counter_at_reconcile=?, updated_at=? "
                "WHERE bucket=? AND month=?",
                (used, calls, now, bucket.value, target),
            )

        return QuotaSnapshot(
            bucket=bucket,
            month=target,
            used=_combine(calls, used, calls),
            limit=_limit_of(bucket),
            local_calls=calls,
            baseline=used,
            warning_sent=self._warning_sent(bucket, target),
        )

    def _warning_sent(self, bucket: QuotaBucket, month: str) -> bool:
        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT warning_sent_at FROM amap_quota_usage "
                "WHERE bucket=? AND month=?",
                (bucket.value, month),
            ).fetchone()
        return bool(row and row[0])


def _limit_of(bucket: QuotaBucket) -> int:
    """读取某池的当前限额。每次现读环境变量，见 `amap_quota.load_limits`。"""
    from app.core.amap_quota import load_limits

    return load_limits()[bucket]


def delete_old_months(keep_months: int = 13, *, db_path: str | Path | None = None) -> int:
    """清理过期的配额行，保留最近 `keep_months` 个月。返回删除行数。

    `init_db` 里已有一份等价的 SQL 清理（服务启动时执行）。这里保留一个可
    单测的入口，避免清理逻辑只存在于 SQL 字符串里而无法被断言。
    """
    if keep_months < 1:
        raise ValueError("keep_months must be at least 1")
    path = Path(db_path) if db_path is not None else get_db_path()
    cutoff = _month_shift(current_month(), -(keep_months - 1))
    with get_conn(path) as conn:
        cursor = conn.execute("DELETE FROM amap_quota_usage WHERE month < ?", (cutoff,))
        return cursor.rowcount or 0


def _month_shift(month: str, delta: int) -> str:
    year, mon = (int(part) for part in month.split("-"))
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"
