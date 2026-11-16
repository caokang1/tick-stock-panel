"""AKShare（东方财富）适配器。只做翻译。

不提供复权因子：实测东方财富的后复权价不是"不复权价 × 因子"，用后复权收盘价除以不复权收盘价
得到的比值每天都在变，据此算出的涨跌幅会失真。复权因子由提供乘法口径因子的来源给出。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import akshare as ak
import numpy as np
import pandas as pd

from app.custom.tdx_formula.ql.core import clock, codes
from app.custom.tdx_formula.ql.core.errors import DataError, SourceUnavailable
from app.custom.tdx_formula.ql.data.interface import Capability
from app.custom.tdx_formula.ql.data.sources.akshare_common import (
    call_akshare,
    compact,
    prefixed_symbol,
)

_LOT = 100  # 东方财富的成交量单位是手
_CLOSE_HOUR = 15  # 收盘前的实时行情不是当天的最终日线


class AkshareEmSource:
    name = "akshare_em"
    capabilities = frozenset({Capability.STOCK_DAILY, Capability.INDEX_DAILY, Capability.MARKET_SNAPSHOT})

    def fetch(self, capability: Capability, **kwargs: Any) -> pd.DataFrame:
        timeout = float(kwargs.pop("timeout"))
        if capability == Capability.STOCK_DAILY:
            return self._stock_daily(timeout, **kwargs)
        if capability == Capability.INDEX_DAILY:
            return self._index_daily(timeout, **kwargs)
        if capability == Capability.MARKET_SNAPSHOT:
            return self._market_snapshot(timeout, **kwargs)
        raise SourceUnavailable(f"东方财富不提供 {capability.value}")

    def close(self) -> None:
        return None

    def _stock_daily(self, timeout: float, code: str, start: date, end: date) -> pd.DataFrame:
        raw = call_akshare(
            self.name, "东方财富个股日线", ak.stock_zh_a_hist, timeout,
            symbol=codes.digits(code), period="daily", start_date=compact(start), end_date=compact(end), adjust="",
        )
        return pd.DataFrame({
            "date": raw["日期"], "code": code,
            "open": raw["开盘"], "high": raw["最高"], "low": raw["最低"], "close": raw["收盘"],
            "preclose": np.nan,  # 来源不直接给前收盘，由下载流程用复权因子推出
            "volume": pd.to_numeric(raw["成交量"], errors="coerce") * _LOT,
            "amount": raw["成交额"], "suspended": False,
        })

    def _index_daily(self, timeout: float, code: str, start: date, end: date) -> pd.DataFrame:
        raw = call_akshare(
            self.name, "东方财富指数日线", ak.stock_zh_index_daily_em, timeout,
            symbol=prefixed_symbol(code), start_date=compact(start), end_date=compact(end),
        )
        return pd.DataFrame({
            "date": raw["date"], "code": code,
            "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
            "volume": pd.to_numeric(raw["volume"], errors="coerce") * _LOT, "amount": raw["amount"],
        })

    def _market_snapshot(self, timeout: float, day: date) -> pd.DataFrame:
        """实时行情接口只能给出"今天"，而且收盘后才等于当天的日线。"""
        now = clock.now()
        if day != now.date() or now.hour < _CLOSE_HOUR:
            raise SourceUnavailable("东方财富的全市场行情只能在当天收盘后取当天的数据")
        raw = call_akshare(self.name, "东方财富全市场行情", ak.stock_zh_a_spot_em, timeout)
        norm: list[str | None] = []
        for c in raw["代码"].astype(str):
            try:
                norm.append(codes.normalize(c))
            except DataError:
                norm.append(None)  # 认不出交易所的代码不要
        close = pd.to_numeric(raw["最新价"], errors="coerce")
        preclose = pd.to_numeric(raw["昨收"], errors="coerce")
        volume = pd.to_numeric(raw["成交量"], errors="coerce") * _LOT
        suspended = (close.isna() | (volume.fillna(0) == 0)).to_numpy()
        out = pd.DataFrame({
            "date": pd.Timestamp(day), "code": norm,
            "open": pd.to_numeric(raw["今开"], errors="coerce"),
            "high": pd.to_numeric(raw["最高"], errors="coerce"),
            "low": pd.to_numeric(raw["最低"], errors="coerce"),
            "close": close, "preclose": preclose,
            "volume": volume.fillna(0.0), "amount": pd.to_numeric(raw["成交额"], errors="coerce").fillna(0.0),
            "suspended": suspended,
            "is_st": raw["名称"].astype(str).str.upper().str.contains("ST").to_numpy(),
        })
        if "流通市值" in raw.columns:  # 流通股本 = 流通市值 ÷ 最新价
            cap = pd.to_numeric(raw["流通市值"], errors="coerce")
            out["float_shares"] = (cap / close).where((cap > 0) & (close > 0))
        return out[out["code"].notna()]
