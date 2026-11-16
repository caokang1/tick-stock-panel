"""“每日复盘”页首页用的数据：最近若干个交易日的走势（上证收盘、成交额、涨停跌停家数、两融余额）和当天的指标卡。

走势全部按本机日线现算，口径和复盘材料一致（沪深两市个股合计；涨停跌停含 ST）。
两融余额要联网取一次最近若干天的序列，当天取过的存在本机不重复取。
"""

from __future__ import annotations

import logging
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import remote

logger = logging.getLogger(__name__)

WINDOW = 15
INDEX = "000001.SH"


def _days(folder: Path, as_of: str | None, count: int) -> list[str]:
    days = sorted(p.name[5:] for p in folder.glob("date=*"))
    if as_of:
        days = [d for d in days if d <= as_of]
    return days[-count:]


def _read(folder: Path, day: str, columns: list[str]) -> Any:
    import pandas as pd

    files = sorted((folder / f"date={day}").glob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=columns)
    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return frame[[c for c in columns if c in frame.columns]]


def margin_history(root: Path, day: str) -> tuple[list[dict[str, Any]], str | None]:
    """最近若干天的全市场两融（旧的在前）。返回 (序列, 没取到时的原因)。"""
    cache = remote.load_cache(root, day)
    rows = cache.get("margin_history")
    if rows:
        return rows, None
    try:
        rows = remote.market_margin(WINDOW)
    except remote.UnavailableError as e:
        return [], str(e)
    rows = list(reversed(rows))
    remote.save_cache(root, day, {**cache, "margin_history": rows})
    return rows, None


def build(data_dir: Path, root: Path, as_of: str | None = None) -> dict[str, Any]:
    stock_dir, index_dir = data_dir / "kline_daily_enriched", data_dir / "kline_index_daily"
    days = _days(stock_dir, as_of, WINDOW + 1)  # 多读一天，用来算第一天的涨跌
    if not days:
        return {"as_of": None, "days": [], "margin": [], "margin_error": None}
    points: list[dict[str, Any]] = []
    prev = None
    for day in days:
        frame = _read(stock_dir, day, ["symbol", "close", "amount", "consecutive_limit_ups",
                                       "consecutive_limit_downs"]).drop_duplicates("symbol").set_index("symbol")
        market = frame[frame.index.str.endswith((".SH", ".SZ"))]
        point: dict[str, Any] = {
            "date": day,
            "amount": float(market["amount"].sum()) if "amount" in market else None,
            "limit_up": int((market.get("consecutive_limit_ups", 0) > 0).sum()),
            "limit_down": int((market.get("consecutive_limit_downs", 0) > 0).sum()),
            "up": None, "down": None, "flat": None, "sh_close": None, "sh_change": None,
        }
        if prev is not None and len(market):
            change = market["close"] / prev["close"].reindex(market.index) - 1.0
            point.update(up=int((change > 0).sum()), down=int((change < 0).sum()), flat=int((change == 0).sum()))
        index = _read(index_dir, day, ["symbol", "close"])
        hit = index[index["symbol"] == INDEX] if len(index) else index
        if len(hit):
            point["sh_close"] = float(hit["close"].iloc[0])
        points.append(point)
        prev = market
    for before, point in pairwise(points):
        if before["sh_close"] and point["sh_close"]:
            point["sh_change"] = point["sh_close"] / before["sh_close"] - 1.0
        if before["amount"] and point["amount"]:
            point["amount_change"] = point["amount"] - before["amount"]
    points = points[1:] if len(points) > WINDOW else points
    margin, error = margin_history(root, points[-1]["date"])
    return {"as_of": points[-1]["date"], "days": points, "margin": margin, "margin_error": error}
