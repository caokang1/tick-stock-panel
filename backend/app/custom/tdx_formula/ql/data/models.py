"""统一数据结构：各能力返回的 DataFrame 列定义，以及下载、更新、状态的结果对象。

约定：日期列为 pandas 的 ``datetime64[s]``（零点），进 Panel 时转成 ``datetime64[D]``；
代码为 ``sh.600000`` 格式；成交量单位为股，成交额单位为元。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.custom.tdx_formula.ql.core.errors import SourceUnavailable
from app.custom.tdx_formula.ql.data.interface import Capability

DATE_DTYPE = "datetime64[s]"

PRICE_COLUMNS = ("open", "high", "low", "close")
BAR_COLUMNS = ("date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "suspended")

# 每种能力必须有的列。STOCK_DAILY 允许额外带一列 factor（来源顺带给出复权因子时）。
COLUMNS: dict[Capability, tuple[str, ...]] = {
    Capability.TRADE_CALENDAR: ("date",),
    Capability.SECURITIES: ("code", "name", "list_date", "delist_date", "kind"),
    Capability.STOCK_DAILY: BAR_COLUMNS,
    Capability.ADJUST_FACTOR: ("date", "code", "factor"),
    Capability.MARKET_SNAPSHOT: (*BAR_COLUMNS, "is_st"),
    Capability.ST_HISTORY: ("date", "code", "is_st"),
    Capability.INDEX_DAILY: ("date", "code", "open", "high", "low", "close", "volume", "amount"),
    Capability.INDUSTRY: ("code", "industry"),
    Capability.FLOAT_SHARES: ("date", "code", "float_shares"),
}
# 来源顺带给出时才有的列：factor 是复权因子，float_shares 是当天的流通股本（股）。
OPTIONAL_COLUMNS: dict[Capability, tuple[str, ...]] = {
    Capability.STOCK_DAILY: ("factor", "float_shares"),
    Capability.MARKET_SNAPSHOT: ("float_shares",),
}

_FLOAT_COLUMNS = {"open", "high", "low", "close", "preclose", "volume", "amount", "factor", "float_shares"}
_BOOL_COLUMNS = {"suspended", "is_st"}
_DATE_COLUMNS = {"date", "list_date", "delist_date"}

KIND_STOCK = "stock"
KIND_INDEX = "index"


def to_dates(values: Any) -> pd.Series:
    """把各种写法的日期统一成 ``datetime64[s]`` 的零点；无法解析的为 NaT。"""
    return pd.Series(pd.to_datetime(values, errors="coerce")).dt.normalize().astype("datetime64[s]")


def empty_frame(capability: Capability) -> pd.DataFrame:
    return normalize_frame(capability, pd.DataFrame({c: [] for c in COLUMNS[capability]}))


def normalize_frame(capability: Capability, df: pd.DataFrame) -> pd.DataFrame:
    """把适配器翻译好的结果整理成统一口径：列齐全、类型一致、按日期升序。缺列时抛 SourceUnavailable。"""
    required = COLUMNS[capability]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SourceUnavailable(f"来源返回的数据缺少列：{', '.join(missing)}（{capability.value}）")
    keep = [*required, *(c for c in OPTIONAL_COLUMNS.get(capability, ()) if c in df.columns)]
    out = df.loc[:, keep].reset_index(drop=True).copy()
    attrs = dict(df.attrs)
    for col in keep:
        if col in _DATE_COLUMNS:
            out[col] = to_dates(out[col].to_numpy())
        elif col in _FLOAT_COLUMNS:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
        elif col in _BOOL_COLUMNS:
            out[col] = out[col].astype(bool)
        else:
            out[col] = out[col].astype(str)
    if "date" in out.columns:
        out = out[out["date"].notna()]
        keys = ["date", "code"] if "code" in out.columns else ["date"]
        out = out.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
    out.attrs = attrs
    return out


@dataclass(frozen=True)
class Security:
    code: str
    name: str
    list_date: date | None
    delist_date: date | None
    kind: str = KIND_STOCK


@dataclass
class DownloadReport:
    """一次历史下载的结果。``finished`` 为 False 时说明还有没下完的，下次运行会续传。"""

    start: date
    total: int = 0
    done: dict[str, int] = field(default_factory=dict)  # 每项数据已完成的股票数（含以前完成的）
    fetched: dict[str, int] = field(default_factory=dict)  # 本次新取回的
    failed: dict[str, str] = field(default_factory=dict)  # "代码/数据项" -> 原因
    finished: bool = False
    message: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        return d


@dataclass
class UpdateReport:
    """一次每日增量的结果。"""

    days: list[date] = field(default_factory=list)  # 本次补上的交易日
    stocks: int = 0  # 最后一个交易日写入的股票数
    factor_changed: list[str] = field(default_factory=list)  # 复权因子发生变化的股票
    ok: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["days"] = [x.isoformat() for x in self.days]
        return d


@dataclass(frozen=True)
class DataStatus:
    """本地行情数据的概况。"""

    has_data: bool
    first_date: date | None
    latest_date: date | None
    stock_count: int  # 已有日线文件的股票数
    securities_count: int  # 证券清单里的股票数
    st_known: bool
    st_done: int  # 已下载历史 ST 状态的股票数
    shares_known: bool  # 历史流通股本是否都补齐了（流通市值、换手率要用）
    shares_done: int  # 已有历史流通股本的股票数
    download_start: date | None
    download_finished: bool
    last_update_at: datetime | None
    last_update_ok: bool | None
    last_update_message: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("first_date", "latest_date", "download_start", "last_update_at"):
            d[k] = d[k].isoformat() if d[k] is not None else None
        return d


@dataclass(frozen=True)
class SourceOverview:
    """一个数据来源的当前状况。state：normal / cooling / capped / disabled / halted。"""

    name: str
    policy: dict[str, Any]
    capabilities: list[str]
    state: str
    today_requests: int
    cooldown_until: datetime | None
    cooldown_remaining_seconds: int
    consecutive_failures: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cooldown_until"] = self.cooldown_until.isoformat() if self.cooldown_until else None
        return d
