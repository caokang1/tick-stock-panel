"""免费公开来源数据源：把「通达信公式」扩展下载到本机的行情按 TSP 的数据契约读出来。

日线和除权因子不联网：取数、限速、续传、每日更新都在扩展里（app.custom.tdx_formula），
这里只做翻译（实时行情和五档盘口见 quotes.py，那两项是盘中直接向腾讯的行情接口要的）：

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

from . import financials, minutes, quotes

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "adj_factor", "realtime", "depth5", "financial", "minute")
_CHUNK = 200  # 每读这么多只股票交一批、报一次进度
_DAILY_SCHEMA = {"symbol": pl.String, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
                 "low": pl.Float64, "close": pl.Float64, "volume": pl.Int64, "amount": pl.Float64}
_ADJ_SCHEMA = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
_NO_DATA = "本机还没有免费来源的行情。请先到“设置 → 数据源 → 免费公开来源”下载历史行情。"
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


def trading_days() -> list[date]:
    """本机行情库里已知的交易日（随日线下载一起存下来的），只读文件、不联网。没有下载过时返回空。"""
    try:
        days = _store().read_calendar()
    except Exception:  # noqa: BLE001 —— 行情库还没就绪时当作没有日历
        return []
    return [d.astype("datetime64[D]").astype(date) for d in days]


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
    minute_history_days = minutes.HISTORY_DAYS  # 免费来源只留最近 8 个交易日的 1 分钟线

    def __init__(self) -> None:
        self.config = _Config()
        self._snapshot = quotes.SnapshotCache()

    def close(self) -> None:  # loader 重建注册表时会对每个 provider 调 close；这里没有连接要关
        self._snapshot.clear()

    # ---------------- 实时行情 ----------------
    @staticmethod
    def _listed_symbols() -> list[str]:
        """全市场快照要查的股票：本机证券清单里还在上市的。"""
        sec = _store().read_securities()
        if len(sec) == 0:
            return []
        sec = sec[(sec["kind"] == "stock") & sec["delist_date"].isna()]
        return [to_panel_symbol(code) for code in sec["code"].astype(str)]

    def get_realtime(self) -> list[dict]:
        """全市场实时快照。两轮之间至少隔配置的秒数（默认 60），间隔内只给上一轮的结果。失败返回空。"""
        try:
            rows = self._snapshot.rows(self._listed_symbols)
        except Exception as e:  # noqa: BLE001 —— 契约：软失败，不打断轮询线程
            logger.warning("免费来源实时行情拉取失败：%s", getattr(e, "message", e))
            return []
        return [row["quote"] for row in rows.values()]

    def _quotes_of(self, symbols: list[str]) -> list[dict] | None:
        """按代码补拉。快照里有的直接用，没有的才去要；失败返回 None。"""
        cached = self._snapshot.peek()
        found = [cached[s]["quote"] for s in symbols if s in cached]
        missing = [s for s in symbols if s not in cached]
        if missing:
            try:
                fetched = quotes.fetch_symbols(missing)
            except Exception as e:  # noqa: BLE001
                logger.warning("免费来源按代码补拉行情失败：%s", getattr(e, "message", e))
                return None
            found += [row["quote"] for row in fetched.values()]
        return found

    def get_realtime_symbols(self, symbols: list[str], *, asset_type: str = "stock") -> list[dict] | None:
        return self._quotes_of(list(symbols)) if symbols else []

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        return self._quotes_of(list(symbols)) if symbols else []

    # ---------------- 分钟 K ----------------
    def get_minute(self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
                   asset_type: str = "stock", on_chunk_done: Progress | None = None,
                   freq: str = "1m") -> pl.DataFrame:
        """1 分钟线，只支持逐只查看（单次最多 20 只）；多了直接拒绝，不去做全市场的历史分钟同步。"""
        symbols = list(symbols)
        if freq != "1m" or not symbols:
            if on_chunk_done is not None:
                on_chunk_done(0, 0)
            return pl.DataFrame(schema=minutes.SCHEMA)
        if len(symbols) > minutes.MAX_SYMBOLS:
            raise ProviderRequestError(minutes.TOO_MANY)
        _store()  # 确保数据目录已经设好（限流器的计数存在那里）
        try:
            return minutes.get_minute(symbols, start_time, end_time, quotes.to_query_code, on_chunk_done)
        except ProviderRequestError:
            raise
        except Exception as e:
            if not hasattr(e, "message"):
                raise  # 不是取数的错（比如进度回调要求取消），原样交回去
            raise ProviderRequestError(e.message) from e

    # ---------------- 财务数据 ----------------
    def get_financials(self, table: str, symbols: list[str], latest_only: bool = False) -> pl.DataFrame:
        """财务指标和三大报表的主要科目（东方财富），按报告期一次取回全市场，存在本机。"""
        from app.custom.tdx_formula.ql.core import clock
        from app.custom.tdx_formula.ql.core import config as ql_config

        _store()  # 确保数据目录已经设好
        root = Path(ql_config.get_settings().data_dir)
        return financials.get_financials(root, table, list(symbols), latest_only, clock.today())

    # ---------------- 五档盘口 ----------------
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """买卖五档。刚取过的全市场快照里本来就带五档，在间隔内直接用，不多发请求。"""
        cached = self._snapshot.peek()
        out = {s: cached[s]["depth"] for s in symbols if s in cached and cached[s]["depth"]}
        missing = [s for s in symbols if s not in cached]
        if missing:
            for symbol, row in quotes.fetch_symbols(missing).items():
                if row["depth"]:
                    out[symbol] = row["depth"]
        return out

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
        }, schema=_DAILY_SCHEMA).with_columns(pl.col("amount").fill_nan(None))  # 没有成交额就留空，不能写成 NaN

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
            elif dataset in ("realtime", "depth5"):
                fetched = quotes.fetch_symbols(syms)
                if dataset == "realtime":
                    rows = [row["quote"] for row in fetched.values()]
                else:
                    rows = [{"symbol": s, **row["depth"]} for s, row in fetched.items() if row["depth"]]
                return {"provider": self.name, "dataset": dataset, "rows": len(rows),
                        "columns": list(rows[0]) if rows else [], "preview": rows[:5]}
            elif dataset == "minute":
                df = self.get_minute(syms[:1], now - timedelta(days=1), now)
            elif dataset == "financial":
                df = self.get_financials("metrics", syms, latest_only=True)
            else:
                return {"provider": self.name, "dataset": dataset, "rows": 0,
                        "error": "免费公开来源不提供这一项（全量分钟需要付费来源）"}
        except Exception as e:  # noqa: BLE001 —— 试拉只报告结果，不让错误冒到页面外
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": getattr(e, "message", str(e))}
        head = df.head(5).to_dicts()
        for row in head:  # date → ISO 字符串，保证能转成 JSON
            for k, v in list(row.items()):
                if isinstance(v, (date, datetime)):
                    row[k] = v.isoformat()
        return {"provider": self.name, "dataset": dataset, "rows": df.height, "columns": df.columns,
                "preview": head}
