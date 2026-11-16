"""腾讯的实时行情接口：盘中快照和买卖五档。

一次请求最多查 ``BATCH`` 只；每个请求都经过扩展的限流器（排队一个一个发、有间隔、被拒绝后自动暂停），
和日线下载用的是同一套计数。全市场快照另有一道“两轮之间至少隔多久”的闸（默认 60 秒）：
TSP 问得再勤，这段时间内也只给上一轮的结果，不会多发请求。

接口返回的一行（``~`` 分隔）里用到的位置：
1 名称  2 代码  3 最新价  4 昨收  5 今开  6 成交量（手）  9-18 买一到买五的价和量（手）
19-28 卖一到卖五的价和量（手）  30 时间 YYYYMMDDHHMMSS  31 涨跌额  32 涨跌幅（百分数）
33 最高  34 最低  35 价/量/额（额的单位是元）  37 成交额（万元）  38 换手率（百分数）
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

SOURCE = "tencent_quote"
URL = "https://qt.gtimg.cn/q="
BATCH = 800
_MIN_FIELDS = 39
_BEIJING = timezone(timedelta(hours=8))
_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

Fetch = Callable[[str, float], tuple[int, str]]


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    import requests

    response = requests.get(url, timeout=timeout, headers={"Referer": "https://gu.qq.com/"})
    response.encoding = "gbk"
    return response.status_code, response.text


_fetch: Fetch = _http_get


def set_fetch(fetch: Fetch | None) -> None:
    """换掉真正发请求的函数（测试用）；传 None 恢复。"""
    global _fetch
    _fetch = fetch or _http_get


def to_query_code(symbol: str) -> str | None:
    """600519.SH → sh600519；写法认不出来时返回 None。"""
    digits, _, exchange = symbol.partition(".")
    prefix = _PREFIX.get(exchange.upper())
    if prefix is None or len(digits) != 6 or not digits.isdigit():
        return None
    return prefix + digits


def _num(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value == value else None  # 排除 NaN


def _timestamp_ms(text: str, fallback_ms: int) -> int:
    try:
        moment = datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=_BEIJING)
    except ValueError:
        return fallback_ms
    return int(moment.timestamp() * 1000)


def parse(text: str, fallback_ms: int) -> dict[str, dict[str, Any]]:
    """接口返回的文本 → {symbol: {"quote": 行情, "depth": 五档或 None}}。认不出来的行跳过。"""
    out: dict[str, dict[str, Any]] = {}
    for line in text.split(";"):
        head, _, rest = line.strip().partition("=")
        if not head.startswith("v_") or len(head) != 10:
            continue
        exchange, digits = head[2:4].upper(), head[4:]
        if exchange not in _PREFIX or not digits.isdigit():
            continue
        f = rest.strip().strip('"').split("~")
        if len(f) < _MIN_FIELDS:
            continue
        last, prev = _num(f[3]), _num(f[4])
        if last is None or last <= 0 or prev is None or prev <= 0:
            continue  # 没有成交价（未上市、长期停牌等），不拿 0 充数
        symbol = f"{digits}.{exchange}"
        ts = _timestamp_ms(f[30], fallback_ms)
        amount: float | None = None
        parts = f[35].split("/")
        if len(parts) == 3:
            amount = _num(parts[2])
        if amount is None:
            wan = _num(f[37])
            amount = wan * 10_000.0 if wan is not None else None
        pct, turnover = _num(f[32]), _num(f[38])
        open_, high, low = _num(f[5]), _num(f[33]), _num(f[34])
        volume = _num(f[6])
        if volume is not None and exchange == "SH" and digits.startswith(("688", "689")):
            volume = volume / 100.0  # 科创板这一项接口给的是“股”，其余股票是“手”；统一成手（盘口的量本来就是手）
        quote = {
            "symbol": symbol, "name": f[1] or None, "last_price": last, "prev_close": prev,
            # 当天还没有成交时接口给 0；这种情况留空，不把 0 当成价格
            "open": open_ if open_ else None, "high": high if high else None, "low": low if low else None,
            "volume": volume, "amount": amount,
            "change_pct": pct / 100.0 if pct is not None else (last - prev) / prev,
            "change_amount": _num(f[31]) if _num(f[31]) is not None else last - prev,
            "amplitude": None,
            "turnover_rate": turnover / 100.0 if turnover is not None else None,
            "timestamp": ts, "session": None,
        }
        bid_p = [_num(f[i]) for i in (9, 11, 13, 15, 17)]
        bid_v = [_num(f[i]) for i in (10, 12, 14, 16, 18)]
        ask_p = [_num(f[i]) for i in (19, 21, 23, 25, 27)]
        ask_v = [_num(f[i]) for i in (20, 22, 24, 26, 28)]
        depth: dict[str, Any] | None = None
        if any(bid_p) or any(ask_p):  # 指数没有盘口，全是 0
            depth = {
                "bid_prices": [p or 0.0 for p in bid_p], "bid_volumes": [int(v or 0) for v in bid_v],
                "ask_prices": [p or 0.0 for p in ask_p], "ask_volumes": [int(v or 0) for v in ask_v],
                "timestamp": ts,
            }
        out[symbol] = {"quote": quote, "depth": depth}
    return out


def _limiter() -> Any:
    from app.custom.tdx_formula.ql.data.runtime import get_runtime

    return get_runtime().limiter


def _timeout() -> float:
    from app.custom.tdx_formula.ql.data.runtime import current_config

    return float(current_config().sources[SOURCE].timeout_seconds)


def fetch_symbols(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """按代码取行情和五档。分批、逐个经限流器发出；来源不可用或被限制时向上抛扩展的数据错误。"""
    from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable

    codes = [c for c in (to_query_code(s) for s in symbols) if c]
    out: dict[str, dict[str, Any]] = {}
    limiter, timeout = _limiter(), _timeout()
    for i in range(0, len(codes), BATCH):
        url = URL + ",".join(codes[i:i + BATCH])

        def one(url: str = url) -> str:
            try:
                status, text = _fetch(url, timeout)
            except Exception as e:  # 网络层的任何失败都按“暂时不可用”
                raise SourceUnavailable(f"腾讯实时行情请求失败：{type(e).__name__}: {e}") from e
            if status in (403, 429):
                raise SourceRejected(f"腾讯实时行情限制了访问（HTTP {status}）")
            if status != 200:
                raise SourceUnavailable(f"腾讯实时行情返回了 HTTP {status}")
            return text

        text = limiter.call(SOURCE, one)
        parsed = parse(text, int(time.time() * 1000))
        if not parsed and len(text) > 200:
            logger.warning("腾讯实时行情返回了内容但一行都认不出来，接口的格式可能变了")
        out.update(parsed)
    return out


class SnapshotCache:
    """全市场快照。两轮之间至少隔配置的秒数；间隔内重复来问只给上一轮的结果。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._at: float | None = None  # 上一轮开始取的时刻（单调时钟）
        self._ok = False

    @staticmethod
    def interval() -> float:
        from app.custom.tdx_formula.ql.data.runtime import current_config

        return float(current_config().realtime_interval)

    def fresh(self) -> bool:
        return self._at is not None and time.monotonic() - self._at < self.interval()

    def rows(self, symbols: Callable[[], list[str]]) -> dict[str, dict[str, Any]]:
        """到了间隔就重取一轮，否则给上一轮的。取失败时也要等满间隔才再试，不连着重试。"""
        with self._lock:
            if self.fresh():
                return self._rows if self._ok else {}
            self._at = time.monotonic()
            try:
                self._rows = fetch_symbols(symbols())
                self._ok = True
            except Exception:
                self._ok = False
                raise
            return self._rows

    def peek(self) -> dict[str, dict[str, Any]]:
        """上一轮的结果还在间隔内时给出来，否则给空。不发请求。"""
        return self._rows if self._ok and self.fresh() else {}

    def clear(self) -> None:
        with self._lock:
            self._rows, self._at, self._ok = {}, None, False
