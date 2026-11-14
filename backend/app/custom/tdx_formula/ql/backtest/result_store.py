"""每个策略最近一次组合回测的结果。只保留最新的一份。"""

from __future__ import annotations

import json
from typing import Any

from app.custom.tdx_formula.ql.core.db import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtests (
    strategy_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    result_json TEXT NOT NULL
)
"""


def init_schema() -> None:
    with connect() as conn:
        conn.execute(_SCHEMA)


def save(strategy_id: str, result: dict[str, Any]) -> None:
    init_schema()
    with connect() as conn:
        conn.execute(
            "INSERT INTO backtests (strategy_id, created_at, result_json) VALUES (?, ?, ?)"
            " ON CONFLICT(strategy_id) DO UPDATE SET created_at = excluded.created_at,"
            " result_json = excluded.result_json",
            (strategy_id, str(result.get("created_at", "")), json.dumps(result, ensure_ascii=False)),
        )


def load(strategy_id: str) -> dict[str, Any] | None:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT result_json FROM backtests WHERE strategy_id = ?", (strategy_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def load_all() -> dict[str, dict[str, Any]]:
    init_schema()
    with connect() as conn:
        rows = conn.execute("SELECT strategy_id, result_json FROM backtests").fetchall()
    return {r["strategy_id"]: json.loads(r["result_json"]) for r in rows}


def delete(strategy_id: str) -> None:
    init_schema()
    with connect() as conn:
        conn.execute("DELETE FROM backtests WHERE strategy_id = ?", (strategy_id,))
