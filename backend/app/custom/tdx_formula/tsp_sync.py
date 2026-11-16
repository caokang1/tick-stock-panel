"""行情更新完以后，让 TSP 接着同步一次。

免费公开来源每天 18:30 之后才有当天的行情，而 TSP 自己的盘后同步默认在 15:35，那时拿不到当天的数据。
所以这里每分钟看一眼：TSP 的日 K 用的是“免费公开来源”，而且本机行情更新到的这一天还没有让 TSP 同步过，
就替用户点一次 TSP 的“同步”（走的是 TSP 自己的盘后管道，和在页面上点是同一条路）。

只看“TSP 里最新的一天比本机旧”是不够的：开着实时行情时，TSP 在收盘时就用行情快照写出了当天的数据，
日期上已经是最新，但那不是正式日线。所以另外记一笔“已经为哪一天同步过”，每个交易日的正式行情都同步一次。

不成功不会一直重试：同一天的行情最多试 ``MAX_ATTEMPTS`` 次，每次至少隔 ``RETRY_MINUTES`` 分钟。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .ql.live import store as live_store

logger = logging.getLogger(__name__)

KEY_ENABLED = "tsp_auto_sync"
KEY_SYNCED = "tsp_synced_through"  # 已经为哪一天的正式行情让 TSP 同步过
RETRY_MINUTES = 30
MAX_ATTEMPTS = 3
SOURCE = "freecn"


def tsp_latest_date(data_dir: Path) -> date | None:
    """TSP 自己那份日 K 里最新的一天（按 kline_daily 下面的日期目录看）；一天都没有时返回 None。"""
    latest: date | None = None
    root = data_dir / "kline_daily"
    if not root.is_dir():
        return None
    for child in root.iterdir():
        name = child.name
        if not name.startswith("date=") or not child.is_dir():
            continue
        try:
            day = date.fromisoformat(name[5:])
        except ValueError:
            continue
        if latest is None or day > latest:
            latest = day
    return latest


def _daily_source() -> str:
    from app.services import preferences

    return str(preferences.load().get("daily_data_provider") or "tickflow").lower()


class TspSync:
    def __init__(self) -> None:
        self._attempts: dict[date, int] = {}
        self._last_attempt: datetime | None = None
        self._message: str | None = None

    @staticmethod
    def enabled() -> bool:
        return str(live_store.get_setting(KEY_ENABLED, "1")) != "0"

    @staticmethod
    def set_enabled(value: bool) -> None:
        live_store.set_setting(KEY_ENABLED, "1" if value else "0")

    @staticmethod
    def synced_through() -> date | None:
        try:
            return date.fromisoformat(str(live_store.get_setting(KEY_SYNCED, "")))
        except ValueError:
            return None

    def info(self) -> dict[str, Any]:
        return {"auto_sync_tsp": self.enabled(), "tsp_sync_message": self._message}

    def due(self, data_dir: Path, local_latest: date | None, now: datetime) -> bool:
        """现在该不该让 TSP 同步一次。"""
        if local_latest is None or not self.enabled() or _daily_source() != SOURCE:
            return False
        tsp_latest = tsp_latest_date(data_dir)
        # TSP 一天数据都没有时不替用户做第一次同步：那是一次大的初始化，留给用户自己决定
        if tsp_latest is None:
            return False
        synced = self.synced_through()
        if tsp_latest >= local_latest and synced is not None and synced >= local_latest:
            return False  # 日期不落后，而且这一天的正式行情已经同步过
        if self._attempts.get(local_latest, 0) >= MAX_ATTEMPTS:
            return False
        return self._last_attempt is None or now - self._last_attempt >= timedelta(minutes=RETRY_MINUTES)

    def trigger(self, app: Any, loop: asyncio.AbstractEventLoop, local_latest: date, now: datetime) -> None:
        """在 TSP 的事件循环里发起一次盘后管道。已经有任务在跑时 TSP 会复用那个任务，不会并发。"""
        from app.api.pipeline import trigger_pipeline_job

        self._last_attempt = now
        self._attempts = {local_latest: self._attempts.get(local_latest, 0) + 1}  # 只记当前这一天的
        state = app.state
        coroutine = trigger_pipeline_job(state.repo, state.capabilities, getattr(state, "quote_service", None))
        try:
            result = asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=30)
        except Exception as e:  # noqa: BLE001 —— 触发不成功只记下来，下一轮再看
            self._message = f"{now:%m-%d %H:%M} 想让 TSP 同步 {local_latest} 的行情，没有成功：{type(e).__name__}"
            logger.warning("让 TSP 同步没有成功：%s", e)
            return
        live_store.set_setting(KEY_SYNCED, local_latest.isoformat())
        reused = bool(isinstance(result, dict) and result.get("reused"))
        self._message = (f"{now:%m-%d %H:%M} 行情已更新到 {local_latest}，"
                         + ("TSP 正好有同步任务在跑，没有另开。" if reused else "已让 TSP 开始同步。"))
        logger.info("行情已更新到 %s，已触发 TSP 的盘后管道（reused=%s）", local_latest, reused)
