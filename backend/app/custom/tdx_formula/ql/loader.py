"""从 quant-lab 的本地行情目录读出全市场日线，拼成 ``Panel``。只读，不往那个目录里写任何东西。

目录结构（quant-lab 的 ``data/market``）：``daily/{code}.parquet`` 每只股票一个文件（不复权日线、复权因子、
流通股本、当天是否 ST），``calendar.parquet`` 交易日历，``securities.parquet`` 证券清单，``industry.parquet`` 行业。
拼的时候按交易日历对齐、停牌日补行、乘以复权因子得到后复权价格。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from app.custom.tdx_formula.ql.core.errors import DataError
from app.custom.tdx_formula.ql.core.panel import Panel

logger = logging.getLogger(__name__)

_VALUE_COLUMNS = ("open", "high", "low", "close", "preclose", "volume", "amount", "factor", "float_shares")
_DAILY_SCHEMA = pa.schema(
    [("date", pa.date32()), ("code", pa.string())]
    + [(c, pa.float64()) for c in _VALUE_COLUMNS]
    + [("is_st", pa.bool_()), ("source", pa.string())]
)

_lock = threading.Lock()
_memory: dict[tuple[str, str], Panel] = {}


def _dates(values: object) -> pd.Series:
    return pd.Series(pd.to_datetime(values, errors="coerce")).dt.normalize().astype("datetime64[s]")


def read_calendar(market_dir: Path) -> np.ndarray:
    path = market_dir / "calendar.parquet"
    if not path.exists():
        return np.array([], dtype="datetime64[D]")
    return pq.read_table(path)["date"].to_numpy().astype("datetime64[D]")


def read_securities(market_dir: Path) -> pd.DataFrame:
    path = market_dir / "securities.parquet"
    if not path.exists():
        return pd.DataFrame({"code": [], "name": [], "list_date": [], "delist_date": [], "kind": []})
    df = pq.read_table(path).to_pandas()
    df["list_date"] = _dates(df["list_date"].to_numpy())
    df["delist_date"] = _dates(df["delist_date"].to_numpy())
    return df


def read_industry(market_dir: Path) -> dict[str, str]:
    path = market_dir / "industry.parquet"
    if not path.exists():
        return {}
    df = pq.read_table(path).to_pandas()
    return {str(c): str(i) for c, i in zip(df["code"], df["industry"], strict=True) if str(i)}


def stock_names(market_dir: Path) -> dict[str, str]:
    df = read_securities(market_dir)
    return dict(zip(df["code"].astype(str), df["name"].astype(str), strict=False))


def fingerprint(market_dir: Path) -> str | None:
    """数据文件的"指纹"：文件个数、最新修改时间、总大小。任何一个文件变了就要重新拼。没有数据时返回 None。"""
    daily_dir = market_dir / "daily"
    if not daily_dir.exists():
        return None
    count, newest, size = 0, 0, 0
    with os.scandir(daily_dir) as it:
        for entry in it:
            if entry.name.endswith(".parquet"):
                st = entry.stat()
                count += 1
                newest = max(newest, st.st_mtime_ns)
                size += st.st_size
    if count == 0:
        return None
    parts = [str(count), str(newest), str(size)]
    for name in ("calendar.parquet", "securities.parquet", "industry.parquet"):
        p = market_dir / name
        parts.append(str(p.stat().st_mtime_ns) if p.exists() else "-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def build_panel(market_dir: Path) -> Panel:
    """拼好的结果留在内存里，数据文件有变化时自动重拼。没有数据时抛 ``DataError``。"""
    with _lock:
        key = fingerprint(market_dir)
        if key is None:
            raise DataError(f"在 {market_dir} 里没有找到行情数据。")
        cached = _memory.get((str(market_dir), key))
        if cached is None:
            cached = _assemble_panel(market_dir)
            _memory.clear()
            _memory[(str(market_dir), key)] = cached
        return cached


def clear_cache() -> None:
    with _lock:
        _memory.clear()


def _take_previous(values: np.ndarray, prev_idx: np.ndarray) -> np.ndarray:
    """每个格子取"最近一次有数据的那一行"的值；之前从没有过数据的格子为 NaN。"""
    out = np.take_along_axis(values, np.maximum(prev_idx, 0), axis=0)
    out[prev_idx < 0] = np.nan
    return out


def _previous_index(mask: np.ndarray) -> np.ndarray:
    """对每个格子，给出同一列里不晚于当天、最近一个 mask 为 True 的行号；没有则为 -1。"""
    rows = np.arange(mask.shape[0], dtype=np.int32)[:, None]
    idx = np.where(mask, rows, np.int32(-1))
    np.maximum.accumulate(idx, axis=0, out=idx)
    return idx


def _assemble_panel(market_dir: Path) -> Panel:
    daily_dir = market_dir / "daily"
    columns = ["date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "factor",
               "float_shares", "is_st"]
    # 只读 *.parquet：别的进程正在写入时目录里会有 .tmp 临时文件，中断的写入也可能留下残片
    files = sorted(str(p) for p in daily_dir.glob("*.parquet"))
    if not files:
        raise DataError("本地还没有行情数据，请先下载历史数据")
    table = pads.dataset(files, format="parquet", schema=_DAILY_SCHEMA).to_table(columns=columns)
    if table.num_rows == 0:
        raise DataError("本地还没有行情数据，请先下载历史数据")

    encoded = table["code"].dictionary_encode().unify_dictionaries().combine_chunks()
    file_codes = np.array(encoded.dictionary.to_pylist(), dtype=str)
    order = np.argsort(file_codes)
    rank = np.empty(len(order), dtype=np.int64)
    rank[order] = np.arange(len(order))
    codes = file_codes[order]
    ci = rank[encoded.indices.to_numpy(zero_copy_only=False)]

    row_dates = table["date"].to_numpy().astype("datetime64[D]")
    close_rows = table["close"].to_numpy()
    bar_rows = ~np.isnan(close_rows)
    if not bar_rows.any():
        raise DataError("本地还没有行情数据，请先下载历史数据")
    first, last = row_dates[bar_rows].min(), row_dates[bar_rows].max()

    calendar = read_calendar(market_dir)
    calendar = calendar[(calendar >= first) & (calendar <= last)]
    if len(calendar) == 0:
        # 没有交易日历时退而求其次：用出现过行情的日期
        calendar = np.unique(row_dates[bar_rows])
    dates = calendar
    t, n = len(dates), len(codes)

    ti = np.searchsorted(dates, row_dates)
    on_calendar = (ti < t) & (dates[np.minimum(ti, t - 1)] == row_dates)
    dropped = int((bar_rows & ~on_calendar).sum())
    if dropped:
        logger.warning("有 %d 行日线的日期不在交易日历里，已忽略", dropped)

    def scatter(name: str, rows: np.ndarray) -> np.ndarray:
        m = np.full((t, n), np.nan)
        m[ti[rows], ci[rows]] = table[name].to_numpy()[rows]
        return m

    keep = bar_rows & on_calendar
    # 只有 ST 标记、一根日线都没有的股票不进 Panel
    with_bars = np.bincount(ci[keep], minlength=n) > 0
    if not with_bars.all():
        remap = np.cumsum(with_bars) - 1
        in_panel = with_bars[ci]
        keep &= in_panel
        on_calendar &= in_panel
        ci = remap[ci]
        codes = codes[with_bars]
        n = len(codes)
    has_bar = np.zeros((t, n), dtype=bool)
    has_bar[ti[keep], ci[keep]] = True
    raw_open, raw_high, raw_low, raw_close = (scatter(c, keep) for c in ("open", "high", "low", "close"))
    raw_preclose, volume, amount, factor = (scatter(c, keep) for c in ("preclose", "volume", "amount", "factor"))
    float_shares = scatter("float_shares", keep)

    st_col = table["is_st"]
    st_rows = on_calendar & ~st_col.is_null().to_numpy(zero_copy_only=False)
    st = np.full((t, n), np.nan)
    st[ti[st_rows], ci[st_rows]] = st_col.fill_null(False).to_numpy(zero_copy_only=False)[st_rows].astype(float)

    # ---- 上市区间 ----
    sec = read_securities(market_dir)
    sec = sec.drop_duplicates("code").set_index("code").reindex(codes)
    list_date = sec["list_date"].to_numpy().astype("datetime64[D]")
    delist_date = sec["delist_date"].to_numpy().astype("datetime64[D]")
    first_bar = np.where(has_bar.any(axis=0), dates[np.argmax(has_bar, axis=0)], np.datetime64("NaT", "D"))
    listed_from = np.where(np.isnat(list_date), first_bar, list_date)
    d = dates[:, None]
    listed = (d >= listed_from[None, :]) & (np.isnat(delist_date)[None, :] | (d <= delist_date[None, :]))
    has_bar &= listed
    suspended = listed & ~has_bar

    # ---- 停牌日补行：价格沿用上一交易日收盘价，成交量为 0 ----
    prev = _previous_index(has_bar)
    prev_close = _take_previous(raw_close, prev)
    prev_factor = _take_previous(factor, prev)

    def fill(m: np.ndarray, filler: np.ndarray | float) -> np.ndarray:
        out = np.where(has_bar, m, filler)
        out[~listed] = np.nan
        return out

    raw_open, raw_high, raw_low = fill(raw_open, prev_close), fill(raw_high, prev_close), fill(raw_low, prev_close)
    raw_preclose = fill(raw_preclose, prev_close)
    raw_close = fill(raw_close, prev_close)
    volume, amount = fill(volume, 0.0), fill(amount, 0.0)
    factor = fill(factor, prev_factor)
    # 流通股本很少变：某天没有就沿用之前最近一次知道的值；第一次知道之前的日子保持未知
    float_shares = _take_previous(float_shares, _previous_index(np.isfinite(float_shares) & (float_shares > 0)))
    float_shares[~listed] = np.nan
    industry_of = read_industry(market_dir)
    industry = np.array([industry_of.get(str(c), "") for c in codes], dtype=str)
    missing_factor = int((has_bar & np.isnan(factor)).any(axis=0).sum())
    if missing_factor:
        logger.warning("有 %d 只股票缺复权因子，它们的后复权价格为空", missing_factor)

    # ---- ST：有历史数据的用历史数据（停牌日沿用之前的状态），没有的按当前名称近似 ----
    st_idx = _previous_index(~np.isnan(st))
    st_filled = _take_previous(st, st_idx)
    unknown = np.isnan(st_filled)
    st_known = not bool((unknown & has_bar).any())
    st_now = np.array(["ST" in str(name).upper() for name in sec["name"].astype(object)], dtype=bool)
    is_st = np.where(unknown, st_now[None, :], st_filled == 1.0) & listed

    return Panel(
        dates=dates, codes=codes,
        open=raw_open * factor, high=raw_high * factor, low=raw_low * factor, close=raw_close * factor,
        volume=volume, amount=amount,
        raw_open=raw_open, raw_high=raw_high, raw_low=raw_low, raw_close=raw_close, raw_preclose=raw_preclose,
        listed=listed, suspended=suspended, is_st=is_st, st_known=st_known,
        float_shares=float_shares, industry=industry,
    )
