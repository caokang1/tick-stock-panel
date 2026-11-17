"""手动复盘和定时复盘之间的配合。

- ``ensure_today``：手动点“开始复盘”时，今天的行情还没进面板（正式数据 17:30 才有），就先让面板按实时行情
  取一次当天的快照，这样任何时候生成的都是当天的复盘，不会还是上一个交易日的。
- ``already_done``：定时复盘开跑之前看一眼：最新这一天的复盘，收盘以后已经有人生成过了，就不再跑一遍。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

logger = logging.getLogger(__name__)

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(15, 0)
PROBE_SYMBOL = "600519.SH"  # 用它的最新成交时间判断今天有没有开市（节假日、还没开盘时不是今天）


def _now() -> datetime:
    from app.market_time import cn_now

    return cn_now()


def _daily_source() -> str:
    from app.services import preferences

    return str(preferences.load().get("daily_data_provider") or "tickflow").lower()


def _traded_today(now: datetime) -> bool:
    """今天有没有成交。取一只股票的实时行情看成交时间，经限流器，只发一个请求。"""
    from app.plugins.freecn import quotes

    row = quotes.fetch_symbols([PROBE_SYMBOL]).get(PROBE_SYMBOL)
    stamp = (row or {}).get("timestamp")
    if not stamp:
        return False
    return datetime.fromtimestamp(int(stamp) / 1000, tz=now.tzinfo).date() == now.date()


def ensure_today(repo: Any, quote_service: Any, now: datetime | None = None) -> bool:
    """今天开过市而面板里还没有今天的行情时，让面板取一次当天的快照。取了返回 True。

    只在日 K 用的是“免费公开来源”时做；任何一步不成都只记日志，复盘照旧用手头最新的数据。
    """
    try:
        now = now or _now()
        if repo is None or quote_service is None or now.weekday() >= 5 or now.time() < MARKET_OPEN:
            return False
        if _daily_source() != "freecn":
            return False
        latest = repo.latest_enriched_date()
        if latest is not None and latest >= now.date():
            return False  # 今天的已经有了（实时行情写的，或者正式数据已经同步）
        if not _traded_today(now):
            return False
        quote_service.refresh()
        logger.info("复盘前取了一次当天的行情快照（面板里原来最新的是 %s）", latest)
        return True
    except Exception:  # 取不到就用手头最新的数据复盘，不能让复盘失败
        logger.exception("复盘前取当天行情没有成功")
        return False


def already_done(reports: list[dict], latest: date | None) -> bool:
    """最新这一天（latest）的复盘是不是在那天收盘以后已经生成过。

    盘中生成的不算：那时候当天的行情还没走完，收盘后仍然需要一份完整的。
    """
    if latest is None:
        return False
    close = datetime.combine(latest, MARKET_CLOSE)
    for report in reports:
        if str(report.get("as_of") or "")[:10] != latest.isoformat():
            continue
        try:
            created = datetime.fromisoformat(str(report.get("created_at") or "")).replace(tzinfo=None)
        except ValueError:
            continue
        if created >= close:
            return True
    return False
