"""来源路由：按能力和配置的优先级选来源，失败、被限制或冷却时自动换下一个。

所有对外请求都从这里经过限流器发出。这一层不出现任何具体来源的名字，
来源由登记表（名称 -> 构造函数）提供，第一次用到时才创建。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd

from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
from app.custom.tdx_formula.ql.data.config import DataConfig
from app.custom.tdx_formula.ql.data.interface import (
    Capability,
    DataSource,
    NoSourceAvailable,
    SessionSource,
    SourceBlocked,
)
from app.custom.tdx_formula.ql.data.limiter import RateLimiter
from app.custom.tdx_formula.ql.data.models import normalize_frame

logger = logging.getLogger(__name__)

SourceFactory = Callable[[], DataSource]


class Router:
    def __init__(
        self,
        config_of: Callable[[], DataConfig],
        limiter: RateLimiter,
        registry: Mapping[str, SourceFactory],
    ) -> None:
        self._config_of = config_of
        self._limiter = limiter
        self._registry = dict(registry)
        self._adapters: dict[str, DataSource] = {}
        self._guard = threading.Lock()

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def source_names(self, capability: Capability) -> tuple[str, ...]:
        return self._config_of().priority.get(capability, ())

    def fetch(
        self,
        capability: Capability,
        *,
        prefer: str | None = None,
        accept_empty: bool = True,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """按优先级取数，返回统一口径的 DataFrame；``df.attrs["source"]`` 记录来自哪个来源。

        ``prefer``：优先尝试的来源（例如要求复权因子与日线同源时）。
        ``accept_empty=False``：某个来源返回空结果时继续问下一个；全部为空则返回空结果。
        全部来源都失败时抛 ``NoSourceAvailable``。
        """
        config = self._config_of()
        names = list(config.priority.get(capability, ()))
        if prefer in names:
            names.remove(prefer)  # type: ignore[arg-type]
            names.insert(0, prefer)  # type: ignore[arg-type]
        errors: dict[str, Exception] = {}
        empty: pd.DataFrame | None = None
        for name in names:
            try:
                adapter = self._adapter(name)
                if capability not in adapter.capabilities:
                    continue
                policy = config.sources[name]
                df = self._fetch_from(name, adapter, capability, policy.timeout_seconds, kwargs)
                if policy.count_pages_as_requests:
                    self._limiter.add_usage(name, int(df.attrs.get("requests", 1)) - 1)
            except SourceRejected as e:
                errors[name] = e
                logger.warning("%s：来源 %s 限制了访问，换下一个来源", capability.value, name)
                continue
            except SourceUnavailable as e:
                errors[name] = e
                logger.info("%s：来源 %s 不可用（%s），换下一个来源", capability.value, name, e.message)
                continue
            df = normalize_frame(capability, df)
            df.attrs["source"] = name
            if df.empty and not accept_empty:
                empty = df
                continue
            return df
        if empty is not None:
            return empty
        if not errors:
            raise NoSourceAvailable(f"没有配置能提供「{capability.value}」的数据来源", {}, blocked=True)
        blocked = all(isinstance(e, SourceBlocked | SourceRejected) for e in errors.values())
        detail = "；".join(f"{n}：{getattr(e, 'message', e)}" for n, e in errors.items())
        raise NoSourceAvailable(f"「{capability.value}」的所有数据来源都没能取到数据。{detail}", errors, blocked)

    def available(self, capability: Capability) -> bool:
        """这项能力此刻是否至少有一个来源可用。"""
        for name in self.source_names(capability):
            try:
                adapter = self._adapter(name)
            except SourceUnavailable:
                continue
            if capability in adapter.capabilities and self._limiter.state_of(name) == "normal":
                return True
        return False

    def capabilities_of(self, name: str) -> frozenset[Capability]:
        try:
            return self._adapter(name).capabilities
        except SourceUnavailable:
            return frozenset()

    def close(self) -> None:
        """关闭所有已创建的来源（有会话的来源在这里登出）。任务结束或异常退出时都必须调用。"""
        with self._guard:
            adapters = dict(self._adapters)
            self._adapters.clear()
        for name, adapter in adapters.items():
            was_open = isinstance(adapter, SessionSource) and adapter.is_open
            try:
                adapter.close()
            except SourceUnavailable as e:
                logger.warning("关闭数据来源 %s 时出错：%s", name, e.message)
            if was_open:
                self._limiter.add_usage(name, 1)  # 登出也算一次请求

    # ---- 内部 ----
    def _adapter(self, name: str) -> DataSource:
        with self._guard:
            adapter = self._adapters.get(name)
            if adapter is None:
                factory = self._registry.get(name)
                if factory is None:
                    raise SourceUnavailable(f"配置里的数据来源 {name} 没有对应的适配器")
                adapter = factory()
                self._adapters[name] = adapter
            return adapter

    def _fetch_from(
        self, name: str, adapter: DataSource, capability: Capability, timeout: float, kwargs: dict[str, Any]
    ) -> pd.DataFrame:
        def ensure() -> bool:
            if isinstance(adapter, SessionSource) and not adapter.is_open:
                adapter.open(timeout)
                return True
            return False

        return self._limiter.call(name, lambda: adapter.fetch(capability, timeout=timeout, **kwargs), ensure=ensure)
