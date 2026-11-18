"""期货行情的接口。两个缓存各管一道闸：行情至少隔 30 秒才重取，一个品种的日线 30 分钟内不重取。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from . import sina
from .catalog import BY_CODE

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/futures"
QUOTE_INTERVAL = 30.0
KLINE_INTERVAL = 1800.0
KLINE_BARS = 180


class Cache:
    """带最短间隔的缓存。取失败也要等满间隔才再试，失败时把上一次的结果和原因一起给出去。"""

    def __init__(self, interval: float, clock=time.monotonic) -> None:
        self._interval, self._clock = interval, clock
        self._lock = threading.Lock()
        self._slots: dict[str, dict[str, Any]] = {}

    def get(self, key: str, load) -> dict[str, Any]:
        with self._lock:
            slot = self._slots.setdefault(key, {"at": None, "value": None, "fetched_at": None, "error": None})
            if slot["at"] is None or self._clock() - slot["at"] >= self._interval:
                slot["at"] = self._clock()
                try:
                    slot["value"] = load()
                    slot["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    slot["error"] = None
                except Exception as e:  # noqa: BLE001 —— 页面要显示原因，不能让接口报 500
                    logger.warning("期货数据没有取到：%s", getattr(e, "message", e))
                    slot["error"] = str(getattr(e, "message", e))
            return dict(slot)


def build_router() -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["custom-futures"])
    quotes, klines = Cache(QUOTE_INTERVAL), Cache(KLINE_INTERVAL)

    @router.get("/quotes")
    def get_quotes() -> dict[str, Any]:
        """所有品种的最新行情。30 秒内重复来问给的是上一次的结果。"""
        slot = quotes.get("all", sina.fetch_quotes)
        return {"items": slot["value"] or [], "fetched_at": slot["fetched_at"], "error": slot["error"],
                "interval": QUOTE_INTERVAL}

    @router.get("/kline")
    def get_kline(code: str) -> dict[str, Any]:
        """一个品种最近 180 根日线。"""
        item = BY_CODE.get(code)
        if item is None:
            raise HTTPException(status_code=404, detail="没有这个品种。")
        slot = klines.get(code, lambda: sina.fetch_kline(item, KLINE_BARS))
        return {"code": code, "name": item.name, "bars": slot["value"] or [], "error": slot["error"]}

    return router
