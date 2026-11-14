"""模拟账户、提醒、每日任务记录、设置的存取（SQLite）。

这些记录不可重建：账户是从"设为运行中"那天起一天一天推进出来的，删掉就没有了。
"""

from __future__ import annotations

import json
from typing import Any

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.db import connect

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS live_accounts (
        strategy_id TEXT PRIMARY KEY,
        started_on TEXT NOT NULL,
        initial_cash REAL NOT NULL,
        last_date TEXT,
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_days (
        strategy_id TEXT NOT NULL,
        date TEXT NOT NULL,
        equity REAL NOT NULL,
        cash REAL NOT NULL,
        PRIMARY KEY (strategy_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        code TEXT NOT NULL,
        buy_date TEXT NOT NULL,
        buy_price REAL NOT NULL,
        sell_date TEXT NOT NULL,
        sell_price REAL,
        shares INTEGER NOT NULL,
        ret REAL NOT NULL,
        pnl REAL NOT NULL,
        reason TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_reminders (
        strategy_id TEXT NOT NULL,
        date TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (strategy_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        due_date TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        ok INTEGER,
        data_date TEXT,
        pushed INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL
    )
    """,
)


def init_schema() -> None:
    with connect() as conn:
        for statement in _SCHEMA:
            conn.execute(statement)


def _now() -> str:
    return clock.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 设置 ----------------
def get_setting(key: str, default: Any = None) -> Any:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def set_setting(key: str, value: Any) -> None:
    init_schema()
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value_json) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (key, json.dumps(value, ensure_ascii=False)),
        )


# ---------------- 账户 ----------------
def get_account(strategy_id: str) -> dict[str, Any] | None:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT * FROM live_accounts WHERE strategy_id = ?", (strategy_id,)).fetchone()
    if row is None:
        return None
    return {"strategy_id": row["strategy_id"], "started_on": row["started_on"],
            "initial_cash": float(row["initial_cash"]), "last_date": row["last_date"],
            "state": json.loads(row["state_json"]), "created_at": row["created_at"]}


def create_account(strategy_id: str, started_on: str, initial_cash: float, state: dict[str, Any]) -> None:
    init_schema()
    with connect() as conn:
        conn.execute(
            "INSERT INTO live_accounts (strategy_id, started_on, initial_cash, last_date, state_json, created_at)"
            " VALUES (?, ?, ?, NULL, ?, ?)",
            (strategy_id, started_on, initial_cash, json.dumps(state, ensure_ascii=False), _now()),
        )


def save_day(strategy_id: str, day: str, state: dict[str, Any], equity: float, cash: float,
             trades: list[dict[str, Any]], reminder: dict[str, Any]) -> None:
    """一个交易日的全部结果在同一个事务里写入：账户状态、当天资产、当天了结的交易、当天生成的提醒。"""
    with connect() as conn:
        conn.execute("UPDATE live_accounts SET last_date = ?, state_json = ? WHERE strategy_id = ?",
                     (day, json.dumps(state, ensure_ascii=False), strategy_id))
        conn.execute("INSERT OR REPLACE INTO live_days (strategy_id, date, equity, cash) VALUES (?, ?, ?, ?)",
                     (strategy_id, day, equity, cash))
        for t in trades:
            conn.execute(
                "INSERT INTO live_trades (strategy_id, code, buy_date, buy_price, sell_date, sell_price, shares,"
                " ret, pnl, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (strategy_id, t["code"], t["buy_date"], t["buy_price"], t["sell_date"], t["sell_price"],
                 t["shares"], t["ret"], t["pnl"], t["reason"]),
            )
        conn.execute("INSERT OR REPLACE INTO live_reminders (strategy_id, date, payload_json) VALUES (?, ?, ?)",
                     (strategy_id, day, json.dumps(reminder, ensure_ascii=False)))


def latest_reminder(strategy_id: str) -> dict[str, Any] | None:
    init_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM live_reminders WHERE strategy_id = ? ORDER BY date DESC LIMIT 1",
            (strategy_id,)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def reminder_dates(strategy_id: str) -> list[str]:
    init_schema()
    with connect() as conn:
        rows = conn.execute("SELECT date FROM live_reminders WHERE strategy_id = ? ORDER BY date",
                            (strategy_id,)).fetchall()
    return [r["date"] for r in rows]


def get_reminder(strategy_id: str, day: str) -> dict[str, Any] | None:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT payload_json FROM live_reminders WHERE strategy_id = ? AND date = ?",
                           (strategy_id, day)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def days(strategy_id: str) -> list[dict[str, Any]]:
    init_schema()
    with connect() as conn:
        rows = conn.execute("SELECT date, equity, cash FROM live_days WHERE strategy_id = ? ORDER BY date",
                            (strategy_id,)).fetchall()
    return [{"date": r["date"], "equity": float(r["equity"]), "cash": float(r["cash"])} for r in rows]


def trades(strategy_id: str) -> list[dict[str, Any]]:
    init_schema()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM live_trades WHERE strategy_id = ? ORDER BY sell_date DESC, id DESC",
                            (strategy_id,)).fetchall()
    return [{k: r[k] for k in ("code", "buy_date", "buy_price", "sell_date", "sell_price", "shares", "ret", "pnl",
                               "reason")} for r in rows]


# ---------------- 每日任务记录 ----------------
def start_run(due_date: str) -> int:
    init_schema()
    with connect() as conn:
        cur = conn.execute("INSERT INTO live_runs (due_date, started_at) VALUES (?, ?)", (due_date, _now()))
        return int(cur.lastrowid or 0)


def finish_run(run_id: int, ok: bool, data_date: str | None, pushed: bool, message: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE live_runs SET finished_at = ?, ok = ?, data_date = ?, pushed = ?, message = ?"
                     " WHERE id = ?", (_now(), int(ok), data_date, int(pushed), message, run_id))


def close_interrupted_runs() -> int:
    """程序启动时调用：上次运行到一半、程序就退出了的任务，记成"被打断"。返回处理了几条。"""
    init_schema()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE live_runs SET finished_at = ?, ok = 0, message = ? WHERE finished_at IS NULL",
            (_now(), "程序在这次任务进行中被重启，任务没有跑完。稍后会自动重试，也可以手动运行一次。"),
        )
        return int(cur.rowcount or 0)


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    init_schema()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM live_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"due_date": r["due_date"], "started_at": r["started_at"], "finished_at": r["finished_at"],
             "ok": None if r["ok"] is None else bool(r["ok"]), "data_date": r["data_date"],
             "pushed": bool(r["pushed"]), "message": r["message"]} for r in rows]


def last_run() -> dict[str, Any] | None:
    runs = recent_runs(1)
    return runs[0] if runs else None


def last_ok_due_date() -> str | None:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT MAX(due_date) AS d FROM live_runs WHERE ok = 1").fetchone()
    return row["d"] if row else None


def pushed_for(data_date: str) -> bool:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM live_runs WHERE data_date = ? AND pushed = 1 LIMIT 1",
                           (data_date,)).fetchone()
    return row is not None
