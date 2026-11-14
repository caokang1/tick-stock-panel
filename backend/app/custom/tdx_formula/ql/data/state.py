"""SQLite 里的持久状态：请求计数、冷却截止时间、连续失败次数、更新记录。

表只增不改；以后要改表结构必须写迁移脚本（BACKEND_RULES.md §数据安全 R4）。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.custom.tdx_formula.ql.core.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_usage (
    source TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, day)
);
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    cooldown_until TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS data_update_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class SourceState:
    cooldown_until: datetime | None
    consecutive_failures: int


@dataclass(frozen=True)
class UpdateRecord:
    kind: str
    started_at: datetime
    finished_at: datetime
    ok: bool
    message: str


class StateStore:
    """线程安全；每次操作开一个短连接，多个进程可以同时读写。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or get_settings().data_dir / "state.db"
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self._path, timeout=30)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ---- 请求计数 ----
    def get_usage(self, source: str, day: date) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count FROM source_usage WHERE source = ? AND day = ?", (source, day.isoformat())
            ).fetchone()
        return int(row[0]) if row else 0

    def add_usage(self, source: str, day: date, amount: int) -> int:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO source_usage (source, day, count) VALUES (?, ?, ?) "
                "ON CONFLICT (source, day) DO UPDATE SET count = count + excluded.count",
                (source, day.isoformat(), amount),
            )
            row = conn.execute(
                "SELECT count FROM source_usage WHERE source = ? AND day = ?", (source, day.isoformat())
            ).fetchone()
        return int(row[0])

    # ---- 冷却与连续失败 ----
    def get_state(self, source: str) -> SourceState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cooldown_until, consecutive_failures FROM source_state WHERE source = ?", (source,)
            ).fetchone()
        if row is None:
            return SourceState(None, 0)
        return SourceState(datetime.fromisoformat(row[0]) if row[0] else None, int(row[1]))

    def set_cooldown(self, source: str, until: datetime | None) -> None:
        value = until.isoformat() if until else None
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO source_state (source, cooldown_until) VALUES (?, ?) "
                "ON CONFLICT (source) DO UPDATE SET cooldown_until = excluded.cooldown_until",
                (source, value),
            )

    def set_failures(self, source: str, failures: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO source_state (source, consecutive_failures) VALUES (?, ?) "
                "ON CONFLICT (source) DO UPDATE SET consecutive_failures = excluded.consecutive_failures",
                (source, failures),
            )

    def reset_failures(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE source_state SET consecutive_failures = 0")

    # ---- 更新记录 ----
    def log_update(self, kind: str, started_at: datetime, finished_at: datetime, ok: bool, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO data_update_log (kind, started_at, finished_at, ok, message) VALUES (?, ?, ?, ?, ?)",
                (kind, started_at.isoformat(), finished_at.isoformat(), int(ok), message),
            )

    def last_update(self, kind: str | None = None) -> UpdateRecord | None:
        sql = "SELECT kind, started_at, finished_at, ok, message FROM data_update_log"
        args: tuple[str, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (kind,)
        with self._connect() as conn:
            row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if row is None:
            return None
        started, finished = datetime.fromisoformat(row[1]), datetime.fromisoformat(row[2])
        return UpdateRecord(row[0], started, finished, bool(row[3]), row[4])
