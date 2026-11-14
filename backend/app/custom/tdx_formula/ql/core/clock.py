"""全项目唯一读取系统时钟的地方。时区固定为北京时间。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))

_fixed: datetime | None = None


def now() -> datetime:
    """当前北京时间（不带时区信息的本地时间）。"""
    if _fixed is not None:
        return _fixed
    return datetime.now(BEIJING).replace(tzinfo=None)


def today() -> date:
    return now().date()


def set_fixed(moment: datetime | None) -> None:
    """测试用：把时钟固定在某一刻；传 None 恢复真实时间。"""
    global _fixed
    _fixed = moment
