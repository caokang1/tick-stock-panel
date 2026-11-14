"""AKShare（新浪）适配器。只做翻译。成交量单位是股，成交额单位是元。"""

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
    in_range,
    prefixed_symbol,
)


class AkshareSinaSource:
    name = "akshare_sina"
    capabilities = frozenset({
        Capability.STOCK_DAILY, Capability.ADJUST_FACTOR, Capability.INDEX_DAILY, Capability.TRADE_CALENDAR,
        Capability.FLOAT_SHARES,
    })

    def fetch(self, capability: Capability, **kwargs: Any) -> pd.DataFrame:
        timeout = float(kwargs.pop("timeout"))
        if capability == Capability.STOCK_DAILY:
            return self._stock_daily(timeout, **kwargs)
        if capability == Capability.FLOAT_SHARES:
            return self._float_shares(timeout, **kwargs)
        if capability == Capability.ADJUST_FACTOR:
            return self._adjust_factor(timeout, **kwargs)
        if capability == Capability.INDEX_DAILY:
            return self._index_daily(timeout, **kwargs)
        if capability == Capability.TRADE_CALENDAR:
            return self._trade_calendar(timeout, **kwargs)
        raise SourceUnavailable(f"新浪不提供 {capability.value}")

    def close(self) -> None:
        return None

    def _stock_daily(self, timeout: float, code: str, start: date, end: date) -> pd.DataFrame:
        raw = call_akshare(
            "新浪个股日线", ak.stock_zh_a_daily, timeout,
            symbol=prefixed_symbol(code), start_date=compact(start), end_date=compact(end), adjust="",
        )
        out = pd.DataFrame({
            "date": raw["date"], "code": code,
            "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
            "preclose": np.nan,  # 来源不给前收盘，由下载流程用复权因子推出
            "volume": raw["volume"], "amount": raw["amount"], "suspended": False,
        })
        if "outstanding_share" in raw.columns:
            out["float_shares"] = pd.to_numeric(raw["outstanding_share"], errors="coerce")  # 当天的流通股本（股）
        return out

    def _float_shares(self, timeout: float, code: str, start: date, end: date) -> pd.DataFrame:
        bars = self._stock_daily(timeout, code, start, end)
        if "float_shares" not in bars.columns:
            raise SourceUnavailable("新浪个股日线：这次没有返回流通股本")
        return bars.loc[bars["float_shares"].notna(), ["date", "code", "float_shares"]]

    def _adjust_factor(self, timeout: float, code: str, start: date, end: date) -> pd.DataFrame:
        """新浪给的是每次除权除息日起生效的后复权因子；这里展开成逐日（工作日）的因子。"""
        raw = call_akshare(
            "新浪复权因子", ak.stock_zh_a_daily, timeout, symbol=prefixed_symbol(code), adjust="hfq-factor",
        )
        events = pd.DataFrame({
            "date": pd.to_datetime(raw["date"], errors="coerce").astype("datetime64[s]"),
            "factor": pd.to_numeric(raw["hfq_factor"], errors="coerce"),
        }).dropna().sort_values("date")
        if events.empty:
            raise SourceUnavailable("新浪复权因子：返回了空表")
        days = pd.bdate_range(start, end).astype("datetime64[s]")
        idx = np.searchsorted(events["date"].to_numpy(), days.to_numpy(), side="right") - 1
        factor = np.where(idx >= 0, events["factor"].to_numpy()[np.maximum(idx, 0)], 1.0)
        return pd.DataFrame({"date": days, "code": code, "factor": factor})

    def _index_daily(self, timeout: float, code: str, start: date, end: date) -> pd.DataFrame:
        raw = call_akshare("新浪指数日线", ak.stock_zh_index_daily, timeout, symbol=prefixed_symbol(code))
        raw = in_range(raw, start, end)
        return pd.DataFrame({
            "date": raw["date"], "code": code,
            "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
            "volume": raw["volume"], "amount": np.nan,  # 新浪的指数日线没有成交额
        })

    def _trade_calendar(self, timeout: float, start: date, end: date) -> pd.DataFrame:
        raw = call_akshare("新浪交易日历", ak.tool_trade_date_hist_sina, timeout)
        return pd.DataFrame({"date": in_range(raw, start, end, "trade_date")["trade_date"]})
