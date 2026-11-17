"""复盘补充数据扩展的测试。本机数据是造的，联网的部分换成假的，不访问网络。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.custom.recap_plus import EXTENSION_API_VERSION, EXTENSION_ID, context, remote
from app.custom.recap_plus.routes import recap_context
from app.extensions import BACKEND_EXTENSION_API_VERSION

DAYS = [f"2024-03-{d:02d}" for d in (11, 12, 13, 14)]


def write_market(data_dir: Path) -> None:
    rows = {
        "600001.SH": dict(name="沪一", shares=1e9, closes=[10.0, 10.0, 10.0, 11.0], amount=5e8, ups=1, downs=0),
        "000002.SZ": dict(name="深二", shares=5e9, closes=[20.0, 20.0, 20.0, 18.0], amount=9e8, ups=0, downs=1),
        "000003.SZ": dict(name="深三", shares=2e8, closes=[5.0, 5.0, 5.0, 5.5], amount=1e8, ups=2, downs=0),
    }
    for i, day in enumerate(DAYS):
        folder = data_dir / "kline_daily_enriched" / f"date={day}"
        folder.mkdir(parents=True)
        last = i == len(DAYS) - 1
        pd.DataFrame([{
            "symbol": s, "date": day, "close": r["closes"][i], "raw_close": r["closes"][i],
            "amount": r["amount"] * (1.0 if last else 0.5), "turnover_rate": 1.5,
            "consecutive_limit_ups": r["ups"] if last else 0, "consecutive_limit_downs": r["downs"] if last else 0,
        } for s, r in rows.items()]).to_parquet(folder / "part.parquet")
    (data_dir / "instruments").mkdir()
    pd.DataFrame([{"symbol": s, "name": r["name"], "total_shares": r["shares"]} for s, r in rows.items()]).to_parquet(
        data_dir / "instruments" / "instruments.parquet")
    for table, column, values in (("ext_hy_ths", "所属同花顺行业", ["银行", "地产", "银行"]),
                                  ("ext_gn_ths", "所属概念", ["甲;乙", "丙", "丁"])):
        (data_dir / "ext_data" / table).mkdir(parents=True)
        pd.DataFrame({"股票代码": list(rows), "股票简称": [r["name"] for r in rows.values()], column: values,
                      "symbol": list(rows), "code": [s[:6] for s in rows]}).to_parquet(
            data_dir / "ext_data" / table / "part.parquet")
    lots = data_dir / "user_data" / "lots"
    lots.mkdir(parents=True)
    for n, (qty, cost) in enumerate([(1000, 8.0), (1000, 10.0)]):  # 同一只股票的两笔，合成一笔
        (lots / f"lot_{n}.json").write_text(json.dumps({"id": f"lot_{n}", "symbol": "600001.SH", "qty": qty,
                                                         "cost_price": cost}), encoding="utf-8")
    (lots / "lot_9.json").write_text(json.dumps({"id": "lot_9", "symbol": "000002.SZ"}), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.custom.tdx_formula.ql.core import config as ql_config
    from app.custom.tdx_formula.ql.data import runtime

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_market(data_dir)
    ql_config.set_data_dir(data_dir / "freecn")
    runtime.set_sleep(lambda seconds: None)
    calls: list[dict[str, str]] = []
    state = {"fail_flow": False}

    def fetch(url: str, params: dict[str, str], timeout: float) -> tuple[int, str]:
        calls.append({"url": url, **params})
        if "anotice" in url:
            if state.get("fail_notices"):
                return 500, ""
            return 200, json.dumps({"data": {"list": [
                {"art_code": "AN1", "title": "沪一:关于回购股份的公告", "display_time": "2024-03-14 18:30:11:000",
                 "notice_date": "2024-03-15 00:00:00", "columns": [{"column_name": "回购"}],
                 "codes": [{"stock_code": "600001", "short_name": "沪一"}, {"stock_code": "999999", "short_name": "别人"}]},
                {"art_code": "AN0", "title": "深二:年度报告", "display_time": "2024-03-01 17:00:00:000",
                 "notice_date": "2024-03-02 00:00:00", "columns": [],
                 "codes": [{"stock_code": "000002", "short_name": "深二"}]},
                {"art_code": "", "title": "没有编号的不要"}]}})
        if "fflow" in url:
            if state["fail_flow"]:
                return 500, ""
            lines = [f"2024-03-{d:02d},{d * 1e6},-1,-2,3,4" for d in range(1, 15)]
            return 200, json.dumps({"data": {"klines": lines}})
        if params["reportName"] == "RPTA_RZRQ_LSHJ":
            return 200, json.dumps({"result": {"data": [
                {"DIM_DATE": "2024-03-13 00:00:00", "RZYE": 2.2e12, "RZMRE": 1.3e11, "RZJME": -1e10, "RQYE": 3e10,
                 "RZRQYE": 2.23e12},
                {"DIM_DATE": "2024-03-12 00:00:00", "RZYE": 2.0e12, "RZMRE": 1.1e11}]}})
        return 200, json.dumps({"result": {"data": [
            {"DATE": "2024-03-13 00:00:00", "RZYE": 3e8, "RZMRE": 9e6, "RZJME": -2e6, "RQYE": 5e6, "RZYEZB": 3.4}]}})

    remote.set_fetch(fetch)
    remote.set_quotes(lambda symbols: {s: {"extra": {"pe_ttm": -5.0, "pb": 2.0, "total_mv_yi": 100.0,
                                                    "float_mv_yi": 80.0}} for s in symbols})
    yield data_dir, calls, state
    remote.set_fetch(None)
    remote.set_quotes(None)
    runtime.set_sleep(None)


def test_identity():
    assert EXTENSION_ID == "community.recap-plus" and EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION


def test_material_covers_rankings_limits_margin_and_holdings(env):
    data_dir, calls, _ = env
    root = data_dir / "user_data" / "extensions" / "recap_plus"
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"peers": {"600001.SH": ["000003.SZ"]}}), encoding="utf-8")
    text = recap_context(data_dir, "2024-03-14")

    assert "沪市 5.00 亿 + 深市 10.00 亿 = 15.00 亿" in text and "前一交易日 7.50 亿" in text
    assert "| 深二 | 9.00 亿 | -10.00% | 地产 |" in text  # 成交额第一
    assert "| 深二 | 900 亿 | -10.00% |" in text  # 总市值 = 18 × 50 亿股
    assert "涨停 2 家，跌停 1 家" in text and "| 银行 | 2 | 0 | 深三(2板)、沪一 | — |" in text
    assert "数据日期 2024-03-13：融资余额 22000.00 亿" in text and "环比 +10.00%" in text
    # 持仓：两笔合成一笔，成本按数量加权
    assert "持仓：2000 股，市值 2.20 万，成本 9，浮动盈亏 +0.40 万（+22.22%）" in text
    assert "亏损（市盈率为负）" in text and "近5日 +6000.00 万" in text and "对标股票当日：深三 +10.00%" in text
    assert "成本没有登记" in text  # 只登记了代码的那只
    assert "## 持仓公告" in text and "2024-03-14 18:30 沪一：关于回购股份的公告（回购）" in text
    assert "年度报告" not in text  # 太早的公告不进复盘材料
    # 1 个全市场两融 + 每只持仓各 1 个两融、1 个资金流向 + 1 个公告列表
    assert len(calls) == 6

    # 当天再跑一次：用存下来的，不重复请求
    assert recap_context(data_dir, "2024-03-14") == text and len(calls) == 6


def test_a_failed_source_is_reported_and_not_retried(env):
    data_dir, calls, state = env
    state["fail_flow"] = True
    text = recap_context(data_dir, "2024-03-14")
    assert "主力资金：没有取到" in text and "数据缺口：主力资金流向" in text
    # 第一只失败后（限流器自己按设定重试过），不再给第二只发
    assert len({c["secid"] for c in calls if "fflow" in c["url"]}) == 1
    assert "数据日期 2024-03-13" in text  # 之前取到的照常给出


def test_no_data_for_the_day_gives_nothing(env):
    data_dir, calls, _ = env
    assert recap_context(data_dir, "2024-03-20") == "" and recap_context(None, "2024-03-14") == ""
    assert calls == []


def test_the_recap_prompt_carries_the_extra_material():
    from app.services.market_recap import _build_user_prompt

    prompt = _build_user_prompt({"as_of": "2024-03-14"}, [], "", extra_context="## 持仓明细\n- 甲")
    assert "## 持仓明细\n- 甲" in prompt
    assert "## 持仓明细" not in _build_user_prompt({"as_of": "2024-03-14"}, [], "")


def test_helpers():
    assert context.wan(123456) == "+12.35 万" and context.wan(-2.5e8) == "-2.50 亿" and context.amt(5e6) == "500.00 万"
    assert context.pct(None) == "—" and context.yi(None) == "—"


def test_dashboard_gives_the_recent_trend_and_margin_series(env):
    from app.custom.recap_plus import trend

    data_dir, calls, _ = env
    index = data_dir / "kline_index_daily"
    for day, close in zip(DAYS, [3000.0, 3030.0, 3030.0, 2999.7], strict=True):
        (index / f"date={day}").mkdir(parents=True)
        pd.DataFrame([{"symbol": "000001.SH", "close": close}, {"symbol": "399001.SZ", "close": 1.0}]).to_parquet(
            index / f"date={day}" / "part.parquet")
    root = data_dir / "user_data" / "extensions" / "recap_plus"
    result = trend.build(data_dir, root)

    assert result["as_of"] == "2024-03-14" and [d["date"] for d in result["days"]] == DAYS
    last = result["days"][-1]
    assert last["sh_close"] == 2999.7 and last["sh_change"] == pytest.approx(-0.01)
    assert last["amount"] == 15e8 and last["amount_change"] == 7.5e8
    assert (last["limit_up"], last["limit_down"]) == (2, 1) and (last["up"], last["down"], last["flat"]) == (2, 1, 0)
    assert result["days"][0]["up"] is None  # 窗口里的第一天没有前一天可比
    # 两融：旧的在前；只请求一次，当天再看用存下来的
    assert [m["date"] for m in result["margin"]] == ["2024-03-12", "2024-03-13"] and result["margin_error"] is None
    assert trend.build(data_dir, root)["margin"] == result["margin"] and len(calls) == 1
    # 指定日期只看到那一天为止
    assert trend.build(data_dir, root, "2024-03-12")["as_of"] == "2024-03-12"


def test_holding_notices_are_cached_and_survive_a_failed_refresh(env):
    from datetime import datetime, timedelta

    from app.custom.recap_plus import notices

    data_dir, calls, state = env
    root = data_dir / "user_data" / "extensions" / "recap_plus"
    now = datetime(2024, 3, 14, 19, 0)
    held = ["600001.SH", "000002.SZ"]
    first = notices.latest(root, held, now)
    assert first["error"] is None and [n["id"] for n in first["items"]] == ["AN1", "AN0"]  # 新的在前；别人的股票不要
    top = first["items"][0]
    assert top["name"] == "沪一" and top["title"] == "关于回购股份的公告" and top["kind"] == "回购"
    assert top["published_at"] == "2024-03-14 18:30"
    assert top["url"] == "https://data.eastmoney.com/notices/detail/600001/AN1.html"

    # 半小时内不重复请求；过了半小时、或者点了刷新才再取
    assert notices.latest(root, held, now + timedelta(minutes=29))["items"] == first["items"] and len(calls) == 1
    notices.latest(root, held, now + timedelta(minutes=31))
    notices.latest(root, held, now + timedelta(minutes=32), force=True)
    assert len(calls) == 3

    # 取不到时给出上次存下来的，并说明原因
    state["fail_notices"] = True
    failed = notices.latest(root, held, now + timedelta(hours=2))
    assert failed["items"] == first["items"] and "HTTP 500" in failed["error"]
    assert notices.latest(root, [], now) == {"items": [], "fetched_at": None, "error": None, "codes": []}
    # 只看近三天：3 月 14 日的留下，3 月 1 日的不要
    assert [n["id"] for n in notices.within_days(first["items"], now, 3)] == ["AN1"]
    assert notices.within_days(first["items"], now + timedelta(days=3), 3) == []


def test_scheduled_recap_is_skipped_only_when_the_day_was_recapped_after_close():
    from datetime import date

    from app.custom.recap_plus import sync

    day = date(2026, 11, 17)
    assert sync.already_done([], day) is False
    assert sync.already_done([{"as_of": "2026-11-17", "created_at": "2026-11-17T16:20:00"}], day) is True
    # 盘中生成的不算：当天的行情还没走完
    assert sync.already_done([{"as_of": "2026-11-17", "created_at": "2026-11-17T11:00:00"}], day) is False
    # 今天生成的，但复盘的是前一天的数据：今天的仍然要跑
    assert sync.already_done([{"as_of": "2026-11-16", "created_at": "2026-11-17T17:21:00"}], day) is False
    assert sync.already_done([{"as_of": "2026-11-17", "created_at": "乱码"}], day) is False
    assert sync.already_done([{"as_of": "2026-11-17", "created_at": "2026-11-17T16:20:00"}], None) is False


def test_manual_recap_fetches_todays_snapshot_only_when_it_is_missing(monkeypatch):
    import types
    from datetime import date, datetime

    from app.custom.recap_plus import sync

    calls = []
    state = {"latest": date(2026, 11, 16), "traded": True, "source": "freecn"}
    repo = types.SimpleNamespace(latest_enriched_date=lambda: state["latest"])
    quotes = types.SimpleNamespace(refresh=lambda: calls.append("refresh"))
    monkeypatch.setattr(sync, "_daily_source", lambda: state["source"])
    monkeypatch.setattr(sync, "_traded_today", lambda now: state["traded"])
    tuesday = datetime(2026, 11, 17, 16, 0)

    assert sync.ensure_today(repo, quotes, tuesday) is True and calls == ["refresh"]  # 收盘后、当天的还没有：取一次
    state["latest"] = date(2026, 11, 17)
    assert sync.ensure_today(repo, quotes, tuesday) is False  # 当天的已经有了：不重复取
    state["latest"] = date(2026, 11, 16)
    assert sync.ensure_today(repo, quotes, datetime(2026, 11, 17, 9, 0)) is False  # 还没开盘
    assert sync.ensure_today(repo, quotes, datetime(2026, 11, 21, 16, 0)) is False  # 周六
    state["traded"] = False
    assert sync.ensure_today(repo, quotes, tuesday) is False  # 节假日：今天没有成交
    state.update(traded=True, source="tickflow")
    assert sync.ensure_today(repo, quotes, tuesday) is False  # 日 K 不是免费公开来源：不管
    assert sync.ensure_today(repo, None, tuesday) is False and calls == ["refresh"]

    def boom():
        raise RuntimeError("取不到")

    state["source"] = "freecn"
    assert sync.ensure_today(repo, types.SimpleNamespace(refresh=boom), tuesday) is False  # 出错不往外抛
