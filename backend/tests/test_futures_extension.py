"""期货行情扩展的测试。接口的返回是造的，不访问网络。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.futures import EXTENSION_API_VERSION, EXTENSION_ID, routes, sina
from app.custom.futures.catalog import BY_CODE, ITEMS
from app.extensions import BACKEND_EXTENSION_API_VERSION

FIN = ("3928.000,3938.400,3908.000,3912.600,26482,102384360.800,169782.000,0.000,0.000,4310.600,3527.000,0.000,0.000,"
       "3918.200,3918.800,177584.000,3918.000,1,0.000,0,0.000,0,0.000,0,0.000,0,3918.600,5,0.000,0,0.000,0,0.000,0,"
       "0.000,0,2026-11-18,11:08:36,300,1,,,,,,,,,3923.223,沪深300指数期货连续")
COM = ("黄金连续,110837,832.460,839.400,818.260,0.000,819.860,820.000,819.860,0.000,834.020,5,3,147415.000,110060,"
       "沪,黄金,2026-11-18,1,,,,,,,,,828.443")
HF = "3800.654,,3802.800,3803.100,3859.300,3793.000,11:08:29,3864.200,3855.200,0,1,1,2026-11-18,纽约黄金,0"
SPOT = "沪深300,3950.00,3960.00,3957.1291,3970.00,3940.00"
QUOTES = "\n".join(f'var hq_str_{code}="{body}";' for code, body in (
    ("nf_IF0", FIN), ("nf_AU0", COM), ("hf_GC", HF), ("sh000300", SPOT), ("nf_AG0", ""), ("nf_CU0", "铜连续,1,0,0,0,0,0,0,0.000"),
))


def test_extension_identity():
    assert EXTENSION_ID == "community.futures"
    assert EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION


def test_catalog_codes_are_unique_and_index_futures_have_spot():
    assert len(BY_CODE) == len(ITEMS)
    assert all(item.spot for item in ITEMS if item.group == "股指期货")
    assert BY_CODE["GC"].query == "hf_GC" and BY_CODE["AU0"].query == "nf_AU0"


def test_parse_quotes_reads_three_layouts_and_skips_empty_rows():
    rows = {r["code"]: r for r in sina.parse_quotes(QUOTES)}
    assert set(rows) == {"IF0", "AU0", "GC"}  # 空行和最新价为 0 的不要

    fin = rows["IF0"]
    assert fin["last"] == 3912.6 and fin["prev"] == 3918.8
    assert fin["change_pct"] == pytest.approx(3912.6 / 3918.8 - 1)
    assert fin["volume"] == 26482 and fin["open_interest"] == 169782
    assert fin["time"] == "2026-11-18 11:08:36"
    assert fin["spot"] == 3957.1291
    assert fin["basis"] == pytest.approx(3912.6 - 3957.1291)

    com = rows["AU0"]
    assert (com["open"], com["high"], com["low"], com["last"], com["prev"]) == (832.46, 839.4, 818.26, 819.86, 834.02)
    assert com["volume"] == 110060 and com["open_interest"] == 147415
    assert com["time"] == "2026-11-18 11:08:37"
    assert com["spot"] is None and com["basis"] is None

    hf = rows["GC"]
    assert (hf["last"], hf["high"], hf["low"], hf["prev"], hf["open"]) == (3800.654, 3859.3, 3793.0, 3864.2, 3855.2)
    assert hf["volume"] is None and hf["time"] == "2026-11-18 11:08:29"


def test_parse_quotes_tolerates_garbage():
    assert sina.parse_quotes("") == []
    assert sina.parse_quotes('var hq_str_nf_IF0="a,b,c";') == []


def test_parse_kline_accepts_both_field_styles_and_limits():
    inner = '/*x*/\nvar _x=([{"d":"2026-11-14","o":"1","h":"2","l":"0.5","c":"1.5","v":"9"},' \
            '{"d":"2026-11-17","o":"834.44","h":"838.88","l":"828.34","c":"834.66"}]);'
    assert sina.parse_kline(inner, 1) == [
        {"date": "2026-11-17", "open": 834.44, "high": 838.88, "low": 828.34, "close": 834.66}]
    outer = 'var _x=([{"date":"2026-11-18","open":"3851.3","high":"3859.3","low":"3793.0","close":"3802.0"},' \
            '{"date":"2026-11-19","open":"x","high":"1","low":"1","close":"1"}]);'
    assert [b["date"] for b in sina.parse_kline(outer, 10)] == ["2026-11-18"]
    assert sina.parse_kline("不是数据", 10) == []


def make_store(tmp_path, state, now):
    return routes.Store(lambda: tmp_path, lambda: state["live"], lambda: state["interval"], clock=lambda: now[0])


def test_store_waits_for_interval_and_keeps_last_value_on_failure(tmp_path):
    now, state = [0.0], {"live": True, "interval": 30}
    store = make_store(tmp_path, state, now)
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("断了")
        return len(calls)

    assert store.get("k", load)["value"] == 1
    now[0] = 10
    assert store.get("k", load)["value"] == 1 and len(calls) == 1  # 间隔内不重取
    now[0] = 31
    failed = store.get("k", load)
    assert failed["value"] == 1 and failed["error"] == "断了"  # 失败时给上一次的
    now[0] = 40
    assert store.get("k", load)["error"] == "断了" and len(calls) == 2  # 失败后也等满间隔
    now[0] = 62
    ok = store.get("k", load)
    assert ok["value"] == 3 and ok["error"] is None and ok["live"] is True


def test_store_follows_the_interval_setting(tmp_path):
    now, state = [0.0], {"live": True, "interval": 60}
    store = make_store(tmp_path, state, now)
    calls = []
    load = lambda: calls.append(1) or len(calls)  # noqa: E731
    store.get("k", load)
    now[0] = 45
    store.get("k", load)
    assert len(calls) == 1
    state["interval"] = 30  # 设置改小后立即按新的算
    store.get("k", load)
    assert len(calls) == 2


def test_store_never_fetches_when_realtime_is_off_and_serves_saved_data(tmp_path):
    now, state = [0.0], {"live": False, "interval": 30}
    calls = []
    load = lambda: calls.append(1) or {"n": len(calls)}  # noqa: E731

    empty = make_store(tmp_path, state, now).get("k", load)
    assert empty["value"] is None and empty["live"] is False and calls == []  # 关着：不取

    state["live"] = True
    first = make_store(tmp_path, state, now).get("k", load)
    assert first["value"] == {"n": 1}

    state["live"] = False
    now[0] = 9999
    again = make_store(tmp_path, state, now).get("k", load)  # 新建一个，相当于重启
    assert again["value"] == {"n": 1} and again["fetched_at"] == first["fetched_at"]
    assert again["live"] is False and again["error"] is None and len(calls) == 1


def test_store_ignores_a_broken_file(tmp_path):
    (tmp_path / "k.json").write_text("坏的", encoding="utf-8")
    store = make_store(tmp_path, {"live": False, "interval": 30}, [0.0])
    assert store.get("k", lambda: 1)["value"] is None


@pytest.fixture
def client(monkeypatch, tmp_path):
    urls = []
    state = {"live": True, "interval": 30}

    def fake_call(url, what):
        urls.append(url)
        return QUOTES if "hq.sinajs.cn" in url else 'var _x=([{"d":"2026-11-17","o":"1","h":"2","l":"1","c":"2"}]);'

    monkeypatch.setattr(sina, "_call", fake_call)
    monkeypatch.setattr(routes, "interval", lambda: state["interval"])
    app = FastAPI()
    app.include_router(routes.build_router(routes.Store(lambda: tmp_path, lambda: state["live"], lambda: state["interval"])))
    return TestClient(app), urls, state


def test_quotes_endpoint_asks_once_for_everything(client):
    http, urls, _ = client
    body = http.get("/api/custom/futures/quotes").json()
    assert [r["code"] for r in body["items"]] == ["IF0", "AU0", "GC"]
    assert body["error"] is None and body["fetched_at"] and body["live"] is True and body["interval"] == 30
    http.get("/api/custom/futures/quotes")
    assert len(urls) == 1  # 第二次用的是存下来的
    assert all(code in urls[0] for code in ("nf_IF0", "nf_AU0", "hf_XAU", "sh000852"))


def test_endpoints_do_not_go_online_when_realtime_is_off(client):
    http, urls, state = client
    state["live"] = False
    body = http.get("/api/custom/futures/quotes").json()
    assert body["items"] == [] and body["live"] is False and body["error"] is None
    kline = http.get("/api/custom/futures/kline", params={"code": "AU0"}).json()
    assert kline["bars"] == [] and kline["live"] is False
    assert urls == []


def test_kline_endpoint(client):
    http, urls, _ = client
    body = http.get("/api/custom/futures/kline", params={"code": "AU0"}).json()
    assert body["name"] == "沪金" and body["bars"][0]["close"] == 2.0
    assert "InnerFuturesNewService" in urls[-1]
    http.get("/api/custom/futures/kline", params={"code": "GC"})
    assert "GlobalFuturesService" in urls[-1]
    assert http.get("/api/custom/futures/kline", params={"code": "不存在"}).status_code == 404
