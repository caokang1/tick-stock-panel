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
    panel_fn: bool = False  # 实现需要行情本身（如筹码分布要最高价、最低价、换手率）：第一个参数传 Panel

    @property
    def period_positions(self) -> tuple[int, ...]:
        return tuple(i for i, kind in enumerate(self.args) if kind == "p")


FUNCTIONS: dict[str, FuncSpec] = {
    "REF": FuncSpec(f.REF, "sp"),
    "MA": FuncSpec(f.MA, "sp"),
    "EMA": FuncSpec(f.EMA, "sp"),
    "SMA": FuncSpec(f.SMA, "spp"),
    "DMA": FuncSpec(f.DMA, "se"),
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
    # —— 下面是后来成批补上的 ——
    "EXP": FuncSpec(f.EXP, "e"),
    "SIN": FuncSpec(f.SIN, "e"),
    "COS": FuncSpec(f.COS, "e"),
    "TAN": FuncSpec(f.TAN, "e"),
    "ASIN": FuncSpec(f.ASIN, "e"),
    "ACOS": FuncSpec(f.ACOS, "e"),
    "ATAN": FuncSpec(f.ATAN, "e"),
    "CEILING": FuncSpec(f.CEILING, "e"),
    "FLOOR": FuncSpec(f.FLOOR, "e"),
    "SIGN": FuncSpec(f.SIGN, "e"),
    "SGN": FuncSpec(f.SIGN, "e"),
    "FRACPART": FuncSpec(f.FRACPART, "e"),
    "REVERSE": FuncSpec(f.REVERSE, "e"),
    "IFF": FuncSpec(f.IF, "eee"),
    "IFN": FuncSpec(f.IFN, "eee"),
    "REFV": FuncSpec(f.REF, "sp"),
    "EXPMA": FuncSpec(f.EMA, "sp"),
    "EXPMEMA": FuncSpec(f.EMA, "sp"),  # 通达信里它和 EMA 只在最开始几根的算法上不同
    "MEMA": FuncSpec(f.MEMA, "sp"),
    "STDP": FuncSpec(f.STDP, "sp"),
    "VAR": FuncSpec(f.VAR, "sp"),
    "VARP": FuncSpec(f.VARP, "sp"),
    "DEVSQ": FuncSpec(f.DEVSQ, "sp"),
    "FORCAST": FuncSpec(f.FORCAST, "sp"),
    "BARSCOUNT": FuncSpec(f.BARSCOUNT, "s"),
    "BARSSINCE": FuncSpec(f.BARSSINCE, "s"),
    "BARSLASTCOUNT": FuncSpec(f.BARSLASTCOUNT, "s"),
    "VALUEWHEN": FuncSpec(f.VALUEWHEN, "ss"),
    "SUMBARS": FuncSpec(f.SUMBARS, "se"),
    "EXISTR": FuncSpec(f.EXISTR, "spp", True),
    # 筹码分布（按换手率估算，和通达信的数值会有出入）
    "WINNER": FuncSpec(lambda panel, price: f.chip_distribution(panel.high, panel.low, panel.turnover, price, "winner"),
                       "e", panel_fn=True),
    "COST": FuncSpec(lambda panel, percent: f.chip_distribution(panel.high, panel.low, panel.turnover, percent, "cost"),
                     "e", panel_fn=True),
}

# 筹码函数 → chip_pass 里的问题类型。它们算起来慢，求值器会把同一条公式里的几个合在一趟里算
CHIP_FUNCTIONS: dict[str, str] = {"WINNER": "winner", "COST": "cost"}

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

# 日期类的内置序列：由交易日算出来，每一行所有股票相同
DATE_SERIES: frozenset[str] = frozenset({"DATE", "YEAR", "MONTH", "DAY", "WEEKDAY"})
# 大盘指数的行情：名字 → 指数日线里的列。沪市股票对上证指数，深市股票对深证成指
INDEX_SERIES: dict[str, str] = {"INDEXC": "close", "INDEXO": "open", "INDEXH": "high", "INDEXL": "low",
                                "INDEXV": "volume", "INDEXA": "amount"}
INDEX_BY_MARKET: dict[str, str] = {"sh": "sh.000001", "sz": "sz.399001"}
