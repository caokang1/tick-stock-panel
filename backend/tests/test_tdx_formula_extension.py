"""通达信公式扩展：用造出来的小行情目录测试，不读真实数据，不联网。"""

from __future__ import annotations

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
from app.custom.tdx_formula.ql import loader
from app.custom.tdx_formula.routes import PREFIX, build_router
from app.extensions import BACKEND_EXTENSION_API_VERSION

DAYS = pd.bdate_range("2024-01-02", periods=60)
CODES = ("sh.600001", "sh.600002", "sz.000003")


def make_market(root: Path) -> Path:
    """三只股票 60 个交易日。sh.600001 一路上涨，sh.600002 一路下跌，sz.000003 先跌后涨。"""
    market = root / "quant-lab" / "data" / "market"
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
    return market


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("TDX_FORMULA_DATA_DIR", raising=False)
    loader.clear_cache()
    app = FastAPI()
    app.state.repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path / "tsp-data"))
    # 项目根目录指到临时目录里：这样"并排的 quant-lab"也在临时目录里找，碰不到真实数据
    app.include_router(build_router(repo_root=tmp_path / "tsp", background=False))
    with TestClient(app) as client:
        yield client, tmp_path
    loader.clear_cache()


ABOVE_MA = {"pasted": {"name": "站上均线", "formula": "C>MA(C,{n});",
                       "params_spec": [{"key": "n", "label": "周期", "default": 5, "min": 2, "max": 60, "step": 1}]},
            "enabled": True, "params": {"n": 5}}
ALL = {k: False for k in ("exclude_st", "exclude_star", "exclude_chinext", "exclude_bse", "exclude_suspended")}


def test_extension_declares_a_valid_identity():
    assert EXTENSION_ID == "community.tdx-formula" and EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION


def test_without_data_the_page_is_told_what_to_do(env):
    client, _ = env
    status = client.get(f"{PREFIX}/status").json()
    assert status["configured"] is False and status["has_data"] is False
    r = client.post(f"{PREFIX}/stats", json={"conditions": [ABOVE_MA], "universe": ALL})
    assert r.status_code == 400 and "设置行情目录" in r.json()["detail"]
    r = client.put(f"{PREFIX}/config", json={"data_dir": str(env[1] / "nowhere")})
    assert r.status_code == 400 and "没有找到" in r.json()["detail"]


def test_finds_a_sibling_quant_lab_and_reads_it_without_writing(env):
    client, root = env
    market = make_market(root)
    before = sorted(str(p.relative_to(market)) for p in market.rglob("*"))
    status = client.get(f"{PREFIX}/status").json()
    assert status["configured"] and status["has_data"] and status["found_by"] == "与本项目并排的 quant-lab"
    assert status["stock_count"] == 3 and status["latest_date"] == str(DAYS[-1].date())
    assert status["float_shares_ready"] is True
    client.post(f"{PREFIX}/stats", json={"conditions": [ABOVE_MA], "universe": ALL})
    assert sorted(str(p.relative_to(market)) for p in market.rglob("*")) == before  # 只读：一个文件都没多


def test_data_dir_can_be_set_from_the_page(env, tmp_path_factory):
    client, _ = env
    other = make_market(tmp_path_factory.mktemp("elsewhere"))
    status = client.put(f"{PREFIX}/config", json={"data_dir": str(other.parent)}).json()  # 给 data 目录也行
    assert status["has_data"] and status["found_by"] == "页面上设置的目录" and status["data_dir"] == str(other)


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
    app.include_router(build_router(repo_root=tmp_path / "tsp"))
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
