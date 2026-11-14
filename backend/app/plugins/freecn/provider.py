"""免费公开来源数据源：把「通达信公式」扩展下载到本机的行情按 TSP 的数据契约读出来。

这个 provider 自己不联网。取数、限速、续传、每日更新都在扩展里（app.custom.tdx_formula），
这里只做翻译：

- 代码写法 ``sh.600000`` ↔ ``600000.SH``；
- 成交量：本机存的是股，TSP 要的是手（除以 100 取整）；
- 日线是原始价（不复权）；
- 除权因子：本机存的是逐日的后复权累计因子 F，TSP 要的是每次除权除息的单次比例，
  即因子发生变化的那一天 ``ex_factor = F(当天) / F(上一个交易日)``。
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.data_providers.base import ProviderRequestError

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "adj_factor")
_CHUNK = 200  # 每读这么多只股票交一批、报一次进度
_DAILY_SCHEMA = {"symbol": pl.String, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
                 "low": pl.Float64, "close": pl.Float64, "volume": pl.Int64, "amount": pl.Float64}
_ADJ_SCHEMA = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
_NO_DATA = "本机还没有免费来源的行情。请先到“通达信公式 → 数据”页下载历史行情。"
Progress = Callable[[int, int], Any]


def _extension_ready() -> bool:
    try:
        return importlib.util.find_spec("app.custom.tdx_formula.ql.data.store") is not None
    except Exception:  # noqa: BLE001 —— find_spec 对损坏的安装也会抛
        return False


def availability() -> tuple[bool, str]:
    """loader 启动自检。只看扩展在不在：读本机数据不需要 akshare、baostock，那两个库只在下载时才用到。"""
    if not _extension_ready():
        return False, "需要“通达信公式”扩展（backend/app/custom/tdx_formula），没有找到"
    return True, "ok"


def to_panel_symbol(code: str) -> str:
    """sh.600000 → 600000.SH"""
    exchange, _, digits = code.partition(".")
    return f"{digits}.{exchange.upper()}"


def to_local_code(symbol: str) -> str | None:
    """600000.SH → sh.600000；写法认不出来时返回 None。"""
    digits, _, exchange = symbol.partition(".")
    if len(digits) != 6 or not digits.isdigit() or exchange.upper() not in ("SH", "SZ", "BJ"):
        return None
    return f"{exchange.lower()}.{digits}"


def _store() -> Any:
    """扩展的本机行情库。行情目录固定在 TSP 数据目录下的 freecn。"""
    from app.custom.tdx_formula.ql.core import config as ql_config
    from app.custom.tdx_formula.ql.data import store

    if not ql_config.is_configured():
        from app.config import settings

        ql_config.set_data_dir(Path(settings.data_dir) / "freecn")
    return store


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


@dataclass
class _Config:
    """轻量 config shim，让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "freecn"
    display_name: str = "免费公开来源"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class FreeCnProvider:
    name = "freecn"
    builtin = True

    def __init__(self) -> None:
        self.config = _Config()

    def close(self) -> None:  # loader 重建注册表时会对每个 provider 调 close；这里没有连接要关
        return None

    # ---------------- 日线 ----------------
    def _bars(self, code: str, start: date, end: date, asset_type: str) -> pl.DataFrame | None:
        store = _store()
        df = store.read_index(code) if asset_type == "index" else store.read_daily(code)
        if df is None or len(df) == 0:
            return None
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        df = df[df["close"].notna() & (df["date"] >= lo) & (df["date"] <= hi)]
        if len(df) == 0:
            return None
        volume = np.floor(df["volume"].to_numpy(dtype="float64") / 100.0)
        return pl.DataFrame({
            "symbol": [to_panel_symbol(code)] * len(df),
            "date": df["date"].dt.date.tolist(),
            "open": df["open"].to_numpy(dtype="float64"), "high": df["high"].to_numpy(dtype="float64"),
            "low": df["low"].to_numpy(dtype="float64"), "close": df["close"].to_numpy(dtype="float64"),
            "volume": np.nan_to_num(volume).astype("int64"),
            "amount": df["amount"].to_numpy(dtype="float64"),
        }, schema=_DAILY_SCHEMA)

    def iter_daily(self, symbols: list[str], start_time: datetime, end_time: datetime, asset_type: str = "stock",
                   on_chunk_done: Progress | None = None) -> Iterator[pl.DataFrame]:
        """按批交出原始价日线。指数只有本机下载过的那几个，别的返回空，由 TSP 回退到别的来源。"""
        if asset_type not in ("stock", "index"):
            return
        store = _store()
        if asset_type == "stock" and store.latest_date() is None:
            raise ProviderRequestError(_NO_DATA)
        start, end = _as_date(start_time), _as_date(end_time)
        total = len(symbols)
        batch: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols, 1):
            code = to_local_code(symbol)
            frame = self._bars(code, start, end, asset_type) if code else None
            if frame is not None:
                batch.append(frame)
            if i % _CHUNK == 0 or i == total:
                if batch:
                    yield pl.concat(batch)
                    batch = []
                if on_chunk_done is not None:
                    on_chunk_done(i, total)  # 它抛异常表示任务被取消，原样往外传

    def get_daily(self, symbols: list[str], start_time: datetime, end_time: datetime, asset_type: str = "stock",
                  on_chunk_done: Progress | None = None) -> pl.DataFrame:
        frames = list(self.iter_daily(symbols, start_time, end_time, asset_type, on_chunk_done))
        return pl.concat(frames) if frames else pl.DataFrame(schema=_DAILY_SCHEMA)

    # ---------------- 除权因子 ----------------
    def get_adj_factors(self, symbols: list[str], start_time: datetime, end_time: datetime,
                        asset_type: str = "stock", on_chunk_done: Progress | None = None) -> pl.DataFrame:
        if asset_type != "stock":
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        store = _store()
        if store.latest_date() is None:
            raise ProviderRequestError(_NO_DATA)
        lo, hi = pd.Timestamp(_as_date(start_time)), pd.Timestamp(_as_date(end_time))
        rows: dict[str, list[Any]] = {"symbol": [], "trade_date": [], "ex_factor": []}
        total = len(symbols)
        for i, symbol in enumerate(symbols, 1):
            code = to_local_code(symbol)
            df = store.read_daily(code) if code else None
            if df is not None and len(df):
                bars = df[df["close"].notna() & df["factor"].notna()].sort_values("date")
                factor = bars["factor"].to_numpy(dtype="float64")
                ratio = factor[1:] / factor[:-1]
                days = bars["date"].iloc[1:]
                # 因子变了的那一天就是一次除权除息；没变的日子不产生记录
                hit = (np.abs(ratio - 1.0) > 1e-9) & np.isfinite(ratio) & (days >= lo).to_numpy() \
                    & (days <= hi).to_numpy()
                if hit.any():
                    rows["symbol"] += [symbol] * int(hit.sum())
                    rows["trade_date"] += days[hit].dt.date.tolist()
                    rows["ex_factor"] += ratio[hit].tolist()
            if on_chunk_done is not None and (i % _CHUNK == 0 or i == total):
                on_chunk_done(i, total)
        return pl.DataFrame(rows, schema=_ADJ_SCHEMA)

    # ---------------- 证券清单 ----------------
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """现在还在上市的股票。已退市的不列出来：TSP 的清单没有“退市日期”，列出来只会每次同步都白要一遍。"""
        if asset_type != "stock":
            return []
        store = _store()
        sec = store.read_securities()
        if len(sec) == 0:
            raise ProviderRequestError(_NO_DATA)
        sec = sec[(sec["kind"] == "stock") & sec["delist_date"].isna()]
        items = []
        for code, name, listed in zip(sec["code"].astype(str), sec["name"].astype(str), sec["list_date"],
                                      strict=True):
            exchange, _, digits = code.partition(".")
            items.append({
                "symbol": to_panel_symbol(code), "name": name, "code": digits, "exchange": exchange.upper(),
                "region": "CN", "type": "stock",
                "ext": {"listing_date": None if pd.isna(listed) else listed.date().isoformat()},
            })
        return items

    # ---------------- 设置页的“试拉” ----------------
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = list(symbols or [])[:3] or ["000001.SZ"]
        now = datetime.now()
        try:
            if dataset == "daily":
                df = self.get_daily(syms, now - timedelta(days=30), now)
            elif dataset == "adj_factor":
                df = self.get_adj_factors(syms, now - timedelta(days=365 * 3), now)
            else:
                return {"provider": self.name, "dataset": dataset, "rows": 0,
                        "error": "免费公开来源只提供日线和除权因子"}
        except ProviderRequestError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.head(5).to_dicts()
        for row in head:  # date → ISO 字符串，保证能转成 JSON
            for k, v in list(row.items()):
                if isinstance(v, (date, datetime)):
                    row[k] = v.isoformat()
        return {"provider": self.name, "dataset": dataset, "rows": df.height, "columns": df.columns,
                "preview": head}
