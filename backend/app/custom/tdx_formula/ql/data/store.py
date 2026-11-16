"""本地存储：Parquet 读写、下载进度、``build_panel()``。

目录结构见技术设计 §7.4。每只股票一个文件 ``daily/{code}.parquet``，一行一个日期：
不复权日线 + 复权因子 + 当天的流通股本 + 当天是否 ST。只有 ST 标记、没有行情的行（收盘价为空）表示当天停牌。
早期版本下载的文件里没有流通股本这一列，读的时候按"未知"处理，补下载之后才有。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from app.custom.tdx_formula.ql.core.config import get_settings
from app.custom.tdx_formula.ql.core.errors import DataError
from app.custom.tdx_formula.ql.core.panel import Panel, matrix_field_names
from app.custom.tdx_formula.ql.data import config as config_mod
from app.custom.tdx_formula.ql.data.models import KIND_STOCK, to_dates

logger = logging.getLogger(__name__)

_VALUE_COLUMNS = ("open", "high", "low", "close", "preclose", "volume", "amount", "factor", "float_shares")
DAILY_COLUMNS = ("date", "code", *_VALUE_COLUMNS, "is_st", "source")
_DAILY_SCHEMA = pa.schema(
    [("date", pa.date32()), ("code", pa.string())]
    + [(c, pa.float64()) for c in _VALUE_COLUMNS]
    + [("is_st", pa.bool_()), ("source", pa.string())]
)
_INDEX_COLUMNS = ("date", "code", "open", "high", "low", "close", "volume", "amount")
_INDEX_SCHEMA = pa.schema(
    [("date", pa.date32()), ("code", pa.string())] + [(c, pa.float64()) for c in _INDEX_COLUMNS[2:]]
)
_PRICE_TOLERANCE = 0.005  # 不到半分钱的差别视为相等（价格最小单位是分）

_write_lock = threading.RLock()  # 同一个文件的"读出来、改、写回去"不能交叉
_panel_lock = threading.Lock()
_disk_cache_paused = False  # 历史下载进行中：数据文件一直在变，先不写硬盘缓存
_panel_memory: dict[str, Panel] = {}


# ---------------- 路径 ----------------
def _market() -> Path:
    s = get_settings()
    s.ensure_dirs()
    return s.market_dir


def daily_path(code: str) -> Path:
    return get_settings().daily_dir / f"{code}.parquet"


def index_path(code: str) -> Path:
    return get_settings().index_dir / f"{code}.parquet"


def _atomic_write_table(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


def _atomic_write_text(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _date32(values: pd.Series) -> pa.Array:
    days = to_dates(values.to_numpy()).to_numpy().astype("datetime64[D]")
    return pa.array(days, type=pa.date32(), from_pandas=True)


# ---------------- 交易日历与证券清单 ----------------
def write_calendar(dates: pd.Series) -> None:
    """保存交易日历；与已有的日历合并，只增不减。"""
    with _write_lock:
        new = to_dates(dates.to_numpy()).dropna().to_numpy().astype("datetime64[D]")
        merged = np.unique(np.concatenate([read_calendar(), new]))
        _atomic_write_table(pa.table({"date": pa.array(merged, type=pa.date32())}), _market() / "calendar.parquet")


def read_calendar() -> np.ndarray:
    """全部已知的交易日，``datetime64[D]`` 升序。还没有下载时返回空数组。"""
    path = get_settings().market_dir / "calendar.parquet"
    if not path.exists():
        return np.array([], dtype="datetime64[D]")
    return pq.read_table(path)["date"].to_numpy().astype("datetime64[D]")


def write_securities(df: pd.DataFrame) -> None:
    with _write_lock:
        table = pa.table({
            "code": pa.array(df["code"].astype(str).tolist(), type=pa.string()),
            "name": pa.array(df["name"].astype(str).tolist(), type=pa.string()),
            "list_date": _date32(df["list_date"]),
            "delist_date": _date32(df["delist_date"]),
            "kind": pa.array(df["kind"].astype(str).tolist(), type=pa.string()),
        })
        _atomic_write_table(table, _market() / "securities.parquet")


def write_industry(df: pd.DataFrame) -> None:
    """保存每只股票现在所属的行业（code, industry）。整份覆盖。"""
    with _write_lock:
        table = pa.table({
            "code": pa.array(df["code"].astype(str).tolist(), type=pa.string()),
            "industry": pa.array(df["industry"].astype(str).tolist(), type=pa.string()),
        })
        _atomic_write_table(table, _market() / "industry.parquet")


def read_industry() -> dict[str, str]:
    """股票代码 -> 行业名称。还没有下载过、或这只股票没有行业时不在里面。"""
    path = get_settings().market_dir / "industry.parquet"
    if not path.exists():
        return {}
    df = pq.read_table(path).to_pandas()
    return {str(c): str(i) for c, i in zip(df["code"], df["industry"], strict=True) if str(i)}


def read_securities() -> pd.DataFrame:
    """证券清单：code, name, list_date, delist_date（可空）, kind。还没有下载时返回空表。"""
    path = get_settings().market_dir / "securities.parquet"
    if not path.exists():
        return pd.DataFrame({"code": [], "name": [], "list_date": [], "delist_date": [], "kind": []})
    df = pq.read_table(path).to_pandas()
    df["list_date"] = to_dates(df["list_date"].to_numpy())
    df["delist_date"] = to_dates(df["delist_date"].to_numpy())
    return df


# ---------------- 个股日线 ----------------
def read_daily(code: str) -> pd.DataFrame | None:
    path = daily_path(code)
    if not path.exists():
        return None
    df = pq.read_table(path).to_pandas()
    df["date"] = to_dates(df["date"].to_numpy())
    return df


def _write_daily(code: str, df: pd.DataFrame) -> None:
    df = df.sort_values("date")
    arrays: dict[str, pa.Array] = {"date": _date32(df["date"])}
    arrays["code"] = pa.array([code] * len(df), type=pa.string())
    for c in _VALUE_COLUMNS:
        arrays[c] = pa.array(df[c].to_numpy(dtype="float64"), type=pa.float64(), from_pandas=True)
    st = df["is_st"]
    arrays["is_st"] = pa.array([None if pd.isna(v) else bool(v) for v in st], type=pa.bool_())
    arrays["source"] = pa.array([None if pd.isna(v) else str(v) for v in df["source"]], type=pa.string())
    _atomic_write_table(pa.table(arrays, schema=_DAILY_SCHEMA), daily_path(code))
    bars = df.loc[df["close"].notna(), "date"]
    if len(bars):
        _note_range(bars.min().date(), bars.max().date())


def _off_calendar(days: pd.Index) -> np.ndarray:
    """哪些日期落在交易日历的范围之内、却不是交易日（比如来源多给了一个周日）。日历之外的日期不判断。"""
    calendar = read_calendar()
    values = days.to_numpy().astype("datetime64[D]")
    if len(calendar) == 0 or len(values) == 0:
        return np.zeros(len(values), dtype=bool)
    covered = (values >= calendar[0]) & (values <= calendar[-1])
    return covered & ~np.isin(values, calendar)


def merge_daily(code: str, new: pd.DataFrame) -> int:
    """把新数据并入某只股票的文件：按日期对齐，新数据里非空的值覆盖旧值，其余保留。返回文件的总行数。

    ``new`` 必须有 ``date`` 列，其余列是 ``DAILY_COLUMNS`` 的任意子集。
    """
    with _write_lock:
        cols = [c for c in DAILY_COLUMNS if c in new.columns and c not in ("date", "code")]
        incoming = new.loc[:, ["date", *cols]].copy()
        incoming["date"] = to_dates(incoming["date"].to_numpy())
        incoming = incoming.dropna(subset=["date"]).drop_duplicates("date", keep="last").set_index("date")
        incoming = incoming[~_off_calendar(incoming.index)]
        if "is_st" in incoming.columns:
            incoming["is_st"] = incoming["is_st"].astype(object)
        old = read_daily(code)
        if old is None:
            merged = incoming
        else:
            old_indexed = old.drop(columns=["code"]).set_index("date")
            old_indexed["is_st"] = old_indexed["is_st"].astype(object)
            old_indexed["source"] = old_indexed["source"].astype(object)
            merged = incoming.combine_first(old_indexed)
        for c in DAILY_COLUMNS:
            if c not in merged.columns and c not in ("date", "code"):
                merged[c] = None if c in ("is_st", "source") else np.nan
        merged = merged.reset_index()
        _write_daily(code, merged)
        return len(merged)


def list_daily_codes() -> list[str]:
    d = get_settings().daily_dir
    if not d.exists():
        return []
    return sorted(p.name[: -len(".parquet")] for p in d.iterdir() if p.name.endswith(".parquet"))


# ---------------- 指数 ----------------
def merge_index(code: str, new: pd.DataFrame) -> int:
    with _write_lock:
        incoming = new.loc[:, [c for c in _INDEX_COLUMNS if c != "code"]].copy()
        incoming["date"] = to_dates(incoming["date"].to_numpy())
        incoming = incoming.dropna(subset=["date"]).drop_duplicates("date", keep="last").set_index("date")
        old = read_index(code)
        merged = incoming if old is None else incoming.combine_first(old.drop(columns=["code"]).set_index("date"))
        merged = merged.reset_index().sort_values("date")
        arrays: dict[str, pa.Array] = {"date": _date32(merged["date"])}
        arrays["code"] = pa.array([code] * len(merged), type=pa.string())
        for c in _INDEX_COLUMNS[2:]:
            arrays[c] = pa.array(merged[c].to_numpy(dtype="float64"), type=pa.float64(), from_pandas=True)
        _atomic_write_table(pa.table(arrays, schema=_INDEX_SCHEMA), index_path(code))
        return len(merged)


def read_index(code: str) -> pd.DataFrame | None:
    """某个指数的日线：date, code, open, high, low, close, volume, amount。没有下载时返回 None。"""
    path = index_path(code)
    if not path.exists():
        return None
    df = pq.read_table(path).to_pandas()
    df["date"] = to_dates(df["date"].to_numpy())
    return df


# ---------------- 概况（meta.json） ----------------
def _meta_path() -> Path:
    return get_settings().market_dir / "meta.json"


def read_meta() -> dict[str, Any]:
    path = _meta_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("meta.json 已损坏，按空处理")
        return {}


def update_meta(**changes: Any) -> None:
    with _write_lock:
        meta = read_meta()
        meta.update(changes)
        _atomic_write_text(json.dumps(meta, ensure_ascii=False, indent=1), _meta_path())


def _note_range(first: date, last: date) -> None:
    """记下本地行情最早和最新的一根日线是哪天（只往两头扩，不往里收）。"""
    meta = read_meta()
    changes: dict[str, str] = {}
    if not meta.get("latest_date") or meta["latest_date"] < last.isoformat():
        changes["latest_date"] = last.isoformat()
    if meta.get("first_date") and meta["first_date"] > first.isoformat():
        changes["first_date"] = first.isoformat()
    if changes:
        update_meta(**changes)


def latest_date() -> date | None:
    """本地行情的最新日期。"""
    value = read_meta().get("latest_date")
    return date.fromisoformat(value) if value else None


def first_date() -> date | None:
    """本地行情里最早的一根日线是哪天。

    在第一次把全市场数据拼出来时记下（之后写入更早的数据会跟着往前挪）；还没拼过时返回 None。
    """
    value = read_meta().get("first_date")
    return date.fromisoformat(value) if value else None


def complete_through() -> date | None:
    """这一天及之前，库里所有股票的数据都是齐的。

    做过每日更新的，取更新到的那一天；还没做过的，取各只股票历史下载截止日里最早的一个。都没有记录时返回 None。
    """
    return updated_through() or Progress.load().history_end()


def updated_through() -> date | None:
    """每日增量已经做到哪一天（这一天及之前，库里所有股票的数据都是齐的）。"""
    value = read_meta().get("updated_through")
    return date.fromisoformat(value) if value else None


# ---------------- 历史下载进度（progress.json） ----------------
ITEM_DAILY = "daily"
ITEM_FACTOR = "factor"
ITEM_ST = "st"
ITEM_SHARES = "shares"  # 历史上每天的流通股本
ITEMS = (ITEM_DAILY, ITEM_FACTOR, ITEM_ST, ITEM_SHARES)
DONE = "done"


@dataclass
class Progress:
    """历史下载进度。每完成一只股票的一项数据就保存一次，中断后跳过已完成的。"""

    start: str | None = None
    calendar: bool = False
    securities: bool = False
    indices: dict[str, str] = field(default_factory=dict)
    stocks: dict[str, dict[str, str]] = field(default_factory=dict)
    finished: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def load(cls) -> Progress:
        path = get_settings().market_dir / "progress.json"
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("progress.json 已损坏，下载进度按空处理（已下载的数据文件不受影响）")
            return cls()
        return cls(
            start=raw.get("start"), calendar=bool(raw.get("calendar")), securities=bool(raw.get("securities")),
            indices=dict(raw.get("indices", {})), stocks={k: dict(v) for k, v in raw.get("stocks", {}).items()},
            finished=bool(raw.get("finished")),
        )

    def save(self) -> None:
        with self._lock:
            payload = {
                "start": self.start, "calendar": self.calendar, "securities": self.securities,
                "indices": self.indices, "stocks": self.stocks, "finished": self.finished,
            }
            text = json.dumps(payload, ensure_ascii=False)
        with _write_lock:
            _atomic_write_text(text, _market() / "progress.json")

    def is_done(self, code: str, item: str) -> bool:
        return self.stocks.get(code, {}).get(item) == DONE

    def mark(self, code: str, item: str, status: str = DONE, **extra: str) -> None:
        with self._lock:
            entry = self.stocks.setdefault(code, {})
            entry[item] = status
            entry.update(extra)
        self.save()

    def count_done(self, item: str, codes: list[str] | None = None) -> int:
        keys = self.stocks.keys() if codes is None else codes
        return sum(1 for c in keys if self.is_done(c, item))

    def history_end(self) -> date | None:
        """已下完日线的股票里，最早的一个"下载截止日"。这一天及之前所有股票的数据都是齐的。"""
        ends = [v["end"] for v in self.stocks.values() if v.get(ITEM_DAILY) == DONE and v.get("end")]
        return date.fromisoformat(min(ends)) if ends else None


# ---------------- build_panel ----------------
def _fingerprint() -> str | None:
    """数据文件的"指纹"：文件个数、最新修改时间、总大小。任何一个文件变了，缓存就失效。"""
    s = get_settings()
    if not s.daily_dir.exists():
        return None
    count, newest, size = 0, 0, 0
    with os.scandir(s.daily_dir) as it:
        for entry in it:
            if entry.name.endswith(".parquet"):
                st = entry.stat()
                count += 1
                newest = max(newest, st.st_mtime_ns)
                size += st.st_size
    if count == 0:
        return None
    parts = [str(count), str(newest), str(size), str(complete_through())]
    for name in ("calendar.parquet", "securities.parquet", "industry.parquet"):
        p = s.market_dir / name
        parts.append(str(p.stat().st_mtime_ns) if p.exists() else "-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def build_panel(start: date | None = None, end: date | None = None) -> Panel:
    """把本地的各只股票文件拼成 ``Panel``（约定见技术设计 §2）。

    ``start`` / ``end`` 为 None 表示不限。拼好的结果留在内存里，数据文件有变化时自动重拼。
    设置里打开了硬盘缓存（默认打开）时，拼好的结果还会在 ``panel_cache/`` 里另存一份，
    进程重启后直接读它，不用重拼；关掉时不存，已有的那份会被删掉。
    本地没有数据时抛 ``DataError``。
    """
    with _panel_lock:
        key = _fingerprint()
        if key is None:
            raise DataError("本地还没有行情数据，请先下载历史数据")
        panel = _panel_memory.get(key)
        if panel is None:
            on_disk = panel_cache_enabled()
            panel = _load_cached(key) if on_disk else None
            if panel is None:
                panel = _assemble_panel()
                if on_disk and not _disk_cache_paused:
                    _save_cached(key, panel)
            if not on_disk:
                _remove_disk_cache()
            _panel_memory.clear()
            _panel_memory[key] = panel
            if len(panel.dates):
                earliest = str(panel.dates[0])
                if read_meta().get("first_date") != earliest:
                    update_meta(first_date=earliest)
    if start is None and end is None:
        return panel
    return panel.slice_dates(start, end)


def pause_disk_cache(paused: bool) -> None:
    """历史下载开始和结束时调用。暂停期间照常读已有的缓存，只是不写新的。"""
    global _disk_cache_paused
    _disk_cache_paused = paused


def clear_panel_cache() -> None:
    with _panel_lock:
        _panel_memory.clear()
        _remove_disk_cache()


# ---------------- 硬盘缓存（可在设置里关掉） ----------------
_CACHE_FORMAT = 2  # 缓存文件的格式变了就加一，旧的自动作废
_ARRAY_FIELDS = ("dates", "codes", "industry", *matrix_field_names())
_FREE_SPACE_MARGIN = 500 * 1024 * 1024  # 写完缓存后硬盘至少还要剩这么多，否则不写


def _cache_root() -> Path:
    return get_settings().market_dir / "panel_cache"


def panel_cache_enabled() -> bool:
    return config_mod.load_config().panel_cache


def panel_cache_bytes() -> int:
    """硬盘缓存现在占多少字节；没有则为 0。"""
    root = _cache_root()
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def set_panel_cache(enabled: bool) -> None:
    """打开或关闭硬盘缓存。关掉时立刻删掉已有的缓存；打开时，内存里有拼好的结果就立刻存一份。"""
    config_mod.update_panel_cache(enabled)
    with _panel_lock:
        if not enabled:
            _remove_disk_cache()
            return
        for key, panel in _panel_memory.items():
            if not (_cache_root() / key / "done.json").exists():
                _save_cached(key, panel)


def _remove_disk_cache() -> None:
    root = _cache_root()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _load_cached(key: str) -> Panel | None:
    """读硬盘缓存。没有、没写完、格式不对或读不出来时返回 None，由调用方重新拼。"""
    d = _cache_root() / key
    try:
        done = json.loads((d / "done.json").read_text(encoding="utf-8"))
        if done.get("format") != _CACHE_FORMAT:
            return None
        arrays = {name: np.load(d / f"{name}.npy", allow_pickle=False) for name in _ARRAY_FIELDS}
        return Panel(st_known=bool(done["st_known"]), **arrays)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, DataError) as e:
        logger.warning("行情的硬盘缓存读不出来，改为重新拼：%s", e)
        return None


def _save_cached(key: str, panel: Panel) -> None:
    """把拼好的结果存到硬盘。存不下或写失败只记日志，不影响使用。"""
    root = _cache_root()
    _remove_disk_cache()  # 旧数据对应的缓存已经没用了，先腾出空间
    need = sum(getattr(panel, name).nbytes for name in _ARRAY_FIELDS)
    root.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root.parent).free
    if free < need + _FREE_SPACE_MARGIN:
        logger.warning("硬盘剩余空间不够（需要 %d MB，剩 %d MB），这次不存行情缓存", need >> 20, free >> 20)
        return
    d = root / key
    try:
        d.mkdir(parents=True)
        for name in _ARRAY_FIELDS:
            np.save(d / f"{name}.npy", getattr(panel, name), allow_pickle=False)
        # done.json 最后写：有它才说明前面的文件都写完整了
        _atomic_write_text(json.dumps({"format": _CACHE_FORMAT, "st_known": panel.st_known}), d / "done.json")
    except OSError as e:
        logger.warning("行情的硬盘缓存没有存成：%s", e)
        _remove_disk_cache()


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


def _assemble_panel() -> Panel:
    s = get_settings()
    columns = ["date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "factor",
               "float_shares", "is_st"]
    # 只读 *.parquet：别的进程正在写入时目录里会有 .tmp 临时文件，中断的写入也可能留下残片
    files = sorted(str(p) for p in s.daily_dir.glob("*.parquet"))
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
    # 各只股票不是同一天下载的：晚下载的会多出几天。多出来的那几天里，早下载的股票没有行情不是因为停牌，
    # 而是还没取。所以只拼到"所有股票的数据都齐"的那一天，免得把没取到的当成停牌。
    complete = complete_through()
    if complete is not None and first <= np.datetime64(complete, "D") < last:
        logger.info("行情只拼到 %s：之后的日期还有股票没有更新到", complete)
        last = np.datetime64(complete, "D")

    calendar = read_calendar()
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
    sec = read_securities()
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
    industry_of = read_industry()
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


def price_changed(a: float, b: float) -> bool:
    """两个价格是否相差超过半分钱。"""
    return bool(abs(a - b) > _PRICE_TOLERANCE)


def stock_codes_in_range(securities: pd.DataFrame, start: date, end: date) -> list[str]:
    """证券清单里，在 [start, end] 期间上市过的股票（含期间退市的）。"""
    df = securities[securities["kind"] == KIND_STOCK]
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    alive = (df["list_date"].isna() | (df["list_date"] <= hi)) & (df["delist_date"].isna() | (df["delist_date"] >= lo))
    return sorted(df.loc[alive, "code"].astype(str))
