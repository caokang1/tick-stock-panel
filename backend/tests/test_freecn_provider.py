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
    assert sorted(manifest["datasets"]) == sorted(provider.config.datasets) == ["adj_factor", "daily", "depth5", "financial", "minute", "realtime"]
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


def test_index_without_amount_is_empty_not_nan(market):
    """指数来源不带成交额时要留空：NaN 会被面板的日K自检当成坏数据，而且修补不掉。"""
    import pyarrow.parquet as pq

    path = market / "index" / "sh.000300.parquet"
    table = pq.read_table(path).to_pandas()
    table["amount"] = np.nan
    table.to_parquet(path, index=False)
    ql_store._panel_memory.clear()
    df = freecn.FreeCnProvider().get_daily(["000300.SH"], START, END, asset_type="index")
    assert df["amount"].null_count() == df.height and df["amount"].is_nan().sum() in (0, None)
    from app.services import data_integrity
    assert data_integrity.invalid_daily_rows(df).is_empty()


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
    assert "不提供这一项" in p.test_dataset("full_minute")["error"]


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


# ---------------- 财务数据（东方财富数据中心；这里用造出来的返回，不联网） ----------------
@pytest.fixture
def finance_api(market):
    """假的报表接口：2024-03-31 这一期每张报表两页、每页一家公司；别的报告期没有数据。"""
    import json

    from app.custom.tdx_formula.ql.core import clock
    from app.custom.tdx_formula.ql.data import runtime
    from app.plugins.freecn import financials

    calls: list[tuple[str, str, str]] = []
    state = {"status": 200}
    rows = {
        "RPT_LICO_FN_CPD": lambda code: {"REPORTDATE": "2024-03-31 00:00:00", "NOTICE_DATE": "2024-04-20 00:00:00",
                                         "BASIC_EPS": 0.5, "BPS": 6.0, "WEIGHTAVG_ROE": 8.2, "XSMLL": 31.5,
                                         "YSTZ": 12.0, "SJLTZ": -3.0},
        "RPT_DMSK_FN_BALANCE": lambda code: {"REPORT_DATE": "2024-03-31 00:00:00", "NOTICE_DATE": "2024-04-20 00:00:00",
                                             "TOTAL_ASSETS": 1000.0, "MONETARYFUNDS": 100.0, "ACCOUNTS_RECE": 50.0,
                                             "TOTAL_LIABILITIES": 400.0, "TOTAL_EQUITY": 600.0,
                                             "DEBT_ASSET_RATIO": 40.0},
        "RPT_DMSK_FN_INCOME": lambda code: {"REPORT_DATE": "2024-03-31 00:00:00", "NOTICE_DATE": "2024-04-20 00:00:00",
                                            "TOTAL_OPERATE_INCOME": 200.0, "OPERATE_COST": 120.0,
                                            "TOTAL_PROFIT": 50.0, "INCOME_TAX": 10.0, "PARENT_NETPROFIT": 38.0},
        "RPT_DMSK_FN_CASHFLOW": lambda code: {"REPORT_DATE": "2024-03-31 00:00:00",
                                              "NOTICE_DATE": "2024-04-20 00:00:00", "NETCASH_OPERATE": 30.0,
                                              "NETCASH_INVEST": -20.0, "NETCASH_FINANCE": 5.0, "CCE_ADD": 15.0},
    }
    codes = ["600001.SH", "000002.SZ"]

    def fetch(params: dict, timeout: float) -> tuple[int, str]:
        calls.append((params["reportName"], params["filter"][-12:-2], params["pageNumber"]))
        if state["status"] != 200:
            return state["status"], ""
        if "2024-03-31" not in params["filter"]:
            return 200, json.dumps({"success": False, "result": None})
        page = int(params["pageNumber"])
        row = {"SECUCODE": codes[page - 1], **rows[params["reportName"]](codes[page - 1])}
        return 200, json.dumps({"success": True, "result": {"pages": 2, "data": [row]}})

    financials.set_fetch(fetch)
    runtime.set_sleep(lambda seconds: None)
    clock.set_fixed(datetime(2024, 5, 10, 20, 0))
    yield calls, state
    clock.set_fixed(None)
    financials.set_fetch(None)
    runtime.set_sleep(None)


def test_financial_tables_use_tsp_column_names(finance_api):
    del finance_api  # 只需要它把假接口装上
    p = freecn.FreeCnProvider()
    symbols = ["600001.SH", "000002.SZ", "000009.SZ"]
    income = p.get_financials("income", symbols, latest_only=True)
    assert income["symbol"].to_list() == ["000002.SZ", "600001.SH"]  # 两页都取了；没有数据的股票不出现
    row = income.row(0, named=True)
    assert row["period_end"] == "2024-03-31" and row["announce_date"] == "2024-04-20"
    assert row["revenue"] == 200.0 and row["net_income_attributable"] == 38.0
    assert row["net_income"] == 40.0  # 利润总额 - 所得税
    balance = p.get_financials("balance_sheet", symbols, latest_only=True).row(0, named=True)
    assert balance["total_assets"] == 1000.0 and balance["cash_and_equivalents"] == 100.0
    cash = p.get_financials("cash_flow", symbols, latest_only=True).row(0, named=True)
    assert cash["net_operating_cash_flow"] == 30.0 and cash["net_cash_change"] == 15.0
    metrics = p.get_financials("metrics", symbols, latest_only=True).row(0, named=True)
    assert metrics["eps_basic"] == 0.5 and metrics["bps"] == 6.0
    assert metrics["roe"] == 8.2 and metrics["gross_margin"] == 31.5  # 百分数，来源和 TSP 口径一致，不换算
    assert metrics["debt_to_asset_ratio"] == 40.0 and metrics["net_margin"] == pytest.approx(20.0)
    assert p.get_financials("shares", symbols).is_empty()  # 历史股本表这里没有
    assert p.get_financials("income", ["000009.SZ"], latest_only=True).is_empty()


def test_a_report_period_is_downloaded_once_and_then_read_from_disk(finance_api):
    calls, _ = finance_api
    p = freecn.FreeCnProvider()
    p.get_financials("income", ["600001.SH"], latest_only=True)
    # 最近 3 个报告期：2024-03-31 取了 2 页，另外两期各问 1 次发现没有数据
    assert sorted(calls) == [("RPT_DMSK_FN_INCOME", "2023-09-30", "1"), ("RPT_DMSK_FN_INCOME", "2023-12-31", "1"),
                             ("RPT_DMSK_FN_INCOME", "2024-03-31", "1"), ("RPT_DMSK_FN_INCOME", "2024-03-31", "2")]
    calls.clear()
    p.get_financials("income", ["600001.SH"], latest_only=True)
    p.get_financials("metrics", ["600001.SH"], latest_only=True)  # 指标表也要用利润表，直接读本机的
    assert ("RPT_DMSK_FN_INCOME", "2024-03-31", "1") not in calls


def test_financial_refusal_pauses_the_source(finance_api):
    from app.custom.tdx_formula.ql.core.errors import SourceRejected
    from app.custom.tdx_formula.ql.data.runtime import get_runtime

    calls, state = finance_api
    state["status"] = 429
    with pytest.raises(SourceRejected):
        freecn.FreeCnProvider().get_financials("income", ["600001.SH"], latest_only=True)
    assert len(calls) == 1 and get_runtime().limiter.state_of("akshare_em") == "cooling"


def test_recent_report_periods_are_quarter_ends():
    from app.plugins.freecn import financials

    assert financials.recent_periods(date(2024, 5, 10), 3) == [date(2024, 3, 31), date(2023, 12, 31), date(2023, 9, 30)]
    assert financials.recent_periods(date(2024, 3, 30), 2) == [date(2023, 12, 31), date(2023, 9, 30)]


# ---------------- 分钟 K（新浪；这里用造出来的返回，不联网） ----------------
@pytest.fixture
def minute_api(market):
    import json

    from app.custom.tdx_formula.ql.data import runtime
    from app.plugins.freecn import minutes

    calls: list[dict] = []
    state = {"status": 200}

    def fetch(params: dict, timeout: float) -> tuple[int, str]:
        calls.append(dict(params))
        rows = [{"day": f"2024-03-14 {clock}:00", "open": "10.00", "high": "10.20", "low": "9.90", "close": "10.10",
                 "volume": "12300", "amount": "124000.5"} for clock in ("09:31", "09:32", "15:00")]
        rows.insert(0, {"day": "2024-03-13 15:00:00", "open": "9.8", "high": "9.9", "low": "9.7", "close": "9.8",
                        "volume": "500", "amount": "4900"})
        return state["status"], json.dumps(rows)

    minutes.set_fetch(fetch)
    runtime.set_sleep(lambda seconds: None)
    yield calls, state
    minutes.set_fetch(None)
    runtime.set_sleep(None)


def test_minute_bars_follow_the_tsp_contract(minute_api):
    calls, _ = minute_api
    p = freecn.FreeCnProvider()
    df = p.get_minute(["600001.SH"], datetime(2024, 3, 14), datetime(2024, 3, 14, 15, 0))
    assert df.columns == ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
    assert df["datetime"].to_list() == [datetime(2024, 3, 14, 9, 31), datetime(2024, 3, 14, 9, 32),
                                        datetime(2024, 3, 14, 15, 0)]  # 北京时间墙钟；前一天的那根在时间范围外
    assert df["volume"][0] == 123.0 and df["amount"][0] == 124000.5  # 股 → 手；成交额是元
    assert calls == [{"symbol": "sh600001", "scale": "1", "ma": "no", "datalen": "241"}]
    p.get_minute(["600001.SH"], datetime(2024, 3, 14), datetime(2024, 3, 14, 15, 0))
    assert len(calls) == 1  # 刚查过的不再重复查
    assert p.minute_history_days == 8
    assert p.get_minute(["600001.SH"], datetime(2024, 3, 14), datetime(2024, 3, 14, 15, 0), freq="5m").is_empty()


def test_whole_market_minute_sync_is_refused_before_any_request(minute_api):
    calls, _ = minute_api
    many = [f"{600000 + i}.SH" for i in range(21)]
    with pytest.raises(ProviderRequestError, match="一次只能查一只"):
        freecn.FreeCnProvider().get_minute(many, datetime(2024, 3, 14), datetime(2024, 3, 14, 15, 0))
    assert calls == []


def test_minute_refusal_is_reported_and_pauses_the_source(minute_api):
    from app.custom.tdx_formula.ql.data.runtime import get_runtime

    calls, state = minute_api
    state["status"] = 456
    with pytest.raises(ProviderRequestError, match="限制了访问"):
        freecn.FreeCnProvider().get_minute(["600001.SH"], None, None)
    assert len(calls) == 1 and get_runtime().limiter.state_of("akshare_sina") == "cooling"


def test_star_board_volume_is_converted_from_shares_to_lots():
    """科创板的成交量接口给的是股，其余是手。不换算的话科创板的量会大 100 倍，量比和换手都跟着错。"""
    from app.plugins.freecn import quotes

    text = quote_line("sh688001", "科创", 10.5, 10.0) + quote_line("sh600001", "主板", 10.5, 10.0)
    parsed = quotes.parse(text, 0)
    assert parsed["688001.SH"]["quote"]["volume"] == pytest.approx(123.45)
    assert parsed["600001.SH"]["quote"]["volume"] == 12345
    assert parsed["688001.SH"]["depth"]["bid_volumes"] == [10, 20, 30, 40, 50]  # 盘口的量本来就是手，不动
