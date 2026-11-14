"""分钟 K：新浪的 1 分钟线，一次请求只能查一只股票，最多给最近 1970 根（约 8 个交易日）。

所以这里只支持“点开哪只查哪只”：一次最多 ``MAX_SYMBOLS`` 只，多了直接拒绝并说明原因——
全市场的历史分钟同步要发 5000 多次请求，免费来源承受不了，硬做只会被限制访问，还会连累日线的每日更新。

每个请求都经扩展的限流器（和日线下载里的“新浪财经”共用一套间隔和计数）；同一只股票刚查过的，
在实时行情的快照间隔内不再重复查。

来源的一行：``{"day": "2026-11-13 09:31:00", "open", "high", "low", "close", "volume"（股）, "amount"（元）}``，
时间是这一分钟结束的时刻（北京时间），第一根是 09:31。成交量按 TSP 的口径换成手。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

SOURCE = "akshare_sina"
URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
MAX_BARS = 1970
BARS_PER_DAY = 241
HISTORY_DAYS = 8
MAX_SYMBOLS = 20
SCHEMA = {
    "symbol": pl.String, "datetime": pl.Datetime("us"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}
TOO_MANY = (f"免费公开来源的分钟 K 一次只能查一只股票，这里单次最多 {MAX_SYMBOLS} 只，"
            "不支持全市场的历史分钟同步（要发 5000 多次请求，会被来源限制访问）。请在个股详情里逐只查看。")

_BEIJING = timezone(timedelta(hours=8))

Fetch = Callable[[dict[str, str], float], tuple[int, str]]


def _http_get(params: dict[str, str], timeout: float) -> tuple[int, str]:
    import requests

    response = requests.get(URL, params=params, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    return response.status_code, response.text


_fetch: Fetch = _http_get
_lock = threading.Lock()
_recent: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}  # symbol → (取的时刻, 要了几根, 原始行)


def set_fetch(fetch: Fetch | None) -> None:
    """换掉真正发请求的函数（测试用）；传 None 恢复。同时清掉刚查过的记录。"""
    global _fetch
    _fetch = fetch or _http_get
    with _lock:
        _recent.clear()


def bars_needed(start: datetime | None, end: datetime) -> int:
    """这段时间大概有多少根 1 分钟线（按自然日估，宁多勿少），不超过来源一次给的上限。"""
    if start is None:
        return MAX_BARS
    days = max(1, (end.date() - start.date()).days + 1)
    return min(MAX_BARS, days * BARS_PER_DAY)


def _download(code: str, count: int) -> list[dict[str, Any]]:
    from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
    from app.custom.tdx_formula.ql.data.runtime import current_config, get_runtime

    timeout = float(current_config().sources[SOURCE].timeout_seconds)
    params = {"symbol": code, "scale": "1", "ma": "no", "datalen": str(count)}

    def one() -> list[dict[str, Any]]:
        try:
            status, text = _fetch(params, timeout)
        except Exception as e:
            raise SourceUnavailable(f"新浪分钟 K 请求失败：{type(e).__name__}: {e}") from e
        if status in (403, 429, 456):
            raise SourceRejected(f"新浪限制了访问（HTTP {status}）")
        if status != 200:
            raise SourceUnavailable(f"新浪分钟 K 返回了 HTTP {status}")
        try:
            body = json.loads(text)
        except ValueError as e:
            raise SourceUnavailable("新浪分钟 K 返回的不是预期的格式") from e
        return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []

    return get_runtime().limiter.call(SOURCE, one)


def _rows(symbol: str, code: str, count: int) -> list[dict[str, Any]]:
    """这只股票最近 count 根原始行。刚查过（且那次要的不比这次少）就用那次的。"""
    from app.custom.tdx_formula.ql.data.runtime import current_config

    ttl = float(current_config().realtime_interval)
    with _lock:
        hit = _recent.get(symbol)
        if hit is not None and time.monotonic() - hit[0] < ttl and hit[1] >= count:
            return hit[2]
    rows = _download(code, count)
    with _lock:
        _recent[symbol] = (time.monotonic(), count, rows)
        if len(_recent) > 200:  # 只留最近的，不让它一直长
            for key in sorted(_recent, key=lambda k: _recent[k][0])[:100]:
                del _recent[key]
    return rows


def _frame(symbol: str, rows: list[dict[str, Any]], start: datetime | None, end: datetime) -> pl.DataFrame:
    out: dict[str, list[Any]] = {name: [] for name in SCHEMA}
    for r in rows:
        try:
            moment = datetime.strptime(str(r.get("day")), "%Y-%m-%d %H:%M:%S")
            o, h, low, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            volume = float(r["volume"]) / 100.0  # 股 → 手
        except (KeyError, TypeError, ValueError):
            continue
        if (start is not None and moment < start) or moment > end:
            continue
        try:
            amount: float | None = float(r["amount"])
        except (KeyError, TypeError, ValueError):
            amount = None  # 没有可靠的成交额就留空，不估算
        for name, value in zip(SCHEMA, (symbol, moment, o, h, low, c, volume, amount), strict=True):
            out[name].append(value)
    return pl.DataFrame(out, schema=SCHEMA)


def _beijing(moment: datetime) -> datetime:
    """带时区的时间换成北京时间的墙钟（不带时区）；本来就不带时区的当作北京时间。"""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(_BEIJING).replace(tzinfo=None)


def get_minute(symbols: list[str], start: datetime | None, end: datetime | None,
               to_code: Callable[[str], str | None],
               on_chunk_done: Callable[[int, int], Any] | None = None) -> pl.DataFrame:
    """逐只取 1 分钟线。symbols 超过上限时由调用方先拒绝；这里假定数量已经合规。"""
    from app.custom.tdx_formula.ql.core import clock

    start = _beijing(start) if start is not None else None
    end = _beijing(end) if end is not None else clock.now()
    count = bars_needed(start, end)
    frames: list[pl.DataFrame] = []
    for i, symbol in enumerate(symbols, 1):
        code = to_code(symbol)
        if code is not None:
            frame = _frame(symbol, _rows(symbol, code, count), start, end)
            if not frame.is_empty():
                frames.append(frame)
        if on_chunk_done is not None:
            on_chunk_done(i, len(symbols))
    return pl.concat(frames) if frames else pl.DataFrame(schema=SCHEMA)
