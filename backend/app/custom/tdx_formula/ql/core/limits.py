"""涨跌停规则。比例见技术设计文档 §3。"""

from __future__ import annotations

import numpy as np

from app.custom.tdx_formula.ql.core import codes as codes_mod
from app.custom.tdx_formula.ql.core.panel import Panel

_CHINEXT_20PCT_FROM = np.datetime64("2020-08-24", "D")


def limit_ratio(board: str, is_st: bool, day: np.datetime64) -> float:
    if board == codes_mod.BSE:
        return 0.30
    if board == codes_mod.STAR:
        return 0.20
    if board == codes_mod.CHINEXT:
        if day >= _CHINEXT_20PCT_FROM:
            return 0.20
        return 0.05 if is_st else 0.10
    return 0.05 if is_st else 0.10


def round_half_up(x: np.ndarray, digits: int = 2) -> np.ndarray:
    scale = 10**digits
    return np.floor(np.asarray(x, dtype=float) * scale + 0.5 + 1e-9) / scale


def limit_up_price(preclose: np.ndarray, ratio: np.ndarray | float) -> np.ndarray:
    return round_half_up(np.asarray(preclose, dtype=float) * (1 + ratio))


def limit_down_price(preclose: np.ndarray, ratio: np.ndarray | float) -> np.ndarray:
    return round_half_up(np.asarray(preclose, dtype=float) * (1 - ratio))


def ratio_matrix(panel: Panel) -> np.ndarray:
    """(T, N) 每个格子的涨跌幅比例。"""
    t, n = panel.shape
    out = np.empty((t, n), dtype=float)
    after = panel.dates >= _CHINEXT_20PCT_FROM
    for j, code in enumerate(panel.codes):
        board = codes_mod.board_of(str(code))
        st = panel.is_st[:, j]
        if board == codes_mod.BSE:
            out[:, j] = 0.30
        elif board == codes_mod.STAR:
            out[:, j] = 0.20
        elif board == codes_mod.CHINEXT:
            out[:, j] = np.where(after, 0.20, np.where(st, 0.05, 0.10))
        else:
            out[:, j] = np.where(st, 0.05, 0.10)
    return out


def limit_prices(panel: Panel) -> tuple[np.ndarray, np.ndarray]:
    """返回 (涨停价, 跌停价)，均为 (T, N)，不复权。没有前收盘价的格子为 NaN。"""
    ratio = ratio_matrix(panel)
    return limit_up_price(panel.raw_preclose, ratio), limit_down_price(panel.raw_preclose, ratio)


def _eq(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.abs(a - b) < 0.005


def is_limit_up_close(panel: Panel) -> np.ndarray:
    up, _ = limit_prices(panel)
    return _eq(panel.raw_close, up) & panel.tradable


def is_limit_down_close(panel: Panel) -> np.ndarray:
    _, down = limit_prices(panel)
    return _eq(panel.raw_close, down) & panel.tradable


def opens_at_limit_down(panel: Panel) -> np.ndarray:
    _, down = limit_prices(panel)
    with np.errstate(invalid="ignore"):
        return (panel.raw_open <= down + 0.005) & panel.tradable


def opens_at_limit_up(panel: Panel) -> np.ndarray:
    up, _ = limit_prices(panel)
    with np.errstate(invalid="ignore"):
        return (panel.raw_open >= up - 0.005) & panel.tradable
