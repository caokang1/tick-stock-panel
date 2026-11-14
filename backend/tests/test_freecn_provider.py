"""「免费公开来源」数据源插件：从本机行情目录按 TSP 的数据契约读数据。用造出来的小行情，不联网。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.custom.tdx_formula.ql.core import config as ql_config
from app.custom.tdx_formula.ql.data import store as ql_store
from app.data_providers.base import ProviderRequestError
from app.plugins.freecn import provider as freecn

DAYS = pd.bdate_range("2024-03-01", periods=10)


def write_stock(market: Path, code: str, close: np.ndarray, factor: np.ndarray, volume: float = 123_456.0) -> None:
    n = len(DAYS)
    table = pa.table({
        "date": pa.array(DAYS.date, type=pa.date32()), "code": [code] * n,
        "open": close - 0.1, "high": close + 0.2, "low": close - 0.2, "close": close,
        "preclose": np.concatenate([[np.nan], close[:-1]]),
        "volume": np.full(n, volume), "amount": close * volume, "factor": factor,
        "float_shares": np.full(n, 1e8),
        "is_st": pa.array([False] * n, type=pa.bool_()), "source": ["fake"] * n,
    })
    pq.write_table(table, market / "daily" / f"{code}.parquet")


@pytest.fixture
def market(tmp_path):
    ql_config.set_data_dir(tmp_path / "freecn")
    ql_store._panel_memory.clear()
    m = tmp_path / "freecn" / "market"
    (m / "daily").mkdir(parents=True)
    (m / "index").mkdir()
    n = len(DAYS)
    # sh.600001：第 6 个交易日 10 送 10（累计因子 1 → 2，原始价减半）；sz.000002：没有除权
    write_stock(m, "sh.600001", np.where(np.arange(n) < 5, 20.0, 10.0) + 0.1 * np.arange(n),
                np.where(np.arange(n) < 5, 1.0, 2.0))
    write_stock(m, "sz.000002", 8.0 + 0.05 * np.arange(n), np.ones(n))
    write_stock(m, "sz.000003", 5.0 + 0.0 * np.arange(n), np.ones(n))  # 已退市
    pq.write_table(pa.table({
        "code": ["sh.600001", "sz.000002", "sz.000003", "sh.000300"],
        "name": ["甲股份", "乙股份", "丙退", "沪深300"],
        "list_date": pa.array([date(2010, 1, 4), date(2012, 5, 8), date(2005, 6, 1), date(2005, 4, 8)],
                              type=pa.date32()),
        "delist_date": pa.array([None, None, date(2024, 3, 14), None], type=pa.date32()),
        "kind": ["stock", "stock", "stock", "index"],
    }), m / "securities.parquet")
    pq.write_table(pa.table({
        "date": pa.array(DAYS.date, type=pa.date32()), "code": ["sh.000300"] * n,
        "open": 3000.0 + np.arange(n), "high": 3010.0 + np.arange(n), "low": 2990.0 + np.arange(n),
        "close": 3001.0 + np.arange(n), "volume": np.full(n, 1.2e10), "amount": np.full(n, 2e11),
    }), m / "index" / "sh.000300.parquet")
    (m / "meta.json").write_text('{"latest_date": "2024-03-14", "updated_through": "2024-03-14"}', encoding="utf-8")
    yield m
    ql_store._panel_memory.clear()


START, END = datetime(2024, 3, 1), datetime(2024, 3, 31)


def test_symbol_conversion_both_ways():
    assert freecn.to_panel_symbol("sh.600000") == "600000.SH" and freecn.to_panel_symbol("bj.430047") == "430047.BJ"
    assert freecn.to_local_code("600000.SH") == "sh.600000" and freecn.to_local_code("000001.sz") == "sz.000001"
    assert freecn.to_local_code("AAPL.US") is None and freecn.to_local_code("60000.SH") is None


def test_manifest_and_provider_declare_the_same_datasets():
    import yaml

    manifest = yaml.safe_load((Path(freecn.__file__).parent / "plugin.yaml").read_text(encoding="utf-8"))
    provider = freecn.FreeCnProvider()
    assert manifest["name"] == provider.name == "freecn" and "api_key_env" not in manifest
    assert sorted(manifest["datasets"]) == sorted(provider.config.datasets) == ["adj_factor", "daily", "depth5", "realtime"]
    assert manifest["entry"] == "app.plugins.freecn.provider:FreeCnProvider"
    assert freecn.availability() == (True, "ok")


def test_daily_bars_follow_the_tsp_contract(market):
    p = freecn.FreeCnProvider()
    seen = []
    df = p.get_daily(["600001.SH", "000002.SZ", "999999.SH", "AAPL.US"], START, END,
                     on_chunk_done=lambda cur, total: seen.append((cur, total)))
    assert df.schema == {"symbol": pl.String, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
                         "low": pl.Float64, "close": pl.Float64, "volume": pl.Int64, "amount": pl.Float64}
    assert sorted(df["symbol"].unique().to_list()) == ["000002.SZ", "600001.SH"] and df.height == 20
    first = df.filter(pl.col("symbol") == "600001.SH").sort("date")
    assert first["date"][0] == date(2024, 3, 1) and first["close"][0] == 20.0  # 原始价，没有复权
    assert first["close"][5] == pytest.approx(10.5)  # 送股之后的原始价
    assert first["volume"][0] == 1234  # 本机存的是 123456 股 → 1234 手（取整）
    assert first["amount"][0] == pytest.approx(20.0 * 123_456)  # 元
    assert seen[-1] == (4, 4)
    # 只要一段日期
    part = p.get_daily(["600001.SH"], datetime(2024, 3, 6), datetime(2024, 3, 8))
    assert part["date"].to_list() == [date(2024, 3, 6), date(2024, 3, 7), date(2024, 3, 8)]


def test_index_bars_only_for_indexes_that_were_downloaded(market):
    p = freecn.FreeCnProvider()
    df = p.get_daily(["000300.SH", "000905.SH"], START, END, asset_type="index")
    assert df["symbol"].unique().to_list() == ["000300.SH"] and df.height == 10
    assert df["volume"][0] == 120_000_000  # 1.2e10 股 → 手
    assert p.get_daily(["510300.SH"], START, END, asset_type="etf").height == 0


def test_adjust_factors_are_per_event_ratios(market):
    p = freecn.FreeCnProvider()
    df = p.get_adj_factors(["600001.SH", "000002.SZ"], START, END)
    assert df.schema == {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
    # 累计因子 1 → 2 的那一天是一次除权，单次比例 2.0；没有除权的股票没有记录
    assert df.to_dicts() == [{"symbol": "600001.SH", "trade_date": DAYS[5].date(), "ex_factor": 2.0}]
    assert p.get_adj_factors(["600001.SH"], START, datetime(2024, 3, 5)).height == 0  # 窗口里没有除权
    assert p.get_adj_factors(["000300.SH"], START, END, asset_type="index").height == 0


def test_instruments_list_only_currently_listed_stocks(market):
    items = freecn.FreeCnProvider().get_instruments()
    assert [i["symbol"] for i in items] == ["600001.SH", "000002.SZ"]  # 已退市的、指数都不在里面
    assert items[0] == {"symbol": "600001.SH", "name": "甲股份", "code": "600001", "exchange": "SH", "region": "CN",
                        "type": "stock", "ext": {"listing_date": "2010-01-04"}}
    assert freecn.FreeCnProvider().get_instruments("index") == []


def test_without_local_data_it_fails_loudly_instead_of_returning_nothing(tmp_path):
    ql_config.set_data_dir(tmp_path / "empty")
    p = freecn.FreeCnProvider()
    with pytest.raises(ProviderRequestError, match="下载历史行情"):
        p.get_daily(["600001.SH"], START, END)
    with pytest.raises(ProviderRequestError):
        p.get_adj_factors(["600001.SH"], START, END)
    with pytest.raises(ProviderRequestError):
        p.get_instruments()
    result = p.test_dataset("daily", ["600001.SH"])
    assert result["rows"] == 0 and "下载历史行情" in result["error"]


def test_test_dataset_previews_rows_as_json(market):
    import json

    p = freecn.FreeCnProvider()
    result = p.test_dataset("daily", ["600001.SH"])
    assert result["provider"] == "freecn" and result["rows"] >= 0 and "error" not in result
    json.dumps(result)  # 能转成 JSON
    assert "目前只提供" in p.test_dataset("minute")["error"]


def test_cancel_signal_from_progress_callback_is_passed_through(market):
    class CancelledError(Exception):
        pass

    def cancel(cur, total):
        raise CancelledError

    with pytest.raises(CancelledError):
        freecn.FreeCnProvider().get_daily(["600001.SH"], START, END, on_chunk_done=cancel)


# ---------------- 实时行情和五档盘口（腾讯行情接口；这里用造出来的返回，不联网） ----------------
def quote_line(code: str, name: str, last: float, prev: float, stamp: str = "20240314103000") -> str:
    """造一行接口返回。位置含义见 app/plugins/freecn/quotes.py 的说明。"""
    f = [""] * 50
    f[0], f[1], f[2], f[3], f[4], f[5], f[6] = "1", name, code[2:], str(last), str(prev), str(prev), "12345"
    for k in range(5):  # 买一到买五、卖一到卖五
        f[9 + 2 * k], f[10 + 2 * k] = f"{last - 0.01 * (k + 1):.2f}", str(10 * (k + 1))
        f[19 + 2 * k], f[20 + 2 * k] = f"{last + 0.01 * (k + 1):.2f}", str(11 * (k + 1))
    f[30], f[31], f[32] = stamp, f"{last - prev:.2f}", f"{(last / prev - 1) * 100:.2f}"
    f[33], f[34], f[35], f[37], f[38] = str(last + 0.5), str(prev - 0.5), f"{last}/12345/9876543", "987.65", "1.50"
    return f'v_{code}="' + "~".join(f) + '";'


@pytest.fixture
def quote_api(market):
    """假的行情接口：记下每次请求的地址，按地址里的代码造返回。限流器的等待换成不等。"""
    from app.custom.tdx_formula.ql.data import runtime
    from app.plugins.freecn import quotes

    calls: list[str] = []
    state = {"status": 200}

    def fetch(url: str, timeout: float) -> tuple[int, str]:
        calls.append(url)
        codes = url.split("q=")[1].split(",")
        return state["status"], "\n".join(quote_line(c, "名" + c, 10.5, 10.0) for c in codes)

    quotes.set_fetch(fetch)
    runtime.set_sleep(lambda seconds: None)
    yield calls, state
    quotes.set_fetch(None)
    runtime.set_sleep(None)


def test_realtime_snapshot_follows_the_tsp_contract(quote_api):
    calls, _ = quote_api
    rows = {r["symbol"]: r for r in freecn.FreeCnProvider().get_realtime()}
    assert sorted(rows) == ["000002.SZ", "600001.SH"]  # 还在上市的股票；已退市的和指数不在全市场快照里
    r = rows["600001.SH"]
    assert r["last_price"] == 10.5 and r["prev_close"] == 10.0
    assert r["volume"] == 12345  # 手，不再换算
    assert r["amount"] == 9876543.0  # 元
    assert r["change_pct"] == pytest.approx(0.05) and r["turnover_rate"] == pytest.approx(0.015)  # 小数制
    assert r["timestamp"] == int(datetime.fromisoformat("2024-03-14T10:30:00+08:00").timestamp() * 1000)
    assert len(calls) == 1 and "sh600001" in calls[0] and "sz000002" in calls[0]


def test_asking_again_within_the_interval_sends_no_new_request(quote_api):
    calls, _ = quote_api
    p = freecn.FreeCnProvider()
    first = p.get_realtime()
    for _ in range(5):
        assert p.get_realtime() == first
    assert len(calls) == 1
    # 五档盘口直接用同一轮快照里带的数据
    depth = p.get_depth_batch(["600001.SH"])["600001.SH"]
    assert depth["bid_prices"][0] == 10.49 and depth["ask_prices"][0] == 10.51
    assert depth["bid_volumes"] == [10, 20, 30, 40, 50] and depth["ask_volumes"] == [11, 22, 33, 44, 55]
    assert len(calls) == 1


def test_indices_and_extra_symbols_are_fetched_only_when_missing(quote_api):
    calls, _ = quote_api
    p = freecn.FreeCnProvider()
    p.get_realtime()
    assert [r["symbol"] for r in p.get_realtime_symbols(["600001.SH"])] == ["600001.SH"] and len(calls) == 1
    assert [r["symbol"] for r in p.get_realtime_indices(["000300.SH"])] == ["000300.SH"] and len(calls) == 2
    assert [r["symbol"] for r in p.get_realtime_symbols(["510300.SH"], asset_type="etf")] == ["510300.SH"]
    assert p.get_realtime_symbols([]) == [] and len(calls) == 3


def test_a_refusal_pauses_the_source_and_nothing_more_is_sent(quote_api):
    from app.custom.tdx_formula.ql.data.runtime import get_runtime

    calls, state = quote_api
    state["status"] = 403
    p = freecn.FreeCnProvider()
    assert p.get_realtime() == []  # 软失败：不抛错，不打断 TSP 的轮询线程
    assert get_runtime().limiter.state_of("tencent_quote") == "cooling"
    assert p.get_realtime_indices(["000300.SH"]) is None  # 失败和“成功但没有数据”要分得开
    assert len(calls) == 1  # 被拒绝后不重试，暂停期间一个请求都不发


def test_rows_without_a_traded_price_are_skipped():
    from app.plugins.freecn import quotes

    text = quote_line("sh600001", "停牌", 0.0, 10.0) + quote_line("sz000002", "正常", 8.0, 8.0) + 'v_pv_none_match="1";'
    assert list(quotes.parse(text, 0)) == ["000002.SZ"]
    assert quotes.to_query_code("600001.SH") == "sh600001" and quotes.to_query_code("AAPL.US") is None
