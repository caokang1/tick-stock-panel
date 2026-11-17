"""三类特殊函数的名单和对应的提示语。见技术设计文档 §4.2。"""

from __future__ import annotations

from app.custom.tdx_formula.ql.core.errors import FormulaError

# 未来函数：会用到之后的数据，一律拒绝
LOOKAHEAD_FUNCS: frozenset[str] = frozenset(
    {"BACKSET", "BARSTATUS", "ZIG", "PEAK", "PEAKBARS", "TROUGH", "TROUGHBARS", "REFX", "REFXV", "FFT",
     # 下面这些把“最后一根 K 线”的情况搬到了历史上的每一天
     "CONST", "ISLASTBAR", "CURRBARSCOUNT", "TOTALBARSCOUNT", "BARSNEXT", "XMA"}
)

# 画图函数：整条语句忽略。名字以 DRAW 开头的也算
DRAWING_FUNCS: frozenset[str] = frozenset(
    {
        "DRAWLINE", "DRAWBAND", "DRAWKLINE", "DRAWTEXT", "DRAWICON", "STICKLINE",
        "POLYLINE", "PLOYLINE", "DRAWNUMBER", "FILLRGN",
    }
)  # fmt: skip

# 股票池函数：不通过公式求值，由"股票范围"设置实现
POOL_FUNCS: frozenset[str] = frozenset({"NAMELIKE", "CODELIKE", "DYNAINFO", "FINANCE", "INBLOCK"})

MSG_LOOKAHEAD = "这个公式会用到之后的数据，历史上看起来很准，但实盘时这些信号当时并不存在。"
MSG_DRAWING_ONLY = "这是画图指标，没有选股条件"


def is_drawing_func(name: str) -> bool:
    key = name.upper()
    return key in DRAWING_FUNCS or key.startswith("DRAW")


def lookahead_error(names: list[str]) -> FormulaError:
    return FormulaError(f"{MSG_LOOKAHEAD}（涉及的未来函数：{'、'.join(names)}）", kind="lookahead", names=list(names))
