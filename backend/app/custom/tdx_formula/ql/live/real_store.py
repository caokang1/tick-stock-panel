"""实际持仓的存储：每个策略一本账，记你真正买了什么。和模拟账户分开，互不影响。"""

from __future__ import annotations

import json
from typing import Any

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.db import connect
from app.custom.tdx_formula.ql.core.errors import NotFoundError

SIDE_BUY = "buy"
SIDE_SELL = "sell"
PENDING = "pending"
DONE = "done"
SKIPPED = "skipped"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS real_accounts (
        strategy_id TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL,
        capital REAL NOT NULL,
        cash REAL NOT NULL,
        view_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS real_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        code TEXT NOT NULL,
        shares INTEGER NOT NULL,
        buy_date TEXT NOT NULL,
        buy_price REAL NOT NULL,
        cost REAL NOT NULL,
        sell_date TEXT,
        sell_price REAL,
        proceeds REAL,
        reason TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS real_todos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        date TEXT NOT NULL,
        side TEXT NOT NULL,
        code TEXT NOT NULL,
        shares INTEGER NOT NULL,
        price REAL NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        UNIQUE (strategy_id, date, side, code)
    )
    """,
)
_POSITION_KEYS = ("id", "strategy_id", "code", "shares", "buy_date", "buy_price", "cost", "sell_date", "sell_price",
                  "proceeds", "reason")
_TODO_KEYS = ("id", "strategy_id", "date", "side", "code", "shares", "price", "reason", "status")


def init_schema() -> None:
    with connect() as conn:
        for statement in _SCHEMA:
            conn.execute(statement)


# ---------------- 账户 ----------------
def get_account(strategy_id: str) -> dict[str, Any] | None:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT * FROM real_accounts WHERE strategy_id = ?", (strategy_id,)).fetchone()
    if row is None:
        return None
    return {"strategy_id": row["strategy_id"], "enabled": bool(row["enabled"]), "capital": float(row["capital"]),
            "cash": float(row["cash"]), "view": json.loads(row["view_json"])}


def enabled_ids() -> set[str]:
    init_schema()
    with connect() as conn:
        rows = conn.execute("SELECT strategy_id FROM real_accounts WHERE enabled = 1").fetchall()
    return {r["strategy_id"] for r in rows}


def create_account(strategy_id: str, capital: float) -> None:
    init_schema()
    with connect() as conn:
        conn.execute(
            "INSERT INTO real_accounts (strategy_id, enabled, capital, cash, created_at) VALUES (?, 1, ?, ?, ?)",
            (strategy_id, capital, capital, clock.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def update_account(strategy_id: str, *, enabled: bool | None = None, capital: float | None = None,
                   cash_delta: float = 0.0, view: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        if enabled is not None:
            conn.execute("UPDATE real_accounts SET enabled = ? WHERE strategy_id = ?", (int(enabled), strategy_id))
        if capital is not None:
            conn.execute("UPDATE real_accounts SET capital = ? WHERE strategy_id = ?", (capital, strategy_id))
        if cash_delta:
            conn.execute("UPDATE real_accounts SET cash = cash + ? WHERE strategy_id = ?", (cash_delta, strategy_id))
        if view is not None:
            conn.execute("UPDATE real_accounts SET view_json = ? WHERE strategy_id = ?",
                         (json.dumps(view, ensure_ascii=False), strategy_id))


# ---------------- 持仓和已了结的交易 ----------------
def positions(strategy_id: str, *, open_only: bool = False, closed_only: bool = False) -> list[dict[str, Any]]:
    init_schema()
    where = " AND sell_date IS NULL" if open_only else " AND sell_date IS NOT NULL" if closed_only else ""
    order = "sell_date DESC, id DESC" if closed_only else "id"
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM real_positions WHERE strategy_id = ?{where} ORDER BY {order}",
                            (strategy_id,)).fetchall()
    return [{k: r[k] for k in _POSITION_KEYS} for r in rows]


def get_position(position_id: int) -> dict[str, Any]:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT * FROM real_positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        raise NotFoundError("没有这条持仓记录，可能已经被删除。")
    return {k: row[k] for k in _POSITION_KEYS}


def add_position(strategy_id: str, code: str, shares: int, buy_date: str, buy_price: float, cost: float) -> int:
    """记一笔买入，同时从可用资金里扣掉花的钱。"""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO real_positions (strategy_id, code, shares, buy_date, buy_price, cost)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_id, code, shares, buy_date, buy_price, cost),
        )
        conn.execute("UPDATE real_accounts SET cash = cash - ? WHERE strategy_id = ?", (cost, strategy_id))
        return int(cur.lastrowid or 0)


def change_position(position_id: int, shares: int, buy_date: str, buy_price: float, cost: float) -> None:
    """改一笔还没卖的持仓，可用资金按花的钱的差额调整。"""
    old = get_position(position_id)
    with connect() as conn:
        conn.execute("UPDATE real_positions SET shares = ?, buy_date = ?, buy_price = ?, cost = ? WHERE id = ?",
                     (shares, buy_date, buy_price, cost, position_id))
        conn.execute("UPDATE real_accounts SET cash = cash - ? WHERE strategy_id = ?",
                     (cost - float(old["cost"]), old["strategy_id"]))


def close_position(position_id: int, sell_date: str, sell_price: float, proceeds: float, reason: str) -> None:
    """记一笔卖出，同时把收回的钱加回可用资金。"""
    old = get_position(position_id)
    with connect() as conn:
        conn.execute("UPDATE real_positions SET sell_date = ?, sell_price = ?, proceeds = ?, reason = ? WHERE id = ?",
                     (sell_date, sell_price, proceeds, reason, position_id))
        conn.execute("UPDATE real_accounts SET cash = cash + ? WHERE strategy_id = ?",
                     (proceeds, old["strategy_id"]))


def delete_position(position_id: int) -> None:
    """删掉一条记错的记录，把它对可用资金的影响也撤回。"""
    old = get_position(position_id)
    with connect() as conn:
        conn.execute("DELETE FROM real_positions WHERE id = ?", (position_id,))
        conn.execute("UPDATE real_accounts SET cash = cash + ? WHERE strategy_id = ?",
                     (float(old["cost"]) - float(old["proceeds"] or 0.0), old["strategy_id"]))


# ---------------- 待确认的提醒 ----------------
def add_todo(strategy_id: str, day: str, side: str, code: str, shares: int, price: float, reason: str) -> None:
    """同一天、同一只股票、同一个方向只记一条；已经答复过的不会重新出现。"""
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO real_todos (strategy_id, date, side, code, shares, price, reason, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (strategy_id, day, side, code, shares, price, reason, PENDING),
        )


def todos(strategy_id: str, *, status: str | None = PENDING, day: str | None = None) -> list[dict[str, Any]]:
    init_schema()
    sql, args = "SELECT * FROM real_todos WHERE strategy_id = ?", [strategy_id]
    if status is not None:
        sql, args = sql + " AND status = ?", [*args, status]
    if day is not None:
        sql, args = sql + " AND date = ?", [*args, day]
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY date DESC, side DESC, id", args).fetchall()
    return [{k: r[k] for k in _TODO_KEYS} for r in rows]


def get_todo(todo_id: int) -> dict[str, Any]:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT * FROM real_todos WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise NotFoundError("没有这条提醒，可能已经处理过了。")
    return {k: row[k] for k in _TODO_KEYS}


def set_todo_status(todo_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE real_todos SET status = ? WHERE id = ?", (status, todo_id))


def skip_pending(strategy_id: str, *, side: str | None = None, code: str | None = None) -> None:
    sql, args = "UPDATE real_todos SET status = ? WHERE strategy_id = ? AND status = ?", [SKIPPED, strategy_id, PENDING]
    if side is not None:
        sql, args = sql + " AND side = ?", [*args, side]
    if code is not None:
        sql, args = sql + " AND code = ?", [*args, code]
    with connect() as conn:
        conn.execute(sql, args)
