"""AKShare（腾讯）适配器。只做翻译。只提供不复权日线（腾讯的后复权价同样不是乘法口径，不能用来算因子）。"""

from __future__ import annotations

from datetime import date
from typing import Any

import akshare as ak
import numpy as np
import pandas as pd

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
            "腾讯个股日线", ak.stock_zh_a_hist_tx, timeout,
            symbol=prefixed_symbol(code), start_date=compact(start), end_date=compact(end), adjust="",
        )
        # 这个版本的 AKShare 已经把成交量换算成股、成交额换算成元（精度到百股、百元）
        return pd.DataFrame({
            "date": raw["date"], "code": code,
            "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
            "preclose": np.nan,
            "volume": raw["volume"], "amount": raw["amount"], "suspended": False,
        })
