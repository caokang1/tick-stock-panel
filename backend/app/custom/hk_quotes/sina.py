"""新浪财经的港股报价和港元汇率：拼请求和解析。发请求用期货扩展里那个经过限流器的函数。

一次请求把要的港股和汇率一起问回来，一行一个：``var hq_str_<代码>="逗号分隔的字段";``

港股（rt_hk00981）   1 中文名 2 开 3 昨收 4 高 5 低 6 最新 17 日期 18 时间
汇率（fx_shkdcny）   0 时间 1 最新（1 港元 = 多少人民币） 17 日期
"""

from __future__ import annotations

import re
from typing import Any

from app.custom.futures import sina as base

SYMBOL = re.compile(r"^\d{5}\.HK$")
RATE_CODE = "fx_shkdcny"
_LINE = re.compile(r'var hq_str_(\w+)="([^"]*)"')


def query_of(symbol: str) -> str:
    """00981.HK → rt_hk00981"""
    return "rt_hk" + symbol.split(".")[0]


def _num(fields: list[str], i: int) -> float | None:
    try:
        value = float(fields[i])
    except (IndexError, ValueError):
        return None
    return value if value == value else None


def _text(fields: list[str], i: int) -> str:
    return fields[i].strip() if i < len(fields) else ""


def parse(text: str, symbols: list[str]) -> dict[str, Any]:
    """返回 {"items": {代码: 报价}, "rate": 汇率或 None}。取不到的股票直接略过。"""
    lines = {m.group(1): m.group(2).split(",") for m in _LINE.finditer(text)}
    items: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        f = lines.get(query_of(symbol))
        last = _num(f, 6) if f else None
        if not last or last <= 0:
            continue
        prev = _num(f, 3)
        items[symbol] = {
            "symbol": symbol, "name": _text(f, 1), "last": last, "prev": prev or None,
            "change_pct": last / prev - 1 if prev else None,
            "time": f"{_text(f, 17).replace('/', '-')} {_text(f, 18)}".strip(),
        }
    rate = _num(lines.get(RATE_CODE, []), 1)
    return {"items": items, "rate": rate if rate and 0 < rate < 10 else None}


def fetch(symbols: list[str]) -> dict[str, Any]:
    codes = [query_of(s) for s in symbols] + [RATE_CODE]
    return parse(base._call(base.QUOTE_URL + ",".join(codes), "新浪港股报价"), symbols)
