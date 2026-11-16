"""要联网取的三样东西：全市场两融、个股两融、个股主力资金流向（都来自东方财富），以及持仓股的估值（腾讯行情）。

约束：
- 每个请求都经过限流器（来源记在 akshare_em / tencent_quote 名下），不并发；
- 任何一步失败就停下，不重试、不换别的入口，缺的那块在材料里写“没有取到”；
- 同一个交易日取到的结果存在本机，当天再跑复盘不重复请求。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EM_SOURCE = "akshare_em"
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
FLOW = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
MAX_FLOW_STOCKS = 12  # 一次复盘最多为这么多只股票各发一个资金流向请求
FLOW_DAYS = 25

Fetch = Callable[[str, dict[str, str], float], tuple[int, str]]


def _http_get(url: str, params: dict[str, str], timeout: float) -> tuple[int, str]:
    import requests

    response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    return response.status_code, response.text


_fetch: Fetch = _http_get
_quotes: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None


def set_fetch(fetch: Fetch | None) -> None:
    """换掉真正发请求的函数（测试用）；传 None 恢复。"""
    global _fetch
    _fetch = fetch or _http_get


def set_quotes(fn: Callable[[list[str]], dict[str, dict[str, Any]]] | None) -> None:
    """换掉取腾讯行情的函数（测试用）；传 None 恢复。"""
    global _quotes
    _quotes = fn


class UnavailableError(Exception):
    """这一块没有取到。"""


def _call(url: str, params: dict[str, str]) -> Any:
    from app.custom.tdx_formula.ql.core.errors import QuantLabError
    from app.custom.tdx_formula.ql.data.runtime import current_config, get_runtime

    timeout = float(current_config().sources[EM_SOURCE].timeout_seconds)

    def one() -> str:
        from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable

        try:
            status, text = _fetch(url, params, timeout)
        except Exception as e:  # 网络层的任何失败都按“暂时不可用”
            raise SourceUnavailable(f"东方财富请求失败：{type(e).__name__}") from e
        if status in (403, 429):
            raise SourceRejected(f"东方财富限制了访问（HTTP {status}）")
        if status != 200:
            raise SourceUnavailable(f"东方财富返回了 HTTP {status}")
        return text

    try:
        text = get_runtime().limiter.call(EM_SOURCE, one)
        return json.loads(text)
    except QuantLabError as e:
        raise UnavailableError(e.message if hasattr(e, "message") else str(e)) from e
    except ValueError as e:
        raise UnavailableError("东方财富返回的内容认不出来") from e


def _rows(report: str, extra: dict[str, str]) -> list[dict[str, Any]]:
    data = _call(DATACENTER, {"reportName": report, "columns": "ALL", "pageNumber": "1", "source": "WEB",
                              "client": "WEB", "sortTypes": "-1", **extra})
    result = data.get("result") if isinstance(data, dict) else None
    rows = (result or {}).get("data") or []
    return [r for r in rows if isinstance(r, dict)]


def market_margin(count: int = 2) -> list[dict[str, Any]]:
    """全市场两融最近几天（新的在前）：日期、融资余额、融资买入额、融资净买入、融券余额、两融余额（元）。"""
    rows = _rows("RPTA_RZRQ_LSHJ", {"sortColumns": "DIM_DATE", "pageSize": str(count)})
    if not rows:
        raise UnavailableError("没有取到全市场两融数据")
    return [{"date": str(r.get("DIM_DATE") or "")[:10], "rzye": r.get("RZYE"), "rzmre": r.get("RZMRE"),
             "rzjme": r.get("RZJME"), "rqye": r.get("RQYE"), "rzrqye": r.get("RZRQYE")} for r in rows]


def stock_margin(code: str) -> list[dict[str, Any]]:
    """一只股票的两融最近两天（新的在前）。不是两融标的时返回空。"""
    rows = _rows("RPTA_WEB_RZRQ_GGMX", {"sortColumns": "DATE", "pageSize": "2", "filter": f'(SCODE="{code}")'})
    return [{"date": str(r.get("DATE") or "")[:10], "rzye": r.get("RZYE"), "rzmre": r.get("RZMRE"),
             "rzjme": r.get("RZJME"), "rzjme5d": r.get("RZJME5D"), "rzjme10d": r.get("RZJME10D"),
             "rqye": r.get("RQYE"), "rzyezb": r.get("RZYEZB")} for r in rows]


def stock_flow(symbol: str) -> list[dict[str, Any]]:
    """一只股票最近若干天的资金流向（旧的在前，元）：主力、超大单、大单、中单、小单的净流入。"""
    code, _, exchange = symbol.partition(".")
    market = "1" if exchange.upper() == "SH" else "0"
    data = _call(FLOW, {"secid": f"{market}.{code}", "lmt": str(FLOW_DAYS), "klt": "101", "fields1": "f1,f2,f3,f7",
                        "fields2": "f51,f52,f53,f54,f55,f56"})
    lines = ((data or {}).get("data") or {}).get("klines") or []
    out: list[dict[str, Any]] = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            main, small, medium, large, huge = (float(x) for x in parts[1:6])
        except ValueError:
            continue
        out.append({"date": parts[0], "main": main, "huge": huge, "large": large, "medium": medium, "small": small})
    if not out:
        raise UnavailableError("没有取到资金流向")
    return out


def valuations(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """持仓股和对标股的估值等附加项（市盈率、市净率、总市值、量比、振幅），一次请求取完。"""
    from app.custom.tdx_formula.ql.core.errors import QuantLabError

    if _quotes is not None:
        fetched = _quotes(symbols)
    else:
        from app.plugins.freecn import quotes

        try:
            fetched = quotes.fetch_symbols(symbols)
        except QuantLabError as e:
            raise UnavailableError(str(e)) from e
    return {s: v.get("extra") or {} for s, v in fetched.items()}


# ---------------- 当天的结果存在本机，重跑复盘不重复请求 ----------------


def load_cache(root: Path, day: str) -> dict[str, Any]:
    path = root / "cache" / f"{day}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(root: Path, day: str, data: dict[str, Any]) -> None:
    folder = root / "cache"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{day}.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(folder / f"{day}.json")
        for old in sorted(folder.glob("*.json"))[:-10]:  # 只留最近 10 天
            old.unlink(missing_ok=True)
    except OSError as e:
        logger.info("复盘补充数据没有存下来：%s", e)


def collect(root: Path, day: str, holdings: list[str], others: list[str]) -> dict[str, Any]:
    """把要联网的几块取齐。已经存过的不再请求；某一块失败就记下原因，后面依赖东方财富的也不再继续发。

    返回 {"market_margin": [...], "stock_margin": {symbol: [...]}, "flow": {symbol: [...]},
          "valuation": {symbol: {...}}, "errors": {块名: 原因}}。
    """
    cache = load_cache(root, day)
    out: dict[str, Any] = {
        "market_margin": cache.get("market_margin"), "stock_margin": dict(cache.get("stock_margin") or {}),
        "flow": dict(cache.get("flow") or {}), "valuation": dict(cache.get("valuation") or {}), "errors": {},
    }
    em_ok = True

    def em(block: str, fn: Callable[[], Any]) -> Any:
        nonlocal em_ok
        if not em_ok:
            return None
        try:
            return fn()
        except UnavailableError as e:
            em_ok = False  # 东方财富有一个请求没成，就不再继续发
            out["errors"][block] = str(e)
            return None

    if not out["market_margin"]:
        out["market_margin"] = em("全市场两融", market_margin)
    for symbol in holdings:
        if symbol not in out["stock_margin"]:
            rows = em("个股两融", lambda s=symbol: stock_margin(s.split(".")[0]))
            if rows is not None:
                out["stock_margin"][symbol] = rows
    for symbol in holdings[:MAX_FLOW_STOCKS]:
        if symbol not in out["flow"]:
            rows = em("主力资金流向", lambda s=symbol: stock_flow(s))
            if rows is not None:
                out["flow"][symbol] = rows
    wanted = [s for s in dict.fromkeys([*holdings, *others]) if s not in out["valuation"]]
    if wanted:
        try:
            out["valuation"].update(valuations(wanted))
        except UnavailableError as e:
            out["errors"]["估值"] = str(e)
    save_cache(root, day, {k: out[k] for k in ("market_margin", "stock_margin", "flow", "valuation")})
    return out
