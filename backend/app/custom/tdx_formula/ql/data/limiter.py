"""通用限流器：间隔、每日上限、重试、冷却、请求计数只在这里实现一次。

限流器不知道任何具体来源，只按每个来源的 ``SourcePolicy`` 执行（数值全部来自配置）。
时间取自 ``quantlab.core.clock``，等待用可注入的 ``sleep``，测试时不真的等待。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
from app.custom.tdx_formula.ql.data.interface import (
    SourceCapped,
    SourceCooling,
    SourceDisabled,
    SourceHalted,
    SourceNoData,
)
from app.custom.tdx_formula.ql.data.state import StateStore

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class SourcePolicy:
    """一个来源的使用限制。各字段的含义见 backend/config/data_sources.default.toml 里的注释。"""

    enabled: bool
    max_concurrent: int
    daily_request_cap: int  # 0 表示不限
    min_interval_seconds: float
    jitter_seconds: float
    timeout_seconds: float
    max_retries: int
    stop_after_consecutive_failures: int
    cooldown_minutes_on_reject: int
    count_pages_as_requests: bool
    official_daily_limit: int | None
    official_max_concurrent: int | None


STATE_NORMAL = "normal"
STATE_COOLING = "cooling"
STATE_CAPPED = "capped"
STATE_DISABLED = "disabled"
STATE_HALTED = "halted"


class RateLimiter:
    def __init__(
        self,
        policy_of: Callable[[str], SourcePolicy],
        state: StateStore,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._policy_of = policy_of
        self._state = state
        self._sleep = sleep
        self._rng = rng
        self._guard = threading.Lock()
        self._slots: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
        self._next_allowed: dict[str, datetime] = {}
        self._halted: set[str] = set()

    # ---- 对外 ----
    def begin_task(self) -> None:
        """新任务开始：清掉上一次任务留下的"连续失败、已停止"标记。冷却和每日计数不受影响。"""
        with self._guard:
            self._halted.clear()
        self._state.reset_failures()

    def call(
        self, source_name: str, fn: Callable[[], T], cost: int = 1, ensure: Callable[[], bool] | None = None
    ) -> T:
        """经限流执行一次对外请求。

        ``ensure``：每次尝试前调用，用来建立会话（如登录）；它真的发了请求时返回 True，
        这次请求同样计数，并且与随后的 ``fn`` 之间也隔开最小间隔。

        检查停用、冷却、每日上限 → 等到满足最小间隔 → 执行 → 计数。
        失败按 max_retries 重试（每次都计数，间隔逐次加长）；``SourceNoData``（来源明确给不出这项数据）
        不重试，但仍计入连续失败；``SourceRejected`` 不重试，
        写入冷却截止时间后向上抛；连续失败达到阈值后抛 ``SourceHalted`` 并在本次任务内停用该来源。
        """
        policy = self._policy_of(source_name)
        slot = self._slot(source_name, policy.max_concurrent)
        with slot:
            attempt = 0
            while True:
                self._check(source_name, policy, cost)
                self._wait_turn(source_name, policy, attempt)
                try:
                    if ensure is not None and ensure():
                        self._finish(source_name, policy)
                        self._count(source_name, 1)
                        self._wait_turn(source_name, policy, 0)
                    result = fn()
                except SourceRejected as e:
                    self._finish(source_name, policy)
                    self._count(source_name, self._spent(policy, cost, e))
                    until = clock.now() + timedelta(minutes=policy.cooldown_minutes_on_reject)
                    self._state.set_cooldown(source_name, until)
                    logger.error("数据来源 %s 限制了访问，暂停到 %s：%s", source_name, until, e.message)
                    raise
                except Exception as e:  # 适配器的任何其他失败都按"来源不可用"处理
                    self._finish(source_name, policy)
                    self._count(source_name, self._spent(policy, cost, e))
                    error = e if isinstance(e, SourceUnavailable) else SourceUnavailable(f"{type(e).__name__}: {e}")
                    failures = self._state.get_state(source_name).consecutive_failures + 1
                    self._state.set_failures(source_name, failures)
                    if failures >= policy.stop_after_consecutive_failures:
                        with self._guard:
                            self._halted.add(source_name)
                        logger.error("数据来源 %s 连续失败 %d 次，本次任务不再使用", source_name, failures)
                        raise SourceHalted(
                            f"数据来源 {source_name} 连续失败 {failures} 次，本次任务已停止使用它。"
                            f"最后一次的原因：{error.message}"
                        ) from e
                    if attempt >= policy.max_retries or isinstance(error, SourceNoData):
                        raise error from e
                    attempt += 1
                    logger.warning("数据来源 %s 请求失败，第 %d 次重试：%s", source_name, attempt, error.message)
                    continue
                self._finish(source_name, policy)
                self._count(source_name, cost)
                if self._state.get_state(source_name).consecutive_failures:
                    self._state.set_failures(source_name, 0)
                return result

    @staticmethod
    def _spent(policy: SourcePolicy, cost: int, error: Exception) -> int:
        """失败的这次尝试实际发出了多少个请求。适配器知道时会写在异常的 ``requests`` 上（翻页、登出都算）。"""
        if not policy.count_pages_as_requests:
            return cost
        return max(cost, int(getattr(error, "requests", 0)))

    def add_usage(self, source_name: str, amount: int) -> None:
        """补记请求数（例如适配器报告一次查询实际翻了几页）。"""
        if amount > 0:
            self._count(source_name, amount)

    def today_requests(self, source_name: str) -> int:
        return self._state.get_usage(source_name, clock.today())

    def state_of(self, source_name: str) -> str:
        policy = self._policy_of(source_name)
        if not policy.enabled:
            return STATE_DISABLED
        if self.cooldown_until(source_name) is not None:
            return STATE_COOLING
        if policy.daily_request_cap > 0 and self.today_requests(source_name) >= policy.daily_request_cap:
            return STATE_CAPPED
        if source_name in self._halted:
            return STATE_HALTED
        return STATE_NORMAL

    def cooldown_until(self, source_name: str) -> datetime | None:
        """冷却截止时间；不在冷却中返回 None。"""
        until = self._state.get_state(source_name).cooldown_until
        return until if until is not None and until > clock.now() else None

    def consecutive_failures(self, source_name: str) -> int:
        return self._state.get_state(source_name).consecutive_failures

    # ---- 内部 ----
    def _slot(self, source_name: str, max_concurrent: int) -> threading.BoundedSemaphore:
        with self._guard:
            held = self._slots.get(source_name)
            if held is None or held[0] != max_concurrent:
                held = (max_concurrent, threading.BoundedSemaphore(max(1, max_concurrent)))
                self._slots[source_name] = held
            return held[1]

    def _check(self, source_name: str, policy: SourcePolicy, cost: int) -> None:
        if not policy.enabled:
            raise SourceDisabled(f"数据来源 {source_name} 已在配置里停用")
        until = self.cooldown_until(source_name)
        if until is not None:
            raise SourceCooling(f"数据来源 {source_name} 被限制访问，暂停到 {until:%Y-%m-%d %H:%M}")
        if source_name in self._halted:
            raise SourceHalted(f"数据来源 {source_name} 连续失败过多，本次任务已停止使用它")
        if policy.daily_request_cap > 0:
            used = self.today_requests(source_name)
            if used + cost > policy.daily_request_cap:
                raise SourceCapped(
                    f"数据来源 {source_name} 今天已请求 {used} 次，"
                    f"达到每日上限 {policy.daily_request_cap}，明天自动继续"
                )

    def _wait_turn(self, source_name: str, policy: SourcePolicy, attempt: int) -> None:
        """等到距离上一次请求结束满足最小间隔；重试时逐次加长（第 n 次重试多等 n 倍的最小间隔）。"""
        with self._guard:
            now = clock.now()
            ready = self._next_allowed.get(source_name, now)
        ready += timedelta(seconds=policy.min_interval_seconds * attempt)
        wait = (ready - now).total_seconds()
        if wait > 0:
            self._sleep(wait)

    def _finish(self, source_name: str, policy: SourcePolicy) -> None:
        gap = policy.min_interval_seconds + policy.jitter_seconds * self._rng()
        with self._guard:
            self._next_allowed[source_name] = clock.now() + timedelta(seconds=gap)

    def _count(self, source_name: str, amount: int) -> None:
        self._state.add_usage(source_name, clock.today(), amount)
