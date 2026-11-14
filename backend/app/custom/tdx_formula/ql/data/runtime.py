"""把配置、状态库、限流器、路由装配成一套，进程内共用一份。

测试可以用 ``set_registry()`` 换成假适配器，用 ``set_sleep()`` 换掉真实的等待。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.custom.tdx_formula.ql.core.config import get_settings
from app.custom.tdx_formula.ql.data import config as config_mod
from app.custom.tdx_formula.ql.data.config import DataConfig
from app.custom.tdx_formula.ql.data.limiter import RateLimiter, SourcePolicy
from app.custom.tdx_formula.ql.data.router import Router, SourceFactory
from app.custom.tdx_formula.ql.data.state import StateStore


@dataclass
class Runtime:
    data_dir: Path
    state: StateStore
    limiter: RateLimiter
    router: Router


_guard = threading.Lock()
_runtime: Runtime | None = None
_registry: Mapping[str, SourceFactory] | None = None
_sleep: Callable[[float], None] = time.sleep
_config_cache: tuple[tuple[int, int], DataConfig] | None = None


def current_config() -> DataConfig:
    """生效的配置。配置文件没变时用缓存，变了自动重读。"""
    global _config_cache
    s = get_settings()
    stamp = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (s.default_sources_config, s.user_sources_config))
    cached = _config_cache
    if cached is not None and cached[0] == stamp:
        return cached[1]
    config = config_mod.load_config()
    _config_cache = (stamp, config)  # type: ignore[assignment]
    return config


def _policy_of(name: str) -> SourcePolicy:
    return current_config().sources[name]


def get_runtime() -> Runtime:
    global _runtime
    with _guard:
        data_dir = get_settings().data_dir
        if _runtime is None or _runtime.data_dir != data_dir:
            if _registry is None:
                from app.custom.tdx_formula.ql.data.sources import default_registry

                registry: Mapping[str, SourceFactory] = default_registry()
            else:
                registry = _registry
            state = StateStore()
            limiter = RateLimiter(_policy_of, state, sleep=_sleep)
            _runtime = Runtime(data_dir, state, limiter, Router(current_config, limiter, registry))
        return _runtime


def set_registry(registry: Mapping[str, SourceFactory] | None) -> None:
    """换一套适配器登记表（测试用）；传 None 恢复真实来源。"""
    global _registry
    _registry = registry
    reset_runtime()


def set_sleep(sleep: Callable[[float], None] | None) -> None:
    """换掉限流器的等待函数（测试用）；传 None 恢复真实等待。"""
    global _sleep
    _sleep = sleep or time.sleep
    reset_runtime()


def reset_runtime() -> None:
    global _runtime, _config_cache
    with _guard:
        if _runtime is not None:
            _runtime.router.close()
        _runtime = None
        _config_cache = None
