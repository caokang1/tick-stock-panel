"""通达信公式扩展：用造出来的小行情目录测试，不读真实数据，不联网。"""

from __future__ import annotations

import json
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.tdx_formula import EXTENSION_API_VERSION, EXTENSION_ID
from app.custom.tdx_formula.ql.data import store as ql_store
from app.custom.tdx_formula.routes import PREFIX, build_router
from app.extensions import BACKEND_EXTENSION_API_VERSION

DAYS = pd.bdate_range("2024-01-02", periods=60)
CODES = ("sh.600001", "sh.600002", "sz.000003")


def make_market(root: Path, where: str = "tsp-data/freecn") -> Path:
    """三只股票 60 个交易日。sh.600001 一路上涨，sh.600002 一路下跌，sz.000003 先跌后涨。
    默认直接造在扩展自己的行情目录里（TSP 数据目录下的 freecn）。"""
    market = root / where / "market"
    (market / "daily").mkdir(parents=True)
    n = len(DAYS)
    closes = {
        "sh.600001": 10.0 * 1.01 ** np.arange(n),
        "sh.600002": 20.0 * 0.99 ** np.arange(n),
        "sz.000003": np.concatenate([8.0 * 0.99 ** np.arange(30), 8.0 * 0.99**29 * 1.02 ** np.arange(1, 31)]),
    }
    for code, close in closes.items():
        close = np.round(close, 2)
        pre = np.concatenate([[np.nan], close[:-1]])
        table = pa.table({
            "date": pa.array(DAYS.date, type=pa.date32()), "code": [code] * n,
            "open": close, "high": close + 0.05, "low": close - 0.05, "close": close, "preclose": pre,
            "volume": np.full(n, 2e6), "amount": close * 2e6, "factor": np.ones(n),
            "float_shares": np.full(n, 1e8),
            "is_st": pa.array([False] * n, type=pa.bool_()), "source": ["fake"] * n,
        })
        pq.write_table(table, market / "daily" / f"{code}.parquet")
    pq.write_table(pa.table({"date": pa.array(DAYS.date, type=pa.date32())}), market / "calendar.parquet")
    pq.write_table(pa.table({
        "code": list(CODES), "name": ["甲股份", "乙股份", "丙股份"],
        "list_date": pa.array([DAYS[0].date()] * 3, type=pa.date32()),
        "delist_date": pa.array([None] * 3, type=pa.date32()), "kind": ["stock"] * 3,
    }), market / "securities.parquet")
    pq.write_table(pa.table({"code": list(CODES), "industry": ["货币金融服务", "医药制造业", ""]}),
                   market / "industry.parquet")
    latest = str(DAYS[-1].date())
    (market / "meta.json").write_text(f'{{"latest_date": "{latest}", "updated_through": "{latest}"}}',
                                      encoding="utf-8")
    done = {"daily": "done", "factor": "done", "st": "done", "shares": "done"}
    (market / "progress.json").write_text(json.dumps({
        "start": str(DAYS[0].date()), "calendar": True, "securities": True, "indices": {},
        "stocks": {c: done for c in CODES}, "finished": True}), encoding="utf-8")
    return market


@pytest.fixture
def env(tmp_path, monkeypatch):
    ql_store._panel_memory.clear()
    app = FastAPI()
    app.state.repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path / "tsp-data"))
    app.include_router(build_router(background=False))
    with TestClient(app) as client:
        yield client, tmp_path
    ql_store._panel_memory.clear()


ABOVE_MA = {"pasted": {"name": "站上均线", "formula": "C>MA(C,{n});",
                       "params_spec": [{"key": "n", "label": "周期", "default": 5, "min": 2, "max": 60, "step": 1}]},
            "enabled": True, "params": {"n": 5}}
ALL = {k: False for k in ("exclude_st", "exclude_star", "exclude_chinext", "exclude_bse", "exclude_suspended")}


def test_extension_declares_a_valid_identity():
    assert EXTENSION_ID == "community.tdx-formula" and EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION


def test_without_data_the_page_is_told_what_to_do(env):
    client, root = env
    status = client.get(f"{PREFIX}/status").json()
    assert status["has_data"] is False and status["stock_count"] == 0
    assert status["data_dir"] == str(root / "tsp-data" / "freecn")
    r = client.post(f"{PREFIX}/stats", json={"conditions": [ABOVE_MA], "universe": ALL})
    assert r.status_code == 400 and "下载历史行情" in r.json()["detail"]


def test_status_reads_the_extension_own_market_folder(env):
    client, root = env
    make_market(root)
    status = client.get(f"{PREFIX}/status").json()
    assert status["has_data"] and status["stock_count"] == 3 and status["latest_date"] == str(DAYS[-1].date())
    assert status["st_known"] and status["float_shares_ready"] and status["download_finished"]


def test_existing_market_data_can_be_imported_instead_of_downloading_again(env):
    client, root = env
    source = make_market(root, where="quant-lab/data")  # 例如已有的 quant-lab 行情目录
    before = sorted(str(p.relative_to(source)) for p in source.rglob("*"))
    r = client.post(f"{PREFIX}/data/import", json={"path": str(root / "nowhere")})
    assert r.status_code == 400 and "没有找到" in r.json()["detail"]
    done = client.post(f"{PREFIX}/data/import", json={"path": str(source.parent)}).json()  # 给 data 目录也行
    assert done["has_data"] and done["stock_count"] == 3 and done["copied"] >= 7
    assert sorted(str(p.relative_to(source)) for p in source.rglob("*")) == before  # 原目录没有被改动
    stats = client.post(f"{PREFIX}/stats", json={"conditions": [ABOVE_MA], "universe": ALL})
    assert stats.status_code == 200 and stats.json()["frequency"]["total"] > 0
    again = client.post(f"{PREFIX}/data/import", json={"path": str(root / "tsp-data" / "freecn")})
    assert again.status_code == 400 and "正在用的" in again.json()["detail"]


def test_parse_splits_a_formula_into_adjustable_conditions(env):
    client, _ = env
    body = client.post(f"{PREFIX}/parse", json={"formula": "A:=C>MA(C,20);\nB:=V>MA(V,5)*1.5;\nXG:A AND B;"}).json()
    assert len(body["conditions"]) == 2
    first = body["conditions"][0]
    assert "{p1}" in first["formula"] and first["params"][0]["default"] == 20
    assert client.post(f"{PREFIX}/parse", json={"formula": "  "}).status_code == 400


def test_formula_using_future_data_is_refused_with_details(env):
    client, _ = env
    r = client.post(f"{PREFIX}/parse", json={"formula": "XG:C>REFX(C,3);"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["kind"] == "lookahead" and "REFX" in detail["names"] and "未来" in detail["message"]


def test_stats_signals_and_picks(env):
    client, root = env
    make_market(root)
    body = {"conditions": [ABOVE_MA], "universe": ALL}
    stats = client.post(f"{PREFIX}/stats", json=body).json()
    assert stats["range"] == {"start": str(DAYS[0].date()), "end": str(DAYS[-1].date())} and stats["st_known"]
    rows = {(r["entry"], r["hold"]): r for r in stats["stats"]}
    assert set(h for _, h in rows) == {1, 3, 5, 10} and stats["frequency"]["total"] > 0
    # 一路上涨的股票天天满足"站上均线"，买入后都是赚的（扣掉成本后 5 天约 4.9%）
    assert rows[("next_open", 5)]["win_rate"] > 0.5 and rows[("next_open", 5)]["count"] > 0

    signals = client.post(f"{PREFIX}/signals", json={**body, "limit": 5}).json()
    assert signals["total"] == stats["frequency"]["total"] and len(signals["items"]) == 5
    assert signals["items"][0]["date"] == str(DAYS[-1].date()) and signals["items"][0]["name"]

    picks = client.post(f"{PREFIX}/picks", json=body).json()
    assert picks["date"] == str(DAYS[-1].date())
    assert [p["code"] for p in picks["items"]] == ["sh.600001", "sz.000003"]  # 最新一天在 5 日线上方的两只
    first = picks["items"][0]
    assert first["name"] == "甲股份" and first["industry"] == "货币金融服务"
    assert first["float_cap"] == pytest.approx(first["close"] * 1e8) and first["change"] == pytest.approx(0.01, abs=2e-3)

    # 股票范围：排除行业、限定流通市值
    banned = client.post(f"{PREFIX}/picks", json={**body, "universe": {**ALL, "exclude_industries": ["货币金融服务"]}})
    assert [p["code"] for p in banned.json()["items"]] == ["sz.000003"]
    small = client.post(f"{PREFIX}/picks", json={**body, "universe": {**ALL, "max_float_cap": 15}})
    assert [p["code"] for p in small.json()["items"]] == ["sz.000003"]

    assert client.post(f"{PREFIX}/stats", json={"conditions": [], "universe": ALL}).status_code == 400
    r = client.post(f"{PREFIX}/stats", json={**body, "start": "二〇二四"})
    assert r.status_code == 400 and "格式" in r.json()["detail"]


def test_blocks_lists_the_indicator_library(env):
    client, _ = env
    blocks = client.get(f"{PREFIX}/blocks").json()
    assert len(blocks) >= 30 and {"id", "name", "formula", "params", "category"} <= set(blocks[0])
    options = client.get(f"{PREFIX}/options").json()
    assert {b["id"] for b in options["rank_blocks"]} >= {"rank_amount", "rank_float_cap"}
    assert {b["id"] for b in options["timing_blocks"]} >= {"timing_above_ma"}
    assert options["indexes"][0] == {"code": "sh.000300", "name": "沪深300"}


def test_a_fake_repository_never_becomes_a_directory_on_disk(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.state.repo = MagicMock()
    app.include_router(build_router(background=False))
    with TestClient(app) as client:
        assert client.get(f"{PREFIX}/status").status_code == 503
        r = client.post(f"{PREFIX}/strategies", json={"name": "x", "config": {"conditions": [ABOVE_MA]}})
        assert r.status_code == 503
    assert list(tmp_path.iterdir()) == []


# ---------------- 第二步：策略、组合回测、每日提醒、实际持仓 ----------------
TRADE = {"max_positions": 2, "max_hold_days": 3, "take_profit": None, "stop_loss": None}


def make_index(market: Path) -> None:
    (market / "index").mkdir(exist_ok=True)
    n = len(DAYS)
    close = 3000.0 + np.arange(n)
    pq.write_table(pa.table({
        "date": pa.array(DAYS.date, type=pa.date32()), "code": ["sh.000300"] * n,
        "open": close, "high": close + 5, "low": close - 5, "close": close,
        "volume": np.full(n, 1e9), "amount": np.full(n, 1e11),
    }), market / "index" / "sh.000300.parquet")


def set_latest(market: Path, day: str) -> None:
    (market / "meta.json").write_text(f'{{"latest_date": "{day}", "updated_through": "{day}"}}', encoding="utf-8")


@pytest.fixture
def live_env(env):
    from datetime import datetime

    from app.custom.tdx_formula.ql.core import clock

    client, root = env
    market = make_market(root)
    make_index(market)
    set_latest(market, str(DAYS[-1].date()))
    clock.set_fixed(datetime(2024, 4, 1, 20, 0))
    yield client, market
    clock.set_fixed(None)


def new_strategy(client, name="均线策略", **config) -> str:
    body = {"name": name, "config": {"conditions": [ABOVE_MA], "universe": ALL, "trade": TRADE,
                                     "formula": "C>MA(C,5);", **config}}
    r = client.post(f"{PREFIX}/strategies", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_strategy_crud_keeps_the_pasted_formula(live_env):
    client, _ = live_env
    sid = new_strategy(client)
    detail = client.get(f"{PREFIX}/strategies/{sid}").json()
    assert detail["status"] == "draft" and detail["config"]["formula"] == "C>MA(C,5);"
    assert detail["config"]["trade"]["max_positions"] == 2
    dup = client.post(f"{PREFIX}/strategies", json={"name": "均线策略", "config": {"conditions": [ABOVE_MA]}})
    assert dup.status_code == 400 and "换个名字" in dup.json()["detail"]
    renamed = client.put(f"{PREFIX}/strategies/{sid}", json={"name": "改名了"}).json()
    assert renamed["name"] == "改名了" and renamed["config"]["formula"] == "C>MA(C,5);"
    (row,) = client.get(f"{PREFIX}/strategies").json()
    assert row["name"] == "改名了" and row["condition_count"] == 1 and row["live"] is None
    assert client.delete(f"{PREFIX}/strategies/{sid}").json() == {"ok": True}
    assert client.get(f"{PREFIX}/strategies/{sid}").status_code == 404


def test_portfolio_backtest_on_quant_lab_data(live_env):
    client, _ = live_env
    sid = new_strategy(client)
    body = {"conditions": [ABOVE_MA], "universe": ALL, "trade": TRADE, "strategy_id": sid,
            "timing": {"block_id": "timing_above_ma", "params": {"n": 5}, "index_code": "sh.000300",
                       "on_fail": "stop_buy"}}
    result = client.post(f"{PREFIX}/backtest", json=body).json()
    assert result["range"]["end"] == str(DAYS[-1].date()) and result["benchmark"]["name"] == "沪深300"
    assert result["trade_count"] > 0 and result["summary"]["total_return"] is not None
    assert len(result["curve"]["equity"]) == len(DAYS) and result["summary"]["ok_day_ratio"] > 0.5
    assert client.get(f"{PREFIX}/strategies/{sid}/backtest").json()["created_at"] == result["created_at"]
    (row,) = client.get(f"{PREFIX}/strategies").json()
    assert row["backtest"]["annual_return"] == result["summary"]["annual_return"]
    empty = client.post(f"{PREFIX}/backtest", json={"conditions": [], "universe": ALL})
    assert empty.status_code == 400 and "至少要启用一个选股条件" in empty.json()["detail"]


def test_start_makes_the_first_reminder_and_daily_job_follows_new_data(live_env, monkeypatch):
    from app.custom.tdx_formula.live_service import LiveService

    client, market = live_env
    sid = new_strategy(client)
    started = client.post(f"{PREFIX}/strategies/{sid}/start").json()
    assert started["status"] == "running"
    today = client.get(f"{PREFIX}/today").json()
    (block,) = today["strategies"]
    assert block["reminder"]["date"] == str(DAYS[-1].date()) and not block["behind"] and block["real"] is None
    assert [b["code"] for b in block["reminder"]["buys"]] == ["sh.600001", "sz.000003"]
    assert client.delete(f"{PREFIX}/strategies/{sid}").status_code == 400  # 运行中的不能删

    # 行情没有新的一天：每日任务不该跑
    sent: list[str] = []
    monkeypatch.setattr(LiveService, "_send", staticmethod(lambda text: sent.append(text) or ["飞书"]))
    first = client.post(f"{PREFIX}/daily/run").json()
    assert first["ok"] and first["data_date"] == str(DAYS[-1].date()) and first["pushed"]
    assert "【通达信公式 3月25日 收盘提醒】" in sent[0] and "均线策略" in sent[0] and "买入：甲股份" in sent[0]
    again = client.post(f"{PREFIX}/daily/run").json()
    assert again["ok"] and not again["pushed"] and len(sent) == 1  # 同一天只推送一次

    account = client.get(f"{PREFIX}/strategies/{sid}/account").json()
    assert account["initial_cash"] == 1_000_000 and account["curve"]["equity"] == [1.0]
    backups = list((market.parents[2] / "tsp-data" / "user_data" / "extensions" / "tdx_formula" / "backups").glob("*"))
    assert [p.name for p in backups] == ["tdx-2024-04-01.db"]  # 每日任务开始前自动备份


def test_background_check_only_runs_when_new_data_arrives(live_env, monkeypatch):
    from app.custom.tdx_formula.live_service import LiveService

    client, market = live_env
    sid = new_strategy(client)
    client.get(f"{PREFIX}/today")
    svc: LiveService = None  # type: ignore[assignment]
    calls: list[int] = []
    original = LiveService.run_daily

    def spy(self, sender=None):
        nonlocal svc
        svc = self
        calls.append(1)
        return original(self, sender=lambda text: [])

    monkeypatch.setattr(LiveService, "run_daily", spy)
    # 取到服务对象：没有运行中的策略时不到期
    client.post(f"{PREFIX}/daily/run")
    assert svc.is_due() is False and len(calls) == 1
    client.post(f"{PREFIX}/strategies/{sid}/start")
    svc.tick()
    assert len(calls) == 1  # 手动跑过、行情没有新的一天：不重复跑
    set_latest(market, "2024-03-26")  # quant-lab 说行情到了新的一天
    assert svc.is_due() is True
    svc.tick()
    assert len(calls) == 2


def test_real_positions_follow_my_own_trades(live_env):
    client, _ = live_env
    sid = new_strategy(client)
    r = client.put(f"{PREFIX}/strategies/{sid}/real", json={"enabled": True, "capital": 100_000})
    assert r.status_code == 400 and "运行中" in r.json()["detail"]
    client.post(f"{PREFIX}/strategies/{sid}/start")
    view = client.put(f"{PREFIX}/strategies/{sid}/real", json={"enabled": True, "capital": 100_000}).json()
    assert view["enabled"] and view["cash"] == 100_000 and view["positions"] == []
    todos = {t["code"]: t for t in view["todos"]}
    assert set(todos) == {"sh.600001", "sz.000003"} and todos["sh.600001"]["side"] == "buy"
    assert todos["sh.600001"]["shares"] % 100 == 0 and todos["sh.600001"]["shares"] > 0

    latest = str(DAYS[-1].date())
    done = client.post(f"{PREFIX}/real/todos/{todos['sh.600001']['id']}",
                       json={"done": True, "price": 18.0, "shares": 1000, "date": latest}).json()
    (pos,) = done["positions"]
    assert pos["code"] == "sh.600001" and pos["name"] == "甲股份" and pos["buy_price"] == 18.0
    assert done["cash"] == pytest.approx(100_000 - 18_000 - 5)
    skipped = client.post(f"{PREFIX}/real/todos/{todos['sz.000003']['id']}", json={"done": False}).json()
    assert skipped["todos"] == []

    (block,) = client.get(f"{PREFIX}/today").json()["strategies"]
    assert block["real"]["positions"][0]["id"] == pos["id"]
    (row,) = client.get(f"{PREFIX}/strategies").json()
    assert row["real_return"] == pytest.approx(done["equity"] / 100_000 - 1)

    sold = client.post(f"{PREFIX}/real/positions/{pos['id']}/sell", json={"price": 19.0, "date": latest}).json()
    assert sold["positions"] == [] and sold["trades"][0]["reason"] == "手动卖出"
    removed = client.delete(f"{PREFIX}/real/positions/{pos['id']}").json()
    assert removed["trades"] == [] and removed["cash"] == 100_000
    bad = client.post(f"{PREFIX}/strategies/{sid}/real/positions",
                      json={"code": "999999", "shares": 100, "price": 10, "date": latest})
    assert bad.status_code == 400
    off = client.put(f"{PREFIX}/strategies/{sid}/real", json={"enabled": False}).json()
    assert off["enabled"] is False and client.get(f"{PREFIX}/today").json()["strategies"][0]["real"] is None


def test_settings_change_costs_within_limits(live_env):
    client, _ = live_env
    base = client.get(f"{PREFIX}/settings").json()
    assert base["costs"]["commission"] == 0.00025 and base["initial_cash"] == 1_000_000
    assert set(base["push"]) == {"feishu", "wecom"}
    changed = client.put(f"{PREFIX}/settings", json={"costs": {"slippage": 0.002}, "initial_cash": 200_000}).json()
    assert changed["costs"]["slippage"] == 0.002 and changed["initial_cash"] == 200_000
    assert client.put(f"{PREFIX}/settings", json={"costs": {"slippage": 0.5}}).status_code == 400
    assert client.put(f"{PREFIX}/settings", json={"initial_cash": 5}).status_code == 400


# ---------------- 行情的下载、限速和每天的自动更新 ----------------
def test_sources_show_limits_and_refuse_values_above_the_official_caps(env):
    client, _ = env
    body = client.get(f"{PREFIX}/data/sources").json()
    names = [s["name"] for s in body["sources"]]
    assert set(names) == {"akshare_sina", "akshare_tx", "akshare_em", "baostock", "tencent_quote"}
    bao = next(s for s in body["sources"] if s["name"] == "baostock")
    assert bao["label"].startswith("BaoStock") and bao["state"] == "ok" and bao["today_requests"] == 0
    # 默认值和 quant-lab 一样：严格串行、每天 20000 次（官方上限 50000）
    assert bao["policy"]["max_concurrent"] == 1 and bao["policy"]["daily_request_cap"] == 20000
    assert bao["policy"]["official_daily_limit"] == 50000 and bao["defaults"] == bao["policy"]
    assert body["priority"]["stock_daily"][0] == "akshare_sina" and "float_shares" in body["priority"]

    slower = client.put(f"{PREFIX}/data/sources/baostock", json={"daily_request_cap": 10000}).json()
    assert next(s for s in slower["sources"] if s["name"] == "baostock")["policy"]["daily_request_cap"] == 10000
    too_many = client.put(f"{PREFIX}/data/sources/baostock", json={"daily_request_cap": 60000})
    assert too_many.status_code == 400 and "官方" in too_many.json()["detail"]
    parallel = client.put(f"{PREFIX}/data/sources/baostock", json={"max_concurrent": 2})
    assert parallel.status_code == 400
    unknown = client.put(f"{PREFIX}/data/sources/wind", json={"enabled": False})
    assert unknown.status_code == 400
    again = client.get(f"{PREFIX}/data/sources").json()  # 被拒绝的修改没有留下痕迹
    assert next(s for s in again["sources"] if s["name"] == "baostock")["policy"]["daily_request_cap"] == 10000


def test_realtime_snapshot_interval_has_a_floor(env):
    client, _ = env
    base = client.get(f"{PREFIX}/data/realtime").json()
    assert base == {"interval_seconds": 60.0, "interval_min": 15.0, "interval_max": 3600.0, "interval_default": 60.0}
    assert client.put(f"{PREFIX}/data/realtime", json={"interval_seconds": 30}).json()["interval_seconds"] == 30.0
    too_fast = client.put(f"{PREFIX}/data/realtime", json={"interval_seconds": 5})
    assert too_fast.status_code == 400 and "15" in too_fast.json()["detail"]
    assert client.get(f"{PREFIX}/data/realtime").json()["interval_seconds"] == 30.0  # 被拒绝的修改没有留下痕迹


def test_schedule_time_must_be_after_the_data_is_ready(env):
    client, _ = env
    base = client.get(f"{PREFIX}/data/schedule").json()
    assert base == {"run_time": "18:30", "run_time_min": "18:00", "run_time_max": "23:30",
                    "auto_sync_tsp": True, "tsp_sync_message": None}
    off = client.put(f"{PREFIX}/data/schedule", json={"auto_sync_tsp": False}).json()
    assert off["auto_sync_tsp"] is False and off["run_time"] == "18:30"  # 只改开关，不动时间
    assert client.put(f"{PREFIX}/data/schedule", json={"auto_sync_tsp": True}).json()["auto_sync_tsp"] is True
    assert client.put(f"{PREFIX}/data/schedule", json={"run_time": "19:15"}).json()["run_time"] == "19:15"
    early = client.put(f"{PREFIX}/data/schedule", json={"run_time": "15:30"})
    assert early.status_code == 400 and "18:00" in early.json()["detail"]
    assert client.put(f"{PREFIX}/data/schedule", json={"run_time": "晚上"}).status_code == 400


def test_auto_update_is_due_only_after_the_set_time_on_a_trading_weekday(live_env, monkeypatch):
    from datetime import datetime

    from app.custom.tdx_formula import data_service as ds

    client, _market = live_env
    client.get(f"{PREFIX}/data/sources")  # 让服务对象建出来
    svc: ds.DataService = None  # type: ignore[assignment]
    original = ds.DataService.progress

    def spy(self):
        nonlocal svc
        svc = self
        return original(self)

    monkeypatch.setattr(ds.DataService, "progress", spy)
    client.get(f"{PREFIX}/data/progress")
    monkeypatch.setattr(ds, "libraries_ready", lambda: True)
    # 行情到 2024-03-25（周一）。之后一个周二：
    assert svc.update_due(datetime(2024, 3, 26, 17, 0)) is False  # 还没到设定的时间
    assert svc.update_due(datetime(2024, 3, 26, 18, 30)) is True
    assert svc.update_due(datetime(2024, 3, 30, 19, 0)) is False  # 周六
    assert svc.update_due(datetime(2024, 3, 25, 19, 0)) is False  # 当天的行情已经有了
    calls = []
    monkeypatch.setattr(ds.DataService, "_quiet_update", lambda self: calls.append(1))
    from app.custom.tdx_formula.ql.core import clock

    clock.set_fixed(datetime(2024, 3, 26, 18, 31))
    svc.tick()
    svc.tick()  # 刚试过一次：半小时内不再试
    assert len(calls) == 1
    clock.set_fixed(datetime(2024, 3, 26, 19, 5))
    svc.tick()
    assert len(calls) == 2
    monkeypatch.setattr(ds, "libraries_ready", lambda: False)
    assert svc.update_due(datetime(2024, 3, 27, 19, 0)) is False  # 取数用的库没装：不自动更新


def test_download_needs_the_fetching_libraries(env, monkeypatch):
    from app.custom.tdx_formula import data_service as ds

    client, _ = env
    monkeypatch.setattr(ds, "libraries_ready", lambda: False)
    assert client.get(f"{PREFIX}/data/sources").json()["libraries_ready"] is False
    for path in ("data/download", "data/update"):
        r = client.post(f"{PREFIX}/{path}", json={})
        assert r.status_code == 400 and "akshare" in r.json()["detail"]
    assert client.get(f"{PREFIX}/data/progress").json()["running"] is False


def test_progress_shows_whichever_finished_last(env, monkeypatch):
    from datetime import datetime

    from app.custom.tdx_formula import data_service as ds

    client, _ = env
    monkeypatch.setattr(ds, "libraries_ready", lambda: True)
    fake = {"running": False, "phase": "结束", "total": 3, "done": {"daily": 3}, "message": "历史下载完成",
            "finished_at": "2024-03-26T10:00:00"}
    monkeypatch.setattr(ds.ql_data, "get_progress", lambda: dict(fake))
    assert client.get(f"{PREFIX}/data/progress").json()["message"] == "历史下载完成"
    # 之后又更新了一次：显示更新的结果，而不是早先那次下载的
    monkeypatch.setattr(ds.ql_data, "update_daily", lambda: type("R", (), {"message": "已补上 1 个交易日"})())
    monkeypatch.setattr(ds.clock, "now", lambda: datetime(2024, 3, 26, 18, 31))
    monkeypatch.setattr(ds.threading, "Thread", lambda target, **_k: type("T", (), {"start": lambda self: target()})())
    client.post(f"{PREFIX}/data/update", json={})
    body = client.get(f"{PREFIX}/data/progress").json()
    assert body == {"running": False, "kind": "update", "stage": None, "done": 0, "total": 0,
                    "eta_seconds": None, "message": "已补上 1 个交易日"}
    fake["finished_at"] = "2024-03-27T09:00:00"  # 再之后又下载了一次
    assert client.get(f"{PREFIX}/data/progress").json()["message"] == "历史下载完成"


# ---------------- 行情更新完以后让 TSP 接着同步 ----------------
def test_tsp_is_asked_to_sync_only_when_it_is_behind(env, tmp_path, monkeypatch):
    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    from app.custom.tdx_formula import tsp_sync
    from app.custom.tdx_formula.ql.live import store as live_store

    client, _ = env
    client.get(f"{PREFIX}/data/schedule")  # 让扩展把自己的设置库建好
    data_dir = tmp_path / "tsp"
    sync = tsp_sync.TspSync()
    source = {"name": "freecn"}
    monkeypatch.setattr(tsp_sync, "_daily_source", lambda: source["name"])
    now, latest = _datetime(2024, 3, 14, 18, 40), _date(2024, 3, 14)

    assert tsp_sync.tsp_latest_date(data_dir) is None
    assert sync.due(data_dir, latest, now) is False  # TSP 一天数据都没有：第一次同步留给用户自己点
    (data_dir / "kline_daily" / "date=2024-03-13").mkdir(parents=True)
    (data_dir / "kline_daily" / "不是日期").mkdir()
    assert tsp_sync.tsp_latest_date(data_dir) == _date(2024, 3, 13)
    assert sync.due(data_dir, latest, now) is True
    assert sync.due(data_dir, None, now) is False  # 本机还没有行情
    source["name"] = "tickflow"
    assert sync.due(data_dir, latest, now) is False  # TSP 的日 K 不是用免费公开来源，不管
    source["name"] = "freecn"
    sync.set_enabled(False)
    assert sync.due(data_dir, latest, now) is False  # 开关关着
    sync.set_enabled(True)
    (data_dir / "kline_daily" / "date=2024-03-14").mkdir()
    # 日期上已经是最新，但那可能只是收盘时的行情快照：这一天的正式行情还没同步过，仍然要同步一次
    assert sync.due(data_dir, latest, now) is True
    live_store.set_setting(tsp_sync.KEY_SYNCED, "2024-03-14")
    assert sync.synced_through() == latest and sync.due(data_dir, latest, now) is False  # 同步过就不再重复
    assert sync.due(data_dir, _date(2024, 3, 15), now) is True  # 下一个交易日的行情到了，再同步

    # 试过以后半小时内不再试；同一天最多试 3 次
    (data_dir / "kline_daily" / "date=2024-03-14").rmdir()
    for attempt in range(tsp_sync.MAX_ATTEMPTS):
        moment = now + _timedelta(minutes=31 * attempt)
        assert sync.due(data_dir, latest, moment) is True
        sync._last_attempt, sync._attempts = moment, {latest: attempt + 1}
        assert sync.due(data_dir, latest, moment + _timedelta(minutes=5)) is False
    assert sync.due(data_dir, latest, now + _timedelta(hours=5)) is False


def test_trigger_runs_the_tsp_pipeline_on_the_app_event_loop(env, monkeypatch):
    """后台线程里发起、在 TSP 的事件循环里执行；结果写成一句话给页面看。"""
    import asyncio
    import threading
    from datetime import date as _date
    from datetime import datetime as _datetime
    from types import SimpleNamespace

    import app.api.pipeline as pipeline_api
    from app.custom.tdx_formula import tsp_sync

    client, _ = env
    client.get(f"{PREFIX}/data/schedule")
    seen: list[tuple] = []
    outcome = {"value": {"job_id": "j1", "reused": False}}

    async def fake_trigger(repo, capset, quote_service=None):
        seen.append((repo, capset, quote_service, threading.current_thread().name))
        if isinstance(outcome["value"], Exception):
            raise outcome["value"]
        return outcome["value"]

    monkeypatch.setattr(pipeline_api, "trigger_pipeline_job", fake_trigger)
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, name="app-loop", daemon=True)
    runner.start()
    try:
        fake_app = SimpleNamespace(state=SimpleNamespace(repo="repo", capabilities="caps"))
        sync = tsp_sync.TspSync()
        now, latest = _datetime(2024, 3, 14, 18, 40), _date(2024, 3, 14)
        sync.trigger(fake_app, loop, latest, now)
        assert seen == [("repo", "caps", None, "app-loop")]
        assert "已让 TSP 开始同步" in sync.info()["tsp_sync_message"] and sync._attempts == {latest: 1}
        assert sync.synced_through() == latest  # 记下已经为这一天同步过
        outcome["value"] = {"job_id": "j1", "reused": True}
        sync.trigger(fake_app, loop, latest, now)
        assert "没有另开" in sync.info()["tsp_sync_message"] and sync._attempts == {latest: 2}
        outcome["value"] = RuntimeError("boom")
        sync.trigger(fake_app, loop, latest, now)  # 触发失败不抛错，只记下来
        assert "没有成功" in sync.info()["tsp_sync_message"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        loop.close()


# ---------------- 小毛病的回归 ----------------
def test_a_strategy_never_backtested_returns_nothing_instead_of_an_error(live_env):
    client, _ = live_env
    sid = new_strategy(client)
    answer = client.get(f"{PREFIX}/strategies/{sid}/backtest")
    assert answer.status_code == 200 and answer.json() is None
    assert client.get(f"{PREFIX}/strategies/no-such-id/backtest").status_code == 404  # 策略不存在才是“找不到”


def test_rows_with_zero_prices_or_off_calendar_dates_are_not_stored(tmp_path):
    import numpy as np
    import pandas as pd

    from app.custom.tdx_formula.ql.core import config as ql_config
    from app.custom.tdx_formula.ql.data import store as ql_store
    from app.custom.tdx_formula.ql.data.interface import Capability
    from app.custom.tdx_formula.ql.data.models import normalize_frame

    # 价格是 0 的行（来源在没有交易的日子偶尔会给）整行不要
    raw = pd.DataFrame({
        "date": ["2024-11-05", "2024-11-06", "2024-11-07"], "code": ["sh.688143"] * 3,
        "open": [27.0, 0.0, 27.9], "high": [27.8, 0.0, 28.2], "low": [26.9, 0.0, 27.5],
        "close": [27.6, 27.58, 28.0], "preclose": [27.1, 27.6, 27.58],
        "volume": [1e6, 0.0, 1.2e6], "amount": [2.7e7, 0.0, 3.3e7], "suspended": [False, False, False],
    })
    cleaned = normalize_frame(Capability.STOCK_DAILY, raw)
    assert [str(d)[:10] for d in cleaned["date"]] == ["2024-11-05", "2024-11-07"]

    # 落在交易日历范围之内、却不是交易日的日期不写进文件；日历还没覆盖到的新日期照常写
    ql_config.set_data_dir(tmp_path / "freecn")
    (tmp_path / "freecn" / "market" / "daily").mkdir(parents=True)
    ql_store.write_calendar(pd.Series(pd.to_datetime(["2021-06-24", "2021-06-25", "2021-06-28"])))
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2021-06-25", "2021-06-27", "2021-06-28", "2021-06-29"]),
        "open": [1.0, 1.0, 1.1, 1.2], "high": [1.1, 1.0, 1.2, 1.3], "low": [0.9, 1.0, 1.0, 1.1],
        "close": [1.0, 1.0, 1.1, 1.2], "volume": np.full(4, 1e5), "amount": np.full(4, 1e5),
    })
    ql_store.merge_daily("sz.300431", frame)
    stored = ql_store.read_daily("sz.300431")
    assert [str(d)[:10] for d in stored["date"]] == ["2021-06-25", "2021-06-28", "2021-06-29"]


# ---------------- 行业和概念（东方财富数据中心；这里用造出来的返回，不联网） ----------------
def board_row(symbol, name, board, precise=None, kind=None, level=None):
    return {"SECUCODE": symbol, "SECURITY_NAME_ABBR": name, "BOARD_NAME": board, "IS_PRECISE": precise,
            "BOARD_TYPE": kind, "BOARD_LEVEL": level}


def test_board_rows_become_one_industry_and_a_concept_list_per_stock():
    from app.custom.tdx_formula import boards

    rows = [
        board_row("600519.SH", "贵州茅台", "食品饮料", kind="行业", level="1"),
        board_row("600519.SH", "贵州茅台", "白酒Ⅱ", kind="行业", level="2"),
        board_row("600519.SH", "贵州茅台", "白酒Ⅲ", kind="行业", level="3"),
        board_row("600519.SH", "贵州茅台", "贵州板块", "0", kind="板块"),      # 地域：不要
        board_row("600519.SH", "贵州茅台", "超级品牌", "1"),
        board_row("600519.SH", "贵州茅台", "白酒", "1"),
        board_row("600519.SH", "贵州茅台", "HS300_", "0"),                     # 指数成份：不算概念
        board_row("600519.SH", "贵州茅台", "大盘股"),                           # 风格标签：不算概念
        board_row("000001.SZ", "平安银行", "银行", kind="行业", level="1"),     # 只有一级行业
        board_row("000001.SZ", "平安银行", "跨境支付", "1"),
        board_row("AAPL.US", "苹果", "消费电子", "1"),                          # 不是沪深京的代码：跳过
    ]
    concepts, industries = boards.build_tables(rows)
    assert concepts == [
        {"股票代码": "000001.SZ", "股票简称": "平安银行", "所属概念": "跨境支付"},
        {"股票代码": "600519.SH", "股票简称": "贵州茅台", "所属概念": "白酒;超级品牌"},
    ]
    assert industries == [
        {"股票代码": "000001.SZ", "股票简称": "平安银行", "所属同花顺行业": "银行"},
        {"股票代码": "600519.SH", "股票简称": "贵州茅台", "所属同花顺行业": "白酒"},  # 二级，去掉末尾的 Ⅱ
    ]


def test_board_download_pages_through_the_limiter_and_stops_on_refusal(env):
    import json as _json

    from app.custom.tdx_formula import boards
    from app.custom.tdx_formula.ql.core.errors import SourceRejected
    from app.custom.tdx_formula.ql.data import runtime

    client, _ = env
    client.get(f"{PREFIX}/data/sources")  # 让扩展把数据目录和限流器建好
    calls: list[str] = []
    state = {"status": 200}

    def fetch(params, timeout):
        calls.append(params["pageNumber"])
        if state["status"] != 200:
            return state["status"], ""
        row = board_row(f"60000{params['pageNumber']}.SH", "某股", "白酒", "1")
        return 200, _json.dumps({"success": True, "result": {"pages": 3, "data": [row]}})

    boards.set_fetch(fetch)
    runtime.set_sleep(lambda seconds: None)
    try:
        seen: list[tuple[int, int]] = []
        rows = boards.download(lambda done, total: seen.append((done, total)))
        assert calls == ["1", "2", "3"] and len(rows) == 3 and seen == [(1, 3), (2, 3), (3, 3)]
        calls.clear()
        state["status"] = 403
        with pytest.raises(SourceRejected):
            boards.download()
        assert calls == ["1"]  # 被拒绝后不重试、不接着取
    finally:
        boards.set_fetch(None)
        runtime.set_sleep(None)


def test_boards_are_refreshed_about_once_a_week(tmp_path, monkeypatch):
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    from app.custom.tdx_formula import boards

    service = boards.BoardService(tmp_path)
    saturday = _datetime(2024, 3, 16, 10, 0)
    assert service.due(saturday) is True  # 从没取过
    service._save(updated_at=(saturday - _timedelta(days=3)).isoformat())
    assert service.due(saturday) is False  # 才 3 天
    service._save(updated_at=(saturday - _timedelta(days=8)).isoformat())
    assert service.due(saturday) is True  # 满 7 天后的周末
    assert service.due(saturday - _timedelta(days=2)) is False  # 满 7 天但那天是周四（按周四算才 6 天）
    wednesday = _datetime(2024, 3, 20, 10, 0)
    service._save(updated_at=(wednesday - _timedelta(days=8)).isoformat())
    assert service.due(wednesday) is False  # 工作日先不动，等周末
    service._save(updated_at=(wednesday - _timedelta(days=11)).isoformat())
    assert service.due(wednesday) is True  # 已经 10 天以上，不等了
    service._last_attempt = wednesday - _timedelta(hours=1)
    assert service.due(wednesday) is False  # 刚试过（失败后隔几小时再试）
    info = service.info()
    assert info["running"] is False and info["refresh_days"] == 7 and info["updated_at"]


def test_boards_endpoint_reports_the_state(env):
    client, _ = env
    info = client.get(f"{PREFIX}/data/boards").json()
    assert info["running"] is False and info["updated_at"] is None and info["message"] is None


def test_board_tables_are_relabelled_to_match_the_real_source(tmp_path):
    from app.custom.tdx_formula import boards
    from app.services.ext_data import ExtConfigStore
    from app.services.ext_presets import _concept_preset, _industry_preset

    store = ExtConfigStore(tmp_path)
    store.create(_industry_preset())
    store.create(_concept_preset())
    boards.relabel(tmp_path)
    industry = store.get("ext_hy_ths")
    field = next(f for f in industry.fields if f.name == "所属同花顺行业")
    assert field.label == "所属行业"  # 给人看的名字改了；内部名字不动，TSP 别处按它取数
    assert "东方财富" in industry.description and "fuyao" not in industry.description
    assert "东方财富" in store.get("ext_gn_ths").description

    # 用户自己改过的显示名称不覆盖
    field.label = "我的行业"
    industry.description = "我自己接的行业"
    store.upsert(industry)
    boards.relabel(tmp_path)
    again = store.get("ext_hy_ths")
    assert next(f for f in again.fields if f.name == "所属同花顺行业").label == "我的行业"
    assert again.description == "我自己接的行业"


# ---------------- 公式函数：DMA、DYNAINFO 的行情项、可变周期的最高最低 ----------------


def test_dma_follows_the_recursion_with_a_weight_that_changes_every_bar():
    from app.custom.tdx_formula.ql.tdx import funcs

    x = np.array([[10.0], [12.0], [11.0], [np.nan], [14.0]])
    a = np.array([[0.5], [0.25], [np.nan], [0.5], [3.0]])  # 未知的权重沿用上一个值；大于 1 当作 1
    out = funcs.DMA(x, a)[:, 0]
    assert out.tolist() == [10.0, 0.25 * 12 + 0.75 * 10, 10.5, 10.5, 14.0]
    # 权重是固定数字时也能用，并且只用到当天及之前的数据
    full = funcs.DMA(np.arange(1.0, 9.0).reshape(-1, 1), 0.3)
    assert np.allclose(funcs.DMA(np.arange(1.0, 6.0).reshape(-1, 1), 0.3), full[:5])


def test_dynainfo_prices_become_the_bar_prices_so_they_can_mix_with_technical_conditions():
    from app.custom.tdx_formula.ql.blocks.split import split_formula
    from app.custom.tdx_formula.ql.tdx.evaluator import analyze
    from app.custom.tdx_formula.ql.tdx.parser import parse

    program = parse("A:=DYNAINFO(7)/REF(CLOSE,1)>1.05 AND DMA(C,0.5)>0;\nXG:A AND DYNAINFO(4)>0;")
    analysis = analyze(program)
    assert analysis.unsupported == ()
    assert analysis.pool_functions == ("DYNAINFO",)  # 只剩 DYNAINFO(4)>0，仍按“排除停牌”处理

    result = split_formula(program.source)
    assert result.universe.exclude_suspended is True
    assert len(result.conditions) == 1
    assert "DYNAINFO" not in result.conditions[0].formula
    assert "CLOSE/REF(CLOSE,1)" in result.conditions[0].formula


def test_variable_period_highest_and_lowest_match_a_bar_by_bar_lookback():
    from app.custom.tdx_formula.ql.tdx import funcs

    rng = np.random.default_rng(7)
    x = np.round(rng.normal(size=(70, 4)), 1)  # 一位小数，会出现相同的值
    x[rng.random(x.shape) < 0.03] = np.nan
    n = rng.integers(-1, 75, size=x.shape).astype(float)
    n[rng.random(x.shape) < 0.05] = np.nan

    def slow(highest: bool, bars: bool) -> np.ndarray:
        out = np.full(x.shape, np.nan)
        for t in range(x.shape[0]):
            for c in range(x.shape[1]):
                k = n[t, c]
                if not np.isfinite(k) or k < 1 or k > t + 1:
                    continue
                window = x[t - int(k) + 1: t + 1, c][::-1]  # 从近到远
                if np.isnan(window).any():
                    continue
                pos = int(np.argmax(window) if highest else np.argmin(window))  # 相同时取最近的
                out[t, c] = pos if bars else window[pos]
        return out

    assert np.array_equal(funcs.HHV(x, n), slow(True, False), equal_nan=True)
    assert np.array_equal(funcs.LLV(x, n), slow(False, False), equal_nan=True)
    assert np.array_equal(funcs.HHVBARS(x, n), slow(True, True), equal_nan=True)
    assert np.array_equal(funcs.LLVBARS(x, n), slow(False, True), equal_nan=True)


# ---------------- 数据层的几处修正 ----------------
def test_first_date_is_the_first_bar_not_the_requested_start(env):
    client, root = env
    market = make_market(root)
    progress = json.loads((market / "progress.json").read_text(encoding="utf-8"))
    progress["start"] = "2015-01-01"  # 要求从 2015 年下起，但实际最早的行情是 2024-01-02
    (market / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
    assert client.get(f"{PREFIX}/status").json()["first_date"] == "2015-01-01"  # 还没读过行情，只知道要求的起点
    r = client.post(f"{PREFIX}/stats", json={"conditions": [ABOVE_MA], "universe": ALL})
    assert r.status_code == 200, r.text
    assert client.get(f"{PREFIX}/status").json()["first_date"] == str(DAYS[0].date())

    from app.custom.tdx_formula.ql.data import service as ql_service

    assert ql_service.data_status().first_date == DAYS[0].date()


def test_days_not_yet_fetched_for_every_stock_are_left_out_instead_of_shown_as_suspended(env):
    client, root = env
    market = make_market(root)
    client.get(f"{PREFIX}/status")  # 让扩展指到这个数据目录
    next_day = (DAYS[-1] + pd.offsets.BDay(1)).normalize()
    ql_store.write_calendar(pd.Series([next_day]))
    # 只有一只股票多了一天（比如它是第二天才下载的），另外两只还停在前一天
    ql_store.merge_daily("sh.600001", pd.DataFrame({
        "date": [next_day], "open": [18.0], "high": [18.1], "low": [17.9], "close": [18.0], "preclose": [17.9],
        "volume": [2e6], "amount": [3.6e7], "factor": [1.0], "float_shares": [1e8], "is_st": [False],
        "source": ["fake"],
    }))
    meta = json.loads((market / "meta.json").read_text(encoding="utf-8"))
    assert meta["latest_date"] == str(next_day.date()) and meta["updated_through"] == str(DAYS[-1].date())
    panel = ql_store.build_panel()
    assert str(panel.dates[-1]) == str(DAYS[-1].date()) and not panel.suspended[-1].any()

    # 每日更新做到这一天之后，这一天才进来；这时没有行情的才是真的停牌
    set_latest(market, str(next_day.date()))
    panel = ql_store.build_panel()
    assert str(panel.dates[-1]) == str(next_day.date())
    assert list(panel.suspended[-1]) == [False, True, True]


def test_no_data_answers_are_not_retried_but_network_failures_are(tmp_path):
    from app.custom.tdx_formula.ql.core import config as ql_config
    from app.custom.tdx_formula.ql.core.errors import SourceUnavailable
    from app.custom.tdx_formula.ql.data.interface import SourceNoData
    from app.custom.tdx_formula.ql.data.limiter import RateLimiter, SourcePolicy
    from app.custom.tdx_formula.ql.data.state import StateStore

    ql_config.set_data_dir(tmp_path / "freecn")
    policy = SourcePolicy(
        enabled=True, max_concurrent=1, daily_request_cap=0, min_interval_seconds=0.01, jitter_seconds=0.0,
        timeout_seconds=5.0, max_retries=2, stop_after_consecutive_failures=5, cooldown_minutes_on_reject=10,
        count_pages_as_requests=False, official_daily_limit=None, official_max_concurrent=None,
    )
    limiter = RateLimiter(lambda name: policy, StateStore(), sleep=lambda seconds: None)
    calls: list[str] = []

    def fail_with(error: Exception):
        def fn():
            calls.append(type(error).__name__)
            raise error
        return fn

    with pytest.raises(SourceNoData):
        limiter.call("s", fail_with(SourceNoData("没有这只股票")))
    assert calls == ["SourceNoData"] and limiter.today_requests("s") == 1  # 只问了一次
    assert limiter.consecutive_failures("s") == 1  # 仍然计入连续失败
    calls.clear()
    limiter.begin_task()
    with pytest.raises(SourceUnavailable):
        limiter.call("s", fail_with(SourceUnavailable("超时")))
    assert len(calls) == 3  # 网络类的失败照旧重试 2 次


def test_akshare_errors_are_sorted_and_a_timed_out_call_blocks_the_next_one():
    pytest.importorskip("akshare")
    import threading

    import requests

    from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
    from app.custom.tdx_formula.ql.data.interface import SourceNoData
    from app.custom.tdx_formula.ql.data.sources import akshare_common as common
    from app.custom.tdx_formula.ql.data.sources.akshare_tx import requests_for

    assert isinstance(common.translate_error("x", KeyError("date")), SourceNoData)
    assert isinstance(common.translate_error("x", ValueError("factor not available")), SourceNoData)
    timeout = common.translate_error("x", requests.ConnectionError("断了"))
    assert isinstance(timeout, SourceUnavailable) and not isinstance(timeout, SourceNoData)
    bad_json = common.translate_error("x", requests.exceptions.JSONDecodeError("不是 JSON", "<html>", 0))
    assert not isinstance(bad_json, SourceNoData)  # 返回的不是数据（可能是拦截页面）：按来源不可用处理
    response = requests.Response()
    response.status_code = 456
    assert isinstance(common.translate_error("x", requests.HTTPError(response=response)), SourceRejected)

    # 上一次调用超时后还没返回：同一个来源不再发新的请求，别的来源不受影响
    release = threading.Event()
    started: list[str] = []

    def hang() -> pd.DataFrame:
        started.append("hang")
        release.wait(5)
        return pd.DataFrame()

    def quick() -> pd.DataFrame:
        started.append("quick")
        return pd.DataFrame({"a": [1]})

    try:
        with pytest.raises(SourceUnavailable, match="没有返回"):
            common.call_akshare("src_a", "测试", hang, 0.05)
        with pytest.raises(SourceUnavailable, match="还没有结束"):
            common.call_akshare("src_a", "测试", quick, 1.0)
        assert started == ["hang"]
        assert len(common.call_akshare("src_b", "测试", quick, 1.0)) == 1
    finally:
        release.set()
        common._in_flight["src_a"].join(5)
    assert len(common.call_akshare("src_a", "测试", quick, 1.0)) == 1  # 结束之后恢复正常

    # 腾讯日线：AKShare 内部按年份分段发请求，按实际次数计
    from datetime import date

    assert requests_for(date(2015, 1, 1), date(2024, 12, 31)) == 5  # 2015–2024，两年一段
    assert requests_for(date(2024, 3, 1), date(2024, 3, 29)) == 1


def test_data_ready_time_can_be_set_to_the_minute(monkeypatch):
    """数据入库时间可以精确到分钟（如 17:30）：到点之前只处理到前一天，最早的自动更新时间也跟着它走。"""
    from datetime import date as _date
    from datetime import datetime as _datetime

    from app.custom.tdx_formula.data_service import DataService
    from app.custom.tdx_formula.ql.core import clock
    from app.custom.tdx_formula.ql.data import config as data_config
    from app.custom.tdx_formula.ql.data import download

    raw = data_config.load_default_raw()
    assert data_config.parse(raw).download.data_ready_minute == 0
    raw["download"].update(data_ready_hour=17, data_ready_minute=30)
    config = data_config.parse(raw)
    monkeypatch.setattr(clock, "now", lambda: _datetime(2024, 3, 14, 17, 29))
    assert download.effective_end(config) == _date(2024, 3, 13)
    monkeypatch.setattr(clock, "now", lambda: _datetime(2024, 3, 14, 17, 30))
    assert download.effective_end(config) == _date(2024, 3, 14)

    from app.custom.tdx_formula.ql import data as ql_data

    monkeypatch.setattr(ql_data, "get_effective_config", lambda: config)
    assert DataService.earliest_run_time().strftime("%H:%M") == "17:30"

    raw["download"]["data_ready_minute"] = 60
    with pytest.raises(data_config.ConfigError):
        data_config.parse(raw)


# ---------------- 成批补上的公式函数、筹码分布、只检查参与结果的语句 ----------------


def _tiny_panel(close, high=None, low=None, volume=None, float_shares=None, start="2024-03-11"):
    from app.custom.tdx_formula.ql.core.panel import Panel

    close = np.asarray(close, dtype=float)
    t, n = close.shape
    flags = np.zeros((t, n), dtype=bool)
    high = close if high is None else np.asarray(high, dtype=float)
    low = close if low is None else np.asarray(low, dtype=float)
    volume = np.full((t, n), 100.0) if volume is None else np.asarray(volume, dtype=float)
    return Panel(
        dates=np.arange(np.datetime64(start), np.datetime64(start) + t), codes=np.array([f"sh.60000{i}" for i in range(n)]),
        open=close, high=high, low=low, close=close, volume=volume, amount=close * volume,
        raw_open=close, raw_high=high, raw_low=low, raw_close=close, raw_preclose=close,
        listed=np.isfinite(close), suspended=flags, is_st=flags.copy(),
        float_shares=np.full((t, n), 1000.0) if float_shares is None else np.asarray(float_shares, dtype=float),
    )


def test_batch_of_added_formula_functions():
    from app.custom.tdx_formula.ql.tdx import funcs as f

    x = np.array([[1.0], [3.0], [2.0], [5.0], [4.0], [np.nan], [6.0]])
    cond = np.array([[0], [1], [1], [0], [1], [1], [1]], dtype=bool)
    assert f.BARSLASTCOUNT(cond)[:, 0].tolist() == [0, 1, 2, 0, 1, 2, 3]
    assert f.BARSSINCE(cond)[1:, 0].tolist() == [0, 1, 2, 3, 4, 5] and np.isnan(f.BARSSINCE(cond)[0, 0])
    assert f.BARSCOUNT(x)[:, 0].tolist() == [1, 2, 3, 4, 5, 6, 7]
    hit = np.array([[0], [1], [0], [0], [1], [0], [0]], dtype=bool)
    assert f.VALUEWHEN(hit, x)[1:, 0].tolist() == [3, 3, 3, 4, 4, 4]
    assert f.SUMBARS(np.ones((6, 1)), 3.0)[2:, 0].tolist() == [3, 3, 3, 3] and np.isnan(f.SUMBARS(np.ones((6, 1)), 3.0)[1, 0])
    assert f.EXISTR(np.array([[1], [0], [0], [0], [0]], dtype=bool), 3, 1)[:, 0].tolist() == [False, True, True, True, False]

    squares = np.arange(1.0, 8.0).reshape(-1, 1) ** 2
    assert f.VARP(squares, 3)[-1, 0] == pytest.approx(np.var([25, 36, 49]))
    assert f.VAR(squares, 3)[-1, 0] == pytest.approx(np.var([25, 36, 49], ddof=1))
    assert f.STDP(squares, 3)[-1, 0] == pytest.approx(np.std([25, 36, 49]))
    assert f.DEVSQ(squares, 3)[-1, 0] == pytest.approx(np.var([25, 36, 49]) * 3)
    line = np.arange(1.0, 8.0).reshape(-1, 1) * 2 + 1  # 一条直线：回归线在当前这一根上的值就是它自己
    assert f.FORCAST(line, 4)[3:, 0].tolist() == pytest.approx([9, 11, 13, 15])
    assert f.MEMA(line, 3)[:, 0].tolist() == pytest.approx(f.SMA(line, 3, 1)[:, 0].tolist())
    assert f.SIGN(np.array([-2.0, 0.0, 3.0])).tolist() == [-1, 0, 1]
    assert f.FRACPART(np.array([2.75, -2.75])).tolist() == [0.75, -0.75]
    assert f.CEILING(np.array([1.2])).tolist() == [2] and f.FLOOR(np.array([1.8])).tolist() == [1]
    assert f.IFN(np.array([True, False]), 1.0, 2.0).tolist() == [2, 1]


def test_date_series_follow_the_trading_days():
    from app.custom.tdx_formula.ql.tdx.evaluator import evaluate
    from app.custom.tdx_formula.ql.tdx.parser import parse

    panel = _tiny_panel(np.full((3, 1), 10.0), start="2024-03-15")  # 周五、周六、周日
    out = evaluate(parse("A:DATE;\nB:YEAR;\nC1:MONTH;\nD:DAY;\nE:WEEKDAY;"), panel)
    assert out["A"][:, 0].tolist() == [1240315, 1240316, 1240317]
    assert (out["B"][0, 0], out["C1"][0, 0], out["D"][:, 0].tolist()) == (2024, 3, [15, 16, 17])
    assert out["E"][:, 0].tolist() == [5, 6, 7]


def test_chip_distribution_estimates_winner_and_cost():
    from app.custom.tdx_formula.ql.tdx import funcs as f

    # 第一只每天全部换手（筹码永远是当天的），第二只每天换手一半
    price = np.array([[10.0, 10.0], [20.0, 10.0], [20.0, 20.0]])
    turnover = np.array([[100.0, 50.0]] * 3)
    winner, cost = f.chip_pass(price, price, turnover, [("winner", np.full((3, 2), 15.0)), ("cost", 50.0)])
    assert winner.round(3).tolist() == [[1.0, 1.0], [0.0, 1.0], [0.0, 0.5]]
    assert cost[:, 0].tolist() == pytest.approx([10.0, 20.0, 20.0], rel=0.01)
    # 停牌或没有换手率的日子筹码不变；还没有筹码时是 NaN
    gap = np.array([[np.nan], [10.0], [np.nan], [12.0]])
    held = f.chip_distribution(gap, gap, np.array([[1.0], [1.0], [np.nan], [50.0]]), 11.0, "winner")[:, 0]
    assert np.isnan(held[0]) and held[1:].tolist() == [1.0, 1.0, 0.5]


def test_winner_calls_in_one_formula_share_a_single_pass(monkeypatch):
    from app.custom.tdx_formula.ql.tdx import evaluator
    from app.custom.tdx_formula.ql.tdx.parser import parse

    calls: list[int] = []
    real = evaluator.chip_pass
    monkeypatch.setattr(evaluator, "chip_pass", lambda *args: calls.append(len(args[3])) or real(*args))
    close = np.array([[10.0], [11.0], [12.0], [11.0]])
    panel = _tiny_panel(close, high=close * 1.02, low=close * 0.98, volume=np.full((4, 1), 300.0))  # 每天换手 30%
    program = parse("A:=WINNER(CLOSE);\nB:=WINNER(CLOSE*1.1)-WINNER(CLOSE*0.9);\nXG:A>0.5 AND B>0;")
    first = evaluator.evaluate(program, panel)["XG"]
    assert calls == [3]  # 三个筹码函数一趟算完
    assert evaluator.evaluate(program, panel)["XG"].tolist() == first.tolist() and calls == [3]  # 同一份行情再算：用留着的结果
    evaluator.evaluate(program, _tiny_panel(close, high=close * 1.02, low=close * 0.98))
    assert calls == [3, 3]  # 换了一份行情就重新算


def test_statements_that_feed_no_output_are_not_checked_or_evaluated():
    from app.custom.tdx_formula.ql.core.errors import FormulaError
    from app.custom.tdx_formula.ql.tdx.evaluator import analyze, evaluate
    from app.custom.tdx_formula.ql.tdx.parser import parse

    panel = _tiny_panel(np.array([[10.0], [11.0], [12.0]]))
    # T1 只给画图语句用：里面的 CONST（会用到之后的数据）和没见过的函数都不影响结果
    text = "A:=C>REF(C,1);\nT1:=CONST(A) AND 没见过的函数(C);\nDRAWTEXT_FIX(T1,0.5,0.5,0,'提示');\nXG:A;"
    analysis = analyze(parse(text))
    assert analysis.lookahead == () and analysis.unsupported == ()
    assert evaluate(parse(text), panel)["XG"][:, 0].tolist() == [False, True, True]
    # 真正用在结果里的照样拦
    with pytest.raises(FormulaError) as caught:
        evaluate(parse("T1:=CONST(C);\nXG:C>T1;"), panel)
    assert caught.value.kind == "lookahead"
    with pytest.raises(FormulaError) as caught:
        evaluate(parse("T1:=没见过的函数(C);\nXG:C>T1;"), panel)
    assert caught.value.kind == "unsupported"


# ---------------- 送进面板的因子库、信号库 ----------------
@pytest.fixture
def bridge_env(live_env, monkeypatch):
    """TSP 的因子登记默认看 settings.data_dir：指到测试用的数据目录，结束后把登记进去的扩展因子清掉。"""
    from app import config as app_config
    from app.factors import ext_factors
    from app.factors import registry as factor_registry
    from app.services import ext_data

    client, market = live_env
    data_dir = market.parent.parent
    monkeypatch.setattr(app_config.settings, "data_dir", data_dir)

    def reset() -> None:
        ext_factors._frame_cache.clear()
        ext_factors._sync_state = None
        ext_data._load_all_cache.clear()
        for fid in [k for k in list(factor_registry._REGISTRY) if k.startswith(ext_factors.EXT_PREFIX)]:
            factor_registry.unregister_factor(fid)

    reset()
    yield client, data_dir
    reset()


def wait_for_bridge() -> None:
    import threading

    for t in threading.enumerate():
        if t.name == "tdx-formula-bridge":
            t.join(timeout=30)


WITH_VARIABLE = {"pasted": {"name": "站上均线", "formula": "M5:=MA(C,5);\nC>M5;", "params_spec": []},
                 "enabled": True, "params": {}}


def test_a_strategy_can_be_sent_into_the_panel_as_factors_and_signals(bridge_env):
    import polars as pl

    from app.custom.tdx_formula import bridge
    from app.factors import ext_factors
    from app.strategy import custom_signals

    client, data_dir = bridge_env
    sid = new_strategy(client, conditions=[WITH_VARIABLE])
    (row,) = client.get(f"{PREFIX}/bridge").json()
    assert row["enabled"] is False and row["fields"] == []

    assert client.put(f"{PREFIX}/strategies/{sid}/bridge", json={"enabled": True}).status_code == 200
    wait_for_bridge()
    (row,) = client.get(f"{PREFIX}/bridge").json()
    assert row["ok"] is True and row["through"] == str(DAYS[-1].date()) and row["days"] == len(DAYS)
    assert [f["name"] for f in row["fields"]] == ["xg", "m5"]  # 选股结果 + 条件里用到的公式变量
    xg, m5 = bridge.column(sid, "xg"), bridge.column(sid, "m5")

    # TSP 那边：是一张按天存的扩展表，数值字段登记成了因子，可以接到行情帧上
    last = str(DAYS[-1].date())
    part = pl.read_parquet(data_dir / "ext_data" / row["table"] / "timeseries" / f"date={last}" / "part.parquet")
    by = {r["symbol"]: r for r in part.to_dicts()}
    assert by["600001.SH"]["xg"] == 1.0 and by["600002.SH"]["xg"] == 0.0  # 一路涨的选中，一路跌的没选中
    assert by["600001.SH"]["m5"] == pytest.approx(np.round(10.0 * 1.01 ** np.arange(60), 2)[-5:].mean(), abs=1e-3)
    assert {xg, m5} <= ext_factors.ext_factor_ids(data_dir)
    frame = ext_factors.attach_ext_columns(
        pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "date": [last, last]}), include_snapshot=False,
        data_dir=data_dir)
    assert frame[xg].to_list() == [1.0, 0.0]

    # 信号库里多了一条“选中”的信号，条件就是这个字段等于 1
    (sig,) = [s for s in custom_signals.load_all(data_dir) if s["id"] == row["signals"][0]]
    assert sig["kind"] == "entry" and sig["conditions"][0]["left"] == xg and sig["conditions"][0]["op"] == "=="
    custom_signals.validate(sig)

    # 关掉以后，表和信号都去掉
    assert client.put(f"{PREFIX}/strategies/{sid}/bridge", json={"enabled": False}).status_code == 200
    assert not (data_dir / "ext_data" / row["table"]).exists()
    assert not [s for s in custom_signals.load_all(data_dir) if s["id"].startswith(row["table"])]
    (row,) = client.get(f"{PREFIX}/bridge").json()
    assert row["enabled"] is False and row["fields"] == []


def test_bridge_resends_after_new_data_or_an_edit_and_cleans_up_deleted_strategies(bridge_env, monkeypatch):
    from app.custom.tdx_formula import bridge as bridge_module

    client, data_dir = bridge_env
    sid = new_strategy(client, conditions=[WITH_VARIABLE, ABOVE_MA])
    client.put(f"{PREFIX}/strategies/{sid}/bridge", json={"enabled": True})
    wait_for_bridge()
    (row,) = client.get(f"{PREFIX}/bridge").json()
    assert [f["name"] for f in row["fields"]] == ["xg", "c1", "c2", "m5"]  # 两个条件：各自的结果也送过去
    assert len(row["signals"]) == 3

    calls = []
    panel = bridge_module.Bridge(lambda: None, lambda *a: None)  # 只借它的判断逻辑
    assert panel.pending() == []  # 行情没变、策略没改：不重复送
    client.put(f"{PREFIX}/strategies/{sid}", json={"name": "改过了"})
    assert [s.id for s in panel.pending()] == [sid]  # 改过策略：要重新送
    monkeypatch.setattr(bridge_module.Bridge, "export", lambda self, s, d, hooks=None, panel=None: calls.append(s.id))
    real = bridge_module.Bridge(lambda: "panel", lambda *a: None)
    real.tick(data_dir)
    assert calls == [sid]

    # 策略删掉以后，下一轮把它的表和信号清掉
    assert client.delete(f"{PREFIX}/strategies/{sid}").json() == {"ok": True}
    real.tick(data_dir)
    assert not (data_dir / "ext_data" / row["table"]).exists() and real.enabled_ids() == []


def test_a_strategy_without_conditions_cannot_be_sent(bridge_env):
    client, _ = bridge_env
    sid = new_strategy(client, conditions=[])
    client.put(f"{PREFIX}/strategies/{sid}/bridge", json={"enabled": True})
    wait_for_bridge()
    (row,) = client.get(f"{PREFIX}/bridge").json()
    assert row["ok"] is False and "没有启用任何选股条件" in row["message"]


def test_the_generated_panel_strategy_buys_on_the_pick_and_copies_stop_and_holding_days(bridge_env):
    import ast

    from app.custom.tdx_formula import bridge
    from app.custom.tdx_formula.ql.backtest import strategy_store

    client, _ = bridge_env
    sell = {"pasted": {"name": "跌破均线", "formula": "C<MA(C,5);", "params_spec": []}, "enabled": True, "params": {}}
    sid = new_strategy(client, trade={**TRADE, "stop_loss": 0.08, "max_hold_days": 7, "sell_conditions": [sell]})
    strategy = strategy_store.get_strategy(sid)
    blank = np.zeros((1, 1), dtype=np.float32)
    fields = [bridge.Field("xg", "选股结果", blank, "entry"), bridge.Field("sell", "卖出条件", blank, "exit")]
    code = bridge.strategy_code(strategy, fields)
    module = {n.targets[0].id: ast.literal_eval(n.value) for n in ast.parse(code).body
              if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id.isupper()
              and n.targets[0].id != "MATRIX_STRATEGY"}
    assert module["META"]["id"] == bridge.panel_strategy_id(sid) and module["META"]["id"].startswith("custom_")
    assert module["ENTRY_SIGNALS"] == ["csg_" + bridge.signal_id(sid, "xg")]
    assert module["EXIT_SIGNALS"] == ["csg_" + bridge.signal_id(sid, "sell")]
    assert module["STOP_LOSS"] == -0.08 and module["MAX_HOLD_DAYS"] == 7
    assert bridge.column(sid, "xg") in code and bridge.column(sid, "sell") in code
    # 没有卖出条件时不引用卖出字段
    plain = bridge.strategy_code(strategy, fields[:1])
    assert bridge.column(sid, "sell") not in plain and "EXIT_SIGNALS = []" in plain


def test_panel_side_is_asked_to_save_and_later_delete_the_generated_strategy(bridge_env):
    from app.custom.tdx_formula import bridge
    from app.custom.tdx_formula.ql.backtest import strategy_store

    client, data_dir = bridge_env
    sid = new_strategy(client)
    log = []

    class Spy(bridge.Hooks):
        def after(self):
            log.append("after")

        def save_strategy(self, strategy_id, code, name, description):
            log.append(("save", strategy_id, name))
            return None

        def delete_strategy(self, strategy_id):
            log.append(("delete", strategy_id))

    from app.custom.tdx_formula.service import FormulaService

    formulas = FormulaService(data_dir / "freecn")
    b = bridge.Bridge(formulas.panel, formulas._evaluate)
    b.set_enabled(sid, True, data_dir, Spy())
    state = b.export(strategy_store.get_strategy(sid), data_dir, Spy())
    pid = bridge.panel_strategy_id(sid)
    assert state["ok"] and state["panel_strategy"] == pid
    assert ("save", pid, "公式·均线策略") in log and log.index("after") < log.index(("save", pid, "公式·均线策略"))
    log.clear()
    b.set_enabled(sid, False, data_dir, Spy())
    assert log == [("delete", pid), "after"]
