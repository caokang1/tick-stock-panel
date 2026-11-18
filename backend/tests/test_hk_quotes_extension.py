"""港股报价扩展的测试。接口的返回是造的，不访问网络。"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.hk_quotes import EXTENSION_API_VERSION, EXTENSION_ID, routes, sina
from app.extensions import BACKEND_EXTENSION_API_VERSION

SMIC = ("SMIC,中芯国际,49.900,49.200,50.550,48.280,49.180,-0.020,-0.041,49.180,49.200,3811224066.340,77022819,"
        "69.909,0.000,93.000,41.060,2026/11/18,15:38:19,30|3,N|Y|Y")
FX = "15:37:56,0.8495599280,0.8495599280,0.8496321093,7.29,0.8496,0.8502,0.8495,0.8495599280,港元兑人民币"
TEXT = f'var hq_str_rt_hk00981="{SMIC}";\nvar hq_str_rt_hk00001="";\nvar hq_str_fx_shkdcny="{FX}";'


def test_extension_identity():
    assert EXTENSION_ID == "community.hk-quotes"
    assert EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION


def test_parse_reads_quote_and_rate_and_skips_empty():
    got = sina.parse(TEXT, ["00981.HK", "00001.HK", "09999.HK"])
    assert set(got["items"]) == {"00981.HK"}
    row = got["items"]["00981.HK"]
    assert row["name"] == "中芯国际" and row["last"] == 49.18 and row["prev"] == 49.2
    assert row["change_pct"] == pytest.approx(49.18 / 49.2 - 1)
    assert row["time"] == "2026-11-18 15:38:19"
    assert got["rate"] == pytest.approx(0.849559928)


def test_parse_tolerates_garbage():
    assert sina.parse("", ["00981.HK"]) == {"items": {}, "rate": None}
    assert sina.parse('var hq_str_fx_shkdcny="x,abc";', [])["rate"] is None


def test_clean_keeps_only_hk_symbols():
    assert routes.clean("00981.hk, 600519.SH,981.HK,00981.HK,;drop,01211.HK") == ["00981.HK", "01211.HK"]
    assert routes.clean("") == []


def test_last_close_skips_weekend_and_before_close():
    assert routes.last_close(datetime(2026, 11, 18, 17, 0)) == datetime(2026, 11, 18, 16, 10)  # 周三收盘后
    assert routes.last_close(datetime(2026, 11, 18, 10, 0)) == datetime(2026, 11, 17, 16, 10)  # 盘中 → 昨天
    assert routes.last_close(datetime(2026, 11, 16, 9, 0)) == datetime(2026, 11, 13, 16, 10)   # 周一早上 → 上周五
    assert routes.last_close(datetime(2026, 11, 15, 12, 0)) == datetime(2026, 11, 13, 16, 10)  # 周日


class Rig:
    def __init__(self, tmp_path, on=False, start=datetime(2026, 11, 18, 10, 0)):
        self.on, self.t, self.now, self.calls, self.fail = on, 0.0, start, [], False
        self.price = 49.18
        self.path = tmp_path / "hk" / "quotes.json"
        self.store = self.new()

    def new(self):
        return routes.Store(lambda: self.path, lambda: self.on, lambda: 60.0, self.load,
                            clock=lambda: self.t, now=lambda: self.now)

    def load(self, symbols):
        self.calls.append(list(symbols))
        if self.fail:
            raise RuntimeError("断网了")
        return {"items": {s: {"symbol": s, "name": "x", "last": self.price} for s in symbols if s != "09999.HK"},
                "rate": 0.85}


def test_switch_on_follows_interval(tmp_path):
    rig = Rig(tmp_path, on=True)
    assert rig.store.get(["00981.HK"])["items"]["00981.HK"]["last"] == 49.18
    rig.price, rig.t = 50.0, 30.0
    assert rig.store.get(["00981.HK"])["items"]["00981.HK"]["last"] == 49.18  # 间隔没到
    rig.t = 61.0
    got = rig.store.get(["00981.HK"])
    assert got["items"]["00981.HK"]["last"] == 50.0 and got["live"] is True and got["rate"] == 0.85
    assert len(rig.calls) == 2


def test_switch_off_fetches_new_symbol_once_then_only_after_close(tmp_path):
    rig = Rig(tmp_path, on=False, start=datetime(2026, 11, 18, 10, 0))
    assert rig.store.get(["00981.HK"])["items"]  # 刚登记、从没取到过：取一次
    rig.price, rig.t = 50.0, 1000.0
    got = rig.store.get(["00981.HK"])  # 盘中、开关关着：不再取
    assert got["items"]["00981.HK"]["last"] == 49.18 and got["live"] is False and got["error"] is None
    assert len(rig.calls) == 1
    rig.now, rig.t = datetime(2026, 11, 18, 16, 30), 2000.0  # 收盘以后：取一次收盘价
    assert rig.store.get(["00981.HK"])["items"]["00981.HK"]["last"] == 50.0
    rig.t = 3000.0
    rig.store.get(["00981.HK"])
    assert len(rig.calls) == 2


def test_unknown_symbol_does_not_refetch_faster_than_interval(tmp_path):
    rig = Rig(tmp_path, on=False)
    rig.store.get(["09999.HK"])
    rig.t = 10.0
    assert rig.store.get(["09999.HK"])["items"] == {}
    assert len(rig.calls) == 1


def test_failure_keeps_last_and_reports_reason_and_survives_restart(tmp_path):
    rig = Rig(tmp_path, on=True)
    rig.store.get(["00981.HK"])
    rig.fail, rig.t = True, 100.0
    got = rig.store.get(["00981.HK"])
    assert got["items"]["00981.HK"]["last"] == 49.18 and "断网" in got["error"]
    rig.on, rig.t = False, 200.0
    again = rig.new().get(["00981.HK"])  # 重启后从本机文件读回
    assert again["items"]["00981.HK"]["last"] == 49.18 and again["rate"] == 0.85 and again["error"] is None
    assert routes.read_saved(tmp_path / "none.json") == {"items": {}, "rate": None, "fetched_at": None}


def test_no_symbols_never_fetches(tmp_path):
    rig = Rig(tmp_path, on=True)
    assert rig.store.get([])["items"] == {}
    assert rig.calls == []


def test_route(tmp_path):
    rig = Rig(tmp_path, on=True)
    app = FastAPI()
    app.include_router(routes.build_router(rig.store))
    body = TestClient(app).get("/api/custom/hk-quotes/quotes", params={"symbols": "00981.HK,600519.SH"}).json()
    assert list(body["items"]) == ["00981.HK"] and body["rate"] == 0.85 and body["live"] is True
    assert body["interval"] > 0
