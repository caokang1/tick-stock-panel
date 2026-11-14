"""信号统计：信号出现后按固定持有期买卖的历史表现。口径见技术设计文档 §6。

未来收益只在这里计算，结果只作为统计输出，不回流成信号的输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql.core import limits
from app.custom.tdx_formula.ql.core.panel import Panel

ENTRY_NEXT_OPEN = "next_open"
ENTRY_SAME_CLOSE = "same_close"
ENTRIES = (ENTRY_NEXT_OPEN, ENTRY_SAME_CLOSE)
DEFAULT_HOLDS = (1, 3, 5, 10)
TRADING_DAYS_PER_YEAR = 244


@dataclass(frozen=True)
class Costs:
    commission: float = 0.00025
    min_commission: float = 5.0  # 信号统计按比例计算，不使用最低佣金
    stamp_tax: float = 0.0005
    slippage: float = 0.001

    @property
    def buy_rate(self) -> float:
        return self.commission + self.slippage

    @property
    def sell_rate(self) -> float:
        return self.commission + self.stamp_tax + self.slippage


@dataclass(frozen=True)
class Frequency:
    avg_per_day: float
    empty_day_ratio: float
    per_year: float
    total: int


@dataclass(frozen=True)
class StatRow:
    entry: str
    hold: int
    count: int
    win_rate: float | None
    avg_return: float | None
    median_return: float | None
    avg_drawdown: float | None
    worst_drawdown: float | None
    skipped_unbuyable: int


@dataclass(frozen=True)
class Trades:
    """一组按同一口径成交的交易。数组等长。"""

    signal_row: np.ndarray  # 信号日在 panel.dates 里的下标
    col: np.ndarray  # 股票在 panel.codes 里的下标
    buy_row: np.ndarray
    sell_row: np.ndarray
    ret: np.ndarray  # 扣成本后的收益
    drawdown: np.ndarray  # 持有期间相对买入价的最大跌幅（<= 0）
    skipped_unbuyable: int


@dataclass(frozen=True)
class SignalRecord:
    date: str
    code: str
    returns: dict[int, float | None]  # 持有天数 -> 收益（next_open 口径），无法计算为 None


def clean_signal(signal: np.ndarray, panel: Panel) -> np.ndarray:
    """未上市、已退市、停牌的格子上信号一律为 False。"""
    sig = np.asarray(signal)
    if sig.dtype != bool:
        with np.errstate(invalid="ignore"):
            sig = np.nan_to_num(sig.astype(float), nan=0.0) != 0
    return sig & panel.tradable


def signal_frequency(signal: np.ndarray, panel: Panel) -> Frequency:
    sig = clean_signal(signal, panel)
    t = sig.shape[0]
    if t == 0:
        return Frequency(0.0, 1.0, 0.0, 0)
    per_day = sig.sum(axis=1)
    total = int(per_day.sum())
    return Frequency(
        avg_per_day=float(total / t),
        empty_day_ratio=float((per_day == 0).mean()),
        per_year=float(total / t * TRADING_DAYS_PER_YEAR),
        total=total,
    )


class _Market:
    """一次统计里各口径共用的中间结果。"""

    def __init__(self, panel: Panel) -> None:
        self.panel = panel
        tradable = panel.tradable
        self.can_buy_open = tradable & ~limits.opens_at_limit_up(panel)
        self.can_buy_close = tradable & ~limits.is_limit_up_close(panel)
        self.can_sell = tradable & ~limits.is_limit_down_close(panel)
        t, n = panel.shape
        # next_sell[r, j]：从第 r 行起（含）第一个能卖出的行；没有则为 -1
        nxt = np.full((t + 1, n), -1, dtype=np.int64)
        for r in range(t - 1, -1, -1):
            nxt[r] = np.where(self.can_sell[r], r, nxt[r + 1])
        self.next_sell = nxt
        # last_listed[j]：最后一个已上市的行；用于"退市前一直卖不出"的情况
        rows = np.arange(t)[:, None]
        self.last_listed = np.where(panel.listed, rows, -1).max(axis=0) if t else np.full(n, -1)
        self.still_listed_at_end = panel.listed[-1] if t else np.zeros(n, dtype=bool)


def _resolve_sell_rows(mkt: _Market, target: np.ndarray, col: np.ndarray, buy_row: np.ndarray) -> np.ndarray:
    """目标卖出行不可卖时顺延。返回实际卖出行，无法卖出为 -1。"""
    t = mkt.panel.shape[0]
    sell = np.full(target.shape, -1, dtype=np.int64)
    inside = target < t
    sell[inside] = mkt.next_sell[target[inside], col[inside]]
    # 直到数据末尾都没有可卖的一天：如果股票在此期间退市，按最后一个上市日的收盘价了结；
    # 如果只是数据到头了（股票仍在上市），这笔交易不计入
    # 目标卖出日已经在退市之后的情况也算在内：只要买入之后股票退市了，就按最后一个上市日了结。
    # 把这些交易丢掉会让统计结果偏乐观（退市前通常是连续大跌）。
    stuck = sell < 0
    if stuck.any():
        delisted = ~mkt.still_listed_at_end[col[stuck]]
        last = mkt.last_listed[col[stuck]]
        ok = delisted & (last > buy_row[stuck])
        idx = np.nonzero(stuck)[0][ok]
        sell[idx] = last[ok]
    return sell


def _window_min_low(low: np.ndarray, start: np.ndarray, end: np.ndarray, col: np.ndarray) -> np.ndarray:
    """每笔交易在 [start, end] 行区间内的最低价。按窗口长度分组向量化计算。"""
    out = np.full(start.shape, np.nan)
    length = end - start + 1
    for size in np.unique(length):
        if size <= 0:
            continue
        m = length == size
        acc = low[start[m], col[m]].copy()
        for k in range(1, int(size)):
            acc = np.fmin(acc, low[start[m] + k, col[m]])
        out[m] = acc
    return out


def compute_trades(signal: np.ndarray, panel: Panel, entry: str, hold: int, costs: Costs | None = None,
                   _market: _Market | None = None, with_drawdown: bool = True) -> Trades:
    """with_drawdown=False 时不算持有期间的回撤（全为 0）。只需要收益、信号又特别多时用它，快得多。"""
    costs = costs or Costs()
    mkt = _market or _Market(panel)
    sig = clean_signal(signal, panel)
    t, _ = panel.shape
    sig_row, col = np.nonzero(sig)

    if entry == ENTRY_NEXT_OPEN:
        buy_row = sig_row + 1
        has_buy_day = buy_row < t
        sig_row, col, buy_row = sig_row[has_buy_day], col[has_buy_day], buy_row[has_buy_day]
        buyable = mkt.can_buy_open[buy_row, col]
        price_matrix = panel.open
    elif entry == ENTRY_SAME_CLOSE:
        buy_row = sig_row
        buyable = mkt.can_buy_close[buy_row, col]
        price_matrix = panel.close
    else:
        raise ValueError(f"未知的买入方式：{entry}")

    # 持有期超出数据末尾的信号不计入，也不算"买不进"；但买入后股票在目标卖出日之前退市的要计入
    delisted_later = ~mkt.still_listed_at_end[col] & (mkt.last_listed[col] > buy_row)
    in_range = (buy_row + hold < t) | delisted_later
    skipped = int((~buyable & in_range).sum())
    keep = buyable & in_range
    sig_row, col, buy_row = sig_row[keep], col[keep], buy_row[keep]

    sell_row = _resolve_sell_rows(mkt, buy_row + hold, col, buy_row)
    sold = sell_row >= 0
    sig_row, col, buy_row, sell_row = sig_row[sold], col[sold], buy_row[sold], sell_row[sold]

    buy_price = price_matrix[buy_row, col]
    sell_price = panel.close[sell_row, col]
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = sell_price * (1 - costs.sell_rate) / (buy_price * (1 + costs.buy_rate)) - 1
        # 回撤区间：次日开盘买入从买入当天算起；尾盘买入从买入的下一天算起
        if with_drawdown:
            dd_start = buy_row if entry == ENTRY_NEXT_OPEN else buy_row + 1
            min_low = _window_min_low(panel.low, dd_start, sell_row, col)
            drawdown = np.minimum(min_low / buy_price - 1, 0.0)
        else:
            drawdown = np.zeros(ret.shape)

    valid = np.isfinite(ret) & np.isfinite(drawdown)
    return Trades(
        signal_row=sig_row[valid], col=col[valid], buy_row=buy_row[valid], sell_row=sell_row[valid],
        ret=ret[valid], drawdown=drawdown[valid], skipped_unbuyable=skipped,
    )


def _summarize(entry: str, hold: int, trades: Trades) -> StatRow:
    n = int(trades.ret.size)
    if n == 0:
        return StatRow(entry, hold, 0, None, None, None, None, None, trades.skipped_unbuyable)
    return StatRow(
        entry=entry,
        hold=hold,
        count=n,
        win_rate=float((trades.ret > 0).mean()),
        avg_return=float(trades.ret.mean()),
        median_return=float(np.median(trades.ret)),
        avg_drawdown=float(trades.drawdown.mean()),
        worst_drawdown=float(np.percentile(trades.drawdown, 5)),
        skipped_unbuyable=trades.skipped_unbuyable,
    )


def signal_stats(signal: np.ndarray, panel: Panel, holds: tuple[int, ...] = DEFAULT_HOLDS,
                 costs: Costs | None = None) -> list[StatRow]:
    """每个（买入方式, 持有天数）一行。"""
    mkt = _Market(panel)
    rows: list[StatRow] = []
    for entry in ENTRIES:
        for hold in holds:
            rows.append(_summarize(entry, hold, compute_trades(signal, panel, entry, hold, costs, mkt)))
    return rows


def prepare_market(panel: Panel) -> Any:
    """同一份行情要算很多组信号时，先调用一次，把结果传给 ``return_summary``，省掉重复的准备工作。"""
    return _Market(panel)


def return_summary(signal: np.ndarray, panel: Panel, hold: int, costs: Costs | None = None,
                   market: Any = None) -> dict[str, Any]:
    """只要次数、胜率、平均收益（次日开盘买入、持有 hold 天）。不算回撤，比 ``signal_stats`` 快得多。"""
    trades = compute_trades(signal, panel, ENTRY_NEXT_OPEN, hold, costs, market, with_drawdown=False)
    n = int(trades.ret.size)
    if n == 0:
        return {"count": 0, "win_rate": None, "avg_return": None}
    return {"count": n, "win_rate": float((trades.ret > 0).mean()), "avg_return": float(trades.ret.mean())}


def signal_list(signal: np.ndarray, panel: Panel, limit: int = 50, offset: int = 0,
                holds: tuple[int, ...] = DEFAULT_HOLDS, costs: Costs | None = None) -> tuple[int, list[SignalRecord]]:
    """信号明细，按日期从新到旧。返回（信号总数, 当前页）。收益为 next_open 口径。"""
    sig = clean_signal(signal, panel)
    rows, cols = np.nonzero(sig)
    order = np.lexsort((cols, -rows))
    rows, cols = rows[order], cols[order]
    total = int(rows.size)
    page_rows, page_cols = rows[offset : offset + limit], cols[offset : offset + limit]
    if page_rows.size == 0:
        return total, []

    mkt = _Market(panel)
    by_hold: dict[int, dict[tuple[int, int], float]] = {}
    page_mask = np.zeros(panel.shape, dtype=bool)
    page_mask[page_rows, page_cols] = True
    for hold in holds:
        tr = compute_trades(page_mask, panel, ENTRY_NEXT_OPEN, hold, costs, mkt)
        by_hold[hold] = {(int(r), int(c)): float(v) for r, c, v in zip(tr.signal_row, tr.col, tr.ret, strict=True)}

    records = [
        SignalRecord(
            date=str(panel.dates[r]),
            code=str(panel.codes[c]),
            returns={h: by_hold[h].get((int(r), int(c))) for h in holds},
        )
        for r, c in zip(page_rows, page_cols, strict=True)
    ]
    return total, records
