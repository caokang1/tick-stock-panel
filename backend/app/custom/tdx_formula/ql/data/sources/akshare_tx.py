"""AKShare（腾讯）适配器。只做翻译。只提供不复权日线（腾讯的后复权价同样不是乘法口径，不能用来算因子）。"""

from __future__ import annotations

from datetime import date
from typing import Any

import akshare as ak
import akshare.stock.quote.stock_hist_tx as _tx_module
import numpy as np
import pandas as pd

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.errors import SourceUnavailable
from app.custom.tdx_formula.ql.data.interface import Capability
from app.custom.tdx_formula.ql.data.sources.akshare_common import (
    call_akshare,
    compact,
    prefixed_symbol,
)


class AkshareTxSource:
    name = "akshare_tx"
    capabilities = frozenset({Capability.STOCK_DAILY})

    def fetch(self, capability: Capability, **kwargs: Any) -> pd.DataFrame:
        timeout = float(kwargs.pop("timeout"))
        if capability == Capability.STOCK_DAILY:
            return self._stock_daily(timeout, **kwargs)
        raise SourceUnavailable(f"腾讯不提供 {capability.value}")

    def close(self) -> None:
        return None

    def _stock_daily(self, timeout: float, code: str, start: date, end: date) -> pd.DataFrame:
        raw = call_akshare(
            self.name, "腾讯个股日线", ak.stock_zh_a_hist_tx, timeout,
            symbol=prefixed_symbol(code), start_date=compact(start), end_date=compact(end), adjust="",
        )
        # 这个版本的 AKShare 已经把成交量换算成股、成交额换算成元（精度到百股、百元）
        out = pd.DataFrame({
            "date": raw["date"], "code": code,
            "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
            "preclose": np.nan,
            "volume": raw["volume"], "amount": raw["amount"], "suspended": False,
        })
        out.attrs["requests"] = requests_for(start, end)
        return out


def requests_for(start: date, end: date) -> int:
    """AKShare 取这段时间的腾讯日线，内部实际发了几次请求（它按年份分段，一段一次）。

    每段几年读自 AKShare 自己的设置；读不到时按一年一次算，宁可多记。
    """
    step = max(1, int(getattr(_tx_module, "_TX_YEARS_PER_REQUEST", 1)))
    last_year = min(end.year, clock.today().year)
    return max(1, len(range(start.year, last_year + 1, step)))
