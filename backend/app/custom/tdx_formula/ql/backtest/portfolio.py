"""组合回测：按日推进一个模拟账户。口径见技术设计文档 §12.3。

每天的顺序是：开盘处理退市 → 开盘卖出 → 开盘买入 → 收盘算总资产 → 收盘后生成明天的指令。
指令只用当天收盘及之前的数据生成，第二天开盘才成交，所以不会用到之后的数据。

``advance_day`` 是单日推进函数，第三期的模拟账户每天调用一次，与回测共用同一套规则。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql.backtest.signals import TRADING_DAYS_PER_YEAR, Costs
from app.custom.tdx_formula.ql.core import limits
from app.custom.tdx_formula.ql.core.errors import QuantLabError
from app.custom.tdx_formula.ql.core.panel import Panel

ON_FAIL_STOP_BUY = "stop_buy"
ON_FAIL_CLEAR = "clear"
DIRECTION_DESC = "desc"
DIRECTION_ASC = "asc"
LOT = 100

REASON_STOP_LOSS = "止损"
REASON_TAKE_PROFIT = "止盈"
REASON_SELL_SIGNAL = "卖出条件成立"
REASON_TIMING = "大盘择时不满足，清仓"
REASON_DELISTED = "退市"


def _optional_ratio(d: Mapping[str, Any], key: str, label: str, default: float | None) -> float | None:
    if key not in d:
        return default
    value = d[key]
    if value is None:
        return None
    value = float(value)
    if not np.isfinite(value) or value <= 0:
        raise QuantLabError(f"{label}要填大于 0 的数，不用这一条就留空。收到的是：{value:g}")
    return value


@dataclass(frozen=True)
class TradeRules:
    max_positions: int = 5
    position_pct: float | None = None  # None 表示按 1 / max_positions 均分
    max_hold_days: int | None = 5
    take_profit: float | None = 0.10
    stop_loss: float | None = 0.05  # 正数，0.05 表示亏 5% 卖出

    @property
    def pct(self) -> float:
        return self.position_pct if self.position_pct is not None else 1.0 / self.max_positions

    def has_exit(self) -> bool:
        return self.max_hold_days is not None or self.take_profit is not None or self.stop_loss is not None

    def to_dict(self) -> dict[str, Any]:
        return {"max_positions": self.max_positions, "position_pct": self.position_pct,
                "max_hold_days": self.max_hold_days, "take_profit": self.take_profit, "stop_loss": self.stop_loss}

    @staticmethod
    def from_dict(d: Mapping[str, Any] | None) -> TradeRules:
        base = TradeRules()
        if not d:
            return base
        max_positions = int(d.get("max_positions", base.max_positions))
        if not 1 <= max_positions <= 100:
            raise QuantLabError(f"最多持有几只要在 1 到 100 之间，收到的是：{max_positions}")
        pct = _optional_ratio(d, "position_pct", "每只仓位", base.position_pct)
        if pct is not None and pct > 1:
            raise QuantLabError(f"每只仓位不能超过 100%，收到的是：{pct:.0%}")
        hold = d.get("max_hold_days", base.max_hold_days)
        if hold is not None:
            hold = int(hold)
            if not 1 <= hold <= 500:
                raise QuantLabError(f"持有天数要在 1 到 500 之间，不用这一条就留空。收到的是：{hold}")
        return TradeRules(
            max_positions=max_positions, position_pct=pct, max_hold_days=hold,
            take_profit=_optional_ratio(d, "take_profit", "止盈比例", base.take_profit),
            stop_loss=_optional_ratio(d, "stop_loss", "止损比例", base.stop_loss),
        )


@dataclass(frozen=True)
class RankFactor:
    values: np.ndarray  # (T, N) float
    weight: float = 1.0
    direction: str = DIRECTION_DESC


@dataclass
class Position:
    col: int
    shares: int
    buy_row: int
    buy_price: float  # 不复权成交价（含滑点）
    cost: float  # 买入时付出的现金（含佣金）
    units: float  # 按后复权价格折算的份数：市值 = units × 后复权价
    last_close: float  # 最近一个上市日的后复权收盘价
    last_row: int


@dataclass(frozen=True)
class Trade:
    col: int
    buy_row: int
    sell_row: int | None  # None 表示回测结束时还持有
    buy_price: float
    sell_price: float | None
    shares: int
    ret: float
    pnl: float
    reason: str
    open: bool = False

    @property
    def hold_days(self) -> int:
        """持有了几个交易日：买入当天算第 1 天，开盘卖出的那天不算（"持有满 5 天"的交易这里就是 5）。"""
        return 0 if self.sell_row is None else self.sell_row - self.buy_row


@dataclass
class Account:
    cash: float
    positions: dict[int, Position] = field(default_factory=dict)
    pending_sells: dict[int, str] = field(default_factory=dict)  # 股票列 -> 卖出原因
    pending_buys: list[int] = field(default_factory=list)  # 按排名先后
    equity: float = 0.0

    def __post_init__(self) -> None:
        if not self.equity:
            self.equity = self.cash


@dataclass
class Context:
    """一次回测里不变的输入。构造时预先算好涨跌停相关的判断。"""

    panel: Panel
    signal: np.ndarray  # (T, N) bool，收盘后确认的买入信号
    rules: TradeRules = field(default_factory=TradeRules)
    costs: Costs = field(default_factory=Costs)
    rank: Sequence[RankFactor] = ()
    sell_signal: np.ndarray | None = None  # (T, N) bool
    timing_ok: np.ndarray | None = None  # (T,) bool；None 表示不择时
    on_fail: str = ON_FAIL_STOP_BUY

    def __post_init__(self) -> None:
        p = self.panel
        if self.signal.shape != p.shape:
            raise QuantLabError("买入信号的形状与行情数据不一致。")
        if self.timing_ok is not None and len(self.timing_ok) != p.shape[0]:
            raise QuantLabError("择时结果的天数与行情数据不一致。")
        if self.on_fail not in (ON_FAIL_STOP_BUY, ON_FAIL_CLEAR):
            raise QuantLabError(f"未知的择时处理方式：{self.on_fail}")
        tradable = p.tradable
        self.can_buy_open = tradable & ~limits.opens_at_limit_up(p)
        self.can_sell_open = tradable & ~limits.opens_at_limit_down(p)


# ---------------- 排名 ----------------
def score_candidates(ctx: Context, row: int, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把当天的候选股票按分数从高到低排好，返回（排好的股票列，对应的分数 0 到 1）。

    分数 = 各因子在候选里的百分位名次按权重加权平均；因子算不出来的排最后。没有排名因子时分数全为 0。
    """
    if len(cols) == 0 or not ctx.rank:
        return cols, np.zeros(len(cols))
    if len(cols) == 1:
        return cols, np.ones(1)
    score = np.zeros(len(cols))
    total_weight = 0.0
    for factor in ctx.rank:
        values = factor.values[row, cols].astype(float)
        if factor.direction == DIRECTION_ASC:
            values = -values
        missing = ~np.isfinite(values)
        order = np.argsort(np.where(missing, -np.inf, values), kind="stable")
        pct = np.empty(len(cols))
        pct[order] = np.arange(len(cols)) / (len(cols) - 1)
        pct[missing] = 0.0
        score += factor.weight * pct
        total_weight += abs(factor.weight)
    if total_weight > 0:
        score /= total_weight
    order = np.argsort(-score, kind="stable")
    return cols[order], score[order]


def rank_candidates(ctx: Context, row: int, cols: np.ndarray) -> np.ndarray:
    return score_candidates(ctx, row, cols)[0]


def candidates(ctx: Context, row: int, exclude: Sequence[int] = ()) -> tuple[np.ndarray, np.ndarray]:
    """当天选出的股票（去掉 exclude 里的），按分数从高到低。返回（股票列，分数）。"""
    cols = np.nonzero(ctx.signal[row])[0]
    if len(exclude):
        cols = cols[~np.isin(cols, np.asarray(list(exclude), dtype=np.int64))]
    return score_candidates(ctx, row, cols)


# ---------------- 成交 ----------------
def _sell_fees(gross: float, costs: Costs) -> float:
    return max(costs.min_commission, gross * costs.commission) + gross * costs.stamp_tax


def _close_position(account: Account, pos: Position, gross: float, sell_row: int, sell_price: float | None,
                    reason: str, costs: Costs) -> Trade:
    net = gross - _sell_fees(gross, costs)
    account.cash += net
    del account.positions[pos.col]
    account.pending_sells.pop(pos.col, None)
    return Trade(col=pos.col, buy_row=pos.buy_row, sell_row=sell_row, buy_price=pos.buy_price,
                 sell_price=sell_price, shares=pos.shares, ret=net / pos.cost - 1, pnl=net - pos.cost, reason=reason)


def _execute_open(account: Account, row: int, ctx: Context) -> list[Trade]:
    p, costs = ctx.panel, ctx.costs
    closed: list[Trade] = []

    # 1. 已退市的持仓：按最后一个上市日的收盘价了结
    for gone in [x for x in account.positions.values() if not p.listed[row, x.col]]:
        raw_last = float(p.raw_close[gone.last_row, gone.col])
        closed.append(_close_position(account, gone, gone.units * gone.last_close, gone.last_row, raw_last,
                                      REASON_DELISTED, costs))

    # 2. 卖出。卖不出的指令留在 pending_sells 里，下一个交易日再试
    for col, reason in list(account.pending_sells.items()):
        pos = account.positions.get(col)
        if pos is None:
            account.pending_sells.pop(col, None)
            continue
        if not ctx.can_sell_open[row, col]:
            continue
        gross = pos.units * float(p.open[row, col]) * (1 - costs.slippage)
        price = float(p.raw_open[row, col]) * (1 - costs.slippage)
        closed.append(_close_position(account, pos, gross, row, price, reason, costs))

    # 3. 买入。买不进的直接放弃
    budget_each = account.equity * ctx.rules.pct
    for col in account.pending_buys:
        if len(account.positions) >= ctx.rules.max_positions:
            break
        if col in account.positions or not ctx.can_buy_open[row, col]:
            continue
        price = float(p.raw_open[row, col]) * (1 + costs.slippage)
        adj_open = float(p.open[row, col])
        if not (np.isfinite(price) and price > 0 and np.isfinite(adj_open) and adj_open > 0):
            continue
        budget = min(budget_each, account.cash)
        shares = int(budget / (price * (1 + costs.commission)) // LOT) * LOT
        while shares > 0:
            amount = shares * price
            cost = amount + max(costs.min_commission, amount * costs.commission)
            if cost <= budget:
                break
            shares -= LOT
        if shares <= 0:
            continue
        account.cash -= cost
        account.positions[col] = Position(
            col=col, shares=shares, buy_row=row, buy_price=price, cost=cost,
            units=shares * float(p.raw_open[row, col]) / adj_open, last_close=adj_open, last_row=row,
        )
    account.pending_buys = []
    return closed


def _mark_to_market(account: Account, row: int, panel: Panel) -> None:
    value = account.cash
    for pos in account.positions.values():
        close = float(panel.close[row, pos.col])
        if panel.listed[row, pos.col] and np.isfinite(close):
            pos.last_close, pos.last_row = close, row
        value += pos.units * pos.last_close
    account.equity = value


def exit_reason(pos: Position, row: int, ctx: Context, timing_ok: bool) -> str | None:
    """这笔持仓在 row 这天收盘后是否该卖；该卖时返回原因。模拟账户和实际持仓共用这一条规则。"""
    rules = ctx.rules
    gain = pos.units * pos.last_close / pos.cost - 1
    if rules.stop_loss is not None and gain <= -rules.stop_loss:
        return REASON_STOP_LOSS
    if rules.take_profit is not None and gain >= rules.take_profit:
        return REASON_TAKE_PROFIT
    if ctx.sell_signal is not None and ctx.sell_signal[row, pos.col]:
        return REASON_SELL_SIGNAL
    if rules.max_hold_days is not None and row - pos.buy_row + 1 >= rules.max_hold_days:
        return f"持有满 {rules.max_hold_days} 天"
    if not timing_ok and ctx.on_fail == ON_FAIL_CLEAR:
        return REASON_TIMING
    return None


def _plan_next_day(account: Account, row: int, ctx: Context) -> None:
    timing_ok = True if ctx.timing_ok is None else bool(ctx.timing_ok[row])
    for col, pos in account.positions.items():
        if col in account.pending_sells:
            continue
        reason = exit_reason(pos, row, ctx, timing_ok)
        if reason:
            account.pending_sells[col] = reason

    account.pending_buys = []
    if not timing_ok:
        return
    free = ctx.rules.max_positions - (len(account.positions) - len(account.pending_sells))
    if free <= 0:
        return
    cols = np.nonzero(ctx.signal[row])[0]
    if len(cols) == 0:
        return
    held = np.fromiter(account.positions.keys(), dtype=np.int64, count=len(account.positions))
    cols = cols[~np.isin(cols, held)]
    account.pending_buys = [int(c) for c in rank_candidates(ctx, row, cols)[:free]]


def advance_day(account: Account, row: int, ctx: Context) -> list[Trade]:
    """把账户推进一个交易日。返回当天了结的交易；明天要执行的指令留在 account.pending_* 里。"""
    closed = _execute_open(account, row, ctx)
    _mark_to_market(account, row, ctx.panel)
    _plan_next_day(account, row, ctx)
    return closed


# ---------------- 整段回测 ----------------
@dataclass(frozen=True)
class BacktestResult:
    equity: np.ndarray  # (T,) 每天收盘后的总资产
    trades: list[Trade]  # 已了结的在前，回测结束时仍持有的在后
    account: Account
    initial_cash: float


def run_backtest(ctx: Context, initial_cash: float = 1_000_000.0) -> BacktestResult:
    if not np.isfinite(initial_cash) or initial_cash < 10_000:
        raise QuantLabError("初始资金至少要 1 万元。")
    if not ctx.rules.has_exit() and ctx.sell_signal is None:
        raise QuantLabError("买卖规则里至少要有一种卖出方式：持有满几天、止盈、止损，或者卖出条件。")
    t = ctx.panel.shape[0]
    account = Account(cash=float(initial_cash))
    equity = np.empty(t)
    trades: list[Trade] = []
    for row in range(t):
        trades.extend(advance_day(account, row, ctx))
        equity[row] = account.equity
    for pos in account.positions.values():
        value = pos.units * pos.last_close
        trades.append(Trade(col=pos.col, buy_row=pos.buy_row, sell_row=None, buy_price=pos.buy_price,
                            sell_price=None, shares=pos.shares, ret=value / pos.cost - 1, pnl=value - pos.cost,
                            reason="持有中", open=True))
    return BacktestResult(equity=equity, trades=trades, account=account, initial_cash=float(initial_cash))


# ---------------- 指标 ----------------
def max_drawdown(curve: np.ndarray) -> float:
    if len(curve) == 0:
        return 0.0
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1).min())


def annualized(total_return: float, days: int) -> float:
    if days <= 1 or total_return <= -1:
        return 0.0 if days <= 1 else -1.0
    return float((1 + total_return) ** (TRADING_DAYS_PER_YEAR / (days - 1)) - 1)


def sharpe_ratio(curve: np.ndarray) -> float | None:
    if len(curve) < 3:
        return None
    daily = curve[1:] / curve[:-1] - 1
    std = float(daily.std(ddof=1))
    if not np.isfinite(std) or std < 1e-12:
        return None
    return float(daily.mean() / std * np.sqrt(TRADING_DAYS_PER_YEAR))


def yearly_returns(dates: np.ndarray, curve: np.ndarray, start_value: float) -> dict[int, float]:
    """每个自然年的收益。第一年从起始资金算起。"""
    out: dict[int, float] = {}
    years = dates.astype("datetime64[Y]").astype(int) + 1970
    prev = start_value
    for year in np.unique(years):
        last = float(curve[np.nonzero(years == year)[0][-1]])
        out[int(year)] = last / prev - 1 if prev > 0 else 0.0
        prev = last
    return out


def summarize(result: BacktestResult, dates: np.ndarray) -> dict[str, Any]:
    equity = result.equity
    t = len(equity)
    final = float(equity[-1]) if t else result.initial_cash
    total = final / result.initial_cash - 1
    closed = [x for x in result.trades if not x.open]
    wins = sum(1 for x in closed if x.ret > 0)
    curve = np.concatenate([[result.initial_cash], equity]) if t else equity
    return {
        "annual_return": annualized(total, t + 1),
        "total_return": total,
        "max_drawdown": max_drawdown(curve),
        "sharpe": sharpe_ratio(curve),
        "win_rate": wins / len(closed) if closed else None,
        "trade_count": len(closed),
        "avg_hold_days": float(np.mean([x.hold_days for x in closed])) if closed else None,
        "final_equity": final,
        "initial_cash": result.initial_cash,
        "yearly": yearly_returns(dates, equity, result.initial_cash) if t else {},
    }
