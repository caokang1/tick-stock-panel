"""扩展自己的业务库（SQLite）：策略、模拟账户、实际持仓、提醒记录都在里面。文件位置由扩展在启动时指定。"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path

from app.custom.tdx_formula.ql.core.errors import DataError

logger = logging.getLogger(__name__)

BACKUP_KEEP = 14  # 保留最近多少天的备份
_path: Path | None = None


def set_path(path: Path) -> None:
    global _path
    _path = Path(path)


def db_path() -> Path:
    if _path is None:
        raise DataError("扩展的数据目录还没有就绪。")
    return _path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开业务库；正常结束时提交，出错时回滚。"""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def backup_dir() -> Path:
    return db_path().parent / "backups"


def list_backups() -> list[Path]:
    """已有的备份，从旧到新。"""
    d = backup_dir()
    return sorted(d.glob("tdx-*.db")) if d.exists() else []


def backup(day: date, keep: int = BACKUP_KEEP) -> Path | None:
    """给业务库留一份当天的备份（模拟账户、实际持仓和提醒记录丢了无法重建）。

    一天只留一份：当天已经备份过就不再覆盖，这样留下的是当天改动之前的样子。只保留最近 keep 份。
    业务库还不存在时返回 None。用 SQLite 自带的备份接口，程序正在读写时也能得到完整的一份。
    """
    source = db_path()
    if not source.exists():
        return None
    target = backup_dir() / f"tdx-{day.isoformat()}.db"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.unlink(missing_ok=True)
        with closing(sqlite3.connect(source, timeout=30)) as src, closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
        os.replace(tmp, target)
        logger.info("通达信公式扩展的业务库已备份到 %s", target)
    for old in list_backups()[:-keep]:
        old.unlink(missing_ok=True)
    return target
