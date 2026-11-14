"""财务数据：东方财富数据中心的业绩报表和三大报表，按报告期一次取回全市场。

- 每个报告期、每张报表约 11 页（一页 500 家），每一页都是一次请求，逐个经扩展的限流器发出
  （和日线下载里的“东方财富”共用一套间隔和计数）。
- 取回来的原始数据按“报表/报告期”存在本机（data/freecn/financials）。已经过了披露期的报告期
  不会再取第二次；还在披露期内的（报告期结束后 150 天以内）每 20 小时最多重取一次。
- 比率类（ROE、毛利率、同比、资产负债率）来源给的就是百分数，TSP 要的也是百分数，不换算。
- 来源没有的科目不填，不拿别的数凑。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

SOURCE = "akshare_em"
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PAGE_SIZE = 500
MAX_PAGES = 40  # 一张报表正常 11~12 页；超过这个数说明接口不对劲，停下
HISTORY_PERIODS = 8  # 不是“只要最新”时取最近几个报告期（两年）
LATEST_PERIODS = 3  # “只要最新”时取最近几个报告期：年报和一季报同在 4 月披露，取 3 个才不会漏
OPEN_DAYS = 150  # 报告期结束后这么多天内算“还在披露期”，数据可能还会变
REFRESH_HOURS = 20

_AB_SHARES = '(SECURITY_TYPE_CODE in ("058001001","058001008"))(TRADE_MARKET_CODE!="069001017")'
# 报表名 → (接口里的报表名, 报告期的字段名, 排序)
_REPORTS = {
    "summary": ("RPT_LICO_FN_CPD", "REPORTDATE", "UPDATE_DATE,SECURITY_CODE", ""),
    "balance": ("RPT_DMSK_FN_BALANCE", "REPORT_DATE", "NOTICE_DATE,SECURITY_CODE", _AB_SHARES),
    "income": ("RPT_DMSK_FN_INCOME", "REPORT_DATE", "NOTICE_DATE,SECURITY_CODE", _AB_SHARES),
    "cashflow": ("RPT_DMSK_FN_CASHFLOW", "REPORT_DATE", "NOTICE_DATE,SECURITY_CODE", _AB_SHARES),
}
# 来源的字段 → TSP 的列名
_INCOME = {
    "TOTAL_OPERATE_INCOME": "revenue", "OPERATE_COST": "operating_cost", "SALE_EXPENSE": "selling_expense",
    "MANAGE_EXPENSE": "admin_expense", "FINANCE_EXPENSE": "financial_expense",
    "OPERATE_PROFIT": "operating_profit", "TOTAL_PROFIT": "total_profit", "INCOME_TAX": "income_tax",
    "PARENT_NETPROFIT": "net_income_attributable",
}
_BALANCE = {
    "TOTAL_ASSETS": "total_assets", "MONETARYFUNDS": "cash_and_equivalents", "ACCOUNTS_RECE": "accounts_receivable",
    "TOTAL_LIABILITIES": "total_liabilities", "TOTAL_EQUITY": "total_equity",
    "INVENTORY": "inventory", "FIXED_ASSET": "fixed_assets", "ACCOUNTS_PAYABLE": "accounts_payable",
}
_CASHFLOW = {
    "NETCASH_OPERATE": "net_operating_cash_flow", "NETCASH_INVEST": "net_investing_cash_flow",
    "NETCASH_FINANCE": "net_financing_cash_flow", "CONSTRUCT_LONG_ASSET": "capex", "CCE_ADD": "net_cash_change",
}
_SUMMARY = {
    "BASIC_EPS": "eps_basic", "BPS": "bps", "WEIGHTAVG_ROE": "roe", "XSMLL": "gross_margin",
    "YSTZ": "revenue_yoy", "SJLTZ": "net_income_yoy",
}
_TABLES = {"income": ("income", _INCOME), "balance_sheet": ("balance", _BALANCE),
           "cash_flow": ("cashflow", _CASHFLOW)}

Fetch = Callable[[dict[str, str], float], tuple[int, str]]


def _http_get(params: dict[str, str], timeout: float) -> tuple[int, str]:
    import requests

    response = requests.get(URL, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    return response.status_code, response.text


_fetch: Fetch = _http_get


def set_fetch(fetch: Fetch | None) -> None:
    """换掉真正发请求的函数（测试用）；传 None 恢复。"""
    global _fetch
    _fetch = fetch or _http_get


def recent_periods(today: date, count: int) -> list[date]:
    """今天之前（含今天）最近的几个季度末，从新到旧。"""
    ends = []
    year = today.year
    while len(ends) < count:
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            end = date(year, month, day)
            if end <= today and len(ends) < count:
                ends.append(end)
        year -= 1
    return ends


def _cache_file(root: Path, report: str, period: date) -> Path:
    return root / "financials" / report / f"{period.isoformat()}.parquet"


def _cache_usable(path: Path, period: date, today: date) -> bool:
    if not path.exists():
        return False
    if today > period + timedelta(days=OPEN_DAYS):
        return True  # 披露期早过了，数据不会再变
    return time.time() - path.stat().st_mtime < REFRESH_HOURS * 3600


def _download(report: str, period: date) -> list[dict[str, Any]]:
    """把一张报表一个报告期的全市场数据逐页取回来。来源限制访问或不可用时向上抛扩展的数据错误。"""
    from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
    from app.custom.tdx_formula.ql.data.runtime import current_config, get_runtime

    name, period_field, sort, extra = _REPORTS[report]
    limiter = get_runtime().limiter
    timeout = float(current_config().sources[SOURCE].timeout_seconds)
    rows: list[dict[str, Any]] = []
    page, pages = 1, 1
    while page <= pages:
        if page > MAX_PAGES:
            raise SourceUnavailable(f"东方财富的{report}报表页数异常（超过 {MAX_PAGES} 页），已停止")
        params = {
            "sortColumns": sort, "sortTypes": "-1,-1", "pageSize": str(PAGE_SIZE), "pageNumber": str(page),
            "reportName": name, "columns": "ALL", "filter": f"{extra}({period_field}='{period.isoformat()}')",
        }

        def one(params: dict[str, str] = params) -> dict[str, Any]:
            try:
                status, text = _fetch(params, timeout)
            except Exception as e:
                raise SourceUnavailable(f"东方财富财务数据请求失败：{type(e).__name__}: {e}") from e
            if status in (403, 429):
                raise SourceRejected(f"东方财富限制了访问（HTTP {status}）")
            if status != 200:
                raise SourceUnavailable(f"东方财富财务数据返回了 HTTP {status}")
            try:
                body = json.loads(text)
            except ValueError as e:
                raise SourceUnavailable("东方财富财务数据返回的不是预期的格式") from e
            return body if isinstance(body, dict) else {}

        body = limiter.call(SOURCE, one)
        result = body.get("result")
        if not isinstance(result, dict):  # 这个报告期还没有任何公司披露时，来源返回 result=null
            break
        data = result.get("data") or []
        if not data:
            break
        rows.extend(r for r in data if isinstance(r, dict))
        pages = int(result.get("pages") or 1)
        page += 1
    return rows


def load_report(root: Path, report: str, period: date, today: date) -> pl.DataFrame:
    """一张报表一个报告期的全市场原始数据：本机有且还能用就读本机的，否则取回来存好。"""
    path = _cache_file(root, report, period)
    if _cache_usable(path, period, today):
        return pl.read_parquet(path)
    rows = _download(report, period)
    frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if frame.is_empty():
        return frame  # 空结果不存：可能只是还没人披露，下次再问
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    frame.write_parquet(tmp)
    tmp.replace(path)
    return frame


def _day(column: str) -> pl.Expr:
    """来源的日期是 '2026-09-30 00:00:00' 这样的字，只留日期部分。"""
    return pl.col(column).cast(pl.String).str.slice(0, 10)


def _pick(frame: pl.DataFrame, period_field: str, mapping: dict[str, str]) -> pl.DataFrame:
    if frame.is_empty() or "SECUCODE" not in frame.columns:
        return pl.DataFrame()
    cols = [pl.col("SECUCODE").alias("symbol"), _day(period_field).alias("period_end")]
    cols.append(_day("NOTICE_DATE").alias("announce_date") if "NOTICE_DATE" in frame.columns
                else pl.lit(None, dtype=pl.String).alias("announce_date"))
    cols += [pl.col(src).cast(pl.Float64, strict=False).alias(dst) for src, dst in mapping.items()
             if src in frame.columns]
    return frame.select(cols).unique(subset=["symbol", "period_end"], keep="first")


def get_financials(root: Path, table: str, symbols: list[str], latest_only: bool, today: date) -> pl.DataFrame:
    """TSP 要的一张财务表。``shares``（历史股本）这里没有，返回空。"""
    if table == "shares":
        return pl.DataFrame()
    if table not in _TABLES and table != "metrics":
        return pl.DataFrame()
    periods = recent_periods(today, LATEST_PERIODS if latest_only else HISTORY_PERIODS)
    frames: list[pl.DataFrame] = []
    for period in periods:
        if table == "metrics":
            frame = _metrics(root, period, today)
        else:
            report, mapping = _TABLES[table]
            frame = _pick(load_report(root, report, period, today), "REPORT_DATE", mapping)
            if table == "income" and {"total_profit", "income_tax"} <= set(frame.columns):
                # 净利润 = 利润总额 - 所得税（会计恒等式）；两项都有才算
                frame = frame.with_columns((pl.col("total_profit") - pl.col("income_tax")).alias("net_income"))
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        return pl.DataFrame()
    out = pl.concat(frames, how="diagonal_relaxed")
    return out.filter(pl.col("symbol").is_in(symbols)).sort(["symbol", "period_end"])


def _metrics(root: Path, period: date, today: date) -> pl.DataFrame:
    """指标表：业绩报表里的每股收益、每股净资产、ROE、毛利率、两个同比，加上资产负债率和净利率。"""
    base = _pick(load_report(root, "summary", period, today), "REPORTDATE", _SUMMARY)
    if base.is_empty():
        return base
    balance = _pick(load_report(root, "balance", period, today), "REPORT_DATE",
                    {"DEBT_ASSET_RATIO": "debt_to_asset_ratio"})
    if not balance.is_empty() and "debt_to_asset_ratio" in balance.columns:
        base = base.join(balance.select("symbol", "period_end", "debt_to_asset_ratio"),
                         on=["symbol", "period_end"], how="left")
    income = _pick(load_report(root, "income", period, today), "REPORT_DATE",
                   {"TOTAL_OPERATE_INCOME": "revenue", "TOTAL_PROFIT": "total_profit", "INCOME_TAX": "income_tax"})
    if not income.is_empty() and {"revenue", "total_profit", "income_tax"} <= set(income.columns):
        # 净利率（%）= (利润总额 - 所得税) / 营业总收入 × 100；收入不是正数时不算
        margin = income.select(
            "symbol", "period_end",
            pl.when(pl.col("revenue") > 0)
            .then((pl.col("total_profit") - pl.col("income_tax")) / pl.col("revenue") * 100.0)
            .otherwise(None).alias("net_margin"),
        )
        base = base.join(margin, on=["symbol", "period_end"], how="left")
    return base
