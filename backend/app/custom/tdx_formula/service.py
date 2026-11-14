"""找到 quant-lab 的行情目录、读出全市场数据、带缓存地求值、解析公式、信号统计。"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from .ql import loader
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
from .ql.core.errors import DataError, QuantLabError
from .ql.core.panel import Panel

ENV_DATA_DIR = "TDX_FORMULA_DATA_DIR"
_CACHE_SIZE = 64


class FormulaService:
    """一个进程里只有一份。``root`` 是扩展自己的数据目录（保存设置和粘贴过的公式）。"""

    def __init__(self, root: Path, repo_root: Path) -> None:
        self._root = root
        self._repo_root = repo_root
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[Any, ...], tuple[np.ndarray, np.ndarray]] = OrderedDict()

    # ---------------- 行情目录 ----------------
    def _config_path(self) -> Path:
        return self._root / "config.json"

    def _read_config(self) -> dict[str, Any]:
        try:
            return json.loads(self._config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _market_of(path: Path) -> Path | None:
        """给的可以是 quant-lab 的 data 目录，也可以直接是其中的 market 目录。"""
        for candidate in (path, path / "market"):
            if (candidate / "daily").is_dir():
                return candidate
        return None

    def market_dir(self) -> tuple[Path | None, str]:
        """行情目录和它是怎么找到的。依次看：页面上设置的、环境变量、和本项目并排放着的 quant-lab。"""
        saved = str(self._read_config().get("data_dir") or "").strip()
        candidates = [
            (saved, "页面上设置的目录"),
            (os.environ.get(ENV_DATA_DIR, "").strip(), f"环境变量 {ENV_DATA_DIR}"),
            (str(self._repo_root.parent / "quant-lab" / "data"), "与本项目并排的 quant-lab"),
        ]
        for text, how in candidates:
            if text:
                found = self._market_of(Path(text).expanduser())
                if found is not None:
                    return found, how
        return None, ""

    def set_data_dir(self, text: str) -> None:
        text = text.strip()
        if text and self._market_of(Path(text).expanduser()) is None:
            raise QuantLabError(f"在 {text} 里没有找到 quant-lab 的行情数据（应该有 market/daily 这个文件夹）。")
        self._write_json(self._config_path(), {**self._read_config(), "data_dir": text})
        self.clear_cache()

    # ---------------- 全市场数据 ----------------
    def panel(self, start: str | None = None, end: str | None = None) -> Panel:
        market, _ = self.market_dir()
        if market is None:
            raise DataError("还没有找到 quant-lab 的行情数据。请先在本页设置行情目录。")
        panel = loader.build_panel(market)
        lo, hi = _day(start, "开始"), _day(end, "结束")
        if lo or hi:
            panel = panel.slice_dates(lo, hi)
        if panel.shape[0] == 0 or panel.shape[1] == 0:
            raise DataError("选定的时间范围内没有数据。")
        return panel

    def status(self) -> dict[str, Any]:
        market, how = self.market_dir()
        costs = Costs()
        out: dict[str, Any] = {
            "configured": market is not None, "data_dir": str(market) if market else "", "found_by": how,
            "saved_data_dir": str(self._read_config().get("data_dir") or ""),
            "has_data": False, "latest_date": None, "first_date": None, "stock_count": 0,
            "float_shares_ready": False,
            "costs": {"commission": costs.commission, "stamp_tax": costs.stamp_tax, "slippage": costs.slippage},
        }
        if market is None or loader.fingerprint(market) is None:
            return out
        panel = loader.build_panel(market)
        known = np.isfinite(panel.float_shares[-1][panel.listed[-1]])
        out.update(has_data=True, first_date=str(panel.dates[0]), latest_date=str(panel.dates[-1]),
                   stock_count=int(panel.shape[1]),
                   float_shares_ready=bool(known.size and known.mean() > 0.95))
        return out

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
        market, _ = self.market_dir()
        names = loader.stock_names(market) if market else {}
        return {"total": total, "items": [
            {"date": r.date, "code": r.code, "name": names.get(r.code, ""),
             "returns": {str(h): v for h, v in r.returns.items()}} for r in records]}

    def picks(self, conditions: Sequence[Mapping[str, Any]], universe: Mapping[str, Any] | None) -> dict[str, Any]:
        """最新一个交易日收盘后，哪些股票满足条件。"""
        panel = self.panel()
        signal = self._signal(panel, conditions, universe)
        cols = np.nonzero(signal[-1])[0]
        market, _ = self.market_dir()
        names = loader.stock_names(market) if market else {}
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
