"""首页总市值榜: 取前十, 按 收盘价 x 总股本 排序。"""
from app.services.market_overview_builder import _market_cap, _top_rows


def test_market_cap_needs_price_and_shares():
    assert _market_cap({"close": 10.0, "total_shares": 2e8}) == 2e9
    assert _market_cap({"close": 10.0}) is None
    assert _market_cap({"close": 0, "total_shares": 2e8}) is None
    assert _market_cap({"close": float("nan"), "total_shares": 2e8}) is None


def test_lists_take_top_ten_sorted_by_market_cap():
    rows = [{"symbol": f"{i:06d}.SZ", "close": 1.0, "total_shares": float(i)} for i in range(1, 16)]
    rows.append({"symbol": "600000.SH", "close": 5.0})  # 没有总股本, 不上榜
    for r in rows:
        r["market_cap"] = _market_cap(r)
    top = _top_rows(rows, "market_cap", True)
    assert len(top) == 10
    assert [r["market_cap"] for r in top] == [float(i) for i in range(15, 5, -1)]
    assert all(r["symbol"] != "600000.SH" for r in top)


def test_industry_amount_leaders_rank_by_amount_with_market_share():
    from app.services.market_overview_builder import _amount_leaders

    items = [{"name": f"行业{i}", "amount": float(i)} for i in range(0, 13)]
    top = _amount_leaders(items, total_amount=200.0)
    assert [it["name"] for it in top] == [f"行业{i}" for i in range(12, 2, -1)]
    assert top[0]["share_pct"] == 6.0
    assert _amount_leaders(items, total_amount=0)[0]["share_pct"] is None
    assert "share_pct" not in items[12]  # 不改动原来的板块数据
