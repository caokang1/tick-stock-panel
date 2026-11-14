"""公式里可以使用的函数登记表：函数名 → 实现、参数种类、结果是不是条件。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.custom.tdx_formula.ql.tdx import funcs as f


@dataclass(frozen=True)
class FuncSpec:
    fn: Callable[..., Any]
    args: str  # 每个参数一个字符："s" 序列，"e" 逐格计算（数字或序列），"p" 周期类参数
    returns_bool: bool = False

    @property
    def period_positions(self) -> tuple[int, ...]:
        return tuple(i for i, kind in enumerate(self.args) if kind == "p")


FUNCTIONS: dict[str, FuncSpec] = {
    "REF": FuncSpec(f.REF, "sp"),
    "MA": FuncSpec(f.MA, "sp"),
    "EMA": FuncSpec(f.EMA, "sp"),
    "SMA": FuncSpec(f.SMA, "spp"),
    "WMA": FuncSpec(f.WMA, "sp"),
    "HHV": FuncSpec(f.HHV, "sp"),
    "LLV": FuncSpec(f.LLV, "sp"),
    "HHVBARS": FuncSpec(f.HHVBARS, "sp"),
    "LLVBARS": FuncSpec(f.LLVBARS, "sp"),
    "SUM": FuncSpec(f.SUM, "sp"),
    "COUNT": FuncSpec(f.COUNT, "sp"),
    "STD": FuncSpec(f.STD, "sp"),
    "AVEDEV": FuncSpec(f.AVEDEV, "sp"),
    "SLOPE": FuncSpec(f.SLOPE, "sp"),
    "EXIST": FuncSpec(f.EXIST, "sp", True),
    "EVERY": FuncSpec(f.EVERY, "sp", True),
    "CROSS": FuncSpec(f.CROSS, "ss", True),
    "LONGCROSS": FuncSpec(f.LONGCROSS, "ssp", True),
    "BARSLAST": FuncSpec(f.BARSLAST, "s"),
    "BARSSINCEN": FuncSpec(f.BARSSINCEN, "sp"),
    "FILTER": FuncSpec(f.FILTER, "sp", True),
    "LAST": FuncSpec(f.LAST, "spp", True),
    "IF": FuncSpec(f.IF, "eee"),
    "NOT": FuncSpec(f.NOT, "e", True),
    "BETWEEN": FuncSpec(f.BETWEEN, "eee", True),
    "RANGE": FuncSpec(f.RANGE, "eee", True),
    "UPNDAY": FuncSpec(f.UPNDAY, "sp", True),
    "DOWNNDAY": FuncSpec(f.DOWNNDAY, "sp", True),
    "NDAY": FuncSpec(f.NDAY, "ssp", True),
    "ABS": FuncSpec(f.ABS, "e"),
    "MAX": FuncSpec(f.MAX, "ee"),
    "MIN": FuncSpec(f.MIN, "ee"),
    "SQRT": FuncSpec(f.SQRT, "e"),
    "POW": FuncSpec(f.POW, "ee"),
    "LN": FuncSpec(f.LN, "e"),
    "LOG": FuncSpec(f.LOG, "e"),
    "ROUND": FuncSpec(f.ROUND, "e"),
    "INTPART": FuncSpec(f.INTPART, "e"),
    "MOD": FuncSpec(f.MOD, "ee"),
}

# 内置序列名 → Panel 的字段
BUILTIN_SERIES: dict[str, str] = {
    "C": "close", "CLOSE": "close",
    "O": "open", "OPEN": "open",
    "H": "high", "HIGH": "high",
    "L": "low", "LOW": "low",
    "V": "volume", "VOL": "volume",
    "AMOUNT": "amount", "AMO": "amount",
    # 流通股本（股，和这里的成交量单位一致，所以 V/CAPITAL 就是换手率）、流通市值（元）、换手率（%）。
    # 流通股本还没有补下载的日子是 NaN，和它比较的条件在那些日子不成立。
    "CAPITAL": "float_shares", "LTSZ": "float_cap", "HSL": "turnover",
}  # fmt: skip
