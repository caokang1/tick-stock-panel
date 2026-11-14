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
    assert sync.due(data_dir, latest, now) is False  # TSP 已经是最新

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
