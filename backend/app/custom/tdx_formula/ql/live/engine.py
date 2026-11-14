"""模拟账户的逐日推进和提醒生成。成交规则完全复用组合回测的 ``advance_day``。

账户状态按股票代码和日期保存，不保存行列下标和复权价格：行情重建后下标和复权基准都可能变，
读取时再按当前的行情数据换算回来。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql.backtest import portfolio, strategy_run
from app.custom.tdx_formula.ql.backtest.strategy_store import Strategy
from app.custom.tdx_formula.ql.core.errors import DataError, QuantLabError
from app.custom.tdx_formula.ql.core.panel import Panel
from app.custom.tdx_formula.ql.live import settings, store

EMPTY_STATE: dict[str, Any] = {"cash": 0.0, "equity": 0.0, "positions": [], "pending_sells": {}, "pending_buys": []}


def _row_of(panel: Panel, day: str) -> int:
    target = np.datetime64(day, "D")
    row = int(np.searchsorted(panel.dates, target))
    if row >= len(panel.dates) or panel.dates[row] != target:
        raise DataError(f"行情数据里没有 {day} 这一天，模拟账户无法继续推进。")
    return row


def load_account(state: Mapping[str, Any], panel: Panel) -> portfolio.Account:
    account = portfolio.Account(cash=float(state["cash"]), equity=float(state.get("equity") or state["cash"]))
    for p in state.get("positions", []):
        col = panel.index_of(str(p["code"]))
        value_row = _row_of(panel, str(p["value_date"]))
        close = float(panel.close[value_row, col])
        if not np.isfinite(close) or close <= 0:
            raise DataError(f"持仓 {p['code']} 在 {p['value_date']} 没有收盘价，模拟账户无法继续推进。")
        account.positions[col] = portfolio.Position(
            col=col, shares=int(p["shares"]), buy_row=_row_of(panel, str(p["buy_date"])),
            buy_price=float(p["buy_price"]), cost=float(p["cost"]), units=float(p["value"]) / close,
            last_close=close, last_row=value_row,
        )
    account.pending_sells = {panel.index_of(str(code)): str(reason)
                             for code, reason in (state.get("pending_sells") or {}).items()}
    account.pending_buys = [panel.index_of(str(code)) for code in state.get("pending_buys") or []]
    return account


def dump_account(account: portfolio.Account, panel: Panel) -> dict[str, Any]:
    code = lambda col: str(panel.codes[col])  # noqa: E731
    return {
        "cash": account.cash, "equity": account.equity,
        "positions": [
            {"code": code(p.col), "shares": p.shares, "buy_date": str(panel.dates[p.buy_row]),
             "buy_price": p.buy_price, "cost": p.cost, "value": p.units * p.last_close,
             "value_date": str(panel.dates[p.last_row])}
            for p in account.positions.values()
        ],
        "pending_sells": {code(col): reason for col, reason in account.pending_sells.items()},
        "pending_buys": [code(col) for col in account.pending_buys],
    }


def reason_text(names: list[str]) -> str:
    return "同时满足：" + "、".join(names) if len(names) > 1 else f"满足：{names[0]}" if names else ""


def _build_reminder(account: portfolio.Account, row: int, ctx: portfolio.Context, strategy: Strategy,
                    meta: Mapping[str, Any], notes: list[str], names: Mapping[str, str]) -> dict[str, Any]:
    panel = ctx.panel
    code = lambda col: str(panel.codes[col])  # noqa: E731
    label = lambda col: names.get(code(col), "")  # noqa: E731
    timing_ok = True if ctx.timing_ok is None else bool(ctx.timing_ok[row])
    reason = reason_text(strategy_run.condition_names(strategy.config.get("conditions") or []))

    cols, scores = portfolio.candidates(ctx, row, exclude=list(account.positions))
    score_of = {int(c): float(s) for c, s in zip(cols, scores, strict=True)}
    rank_of = {int(c): i + 1 for i, c in enumerate(cols)}
    if timing_ok:
        chosen = list(account.pending_buys)
    else:
        chosen = [int(c) for c in cols[: ctx.rules.max_positions]]  # 建议空仓：照常列出，但只供参考
    buys = [{"code": code(c), "name": label(c), "rank": rank_of.get(c, 0),
             "score": round(score_of.get(c, 0.0) * 100), "reason": reason, "reference_only": not timing_ok}
            for c in chosen]

    sells = []
    for col, why in account.pending_sells.items():
        pos = account.positions.get(col)
        if pos is None:
            continue
        sells.append({"code": code(col), "name": label(col), "reason": why,
                      "gain": pos.units * pos.last_close / pos.cost - 1})

    positions = [
        {"code": code(p.col), "name": label(p.col), "buy_date": str(panel.dates[p.buy_row]),
         "buy_price": round(p.buy_price, 3), "shares": p.shares, "cost": round(p.cost, 2),
         "value": round(p.units * p.last_close, 2), "gain": p.units * p.last_close / p.cost - 1,
         "hold_days": row - p.buy_row + 1}
        for p in account.positions.values()
    ]
    initial = float(meta["initial_cash"])
    return {
        "date": str(panel.dates[row]),
        "timing": {"enabled": ctx.timing_ok is not None, "ok": timing_ok},
        "buys": buys, "sells": sells, "notes": notes, "positions": positions,
        "candidate_count": len(cols),
        "account": {"equity": round(account.equity, 2), "cash": round(account.cash, 2), "initial_cash": initial,
                    "total_return": account.equity / initial - 1, "started_on": str(meta["started_on"])},
    }


def build_context(strategy: Strategy, panel: Panel, evaluator: strategy_run.Evaluator | None) -> portfolio.Context:
    costs = settings.costs()
    if evaluator is None:
        return strategy_run.build_context(panel, strategy.config, costs=costs)[0]
    return strategy_run.build_context(panel, strategy.config, evaluator, costs)[0]


def advance(strategy: Strategy, panel: Panel, names: Mapping[str, str] | None = None,
            evaluator: strategy_run.Evaluator | None = None, ctx: portfolio.Context | None = None) -> list[str]:
    """把这个策略的模拟账户推进到行情的最新一天。返回这次处理了哪些交易日。没有账户时什么都不做。"""
    meta = store.get_account(strategy.id)
    if meta is None:
        return []
    names = names or {}
    after = meta["last_date"]
    if after is None:
        start_row = int(np.searchsorted(panel.dates, np.datetime64(meta["started_on"], "D")))
    else:
        start_row = _row_of(panel, after) + 1
    if start_row >= len(panel.dates):
        return []

    ctx = ctx or build_context(strategy, panel, evaluator)
    account = load_account(meta["state"], panel)
    done: list[str] = []
    for row in range(start_row, len(panel.dates)):
        wanted_buys = list(account.pending_buys)
        wanted_sells = list(account.pending_sells)
        closed = portfolio.advance_day(account, row, ctx)
        label = lambda col: names.get(str(panel.codes[col])) or str(panel.codes[col])  # noqa: E731
        notes = [f"上一个交易日提醒买入的{label(c)}没有买进（开盘涨停、停牌，或资金不够买一手）。"
                 for c in wanted_buys if c not in account.positions]
        notes += [f"上一个交易日提醒卖出的{label(c)}没有卖出（开盘跌停或停牌），顺延到下一个交易日。"
                  for c in wanted_sells if c in account.positions]
        trades = [
            {"code": str(panel.codes[t.col]), "buy_date": str(panel.dates[t.buy_row]), "buy_price": t.buy_price,
             "sell_date": str(panel.dates[t.sell_row if t.sell_row is not None else row]),
             "sell_price": t.sell_price, "shares": t.shares, "ret": t.ret, "pnl": t.pnl, "reason": t.reason}
            for t in closed
        ]
        day = str(panel.dates[row])
        reminder = _build_reminder(account, row, ctx, strategy, meta, notes, names)
        store.save_day(strategy.id, day, dump_account(account, panel), account.equity, account.cash, trades,
                       reminder)
        done.append(day)
    return done


def start(strategy: Strategy, panel: Panel, initial_cash: float, names: Mapping[str, str] | None = None,
          evaluator: strategy_run.Evaluator | None = None) -> list[str]:
    """把策略设为运行中时调用。第一次：开一个模拟账户，并立刻按最新一天的收盘生成第一份提醒。

    以前运行过又停用的策略：沿用原来的账户，把停用期间缺的交易日补上。
    """
    ctx = build_context(strategy, panel, evaluator)  # 配置有问题时在这里报错，不开账户
    if store.get_account(strategy.id) is None:
        if not np.isfinite(initial_cash) or initial_cash < 10_000:
            raise QuantLabError("模拟账户的初始资金至少要 1 万元。")
        state = {**EMPTY_STATE, "cash": float(initial_cash), "equity": float(initial_cash)}
        store.create_account(strategy.id, str(panel.dates[-1]), float(initial_cash), state)
    return advance(strategy, panel, names, evaluator, ctx)
