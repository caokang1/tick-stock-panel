"""腾讯实时行情当“收盘后全市场行情”来源的测试。接口的返回是造的，不访问网络。"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
from app.custom.tdx_formula.ql.data.interface import Capability
from app.custom.tdx_formula.ql.data.sources import default_registry, tencent_quote
from app.plugins.freecn import quotes

DAY = date(2026, 11, 18)


def line(code: str, name: str, last: float, prev: float, volume: float, stamp: str, open_: float = 10.0,
         float_mv_yi: float = 50.0) -> str:
    f = [""] * 50
    f[1], f[2], f[3], f[4], f[5], f[6] = name, code[2:], str(last), str(prev), str(open_), str(volume)
    f[30], f[33], f[34] = stamp, str(max(last, open_)), str(min(last, open_))
    f[35] = f"{last}/{volume}/{volume * 100 * last}"
    f[44] = str(float_mv_yi)
    return f'v_{code}="' + "~".join(f) + '";'


@pytest.fixture
def source(monkeypatch):
    listed = pd.DataFrame({
        "code": ["sh.600001", "sz.000002", "sh.688003", "sz.000004", "sz.000005"],
        "name": ["甲", "*ST乙", "丙", "丁", "ST戊"],
        "list_date": pd.NaT, "delist_date": pd.NaT, "kind": "stock",
    })
    monkeypatch.setattr(tencent_quote.store, "read_securities", lambda: listed)
    clock.set_fixed(datetime(2026, 11, 18, 16, 30))
    calls: list[str] = []
    state = {"text": "", "status": 200}

    def fetch(url: str, timeout: float):
        calls.append(url)
        return state["status"], state["text"]

    quotes.set_fetch(fetch)
    src = tencent_quote.TencentQuoteSource(sleep=lambda s: None)
    yield src, state, calls
    quotes.set_fetch(None)
    clock.set_fixed(None)


AFTER, BEFORE, YESTERDAY = "20261118161500", "20261118113000", "20261117150000"


def test_registered_and_only_offers_market_snapshot():
    assert "tencent_quote" in default_registry()
    assert tencent_quote.TencentQuoteSource.capabilities == frozenset({Capability.MARKET_SNAPSHOT})


def test_snapshot_after_close_translates_units_and_marks_suspended(source):
    src, state, calls = source
    state["text"] = "".join([
        line("sh600001", "甲", 10.5, 10.0, 1000, AFTER),
        line("sz000002", "乙", 5.0, 5.2, 2000, AFTER),            # 名称里已经没有 ST：以行情里的名称为准
        line("sh688003", "丙", 20.0, 19.0, 300000, AFTER),        # 科创板接口给的是股
        line("sz000004", "丁", 8.0, 8.0, 0, YESTERDAY),           # 停牌：没有成交，时间停在昨天
    ])                                                            # 000005 接口里没有
    df = src.fetch(Capability.MARKET_SNAPSHOT, timeout=5, day=DAY).set_index("code")
    assert len(df) == 5 and df.attrs["requests"] == 1 and len(calls) == 1
    a = df.loc["sh.600001"]
    assert (a["open"], a["close"], a["preclose"], a["volume"]) == (10.0, 10.5, 10.0, 100000.0)
    assert a["amount"] == pytest.approx(1000 * 100 * 10.5) and not a["suspended"]
    assert a["float_shares"] == pytest.approx(50e8 / 10.5)
    assert df.loc["sh.688003", "volume"] == 300000.0
    assert not df.loc["sz.000002", "is_st"]
    for code in ("sz.000004", "sz.000005"):
        assert df.loc[code, "suspended"] and np.isnan(df.loc[code, "close"]) and df.loc[code, "volume"] == 0
    assert df.loc["sz.000005", "is_st"]  # 接口没给时按证券清单里的名称


def test_refuses_other_days_and_before_close(source):
    src, _state, calls = source
    with pytest.raises(SourceUnavailable):
        src.fetch(Capability.MARKET_SNAPSHOT, timeout=5, day=date(2026, 11, 17))
    clock.set_fixed(datetime(2026, 11, 18, 15, 1))
    with pytest.raises(SourceUnavailable):
        src.fetch(Capability.MARKET_SNAPSHOT, timeout=5, day=DAY)
    assert calls == []
    with pytest.raises(SourceUnavailable):
        src.fetch(Capability.STOCK_DAILY, timeout=5, code="sh.600001", start=DAY, end=DAY)


def test_refuses_when_quotes_are_not_from_after_today_close(source):
    src, state, _ = source
    state["text"] = "".join(line(c, "x", 10.5, 10.0, 1000, BEFORE) for c in ("sh600001", "sz000002", "sh688003"))
    with pytest.raises(SourceUnavailable, match="还没准备好"):
        src.fetch(Capability.MARKET_SNAPSHOT, timeout=5, day=DAY)


def test_http_errors(source):
    src, state, _ = source
    state["status"] = 403
    with pytest.raises(SourceRejected):
        src.fetch(Capability.MARKET_SNAPSHOT, timeout=5, day=DAY)
    state["status"] = 500
    with pytest.raises(SourceUnavailable):
        src.fetch(Capability.MARKET_SNAPSHOT, timeout=5, day=DAY)

    def boom(url, timeout):
        raise OSError("断网")

    quotes.set_fetch(boom)
    with pytest.raises(SourceUnavailable, match="断网"):
        src.fetch(Capability.MARKET_SNAPSHOT, timeout=5, day=DAY)
