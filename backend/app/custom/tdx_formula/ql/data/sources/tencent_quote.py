"""腾讯实时行情适配器：只在当天收盘后提供"当天的全市场行情"，让每日更新不用等到傍晚。

实时行情接口给的是"此刻"的价和量，收盘以后它就等于当天的日线。所以这里只认"今天、而且已经收盘"：
别的日子、或还没收盘时一律说不可用，由别的来源去取。取回来以后再核对一遍行情自带的时间，
大部分股票的行情时间不是今天收盘后的，就当作还没准备好，不写入。

接口一次最多查 800 只，全市场约 7 次请求；这 7 次在一次限流器调用里按固定间隔依次发出
（和东方财富的全市场行情一样），请求次数通过 ``attrs["requests"]`` 如实计入。
发请求和解析用的是 ``app.plugins.freecn.quotes`` 里盘中快照那一套，不另写一份。
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
from app.custom.tdx_formula.ql.data import store
from app.custom.tdx_formula.ql.data.interface import Capability

READY = time(15, 5)  # 15:00 收盘，留几分钟让收盘价落定
GAP_SECONDS = 1.0  # 一轮里相邻两次请求之间的间隔
MIN_FRESH = 0.5  # 至少这么大比例的股票，行情时间是今天收盘后的，才认为是当天的日线
_LOT = 100  # 接口的成交量单位是手
_BEIJING = timezone(timedelta(hours=8))


def _to_query(code: str) -> str:
    """sh.600000 → sh600000"""
    return code.replace(".", "")


class TencentQuoteSource:
    name = "tencent_quote"
    capabilities = frozenset({Capability.MARKET_SNAPSHOT})

    def __init__(self, sleep: Callable[[float], None] = _time.sleep) -> None:
        self._sleep = sleep

    def fetch(self, capability: Capability, **kwargs: Any) -> pd.DataFrame:
        timeout = float(kwargs.pop("timeout"))
        if capability == Capability.MARKET_SNAPSHOT:
            return self._market_snapshot(timeout, **kwargs)
        raise SourceUnavailable(f"腾讯实时行情不提供 {capability.value}")

    def close(self) -> None:
        return None

    def _listed(self) -> pd.DataFrame:
        sec = store.read_securities()
        return sec[(sec["kind"] == "stock") & sec["delist_date"].isna()]

    def _market_snapshot(self, timeout: float, day: date) -> pd.DataFrame:
        from app.plugins.freecn import quotes

        now = clock.now()
        if day != now.date() or now.time() < READY:
            raise SourceUnavailable("腾讯实时行情只能在当天收盘后取当天的数据")
        listed = self._listed()
        if len(listed) == 0:
            raise SourceUnavailable("本机还没有证券清单，不知道要查哪些股票")
        names = dict(zip(listed["code"].astype(str), listed["name"].astype(str), strict=False))
        codes = list(names)

        parsed: dict[str, dict[str, Any]] = {}
        requests = 0
        for i in range(0, len(codes), quotes.BATCH):
            if requests:
                self._sleep(GAP_SECONDS)
            url = quotes.URL + ",".join(_to_query(c) for c in codes[i:i + quotes.BATCH])
            requests += 1
            try:
                status, text = quotes._fetch(url, timeout)
            except Exception as e:  # 网络层的任何失败都按“暂时不可用”
                raise SourceUnavailable(f"腾讯实时行情请求失败：{type(e).__name__}: {e}") from e
            if status in (403, 429):
                raise SourceRejected(f"腾讯实时行情限制了访问（HTTP {status}）")
            if status != 200:
                raise SourceUnavailable(f"腾讯实时行情返回了 HTTP {status}")
            parsed.update(quotes.parse(text, 0))

        close_ms = int(datetime.combine(day, time(15, 0), tzinfo=_BEIJING).timestamp() * 1000)
        rows: list[dict[str, Any]] = []
        fresh = 0
        for code in codes:
            got = parsed.get(_to_panel(code))
            name = names[code]
            row: dict[str, Any] = {"date": pd.Timestamp(day), "code": code, "open": np.nan, "high": np.nan,
                                   "low": np.nan, "close": np.nan, "preclose": np.nan, "volume": 0.0, "amount": 0.0,
                                   "suspended": True, "is_st": "ST" in name.upper(), "float_shares": np.nan}
            if got is not None:
                q, extra = got["quote"], got["extra"]
                if q.get("name"):
                    row["is_st"] = "ST" in str(q["name"]).upper()
                volume = float(q["volume"] or 0.0) * _LOT
                # 行情时间不是今天收盘后的：今天没有成交（停牌），或接口还停在更早的时刻
                today = int(q["timestamp"]) >= close_ms
                traded = today and volume > 0 and all(q.get(k) for k in ("open", "high", "low"))
                if traded:
                    fresh += 1
                    last = float(q["last_price"])
                    cap = extra.get("float_mv_yi")
                    row.update({
                        "open": float(q["open"]), "high": float(q["high"]), "low": float(q["low"]), "close": last,
                        "preclose": float(q["prev_close"]), "volume": volume, "amount": float(q["amount"] or 0.0),
                        "suspended": False,
                        "float_shares": cap * 1e8 / last if cap and cap > 0 else np.nan,
                    })
            rows.append(row)
        if fresh < len(codes) * MIN_FRESH:
            raise SourceUnavailable(
                f"腾讯实时行情里只有 {fresh}/{len(codes)} 只股票的时间是今天收盘后的，当天的行情还没准备好")
        out = pd.DataFrame(rows)
        out.attrs["requests"] = requests
        return out


def _to_panel(code: str) -> str:
    """sh.600000 → 600000.SH"""
    exchange, _, digits = code.partition(".")
    return f"{digits}.{exchange.upper()}"
