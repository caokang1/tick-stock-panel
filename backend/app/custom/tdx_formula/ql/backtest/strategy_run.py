"""把一份策略配置变成可以运行的回测环境。组合回测和第三期的模拟账户都从这里取，保证两边规则一致。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql import data as data_mod
from app.custom.tdx_formula.ql.backtest import portfolio
from app.custom.tdx_formula.ql.backtest.signals import Costs
from app.custom.tdx_formula.ql.blocks import (
    Block,
    Universe,
    combine_signals,
    enabled_conditions,
    evaluate_block,
    get_block,
    resolve_params,
)
from app.custom.tdx_formula.ql.blocks.model import KIND_RANK, KIND_TIMING
from app.custom.tdx_formula.ql.core.errors import DataError, QuantLabError
from app.custom.tdx_formula.ql.core.panel import Panel, single_series_panel

Evaluator = Callable[[Block, Panel, Mapping[str, float] | None], np.ndarray]

INDEX_NAMES = {
    "sh.000300": "沪深300", "sh.000001": "上证指数", "sz.399001": "深证成指",
    "sh.000905": "中证500", "sh.000852": "中证1000", "sz.399006": "创业板指",
}
DEFAULT_BENCHMARK = "sh.000300"
DEFAULT_RANK: dict[str, Any] = {
    "factors": [{"block_id": "rank_amount", "weight": 1, "direction": portfolio.DIRECTION_DESC}]}


# ---------------- 指数 ----------------
def index_frame(code: str) -> Any:
    df = data_mod.read_index(code)
    if df is None or len(df) == 0:
        name = INDEX_NAMES.get(code, code)
        raise DataError(f"本地没有指数“{name}”的行情。请到“设置”页更新数据后再试。")
    return df.sort_values("date").reset_index(drop=True)


def _align(dates: np.ndarray, src_dates: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把一条按 src_dates 排列的序列对齐到 dates：取每个日期当天或之前最近的一个值。返回（值，是否取到）。"""
    pos = np.searchsorted(src_dates, dates, side="right") - 1
    found = pos >= 0
    return values[np.clip(pos, 0, None)], found


def index_close(code: str, dates: np.ndarray) -> np.ndarray:
    """指数收盘价，对齐到 dates。指数还没有数据的日子为 NaN。"""
    df = index_frame(code)
    src = df["date"].to_numpy().astype("datetime64[D]")
    values, found = _align(dates, src, df["close"].to_numpy(dtype=float))
    return np.where(found, values, np.nan)


def timing_series(timing: Mapping[str, Any] | None, dates: np.ndarray) -> np.ndarray | None:
    """(T,) bool：每天收盘后择时条件是否成立。不择时返回 None。在指数的全部历史上求值后再对齐。"""
    if not timing or not timing.get("block_id"):
        return None
    block = get_block(str(timing["block_id"]))
    if block.kind != KIND_TIMING:
        raise QuantLabError(f"“{block.name}”不是择时条件。")
    code = str(timing.get("index_code") or DEFAULT_BENCHMARK)
    df = index_frame(code)
    src = df["date"].to_numpy().astype("datetime64[D]")
    volume = df["volume"].to_numpy(dtype=float) if "volume" in df else np.zeros(len(df))
    amount = df["amount"].to_numpy(dtype=float) if "amount" in df else np.zeros(len(df))
    mini = single_series_panel(
        src, code, df["open"].to_numpy(dtype=float), df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float), df["close"].to_numpy(dtype=float), volume, amount,
    )
    ok = evaluate_block(block, mini, resolve_params(block, timing.get("params")))[:, 0]
    values, found = _align(dates, src, ok)
    return values & found


# ---------------- 信号、排名、卖出条件 ----------------
def build_signal(panel: Panel, conditions: Sequence[Mapping[str, Any]], universe: Mapping[str, Any] | None,
                 evaluator: Evaluator = evaluate_block) -> np.ndarray:
    conds = enabled_conditions(conditions)
    if not conds:
        raise QuantLabError("至少要启用一个选股条件。")
    return combine_signals(panel, conds, Universe.from_dict(universe), evaluator=evaluator)


def rank_factors(panel: Panel, rank: Mapping[str, Any] | None) -> list[portfolio.RankFactor]:
    out: list[portfolio.RankFactor] = []
    for item in (rank or {}).get("factors") or []:
        block = get_block(str(item.get("block_id", "")))
        if block.kind != KIND_RANK:
            raise QuantLabError(f"“{block.name}”不是排名因子。")
        weight = float(item.get("weight", 1))
        if not np.isfinite(weight) or weight <= 0:
            raise QuantLabError(f"排名因子“{block.name}”的权重要大于 0，收到的是：{weight:g}")
        direction = str(item.get("direction", portfolio.DIRECTION_DESC))
        if direction not in (portfolio.DIRECTION_DESC, portfolio.DIRECTION_ASC):
            raise QuantLabError(f"排名因子“{block.name}”的方向不对：{direction}")
        out.append(portfolio.RankFactor(evaluate_block(block, panel, item.get("params")), weight, direction))
    return out


def sell_signal(panel: Panel, trade: Mapping[str, Any] | None,
                evaluator: Evaluator = evaluate_block) -> np.ndarray | None:
    conds = enabled_conditions((trade or {}).get("sell_conditions") or [])
    if not conds:
        return None
    out = np.zeros(panel.shape, dtype=bool)
    for block, params in conds:
        out |= evaluator(block, panel, params)
    return out & panel.tradable


def condition_names(conditions: Sequence[Mapping[str, Any]]) -> list[str]:
    """启用的选股条件的名称，用来写"入选原因"。"""
    return [block.name for block, _ in enabled_conditions(conditions)]


def build_context(panel: Panel, config: Mapping[str, Any], evaluator: Evaluator = evaluate_block,
                  costs: Costs | None = None) -> tuple[portfolio.Context, list[str]]:
    """返回（回测环境，口径说明）。config 的结构见技术设计 §8 和 §12.1。"""
    signal = build_signal(panel, config.get("conditions") or [], config.get("universe"), evaluator)
    trade_cfg, rank_cfg, timing_cfg = config.get("trade"), config.get("rank"), config.get("timing")
    rules = portfolio.TradeRules.from_dict(trade_cfg)
    assumptions: list[str] = []
    if not trade_cfg:
        assumptions.append(
            "没有设置买卖规则，按默认值回测：最多持有 5 只、均分仓位、持有满 5 天卖出、止盈 10%、止损 5%。")
    if not (rank_cfg or {}).get("factors"):
        rank_cfg = DEFAULT_RANK
        assumptions.append("没有设置排名，候选超过空位时按近 5 日平均成交额从大到小取。")
    timing_ok = timing_series(timing_cfg, panel.dates)
    if timing_ok is None:
        assumptions.append("没有设置大盘择时，任何时候都可以买入。")
    assumptions.append("信号和卖出条件在收盘后判定，按次日开盘价成交；开盘即涨停买不进，开盘即跌停或停牌卖不出则顺延。")
    costs = costs or Costs()
    assumptions.append(
        f"已扣除佣金（万分之 {costs.commission * 10000:g}，单笔最低 {costs.min_commission:g} 元）、"
        f"卖出印花税（千分之 {costs.stamp_tax * 1000:g}）和滑点（千分之 {costs.slippage * 1000:g}）。")
    ctx = portfolio.Context(
        panel=panel, signal=signal, rules=rules, costs=costs, rank=rank_factors(panel, rank_cfg),
        sell_signal=sell_signal(panel, trade_cfg, evaluator), timing_ok=timing_ok,
        on_fail=str((timing_cfg or {}).get("on_fail") or portfolio.ON_FAIL_STOP_BUY),
    )
    if not rules.has_exit() and ctx.sell_signal is None:
        raise QuantLabError("买卖规则里至少要有一种卖出方式：持有满几天、止盈、止损，或者卖出条件。")
    return ctx, assumptions
