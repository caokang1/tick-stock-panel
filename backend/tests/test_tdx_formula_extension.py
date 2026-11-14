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
    app.include_router(build_router(repo_root=tmp_path / "tsp"))
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


def test_saved_formulas_are_kept_in_the_extension_folder(env):
    client, root = env
    item = client.post(f"{PREFIX}/saved", json={"name": "我的公式", "formula": "C>MA(C,5);",
                                                 "conditions": [ABOVE_MA], "universe": ALL}).json()
    assert item["id"] and item["universe"]["exclude_st"] is False
    assert (root / "tsp-data" / "user_data" / "extensions" / "tdx_formula" / "saved.json").exists()
    dup = client.post(f"{PREFIX}/saved", json={"name": "我的公式", "conditions": [ABOVE_MA]})
    assert dup.status_code == 400 and "换个名字" in dup.json()["detail"]
    renamed = client.post(f"{PREFIX}/saved", json={"id": item["id"], "name": "改名了", "conditions": [ABOVE_MA]})
    assert renamed.json()["id"] == item["id"]
    assert [x["name"] for x in client.get(f"{PREFIX}/saved").json()] == ["改名了"]
    assert client.delete(f"{PREFIX}/saved/{item['id']}").json() == {"ok": True}
    assert client.delete(f"{PREFIX}/saved/{item['id']}").status_code == 404
    assert client.get(f"{PREFIX}/saved").json() == []


def test_blocks_lists_the_indicator_library(env):
    client, _ = env
    blocks = client.get(f"{PREFIX}/blocks").json()
    assert len(blocks) >= 30 and {"id", "name", "formula", "params", "category"} <= set(blocks[0])


def test_a_fake_repository_never_becomes_a_directory_on_disk(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.state.repo = MagicMock()
    app.include_router(build_router(repo_root=tmp_path / "tsp"))
    with TestClient(app) as client:
        assert client.get(f"{PREFIX}/status").status_code == 503
        r = client.post(f"{PREFIX}/saved", json={"name": "x", "conditions": [ABOVE_MA]})
        assert r.status_code == 503
    assert list(tmp_path.iterdir()) == []
