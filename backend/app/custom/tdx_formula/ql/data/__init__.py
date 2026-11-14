"""带过来的代码里读行情的几个入口。实际都是读 quant-lab 的行情目录（只读），目录由扩展在用之前指定。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql import loader
from app.custom.tdx_formula.ql.core.errors import DataError

_market: Path | None = None


def set_market_dir(path: Path | None) -> None:
    global _market
    _market = Path(path) if path is not None else None


def market_dir() -> Path:
    if _market is None:
        raise DataError("还没有找到 quant-lab 的行情数据。请先设置行情目录。")
    return _market


def read_calendar() -> np.ndarray:
    return loader.read_calendar(_market) if _market is not None else np.array([], dtype="datetime64[D]")


def read_securities() -> Any:
    return loader.read_securities(market_dir())


def read_index(code: str) -> Any:
    return loader.read_index(market_dir(), code)
