"""期货行情的接口。

期货跟着面板的“实时行情”开关走：开关开着才联网取，更新间隔用的是免费数据源设置里
“每隔多少秒取一轮全市场快照”那个数，和股票行情一致。开关关着时不发任何请求，
给出去的是上一次取到并存在本机的数据。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from . import sina
from .catalog import BY_CODE

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/futures"
DEFAULT_INTERVAL = 60.0
KLINE_BARS = 180


def live() -> bool:
    """面板的实时行情开关是不是开着。"""
    from app.services import preferences

    return bool(preferences.get_realtime_quotes_enabled())


def interval() -> float:
    """两次取数之间至少隔多少秒：和股票的全市场快照用同一个设置。"""
    try:
        from app.plugins.freecn.quotes import SnapshotCache

        return float(SnapshotCache.interval())
    except Exception:  # noqa: BLE001 —— 设置读不到时用默认值，不让页面打不开
        return DEFAULT_INTERVAL


def store_dir() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "user_data" / "extensions" / "futures"


class Store:
    """取到的数据：内存里一份，本机文件里一份（重启后、开关关着时还能看到上一次的）。

    只有 ``live()`` 为真、而且离上次取数满了 ``interval()`` 秒才联网；
    取失败也要等满间隔才再试，失败时把上一次的结果和原因一起给出去。
    """

    def __init__(self, folder: Callable[[], Path], live: Callable[[], bool], interval: Callable[[], float],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._folder, self._live, self._interval, self._clock = folder, live, interval, clock
        self._lock = threading.Lock()
        self._slots: dict[str, dict[str, Any]] = {}

    def _path(self, key: str) -> Path:
        return self._folder() / f"{key}.json"

    def _slot(self, key: str) -> dict[str, Any]:
        slot = self._slots.get(key)
        if slot is None:
            slot = {"at": None, "value": None, "fetched_at": None, "error": None}
            try:
                saved = json.loads(self._path(key).read_text(encoding="utf-8"))
                slot["value"], slot["fetched_at"] = saved["value"], saved["fetched_at"]
            except (OSError, ValueError, KeyError, TypeError):
                pass  # 没存过或文件坏了：当作没有
            self._slots[key] = slot
        return slot

    def get(self, key: str, load: Callable[[], Any]) -> dict[str, Any]:
        with self._lock:
            slot = self._slot(key)
            on = self._live()
            if not on:
                slot["error"] = None  # 关着不算出错
            elif slot["at"] is None or self._clock() - slot["at"] >= self._interval():
                slot["at"] = self._clock()
                try:
                    slot["value"] = load()
                    slot["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    slot["error"] = None
                    self._save(key, slot)
                except Exception as e:  # noqa: BLE001 —— 页面要显示原因，不能让接口报 500
                    logger.warning("期货数据没有取到：%s", getattr(e, "message", e))
                    slot["error"] = str(getattr(e, "message", e))
            return {**slot, "live": on}

    def _save(self, key: str, slot: dict[str, Any]) -> None:
        try:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"value": slot["value"], "fetched_at": slot["fetched_at"]}, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            logger.warning("期货数据没有存到本机", exc_info=True)


def build_router(store: Store | None = None) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["custom-futures"])
    data = store or Store(store_dir, live, interval)

    @router.get("/quotes")
    def get_quotes() -> dict[str, Any]:
        """所有品种的行情。实时行情开关关着时不联网，给的是上一次存下来的。"""
        slot = data.get("quotes", sina.fetch_quotes)
        return {"items": slot["value"] or [], "fetched_at": slot["fetched_at"], "error": slot["error"],
                "live": slot["live"], "interval": interval()}

    @router.get("/kline")
    def get_kline(code: str) -> dict[str, Any]:
        """一个品种最近 180 根日线。和行情一样跟着实时行情开关和更新间隔走。"""
        item = BY_CODE.get(code)
        if item is None:
            raise HTTPException(status_code=404, detail="没有这个品种。")
        slot = data.get(f"kline_{code}", lambda: sina.fetch_kline(item, KLINE_BARS))
        return {"code": code, "name": item.name, "bars": slot["value"] or [], "fetched_at": slot["fetched_at"],
                "error": slot["error"], "live": slot["live"]}

    return router
