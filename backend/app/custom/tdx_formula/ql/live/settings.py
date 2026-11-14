"""可以调整的运行设置：交易成本、模拟账户的初始资金。每一项都有允许的范围。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql.backtest.signals import Costs
from app.custom.tdx_formula.ql.core.errors import QuantLabError
from app.custom.tdx_formula.ql.live import store

KEY_COSTS = "trade_costs"
KEY_INITIAL_CASH = "initial_cash"
DEFAULT_INITIAL_CASH = 1_000_000.0

# 每项成本允许的范围：（下限，上限，名称）。上限取得比实际情况宽，但不允许离谱的数。
COST_LIMITS: dict[str, tuple[float, float, str]] = {
    "commission": (0.0, 0.003, "佣金比例"),
    "min_commission": (0.0, 50.0, "单笔最低佣金"),
    "stamp_tax": (0.0, 0.003, "卖出印花税"),
    "slippage": (0.0, 0.01, "滑点"),
}


def costs() -> Costs:
    saved = store.get_setting(KEY_COSTS, {}) or {}
    base = Costs()
    return Costs(**{k: float(saved.get(k, getattr(base, k))) for k in COST_LIMITS})


def costs_dict() -> dict[str, float]:
    c = costs()
    return {k: getattr(c, k) for k in COST_LIMITS}


def set_costs(changes: Mapping[str, Any]) -> None:
    current = costs_dict()
    for key, raw in changes.items():
        if key not in COST_LIMITS or raw is None:
            continue
        lo, hi, label = COST_LIMITS[key]
        value = float(raw)
        if not np.isfinite(value) or not lo <= value <= hi:
            shown = f"{lo:g} 到 {hi:g} 元" if key == "min_commission" else f"{lo * 100:g}% 到 {hi * 100:g}%"
            raise QuantLabError(f"{label}要在 {shown} 之间。")
        current[key] = value
    store.set_setting(KEY_COSTS, current)


def initial_cash() -> float:
    return float(store.get_setting(KEY_INITIAL_CASH, DEFAULT_INITIAL_CASH))


def set_initial_cash(value: Any) -> None:
    cash = float(value)
    if not np.isfinite(cash) or not 10_000 <= cash <= 1e10:
        raise QuantLabError("模拟账户初始资金要在 1 万到 100 亿之间。")
    store.set_setting(KEY_INITIAL_CASH, cash)
