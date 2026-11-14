"""行情数据（下载到 TSP 数据目录下的 freecn 文件夹）、带缓存的求值、公式解析、信号统计。"""

from __future__ import annotations

import shutil
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from .ql import data as ql_data
from .ql.backtest.signals import Costs, signal_frequency, signal_list, signal_stats
from .ql.blocks import (
    Block,
    Universe,
    all_blocks,
    combine_signals,
    enabled_conditions,
    evaluate_block,
    rendered_formula,
    split_formula,
)
from .ql.core import config as ql_config
from .ql.core.errors import ConflictError, DataError, QuantLabError
from .ql.core.panel import Panel
from .ql.data import store as ql_store

_CACHE_SIZE = 64
# 从别处导入行情时要拷的东西（quant-lab 的 data/market 就是这个结构）
_MARKET_ITEMS = ("daily", "index", "calendar.parquet", "securities.parquet", "industry.parquet", "meta.json",
                 "progress.json")


class FormulaService:
    """一个进程里只有一份。``data_dir`` 是行情数据目录（TSP 数据目录下的 freecn）。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[Any, ...], tuple[np.ndarray, np.ndarray]] = OrderedDict()
        ql_config.set_data_dir(data_dir)

    # ---------------- 行情数据 ----------------
    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def has_data(self) -> bool:
        return ql_store.latest_date() is not None and bool(ql_store.list_daily_codes())

    def latest_data_date(self) -> str | None:
        """行情更新到哪一天了。只读概况文件，不用把全市场数据读进内存。"""
        day = ql_store.updated_through() or ql_store.latest_date()
        return day.isoformat() if day else None

    def stock_names(self) -> dict[str, str]:
        df = ql_store.read_securities()
        return dict(zip(df["code"].astype(str), df["name"].astype(str), strict=False))

    def panel(self, start: str | None = None, end: str | None = None) -> Panel:
        if not self.has_data():
            raise DataError("还没有行情数据。请先到“数据”页下载历史行情。")
        panel = ql_store.build_panel()
        lo, hi = _day(start, "开始"), _day(end, "结束")
        if lo or hi:
            panel = panel.slice_dates(lo, hi)
        if panel.shape[0] == 0 or panel.shape[1] == 0:
            raise DataError("选定的时间范围内没有数据。")
        return panel

    def status(self) -> dict[str, Any]:
        """行情数据的概况。读得很快，不会把全市场数据读进内存。"""
        costs = Costs()
        progress = ql_store.Progress.load()
        codes = ql_store.list_daily_codes()
        latest = ql_store.latest_date()
        shares_done = progress.count_done(ql_store.ITEM_SHARES, codes)
        st_done = progress.count_done(ql_store.ITEM_ST, codes)
        return {
            "has_data": bool(codes) and latest is not None, "data_dir": str(self._data_dir),
            "first_date": progress.start, "latest_date": latest.isoformat() if latest else None,
            "stock_count": len(codes), "download_finished": progress.finished,
            "st_known": bool(codes) and st_done == len(codes),
            "float_shares_ready": bool(codes) and shares_done == len(codes),
            "costs": {"commission": costs.commission, "stamp_tax": costs.stamp_tax, "slippage": costs.slippage},
        }

    def import_market(self, source: str) -> dict[str, Any]:
        """从已有的行情目录（例如 quant-lab 的 data 目录）拷一份过来，省掉重新下载。原目录只读、不改动。"""
        text = source.strip()
        if not text:
            raise QuantLabError("请填写要导入的行情目录。")
        root = Path(text).expanduser()
        market = next((c for c in (root, root / "market") if (c / "daily").is_dir()), None)
        if market is None:
            raise QuantLabError(f"在 {text} 里没有找到行情数据（应该有 market/daily 这个文件夹）。")
        if market.resolve() == ql_config.get_settings().market_dir.resolve():
            raise QuantLabError("这就是现在正在用的行情目录，不需要导入。")
        if ql_data.get_progress().get("running"):
            raise ConflictError("正在下载行情，等它结束后再导入。")
        target = ql_config.get_settings().market_dir
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for name in _MARKET_ITEMS:
            src = market / name
            if src.is_dir():
                shutil.copytree(src, target / name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("*.tmp"))
                copied += sum(1 for _ in (target / name).iterdir())
            elif src.is_file():
                shutil.copy2(src, target / name)
                copied += 1
        ql_store.clear_panel_cache()
        self.clear_cache()
        return {"copied": copied, **self.status()}

    # ---------------- 求值 ----------------
    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _evaluate(self, block: Block, panel: Panel, params: Mapping[str, float] | None = None) -> np.ndarray:
        """同一份数据上，“公式 + 参数”相同的条件只算一次。"""
        t, n = panel.shape
        key = (str(panel.dates[0]), str(panel.dates[-1]), t, n, rendered_formula(block, params))
        owner = panel.close
        while owner.base is not None and isinstance(owner.base, np.ndarray):
            owner = owner.base
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] is owner:
                self._cache.move_to_end(key)
                return hit[1]
        out = evaluate_block(block, panel, params)
        with self._lock:
            self._cache[key] = (owner, out)
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return out

    def _signal(self, panel: Panel, conditions: Sequence[Mapping[str, Any]],
                universe: Mapping[str, Any] | None) -> np.ndarray:
        conds = enabled_conditions(conditions)
        if not conds:
            raise QuantLabError("至少要启用一个选股条件。")
        return combine_signals(panel, conds, Universe.from_dict(universe), evaluator=self._evaluate)

    # ---------------- 给页面用的内容 ----------------
    @staticmethod
    def blocks() -> list[dict[str, Any]]:
        return [b.to_dict() for b in all_blocks()]

    @staticmethod
    def parse(formula: str) -> dict[str, Any]:
        if not formula.strip():
            raise QuantLabError("请先粘贴公式内容。")
        result = split_formula(formula)
        return {
            "conditions": [{"name": c.name, "description": c.description, "formula": c.formula,
                            "params": [p.to_dict() for p in c.params]} for c in result.conditions],
            "universe": result.universe.to_dict(),
            "warnings": list(result.warnings),
        }

    def stats(self, conditions: Sequence[Mapping[str, Any]], universe: Mapping[str, Any] | None,
              start: str | None = None, end: str | None = None) -> dict[str, Any]:
        panel = self.panel(start, end)
        signal = self._signal(panel, conditions, universe)
        return {
            "frequency": signal_frequency(signal, panel).__dict__,
            "stats": [r.__dict__ for r in signal_stats(signal, panel)],
            "range": {"start": str(panel.dates[0]), "end": str(panel.dates[-1])},
            "st_known": panel.st_known,
        }

    def signals(self, conditions: Sequence[Mapping[str, Any]], universe: Mapping[str, Any] | None,
                limit: int, offset: int, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        panel = self.panel(start, end)
        signal = self._signal(panel, conditions, universe)
        total, records = signal_list(signal, panel, limit=limit, offset=offset)
        names = self.stock_names()
        return {"total": total, "items": [
            {"date": r.date, "code": r.code, "name": names.get(r.code, ""),
             "returns": {str(h): v for h, v in r.returns.items()}} for r in records]}

    def picks(self, conditions: Sequence[Mapping[str, Any]], universe: Mapping[str, Any] | None) -> dict[str, Any]:
        """最新一个交易日收盘后，哪些股票满足条件。"""
        panel = self.panel()
        signal = self._signal(panel, conditions, universe)
        cols = np.nonzero(signal[-1])[0]
        names = self.stock_names()
        prev = panel.raw_preclose[-1]
        items = []
        for c in cols:
            code = str(panel.codes[c])
            close = float(panel.raw_close[-1, c])
            change = close / float(prev[c]) - 1 if np.isfinite(prev[c]) and prev[c] > 0 else None
            cap = float(panel.raw_close[-1, c] * panel.float_shares[-1, c])
            items.append({"code": code, "name": names.get(code, ""), "close": round(close, 3), "change": change,
                          "float_cap": cap if np.isfinite(cap) else None, "industry": str(panel.industry[c])})
        return {"date": str(panel.dates[-1]), "items": items}


def _day(text: str | None, label: str) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise QuantLabError(f"{label}日期的格式应为 2024-06-03 这样，收到的是：{text}") from e
