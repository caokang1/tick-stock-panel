"""策略的保存与读取。配置结构见技术设计文档 §8。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.db import connect
from app.custom.tdx_formula.ql.core.errors import ConflictError, NotFoundError, QuantLabError

STATUS_DRAFT = "draft"
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUSES = (STATUS_DRAFT, STATUS_RUNNING, STATUS_STOPPED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    status: str
    builtin: bool
    config: dict[str, Any]
    created_at: str
    updated_at: str


def _now() -> str:
    return clock.now().strftime("%Y-%m-%d %H:%M:%S")


def init_schema() -> None:
    with connect() as conn:
        conn.execute(_SCHEMA)


def _row(r: Any) -> Strategy:
    return Strategy(
        id=r["id"], name=r["name"], status=r["status"], builtin=bool(r["builtin"]),
        config=json.loads(r["config_json"]), created_at=r["created_at"], updated_at=r["updated_at"],
    )


def empty_config() -> dict[str, Any]:
    return {
        "conditions": [],
        "universe": {
            "exclude_st": True, "exclude_star": True, "exclude_chinext": True,
            "exclude_bse": True, "exclude_suspended": True,
        },
        "rank": None, "trade": None, "timing": None,
    }


def list_strategies() -> list[Strategy]:
    init_schema()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM strategies ORDER BY builtin DESC, created_at ASC, id ASC").fetchall()
    return [_row(r) for r in rows]


def get_strategy(strategy_id: str) -> Strategy:
    init_schema()
    with connect() as conn:
        r = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if r is None:
        raise NotFoundError("没有找到这个策略，它可能已被删除。")
    return _row(r)


def _check(name: str, status: str) -> None:
    if not name.strip():
        raise QuantLabError("策略名称不能为空。")
    if status not in STATUSES:
        raise QuantLabError(f"未知的策略状态：{status}")


def create_strategy(name: str, config: dict[str, Any] | None = None, status: str = STATUS_DRAFT,
                    builtin: bool = False, strategy_id: str | None = None) -> Strategy:
    _check(name, status)
    init_schema()
    sid = strategy_id or uuid.uuid4().hex[:12]
    now = _now()
    with connect() as conn:
        if conn.execute("SELECT 1 FROM strategies WHERE id = ?", (sid,)).fetchone():
            raise ConflictError("已经存在相同编号的策略。")
        conn.execute(
            "INSERT INTO strategies (id, name, status, builtin, config_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, name.strip(), status, int(builtin), json.dumps(config or empty_config(), ensure_ascii=False),
             now, now),
        )
    return get_strategy(sid)


def update_strategy(strategy_id: str, name: str | None = None, config: dict[str, Any] | None = None,
                    status: str | None = None) -> Strategy:
    current = get_strategy(strategy_id)
    new_name = current.name if name is None else name
    new_status = current.status if status is None else status
    _check(new_name, new_status)
    new_config = current.config if config is None else config
    with connect() as conn:
        conn.execute(
            "UPDATE strategies SET name = ?, status = ?, config_json = ?, updated_at = ? WHERE id = ?",
            (new_name.strip(), new_status, json.dumps(new_config, ensure_ascii=False), _now(), strategy_id),
        )
    return get_strategy(strategy_id)


def delete_strategy(strategy_id: str) -> None:
    current = get_strategy(strategy_id)
    if current.builtin:
        raise QuantLabError("内置策略不能删除。可以先复制一份，再修改复制出来的策略。")
    with connect() as conn:
        conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))


def copy_strategy(strategy_id: str) -> Strategy:
    src = get_strategy(strategy_id)
    return create_strategy(f"{src.name}（副本）", config=src.config, status=STATUS_DRAFT, builtin=False)


def ensure_builtin(strategy_id: str, name: str, config: dict[str, Any]) -> Strategy:
    """内置策略不存在时写入；已存在则保持用户看到的内容不变。"""
    init_schema()
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if exists:
        return get_strategy(strategy_id)
    return create_strategy(name, config=config, status=STATUS_DRAFT, builtin=True, strategy_id=strategy_id)
