"""实际持仓：按你真正买了什么来生成买卖提醒。

规则和模拟账户是同一套（同一个 ``exit_reason``、同一套排名），只是持仓、买入价、买入日期和资金换成你自己记的。
模拟账户不受这里任何操作的影响：它继续按"每条提醒都照做"推进，用来衡量策略本身。

没有答复的提醒一律当作没有成交，并一直留在"待确认"里。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql import data as data_mod
from app.custom.tdx_formula.ql.backtest import portfolio, strategy_run
from app.custom.tdx_formula.ql.backtest.signals import Costs
from app.custom.tdx_formula.ql.backtest.strategy_store import Strategy
from app.custom.tdx_formula.ql.core import clock, codes
from app.custom.tdx_formula.ql.core.errors import DataError, QuantLabError
from app.custom.tdx_formula.ql.core.panel import Panel
from app.custom.tdx_formula.ql.live import engine, settings, store
from app.custom.tdx_formula.ql.live import real_store as rs

REASON_MANUAL = "手动卖出"
MIN_CAPITAL = 1_000.0
MAX_CAPITAL = 1e10
_TRADES_SHOWN = 50


# ---------------- 费用与校验 ----------------
def _buy_cost(amount: float, costs: Costs) -> float:
    return amount + max(costs.min_commission, amount * costs.commission)


def _sell_proceeds(amount: float, costs: Costs) -> float:
    return amount - max(costs.min_commission, amount * costs.commission) - amount * costs.stamp_tax


def _check_capital(value: float) -> float:
    capital = float(value)
    if not np.isfinite(capital) or not MIN_CAPITAL <= capital <= MAX_CAPITAL:
        raise QuantLabError("实际投入的资金要在 1000 元到 100 亿之间。")
    return capital


def _check_amounts(shares: Any, price: Any) -> tuple[int, float]:
    try:
        n, p = int(shares), float(price)
    except (TypeError, ValueError) as e:
        raise QuantLabError("股数和价格要填数字。") from e
    if n <= 0 or n != float(shares):
        raise QuantLabError("股数要是大于 0 的整数。")
    if not np.isfinite(p) or p <= 0:
        raise QuantLabError("成交价要大于 0。")
    return n, p


def _trade_day(text: Any) -> str:
    """成交日期：不能是将来，也不能是休市的日子。"""
    try:
        day = date.fromisoformat(str(text))
    except ValueError as e:
        raise QuantLabError(f"成交日期的格式应为 2024-06-03 这样，收到的是：{text}") from e
    if day > clock.now().date():
        raise QuantLabError("成交日期不能是将来的日子。")
    calendar = data_mod.read_calendar()
    stamp = np.datetime64(day, "D")
    if len(calendar) and calendar[0] <= stamp <= calendar[-1] and stamp not in calendar:
        raise QuantLabError(f"{day.isoformat()} 不是交易日。")
    return day.isoformat()


def default_trade_day(reminder_day: str) -> str:
    """一条提醒默认的成交日期：提醒之后的第一个交易日；那一天还没到就用今天。"""
    today = clock.now().date()
    calendar = data_mod.read_calendar()
    later = calendar[calendar > np.datetime64(reminder_day, "D")]
    if len(later):
        nxt = later[0].astype(object)
        if nxt <= today:
            return str(nxt.isoformat())
    return today.isoformat()


def _account(strategy_id: str) -> dict[str, Any]:
    acct = rs.get_account(strategy_id)
    if acct is None or not acct["enabled"]:
        raise QuantLabError("这个策略还没有打开“实际持仓”。")
    return acct


# ---------------- 打开、关闭 ----------------
def enable(strategy: Strategy, capital: float) -> None:
    """打开实际持仓。第一次打开要给出实际投入的资金；以后再给，表示把投入的资金改成这个数。"""
    if store.get_account(strategy.id) is None:
        raise QuantLabError("请先把这个策略设为“运行中”，再打开实际持仓。")
    capital = _check_capital(capital)
    acct = rs.get_account(strategy.id)
    if acct is None:
        rs.create_account(strategy.id, capital)
    else:
        rs.update_account(strategy.id, enabled=True, capital=capital, cash_delta=capital - acct["capital"])


def disable(strategy_id: str) -> None:
    """关闭后提醒恢复按模拟账户生成；已经记下的持仓和交易都留着，重新打开时接着用。"""
    if rs.get_account(strategy_id) is not None:
        rs.update_account(strategy_id, enabled=False)


# ---------------- 记账 ----------------
def add_position(strategy_id: str, code: str, shares: Any, price: Any, day: Any, panel: Panel) -> int:
    _account(strategy_id)
    try:
        code = codes.normalize(str(code))
        panel.index_of(code)
    except (DataError, ValueError, IndexError) as e:
        raise QuantLabError(f"行情数据里没有这只股票：{code}") from e
    n, p = _check_amounts(shares, price)
    buy_day = _trade_day(day)
    if any(x["code"] == code for x in rs.positions(strategy_id, open_only=True)):
        raise QuantLabError(f"实际持仓里已经有 {code} 了。加仓请直接修改那条持仓的股数和买入价。")
    return rs.add_position(strategy_id, code, n, buy_day, p, _buy_cost(n * p, settings.costs()))


def edit_position(position_id: int, shares: Any, price: Any, day: Any) -> str:
    pos = rs.get_position(position_id)
    if pos["sell_date"] is not None:
        raise QuantLabError("这笔已经卖出了，不能再改。记错了可以删除后重新记。")
    n, p = _check_amounts(shares, price)
    rs.change_position(position_id, n, _trade_day(day), p, _buy_cost(n * p, settings.costs()))
    return str(pos["strategy_id"])


def sell_position(position_id: int, price: Any, day: Any, reason: str = REASON_MANUAL) -> str:
    pos = rs.get_position(position_id)
    if pos["sell_date"] is not None:
        raise QuantLabError("这笔已经卖出了。")
    _, p = _check_amounts(pos["shares"], price)
    sell_day = _trade_day(day)
    if sell_day < str(pos["buy_date"]):
        raise QuantLabError("卖出日期不能早于买入日期。")
    rs.close_position(position_id, sell_day, p, _sell_proceeds(pos["shares"] * p, settings.costs()), reason)
    rs.skip_pending(str(pos["strategy_id"]), side=rs.SIDE_SELL, code=str(pos["code"]))
    return str(pos["strategy_id"])


def remove_position(position_id: int) -> str:
    pos = rs.get_position(position_id)
    rs.delete_position(position_id)
    if pos["sell_date"] is None:
        rs.skip_pending(str(pos["strategy_id"]), side=rs.SIDE_SELL, code=str(pos["code"]))
    return str(pos["strategy_id"])


def answer_todo(todo_id: int, done: bool, panel: Panel, price: Any = None, shares: Any = None,
                day: Any = None) -> str:
    """答复一条提醒：照做了（可以改成实际的价格、股数、日期），或者没做。返回策略编号。"""
    todo = rs.get_todo(todo_id)
    strategy_id = str(todo["strategy_id"])
    if todo["status"] != rs.PENDING:
        raise QuantLabError("这条提醒已经处理过了。")
    if not done:
        rs.set_todo_status(todo_id, rs.SKIPPED)
        return strategy_id
    trade_day = day if day is not None else default_trade_day(str(todo["date"]))
    use_price = todo["price"] if price is None else price
    if todo["side"] == rs.SIDE_BUY:
        add_position(strategy_id, str(todo["code"]), todo["shares"] if shares is None else shares, use_price,
                     trade_day, panel)
    else:
        held = [x for x in rs.positions(strategy_id, open_only=True) if x["code"] == todo["code"]]
        if not held:
            raise QuantLabError("实际持仓里已经没有这只股票了，这条提醒可以点“没卖”关掉。")
        sell_position(int(held[0]["id"]), use_price, trade_day, str(todo["reason"]))
    rs.set_todo_status(todo_id, rs.DONE)
    return strategy_id


def skip_all(strategy_id: str) -> None:
    rs.skip_pending(strategy_id)


# ---------------- 按实际持仓生成提醒 ----------------
def refresh(strategy: Strategy, panel: Panel, ctx: portfolio.Context) -> None:
    """按行情最新一天的收盘，重算实际持仓的市值和盈亏，并生成明天的买卖提醒。没有打开实际持仓时什么都不做。

    可以反复调用：同一天、同一只股票、同一个方向的提醒只会有一条，答复过的不会重新出现。
    """
    acct = rs.get_account(strategy.id)
    if acct is None or not acct["enabled"]:
        return
    row = len(panel.dates) - 1
    day = str(panel.dates[row])
    timing_ok = True if ctx.timing_ok is None else bool(ctx.timing_ok[row])
    asked = {t["code"]: t for t in rs.todos(strategy.id) if t["side"] == rs.SIDE_SELL}
    opened = rs.positions(strategy.id, open_only=True)

    items: list[dict[str, Any]] = []
    held_cols: list[int] = []
    value_total = 0.0
    leaving = 0
    for p in opened:
        code = str(p["code"])
        cost, shares = float(p["cost"]), int(p["shares"])
        item = {"id": p["id"], "code": code, "buy_date": p["buy_date"], "buy_price": round(float(p["buy_price"]), 3),
                "shares": shares, "cost": round(cost, 2), "value": round(cost, 2), "gain": 0.0, "hold_days": 0,
                "sell_reason": None}
        reason: str | None = None
        try:
            col = panel.index_of(code)
        except DataError:
            col = -1
        buy_row = int(np.searchsorted(panel.dates, np.datetime64(str(p["buy_date"]), "D")))
        if col >= 0:
            held_cols.append(col)
        if col >= 0 and buy_row <= row:
            # 市值按后复权价格折算，分红送股已经算在里面（和模拟账户的算法一样）
            factor = float(panel.close[buy_row, col]) / float(panel.raw_close[buy_row, col])
            close = float(panel.close[row, col])
            if np.isfinite(factor) and factor > 0 and np.isfinite(close):
                pos = portfolio.Position(col=col, shares=shares, buy_row=buy_row, buy_price=float(p["buy_price"]),
                                         cost=cost, units=shares / factor, last_close=close, last_row=row)
                value = pos.units * close
                item.update(value=round(value, 2), gain=value / cost - 1, hold_days=row - buy_row + 1)
                if not panel.listed[row, col]:
                    reason = portfolio.REASON_DELISTED
                else:
                    reason = portfolio.exit_reason(pos, row, ctx, timing_ok)
        value_total += float(item["value"])
        if code in asked:
            item["sell_reason"] = asked[code]["reason"]
            leaving += 1
        elif reason:
            price = float(panel.raw_close[row, col])
            rs.add_todo(strategy.id, day, rs.SIDE_SELL, code, shares, price if np.isfinite(price) else 0.0, reason)
            item["sell_reason"] = reason
            leaving += 1
        items.append(item)

    cash = float(acct["cash"])
    equity = cash + value_total
    free = ctx.rules.max_positions - (len(opened) - leaving)
    if timing_ok and free > 0:
        cols = np.nonzero(ctx.signal[row])[0]
        cols = cols[~np.isin(cols, np.array(held_cols, dtype=np.int64))]
        why = engine.reason_text(strategy_run.condition_names(strategy.config.get("conditions") or []))
        budget = min(equity * ctx.rules.pct, max(cash, 0.0))
        for c in portfolio.rank_candidates(ctx, row, cols)[:free]:
            price = float(panel.raw_close[row, int(c)])
            if not np.isfinite(price) or price <= 0:
                continue
            shares = int(budget / (price * (1 + ctx.costs.commission)) // portfolio.LOT) * portfolio.LOT
            rs.add_todo(strategy.id, day, rs.SIDE_BUY, str(panel.codes[int(c)]), max(shares, 0), price, why)

    rs.update_account(strategy.id, view={
        "date": day, "equity": round(equity, 2), "positions": items,
        "timing": {"enabled": ctx.timing_ok is not None, "ok": timing_ok},
    })


# ---------------- 给页面和推送用的内容 ----------------
def payload(strategy_id: str, names: Mapping[str, str]) -> dict[str, Any] | None:
    """实际持仓的全部内容；从没打开过时返回 None。"""
    acct = rs.get_account(strategy_id)
    if acct is None:
        return None
    view = acct["view"]
    capital = acct["capital"]
    equity = float(view.get("equity", acct["cash"]))
    label = lambda code: names.get(str(code), "")  # noqa: E731
    pending = [{**t, "name": label(t["code"]), "default_date": default_trade_day(str(t["date"]))}
               for t in rs.todos(strategy_id)]
    closed = rs.positions(strategy_id, closed_only=True)[:_TRADES_SHOWN]
    return {
        "enabled": acct["enabled"], "capital": capital, "cash": round(acct["cash"], 2), "equity": round(equity, 2),
        "total_return": equity / capital - 1, "date": view.get("date"),
        "timing": view.get("timing") or {"enabled": False, "ok": True},
        "positions": [{**p, "name": label(p["code"])} for p in view.get("positions", [])],
        "todos": pending,
        "trades": [{"id": t["id"], "code": t["code"], "name": label(t["code"]), "buy_date": t["buy_date"],
                    "buy_price": round(float(t["buy_price"]), 3), "sell_date": t["sell_date"],
                    "sell_price": round(float(t["sell_price"]), 3), "shares": t["shares"],
                    "return": float(t["proceeds"]) / float(t["cost"]) - 1,
                    "pnl": round(float(t["proceeds"]) - float(t["cost"]), 2), "reason": t["reason"]}
                   for t in closed],
    }


def total_return(strategy_id: str) -> float | None:
    """实际持仓到目前为止的收益；没有打开时返回 None。"""
    acct = rs.get_account(strategy_id)
    if acct is None or not acct["enabled"]:
        return None
    return float(acct["view"].get("equity", acct["cash"])) / acct["capital"] - 1


def as_reminder(strategy_id: str, day: str, names: Mapping[str, str]) -> dict[str, Any] | None:
    """把实际持仓这一天的提醒整理成推送用的样子；没有打开、或这一天还没算过时返回 None。"""
    data = payload(strategy_id, names)
    if data is None or not data["enabled"] or data["date"] != day:
        return None
    gains = {p["code"]: p["gain"] for p in data["positions"]}
    today = [t for t in data["todos"] if t["date"] == day]
    return {
        "real": True, "date": day, "timing": data["timing"],
        "buys": [{"code": t["code"], "name": t["name"], "reason": t["reason"], "shares": t["shares"]}
                 for t in today if t["side"] == rs.SIDE_BUY],
        "sells": [{"code": t["code"], "name": t["name"], "reason": t["reason"], "gain": gains.get(t["code"], 0.0)}
                  for t in today if t["side"] == rs.SIDE_SELL],
        "notes": [], "positions": data["positions"],
        "account": {"total_return": data["total_return"]},
        "older_pending": len(data["todos"]) - len(today),
    }
