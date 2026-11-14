"""同一时间只允许一个数据任务（历史下载、每日更新）在运行，保证同一来源不会被两个任务同时访问。

拿不到锁不排队，直接报"已有任务在运行"。行情目录归一个 TSP 进程独占，所以用进程内的锁就够了。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.custom.tdx_formula.ql.core.errors import ConflictError

_lock = threading.Lock()


@contextmanager
def job_lock(path: Path | None = None) -> Iterator[None]:
    if not _lock.acquire(blocking=False):
        raise ConflictError("已有数据任务在运行，请等它结束后再试")
    try:
        yield
    finally:
        _lock.release()


def is_locked(path: Path | None = None) -> bool:
    """是否有数据任务正持有锁（只用于显示，不保证下一刻仍然成立）。"""
    return _lock.locked()
