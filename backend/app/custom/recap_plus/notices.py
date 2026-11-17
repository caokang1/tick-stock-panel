"""持仓股票的上市公司公告（交易所披露的正式公告，经东方财富的公告列表取得）。

一次请求取全部持仓最近的公告，结果存在本机；半小时内再看不重复请求。取不到时给出上次存下来的，并说明原因。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import remote

logger = logging.getLogger(__name__)

REFRESH_MINUTES = 30
COUNT = 50
DETAIL = "https://data.eastmoney.com/notices/detail/{code}/{art}.html"


def parse(rows: list[dict[str, Any]], wanted: set[str]) -> list[dict[str, Any]]:
    """接口返回的公告 → 页面用的条目（新的在前）。只留持仓里的股票；一条公告涉及几只持仓时各列一条。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        art = str(row.get("art_code") or "").strip()
        title = str(row.get("title_ch") or row.get("title") or "").strip()
        if not art or not title:
            continue
        kinds = [str(c.get("column_name")) for c in row.get("columns") or [] if isinstance(c, dict) and c.get("column_name")]
        for stock in row.get("codes") or []:
            code = str((stock or {}).get("stock_code") or "")
            if code not in wanted or (code, art) in seen:
                continue
            seen.add((code, art))
            name = str(stock.get("short_name") or "")
            shown = title[len(name) + 1:] if name and title.startswith((f"{name}:", f"{name}：")) else title
            out.append({
                "id": art, "code": code, "name": name, "title": shown, "kind": kinds[0] if kinds else "",
                # display_time 是公告挂出来的时刻；notice_date 是公告日期（晚上挂出的算第二天）
                "published_at": str(row.get("display_time") or "")[:16], "notice_date": str(row.get("notice_date") or "")[:10],
                "url": DETAIL.format(code=code, art=art),
            })
    return sorted(out, key=lambda item: item["published_at"], reverse=True)


def _load(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "notices.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(root: Path, data: dict[str, Any]) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / "notices.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(root / "notices.json")
    except OSError as e:
        logger.info("持仓公告没有存下来：%s", e)


def latest(root: Path, symbols: list[str], now: datetime, force: bool = False) -> dict[str, Any]:
    """持仓的最新公告。返回 {"items", "fetched_at", "error", "codes"}。

    半小时内取过、而且持仓没有变，就直接用存下来的；force=True 时不管多久都重新取。
    """
    codes = sorted({s.split(".")[0] for s in symbols if s})
    if not codes:
        return {"items": [], "fetched_at": None, "error": None, "codes": []}
    saved = _load(root)
    fresh = False
    if saved.get("codes") == codes and saved.get("fetched_at"):
        try:
            fresh = now - datetime.fromisoformat(saved["fetched_at"]) < timedelta(minutes=REFRESH_MINUTES)
        except ValueError:
            fresh = False
    if fresh and not force:
        return {**saved, "error": None}
    try:
        rows = remote.announcements(codes, COUNT)
    except remote.UnavailableError as e:
        return {"items": saved.get("items") or [] if saved.get("codes") == codes else [],
                "fetched_at": saved.get("fetched_at") if saved.get("codes") == codes else None,
                "error": str(e), "codes": codes}
    data = {"items": parse(rows, set(codes)), "fetched_at": now.isoformat(timespec="seconds"), "codes": codes}
    _save(root, data)
    return {**data, "error": None}


def within_days(items: list[dict[str, Any]], now: datetime, days: int) -> list[dict[str, Any]]:
    """只留最近 days 天（含今天）挂出来的公告。"""
    since = (now.date() - timedelta(days=days - 1)).isoformat()
    return [n for n in items if n["published_at"][:10] >= since]


def recap_block(root: Path, symbols: list[str], as_of: str, now: datetime) -> list[str]:
    """给 AI 复盘用的一段：持仓最近几天的公告标题。取不到就不写这一段。"""
    result = latest(root, symbols, now)
    try:
        since = (datetime.fromisoformat(as_of) - timedelta(days=4)).date().isoformat()
    except ValueError:
        return []
    recent = [n for n in result["items"] if n["published_at"][:10] >= since][:20]
    if not recent:
        return []
    lines = ["", "## 持仓公告（近几天的上市公司正式公告，只有标题，没有正文；不要推测公告的具体内容）"]
    for n in recent:
        kind = f"（{n['kind']}）" if n["kind"] else ""
        lines.append(f"- {n['published_at']} {n['name']}：{n['title']}{kind}")
    return lines
