"""港股报价的接口。

和期货一样跟着面板的“实时行情”开关走：开关开着时按股票行情的更新间隔联网取。
开关关着时不跟着盘中变，只在两种情况下取一次：
  1. 存着的数据早于最近一次港股收盘 —— 相当于 A 股每天更新一次的收盘价；
  2. 要的股票里有从没取到过的（刚登记的持仓）。
其余时候给出去的是上一次取到并存在本机的数据。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.custom.futures.routes import interval, live

from . import sina

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/hk-quotes"
MAX_SYMBOLS = 50
STAMP = "%Y-%m-%d %H:%M:%S"


def store_path() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "user_data" / "extensions" / "hk_quotes" / "quotes.json"


def last_close(now: datetime) -> datetime:
    """最近一次已经过去的港股收盘（工作日 16:10，留出收盘竞价）。不管节假日：假日多取一次无妨。"""
    day = now.replace(hour=16, minute=10, second=0, microsecond=0)
    if now < day:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def clean(symbols: str) -> list[str]:
    """逗号分隔的代码 → 去重、只留写法正确的港股代码（00981.HK）。"""
    seen: list[str] = []
    for raw in symbols.split(","):
        symbol = raw.strip().upper()
        if sina.SYMBOL.match(symbol) and symbol not in seen:
            seen.append(symbol)
    return seen[:MAX_SYMBOLS]


def read_saved(path: Path) -> dict[str, Any]:
    """本机存着的报价：{"items", "rate", "fetched_at"}。没存过或文件坏了给空的。"""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return {"items": dict(saved["items"]), "rate": saved.get("rate"), "fetched_at": saved.get("fetched_at")}
    except (OSError, ValueError, KeyError, TypeError):
        return {"items": {}, "rate": None, "fetched_at": None}


class Store:
    def __init__(self, path: Callable[[], Path], live: Callable[[], bool], interval: Callable[[], float],
                 load: Callable[[list[str]], dict[str, Any]], clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._path, self._live, self._interval, self._load = path, live, interval, load
        self._clock, self._now = clock, now
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._at: float | None = None  # 上次联网（不管成没成）的时刻
        self._error: str | None = None

    def _due(self, symbols: list[str], on: bool) -> bool:
        data = self._data or {}
        if self._at is not None and self._clock() - self._at < self._interval():
            return False  # 任何情况下两次联网都至少隔一个间隔
        if on:
            return True
        if any(s not in data["items"] for s in symbols):
            return True
        try:
            fetched = datetime.strptime(data["fetched_at"] or "", STAMP)
        except ValueError:
            return True
        return fetched < last_close(self._now())

    def get(self, symbols: list[str]) -> dict[str, Any]:
        with self._lock:
            if self._data is None:
                self._data = read_saved(self._path())
            on = self._live()
            if symbols and self._due(symbols, on):
                self._at = self._clock()
                try:
                    got = self._load(symbols)
                    self._data["items"].update(got["items"])
                    if got.get("rate"):
                        self._data["rate"] = got["rate"]
                    self._data["fetched_at"] = self._now().strftime(STAMP)
                    self._error = None
                    self._save()
                except Exception as e:  # noqa: BLE001 —— 页面要显示原因，不能让接口报 500
                    logger.warning("港股报价没有取到：%s", getattr(e, "message", e))
                    self._error = str(getattr(e, "message", e))
            elif not on:
                self._error = None
            items = {s: self._data["items"][s] for s in symbols if s in self._data["items"]}
            return {"items": items, "rate": self._data["rate"], "fetched_at": self._data["fetched_at"],
                    "error": self._error, "live": on}

    def _save(self) -> None:
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("港股报价没有存到本机", exc_info=True)


def build_router(store: Store | None = None) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["custom-hk-quotes"])
    data = store or Store(store_path, live, interval, sina.fetch)

    @router.get("/quotes")
    def get_quotes(symbols: str = "") -> dict[str, Any]:
        """几只港股的报价和港元兑人民币汇率。写法不对的代码直接忽略。"""
        return {**data.get(clean(symbols)), "interval": interval()}

    return router
