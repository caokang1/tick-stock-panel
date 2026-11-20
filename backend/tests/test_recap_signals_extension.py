"""复盘信号：摘价位、核对、写规则、过期清除。全部用假数据和假的 AI，不联网，不碰真实数据。"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.custom.recap_signals import extract, routes
from app.strategy import monitor_rules

RECAP = """### 三、行业
无关内容 99.99

### 四、持仓复盘

#### 1. 甲股份（000001）

- 收盘 10.00，成本 8.50。
- 操作建议：以近 25 日低点 9.50 为防守线，收盘跌破再减半。若放量突破 10.30 可停止减仓。借反弹到 12.80 减仓。

#### 2. 乙科技（688001）

- 收盘 20.00。若继续跌破 20.00 创新低，减半。

#### 3. 丙控股（00700.HK）

- 报价 300，观望。

### 五、明日计划
甲股份 7.77
"""

HOLDINGS = [
    {"symbol": "000001.SZ", "name": "甲股份", "close": 10.0, "asset_type": "stock"},
    {"symbol": "688001.SH", "name": "乙科技", "close": 20.0, "asset_type": "stock"},
]


def _blocks():
    return extract.blocks_of(extract.holding_section(RECAP), HOLDINGS)


def test_holding_section_stops_at_next_heading():
    section = extract.holding_section(RECAP)
    assert "甲股份" in section and "明日计划" not in section and "99.99" not in section
    assert extract.holding_section("### 一、市场\n没有持仓") == ""


def test_blocks_are_split_per_holding():
    blocks = _blocks()
    assert set(blocks) == {"000001.SZ", "688001.SH"}
    assert "9.50" in blocks["000001.SZ"] and "9.50" not in blocks["688001.SH"]


def test_parse_reply_tolerates_wrapping_text():
    assert extract.parse_reply('好的：\n```json\n[{"symbol": "000001.SZ"}]\n```') == [{"symbol": "000001.SZ"}]
    with pytest.raises(ValueError):
        extract.parse_reply("没有可摘的内容")


def test_clean_keeps_only_levels_written_in_that_holdings_block():
    items = [
        {"symbol": "000001.SZ", "direction": "below", "price": 9.5, "action": "收盘跌破再减半"},
        {"symbol": "000001.SZ", "direction": "above", "price": 10.3, "action": "停止减仓"},
        {"symbol": "000001.SZ", "direction": "below", "price": 9.5, "action": "重复的"},
        {"symbol": "000001.SZ", "direction": "below", "price": 9.6, "action": "AI 自己编的价位"},
        {"symbol": "000001.SZ", "direction": "above", "price": 12.8, "action": "一天到不了"},
        {"symbol": "000001.SZ", "direction": "below", "price": 8.5, "action": "成本价，而且一天到不了"},
        {"symbol": "688001.SH", "direction": "below", "price": 20.0, "action": "跌破创新低减半"},
        {"symbol": "688001.SH", "direction": "below", "price": 9.5, "action": "别的股票的价位"},
        {"symbol": "00700.HK", "direction": "below", "price": 300, "action": "港股监控不了"},
        {"symbol": "000001.SZ", "direction": "sideways", "price": 9.5, "action": "方向不对"},
    ]
    kept, dropped = extract.clean(items, HOLDINGS, _blocks())
    assert [(s["symbol"], s["direction"], s["price"]) for s in kept] == [
        ("000001.SZ", "below", 9.5), ("000001.SZ", "above", 10.3), ("688001.SH", "below", 20.0)]
    assert len(dropped) == 6
    assert any("原文里没有" in d for d in dropped) and any("到不了" in d for d in dropped)


def test_clean_drops_levels_already_crossed_at_close():
    holdings = [{"symbol": "000001.SZ", "name": "甲股份", "close": 9.4, "asset_type": "stock"}]
    kept, dropped = extract.clean(
        [{"symbol": "000001.SZ", "direction": "below", "price": 9.5, "action": "x"}], holdings, _blocks())
    assert kept == [] and "已经跌破" in dropped[0]


def test_limit_pct_by_board():
    assert extract.limit_pct("000001.SZ", "甲股份", "stock") == 0.10
    assert extract.limit_pct("688001.SH", "乙科技", "stock") == 0.20
    assert extract.limit_pct("300001.SZ", "丁", "stock") == 0.20
    assert extract.limit_pct("000002.SZ", "*ST 戊", "stock") == 0.05
    assert extract.limit_pct("510300.SH", "沪深300ETF", "etf") == 0.20


def test_to_rules_are_valid_monitor_rules():
    kept, _ = extract.clean([
        {"symbol": "000001.SZ", "direction": "below", "price": 9.5, "action": "收盘跌破再减半"},
        {"symbol": "000001.SZ", "direction": "above", "price": 10.3, "action": ""},
    ], HOLDINGS, _blocks())
    rules = extract.to_rules(kept, "2026-11-19", ["feishu"])
    assert [r["id"] for r in rules] == ["rs_000001_d1", "rs_000001_u1"]
    assert rules[0]["conditions"] == [{"field": "close", "op": "<", "value": 9.5}]
    assert rules[0]["name"] == "复盘信号 · 甲股份 跌破 9.5" and "收盘跌破再减半" in rules[0]["message"]
    assert rules[1]["conditions"][0]["op"] == ">" and rules[1]["webhook_channels"] == ["feishu"]
    for rule in rules:
        monitor_rules.validate(monitor_rules.normalize(rule))


def test_pick_recap_only_takes_post_close_reports_of_latest_day():
    day = date(2026, 11, 19)
    reports = [
        {"id": "a", "as_of": "2026-11-19", "content": "x", "created_at": "2026-11-19T11:00:00"},
        {"id": "b", "as_of": "2026-11-19", "content": "x", "created_at": "2026-11-19T18:07:00"},
        {"id": "c", "as_of": "2026-11-19", "content": "x", "created_at": "2026-11-19T19:30:00"},
        {"id": "d", "as_of": "2026-11-18", "content": "x", "created_at": "2026-11-19T20:00:00"},
    ]
    assert routes.pick_recap(reports, day)["id"] == "c"
    assert routes.pick_recap(reports[:1], day) is None
    assert routes.pick_recap(reports, None) is None


def test_expired_after_the_next_trading_day_has_closed():
    state = {"recap_day": "2026-11-19", "rule_ids": ["rs_000001_d1"]}
    same, nxt = date(2026, 11, 19), date(2026, 11, 20)
    assert not routes.expired(state, same, datetime(2026, 11, 20, 10, 0))   # 第二天盘中，行情还是前一天的
    assert not routes.expired(state, nxt, datetime(2026, 11, 20, 10, 0))    # 第二天盘中，当天行情提前进来了
    assert routes.expired(state, nxt, datetime(2026, 11, 20, 17, 45))       # 第二天收盘后
    assert routes.expired(state, nxt, datetime(2026, 11, 21, 9, 0))         # 第三天
    assert not routes.expired(state, same, datetime(2026, 11, 23, 10, 0))   # 周末、假期：没有新的交易日
    assert routes.expired(state, same, datetime(2026, 12, 5, 10, 0))        # 行情一直没更新的兜底
    assert not routes.expired({"recap_day": "2026-11-19", "rule_ids": []}, nxt, datetime(2026, 11, 21, 9, 0))


# ---------------- 整条流程（假的面板、假的 AI） ----------------


class _Repo:
    def __init__(self, data_dir, latest):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.latest = latest

    def latest_enriched_date(self):
        return self.latest

    def resolve_asset_type(self, symbol):
        return "stock"


@pytest.fixture
def panel(tmp_path, monkeypatch):
    reports = [{"id": "r1", "as_of": "2026-11-19", "content": RECAP, "created_at": "2026-11-19T18:07:00"}]
    monkeypatch.setattr("app.services.market_recap_reports.list_reports", lambda: reports)
    monkeypatch.setattr("app.services.preferences.get_webhook_default_channels", lambda: ["feishu"])
    monkeypatch.setattr(routes, "holdings_for", lambda repo, data_dir, day: (list(HOLDINGS), ["00700.HK"]))
    app = SimpleNamespace(state=SimpleNamespace(repo=_Repo(tmp_path, date(2026, 11, 19))))
    reloads = []
    calls = []

    def ask(system, user):
        calls.append(user)
        return json.dumps([
            {"symbol": "000001.SZ", "direction": "below", "price": 9.5, "action": "收盘跌破再减半"},
            {"symbol": "688001.SH", "direction": "below", "price": 20.0, "action": "跌破创新低减半"},
        ])

    return SimpleNamespace(app=app, reports=reports, ask=ask, calls=calls, reloads=reloads,
                           reload=lambda a: reloads.append(1), dir=tmp_path,
                           state=lambda: routes._read_json(routes.root_of(tmp_path) / "state.json"))


def _ids(data_dir):
    return sorted(r["id"] for r in monitor_rules.load_all(data_dir))


def test_tick_generates_once_then_replaces_and_expires(panel):
    monitor_rules.save_one(panel.dir, monitor_rules.normalize({
        "id": "mine", "name": "我自己的规则", "type": "price", "symbols": ["000001.SZ"],
        "conditions": [{"field": "close", "op": ">", "value": 1}]}))

    message = routes.tick(panel.app, datetime(2026, 11, 19, 18, 8), panel.ask, panel.reload)
    assert "生成了 2 条信号" in message
    assert _ids(panel.dir) == ["mine", "rs_000001_d1", "rs_688001_d1"]
    assert panel.state()["skipped_hk"] == ["00700.HK"] and len(panel.reloads) == 1
    assert "000001.SZ 甲股份" in panel.calls[0] and "明日计划" not in panel.calls[0]

    # 同一份复盘不会再生成第二遍
    assert routes.tick(panel.app, datetime(2026, 11, 19, 18, 9), panel.ask, panel.reload) is None
    assert len(panel.calls) == 1

    # 第二天盘中：信号还在
    assert routes.tick(panel.app, datetime(2026, 11, 20, 10, 0), panel.ask, panel.reload) is None
    assert "rs_000001_d1" in _ids(panel.dir)

    # 第二天收盘、行情进来：清掉，自己的规则不动；新的复盘还没出来，不重新生成
    panel.app.state.repo.latest = date(2026, 11, 20)
    message = routes.tick(panel.app, datetime(2026, 11, 20, 17, 45), panel.ask, panel.reload)
    assert "已清除" in message and _ids(panel.dir) == ["mine"] and len(panel.calls) == 1

    # 新的复盘出来：换成新的一批
    panel.reports.append({"id": "r2", "as_of": "2026-11-20", "content": RECAP, "created_at": "2026-11-20T18:07:00"})
    routes.tick(panel.app, datetime(2026, 11, 20, 18, 8), panel.ask, panel.reload)
    assert _ids(panel.dir) == ["mine", "rs_000001_d1", "rs_688001_d1"] and panel.state()["recap_id"] == "r2"


def test_regenerated_recap_replaces_old_signals(panel):
    routes.tick(panel.app, datetime(2026, 11, 19, 18, 8), panel.ask, panel.reload)
    panel.reports.append({"id": "r1b", "as_of": "2026-11-19", "content": RECAP, "created_at": "2026-11-19T20:00:00"})

    def only_one(system, user):
        return json.dumps([{"symbol": "000001.SZ", "direction": "above", "price": 10.3, "action": "停止减仓"}])

    routes.tick(panel.app, datetime(2026, 11, 19, 20, 1), only_one, panel.reload)
    assert _ids(panel.dir) == ["rs_000001_u1"]


def test_ai_failure_keeps_old_signals_and_stops_after_three_tries(panel):
    routes.tick(panel.app, datetime(2026, 11, 19, 18, 8), panel.ask, panel.reload)
    panel.reports.append({"id": "r1b", "as_of": "2026-11-19", "content": RECAP, "created_at": "2026-11-19T20:00:00"})
    tries = []

    def broken(system, user):
        tries.append(1)
        raise RuntimeError("AI 没有回应")

    for minute in range(1, 6):
        routes.tick(panel.app, datetime(2026, 11, 19, 20, minute), broken, panel.reload)
    assert len(tries) == routes.MAX_ATTEMPTS
    assert "没有成功" in panel.state()["message"]
    assert _ids(panel.dir) == ["rs_000001_d1", "rs_688001_d1"]


def test_disabled_does_nothing(panel):
    routes._write_json(routes.root_of(panel.dir) / "config.json", {"enabled": False})
    assert routes.tick(panel.app, datetime(2026, 11, 19, 18, 8), panel.ask, panel.reload) is None
    assert _ids(panel.dir) == [] and panel.calls == []


def test_recap_without_levels_clears_previous_signals(panel):
    routes.tick(panel.app, datetime(2026, 11, 19, 18, 8), panel.ask, panel.reload)
    panel.reports.append({"id": "r1b", "as_of": "2026-11-19", "content": RECAP, "created_at": "2026-11-19T20:00:00"})
    message = routes.tick(panel.app, datetime(2026, 11, 19, 20, 1), lambda s, u: "[]", panel.reload)
    assert "没有生成信号" in message and _ids(panel.dir) == []
