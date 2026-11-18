"""新浪财经的期货行情和日线：发请求（经限流器）和解析。

行情接口一次把所有品种和四个现货指数一起问回来，一行一个：``var hq_str_<代码>="逗号分隔的字段";``

商品期货（nf_AU0）  0 名称 1 时间 2 开 3 高 4 低 8 最新 10 昨结算 13 持仓量 14 成交量 17 日期
中金所（nf_IF0）    0 开 1 高 2 低 3 最新 4 成交量 6 持仓量 14 昨结算 36 日期 37 时间
外盘（hf_GC）       0 最新 4 高 5 低 6 时间 7 昨结算 8 开 12 日期
现货指数（sh000300） 1 开 2 昨收 3 最新 4 高 5 低
国内期货的涨跌按昨结算算（交易所和行情软件的口径）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .catalog import ITEMS, Item

SOURCE = "akshare_sina"  # 和分钟 K 共用新浪这一路的限流计数
QUOTE_URL = "https://hq.sinajs.cn/list="
KLINE_URL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/"
_LINE = re.compile(r'var hq_str_(\w+)="([^"]*)"')

Fetch = Callable[[str, float], tuple[int, str]]


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    import requests

    response = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    response.encoding = "gbk"
    return response.status_code, response.text


_fetch: Fetch = _http_get


def set_fetch(fetch: Fetch | None) -> None:
    """换掉真正发请求的函数（测试用）；传 None 恢复。"""
    global _fetch
    _fetch = fetch or _http_get


def _call(url: str, what: str) -> str:
    """经限流器发一个请求。被限制或失败时向上抛扩展的数据错误。"""
    from app.custom.tdx_formula.ql.core import config as ql_config
    from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
    from app.custom.tdx_formula.ql.data.runtime import current_config, get_runtime

    if not ql_config.is_configured():
        from app.config import settings

        ql_config.set_data_dir(settings.data_dir / "freecn")
    timeout = float(current_config().sources[SOURCE].timeout_seconds)

    def one() -> str:
        try:
            status, text = _fetch(url, timeout)
        except Exception as e:  # 网络层的任何失败都按“暂时不可用”
            raise SourceUnavailable(f"{what}请求失败：{type(e).__name__}: {e}") from e
        if status in (403, 429, 456):
            raise SourceRejected(f"{what}限制了访问（HTTP {status}）")
        if status != 200:
            raise SourceUnavailable(f"{what}返回了 HTTP {status}")
        return text

    return get_runtime().limiter.call(SOURCE, one)


def _num(fields: list[str], i: int) -> float | None:
    try:
        value = float(fields[i])
    except (IndexError, ValueError):
        return None
    return value if value == value else None


def _text(fields: list[str], i: int) -> str:
    return fields[i].strip() if i < len(fields) else ""


def _clock(text: str) -> str:
    """110608 → 11:06:08；已经带冒号的原样返回。"""
    return f"{text[:2]}:{text[2:4]}:{text[4:6]}" if len(text) == 6 and text.isdigit() else text


def _row(item: Item, f: list[str]) -> dict[str, Any] | None:
    if item.kind == "com":
        open_, high, low, last, base = (_num(f, i) for i in (2, 3, 4, 8, 10))
        oi, volume, day, clock = _num(f, 13), _num(f, 14), _text(f, 17), _clock(_text(f, 1))
    elif item.kind == "fin":
        open_, high, low, last, base = (_num(f, i) for i in (0, 1, 2, 3, 14))
        oi, volume, day, clock = _num(f, 6), _num(f, 4), _text(f, 36), _text(f, 37)
    else:
        last, high, low, base, open_ = (_num(f, i) for i in (0, 4, 5, 7, 8))
        oi, volume, day, clock = None, None, _text(f, 12), _text(f, 6)
    if not last:  # 没开盘或接口没给：这一行不要
        return None
    return {
        "code": item.code, "name": item.name, "group": item.group,
        "last": last, "open": open_ or None, "high": high or None, "low": low or None,
        "prev": base or None,
        "change": last - base if base else None,
        "change_pct": last / base - 1 if base else None,
        "volume": volume, "open_interest": oi,
        "time": f"{day} {clock}".strip(),
    }


def parse_quotes(text: str) -> list[dict[str, Any]]:
    """把行情接口的返回变成一行一个品种；取不到的品种直接略过。股指期货带上现货和升贴水。"""
    lines = {m.group(1): m.group(2).split(",") for m in _LINE.finditer(text)}
    rows = []
    for item in ITEMS:
        fields = lines.get(item.query)
        row = _row(item, fields) if fields else None
        if row is None:
            continue
        spot = _num(lines.get(item.spot, []), 3) if item.spot else None
        row["spot"] = spot or None
        row["basis"] = row["last"] - spot if spot else None
        row["basis_pct"] = row["last"] / spot - 1 if spot else None
        rows.append(row)
    return rows


def fetch_quotes() -> list[dict[str, Any]]:
    codes = [item.query for item in ITEMS] + [item.spot for item in ITEMS if item.spot]
    return parse_quotes(_call(QUOTE_URL + ",".join(codes), "新浪期货行情"))


def parse_kline(text: str, limit: int) -> list[dict[str, Any]]:
    """日线接口的返回 → 旧的在前的 K 线。国内和外盘的字段名不一样，两种都认。"""
    start, end = text.find("(["), text.rfind("])")
    if start < 0 or end < 0:
        return []
    try:
        raw = json.loads(text[start + 1:end + 1])
    except ValueError:
        return []
    bars = []
    for r in raw[-limit:]:
        try:
            bar = {
                "date": str(r.get("d") or r.get("date"))[:10],
                "open": float(r.get("o") or r.get("open")), "high": float(r.get("h") or r.get("high")),
                "low": float(r.get("l") or r.get("low")), "close": float(r.get("c") or r.get("close")),
            }
        except (TypeError, ValueError):
            continue
        if bar["close"] > 0:
            bars.append(bar)
    return bars


def fetch_kline(item: Item, limit: int) -> list[dict[str, Any]]:
    if item.kind == "hf":
        path = f"GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={item.code}"
    else:
        path = f"InnerFuturesNewService.getDailyKLine?symbol={item.code}"
    return parse_kline(_call(KLINE_URL + path, "新浪期货日线"), limit)
