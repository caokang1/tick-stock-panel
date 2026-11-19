"""收集“每日汇总”要用的数据：市场快照、持仓、当天的监控信号、定时任务情况、当天的复盘。

只读本机已有的数据，不联网（港股报价用的是港股报价扩展存在本机的最近一次结果）。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _f(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _index_pct(value: Any) -> float | None:
    """面板里指数的涨跌幅是百分数（-0.28 就是 -0.28%），个股的是小数；这里统一成小数。"""
    v = _f(value)
    return None if v is None else v / 100


def market_of(overview: dict[str, Any]) -> dict[str, Any]:
    limit, emotion = overview.get("limit") or {}, overview.get("emotion") or {}
    return {
        "indices": [{"symbol": x.get("symbol"), "name": x.get("name"), "last": _f(x.get("last_price")),
                     "pct": _index_pct(x.get("change_pct"))} for x in overview.get("indices") or []],
        "breadth": overview.get("breadth") or {},
        "amount_total": _f((overview.get("amount") or {}).get("total")),
        "limit_up": limit.get("limit_up", 0), "limit_down": limit.get("limit_down", 0),
        "max_boards": limit.get("max_boards", 0),
        "emotion_score": _f(emotion.get("score")), "emotion_label": emotion.get("label"),
        "cap": overview.get("market_cap_leaders") or [],
        "industry": overview.get("industry_amount_leaders") or [],
        "turnover": overview.get("turnover_leaders") or [],
        "active": overview.get("active_leaders") or [],
    }


def holdings_of(lots: list[dict[str, Any]], quotes: dict[str, dict[str, Any]], hk: dict[str, Any]) -> dict[str, Any]:
    """lots：合并好的持仓 [{symbol, qty, cost}]；quotes：A 股和 ETF 的 {代码: {name, price, change_pct}}；
    hk：港股报价扩展存的 {"items": {代码: {name, last, change_pct}}, "rate": 港元兑人民币}。
    市值和盈亏都折成人民币；算不出来的留空，不计入合计。
    """
    rate = _f(hk.get("rate"))
    rows, total_value, total_pnl, missing = [], 0.0, 0.0, []
    for lot in lots:
        symbol, qty, cost = lot["symbol"], _f(lot.get("qty")) or 0.0, _f(lot.get("cost")) or 0.0
        is_hk = symbol.endswith(".HK")
        if is_hk:
            q = (hk.get("items") or {}).get(symbol) or {}
            name, price, change, fx = q.get("name"), _f(q.get("last")), _f(q.get("change_pct")), rate
        else:
            q = quotes.get(symbol) or {}
            name, price, change, fx = q.get("name"), _f(q.get("price")), _f(q.get("change_pct")), 1.0
        row: dict[str, Any] = {"symbol": symbol, "name": name, "qty": qty, "cost": cost or None, "price": price,
                               "change_pct": change, "currency": "HKD" if is_hk else "CNY",
                               "value": None, "pnl": None, "pnl_pct": None}
        if price is not None and fx:
            row["value"] = qty * price * fx
            total_value += row["value"]
            if cost != 0:  # 成本可以是负数（落袋的盈利超过了本金）：只给金额，不给百分比
                row["pnl"] = qty * (price - cost) * fx
                total_pnl += row["pnl"]
                if cost > 0:
                    row["pnl_pct"] = price / cost - 1
        else:
            missing.append(symbol)
        rows.append(row)
    note = ""
    if rate and any(r["currency"] == "HKD" for r in rows):
        note = f"港股按 1 港元 = {rate:.4f} 人民币折算。"
    if missing:
        note += f"没有取到价格、未计入合计：{'、'.join(missing)}。"
    return {"rows": rows, "total_value": total_value, "total_pnl": total_pnl, "note": note}


def alerts_of(events: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    out = []
    for e in events:
        try:
            at = datetime.fromtimestamp(int(e.get("ts")) / 1000)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if at.date() != day:
            continue
        out.append({"time": at.strftime("%H:%M"), "symbol": e.get("symbol"), "name": e.get("name"),
                    "rule_name": e.get("rule_name"), "message": e.get("message"), "price": _f(e.get("price"))})
    return sorted(out, key=lambda a: a["time"])


def recap_of(reports: list[dict[str, Any]], day: date) -> dict[str, Any] | None:
    """这一天最后生成的那份复盘。"""
    same = [r for r in reports if str(r.get("as_of") or "")[:10] == day.isoformat() and r.get("content")]
    if not same:
        return None
    best = max(same, key=lambda r: str(r.get("created_at") or ""))
    return {"content": best["content"], "summary": best.get("summary"),
            "created_at": str(best.get("created_at") or "").replace("T", " ")[:16]}


def _local(stamp: Any) -> str:
    """任务记录里的时间是世界时（结尾带 Z），换成本机时间。"""
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return ""


def tasks_of(day: date, panel_latest: date | None, last_job: dict[str, Any] | None,
             recap: dict[str, Any] | None) -> list[dict[str, Any]]:
    data_ok = panel_latest == day
    tasks = [{"name": "收盘行情", "ok": data_ok,
              "text": f"已更新到 {panel_latest}" if panel_latest else "面板里还没有行情"}]
    if last_job:
        status = str(last_job.get("status") or "")
        finished = _local(last_job.get("finished_at"))
        tasks.append({"name": "盘后同步", "ok": status == "succeeded",
                      "text": f"最近一次：{'成功' if status == 'succeeded' else status or '未知'}"
                              + (f"（{finished}）" if finished else "")
                              + (f"，{last_job.get('error')}" if last_job.get("error") else "")})
    tasks.append({"name": "每日复盘", "ok": recap is not None,
                  "text": f"已生成（{recap['created_at']}）" if recap else "今天的复盘没有生成"})
    return tasks


# ---------------- 从正在运行的面板里取数 ----------------


def _quotes(repo: Any, day: date, symbols: set[str]) -> dict[str, dict[str, Any]]:
    """A 股和 ETF 这一天的收盘价、涨跌幅、名称。"""
    from app.services.screener import ScreenerService

    out: dict[str, dict[str, Any]] = {}
    for asset in ("stock", "etf"):
        try:
            service = ScreenerService(repo, asset)
            df = service._load_enriched_for_date(day)
            if df.is_empty() and (last := service.latest_date()) is not None:
                df = service._load_enriched_for_date(last)  # 这一天这类品种还没有行情（例如 ETF）：用手头最新的
            if df.is_empty() or "symbol" not in df.columns:
                continue
            price_col = "raw_close" if "raw_close" in df.columns else "close"
            cols = [c for c in ("symbol", "name", price_col, "change_pct") if c in df.columns]
            for r in df.filter(df["symbol"].is_in(list(symbols))).select(cols).to_dicts():
                out[str(r["symbol"])] = {"name": r.get("name"), "price": _f(r.get(price_col)),
                                         "change_pct": _f(r.get("change_pct"))}
        except Exception:
            logger.exception("每日汇总：读 %s 的收盘价失败", asset)
    return out


def collect(app_state: Any, day: date, now: datetime) -> dict[str, Any]:
    from app.custom.hk_quotes.routes import read_saved
    from app.custom.recap_plus.context import read_holdings
    from app.services import alert_store, market_recap_reports
    from app.services.market_overview_builder import build_market_overview
    from app.services.pipeline_jobs import job_store

    repo = app_state.repo
    data_dir = Path(repo.store.data_dir)
    overview = build_market_overview(
        repo=repo, quote_service=getattr(app_state, "quote_service", None),
        depth_service=getattr(app_state, "depth_service", None), as_of=day,
    )
    lots = read_holdings(data_dir)
    quotes = _quotes(repo, day, {h["symbol"] for h in lots})
    recap = recap_of(market_recap_reports.list_reports(), day)
    jobs = job_store.list_recent(1)
    return {
        "day": day.isoformat(),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "market": market_of(overview),
        "holdings": holdings_of(lots, quotes, read_saved(data_dir / "user_data" / "extensions" / "hk_quotes" / "quotes.json")),
        "alerts": alerts_of(alert_store.list_recent(data_dir, days=2), day),
        "recap": recap,
        "tasks": tasks_of(day, repo.latest_enriched_date(), jobs[0] if jobs else None, recap),
    }
