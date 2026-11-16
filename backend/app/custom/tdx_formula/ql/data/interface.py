"""统一数据接口：能力枚举、适配器协议、调度层用到的统一异常。

这一层不出现任何具体数据来源的名字（BACKEND_RULES.md §数据源隔离 R7）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from app.custom.tdx_formula.ql.core.errors import SourceUnavailable


class Capability(StrEnum):
    TRADE_CALENDAR = "trade_calendar"
    SECURITIES = "securities"
    STOCK_DAILY = "stock_daily"  # 不复权日线
    ADJUST_FACTOR = "adjust_factor"
    MARKET_SNAPSHOT = "market_snapshot"  # 某日全市场
    ST_HISTORY = "st_history"
    INDEX_DAILY = "index_daily"
    INDUSTRY = "industry"  # 全市场每只股票现在所属的行业
    FLOAT_SHARES = "float_shares"  # 一只股票历史上每天的流通股本（给早期版本下载的数据补这一列用）


class DataSource(Protocol):
    """数据源适配器。职责只有翻译：不得自己等待或重试。

    fetch 的参数（都用关键字传入）：
    - 所有能力：``timeout``（秒，来自配置）
    - TRADE_CALENDAR：``start``、``end``（datetime.date）
    - SECURITIES：无
    - STOCK_DAILY / ADJUST_FACTOR / ST_HISTORY / INDEX_DAILY / FLOAT_SHARES：``code``、``start``、``end``
    - MARKET_SNAPSHOT：``day``
    - INDUSTRY：无

    返回值的列见 ``quantlab.data.models``。一次 fetch 实际发出的请求数（如翻页）
    写在 ``df.attrs["requests"]`` 里，由调度层补记到请求计数。

    来源限制访问时抛 ``SourceRejected``，其他失败抛 ``SourceUnavailable``。
    """

    name: str
    capabilities: frozenset[Capability]

    def fetch(self, capability: Capability, **kwargs: Any) -> pd.DataFrame: ...

    def close(self) -> None: ...


@runtime_checkable
class SessionSource(Protocol):
    """需要先建立会话（登录）才能取数的来源。建立会话本身也算一次请求，由调度层经限流器发起。"""

    @property
    def is_open(self) -> bool: ...

    def open(self, timeout: float) -> None: ...


class SourceBlocked(SourceUnavailable):
    """来源此刻不能用，且换个请求也一样（停用、冷却中、到达每日上限、本次任务已停止）。"""

    code = "source_blocked"


class SourceDisabled(SourceBlocked):
    code = "source_disabled"


class SourceCooling(SourceBlocked):
    code = "source_cooling"


class SourceCapped(SourceBlocked):
    code = "source_capped"


class SourceHalted(SourceBlocked):
    """连续失败次数达到阈值，本次任务内不再使用这个来源。"""

    code = "source_halted"


class SourceNoData(SourceUnavailable):
    """来源有回应，但给不出这一项数据（例如它没有这只股票）。再问一遍结果也一样，所以不重试，直接换下一个来源。

    仍然计入连续失败：来源改了接口或者返回的是拦截页面时，表现也是这样，连续多次就该停下来。
    """

    code = "source_no_data"


class NoSourceAvailable(SourceUnavailable):
    """某项能力的所有来源都没能给出数据。

    ``blocked`` 为 True 表示全部来源都处于不可用状态（再请求别的股票也没用，任务应当暂停）；
    为 False 表示至少有一个来源是正常的，只是这一次请求失败。
    """

    code = "no_source_available"

    def __init__(self, message: str, errors: dict[str, Exception], blocked: bool) -> None:
        super().__init__(message)
        self.errors = errors
        self.blocked = blocked
