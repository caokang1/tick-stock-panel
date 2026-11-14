"""公式的静态检查（analyze）和求值（evaluate）。见技术设计文档 §4.2。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from app.custom.tdx_formula.ql.core.errors import FormulaError
from app.custom.tdx_formula.ql.core.panel import Panel
from app.custom.tdx_formula.ql.tdx.ast import (
    BinOp,
    Call,
    Expr,
    Ident,
    Number,
    Program,
    String,
    Unary,
)
from app.custom.tdx_formula.ql.tdx.funcs import as_bool, as_float
from app.custom.tdx_formula.ql.tdx.inspect import (
    DEFAULT_BOOL_NAMES,
    is_boolean_expr,
    is_drawing_statement,
    output_labels,
    walk,
)
from app.custom.tdx_formula.ql.tdx.registry import BUILTIN_SERIES, FUNCTIONS
from app.custom.tdx_formula.ql.tdx.special import (
    LOOKAHEAD_FUNCS,
    MSG_DRAWING_ONLY,
    POOL_FUNCS,
    is_drawing_func,
    lookahead_error,
)


@dataclass(frozen=True)
class Analysis:
    """analyze 的结果。名字一律大写（输出名保持原文写法）。"""

    functions: frozenset[str]  # 用到的全部函数名
    outputs: tuple[str, ...]  # 输出名，按顺序；不含画图语句
    boolean_outputs: tuple[str, ...]  # 其中结果是条件的输出名
    lookahead: tuple[str, ...]  # 用到的未来函数
    unsupported: tuple[str, ...]  # 用到的不支持的函数（画图语句里的不算）
    drawing_count: int  # 画图语句的数量
    pool_functions: tuple[str, ...]  # 用到的股票池函数
    pool_variables: tuple[str, ...]  # 定义里直接或间接用到股票池函数的名字（原文写法）

    @property
    def drawing_only(self) -> bool:
        """只有画图输出、没有选股条件。此时应提示 special.MSG_DRAWING_ONLY。"""
        return self.drawing_count > 0 and not self.boolean_outputs


def analyze(program: Program) -> Analysis:
    """不求值，只做静态检查。不抛异常。"""
    defined = {stmt.key for stmt in program.statements if stmt.key is not None}
    functions: set[str] = set()
    lookahead: set[str] = set()
    unsupported: set[str] = set()
    pool: set[str] = set()
    tainted: dict[str, bool] = {}  # 名字 → 定义是否涉及股票池函数
    pool_variables: list[str] = []
    drawing_count = 0

    for stmt in program.statements:
        drawing = is_drawing_statement(stmt)
        uses_pool = False
        for node in walk(stmt.expr):
            if isinstance(node, Call):
                key = node.key
                functions.add(key)
                if key in LOOKAHEAD_FUNCS:
                    lookahead.add(key)
                elif drawing:
                    continue
                elif key in POOL_FUNCS:
                    pool.add(key)
                    uses_pool = True
                elif key not in FUNCTIONS:
                    unsupported.add(key)
            elif isinstance(node, Ident):
                if node.key in LOOKAHEAD_FUNCS and node.key not in defined:
                    lookahead.add(node.key)
                elif tainted.get(node.key, False):
                    uses_pool = True
        if drawing:
            drawing_count += 1
        if stmt.key is not None:
            tainted[stmt.key] = uses_pool and not drawing
            if uses_pool and not drawing and stmt.name is not None:
                pool_variables.append(stmt.name)

    labels = output_labels(program)
    boolean = [
        labels[stmt.index]
        for stmt in program.statements
        if stmt.index in labels and is_boolean_expr(stmt.expr, program, stmt, DEFAULT_BOOL_NAMES)
    ]
    return Analysis(
        functions=frozenset(functions),
        outputs=tuple(labels.values()),
        boolean_outputs=tuple(boolean),
        lookahead=tuple(sorted(lookahead)),
        unsupported=tuple(sorted(unsupported)),
        drawing_count=drawing_count,
        pool_functions=tuple(sorted(pool)),
        pool_variables=tuple(pool_variables),
    )


def ensure_evaluable(program: Program) -> Analysis:
    """evaluate 之前的检查。未来函数的判断优先于其他错误。"""
    analysis = analyze(program)
    if analysis.lookahead:
        raise lookahead_error(list(analysis.lookahead))
    if analysis.unsupported:
        names = list(analysis.unsupported)
        if any(is_drawing_func(n) for n in names):
            detail = "画图函数只能单独作为一条语句，不能参与计算"
        else:
            detail = "请改用其他写法"
        raise FormulaError(
            f"公式里用到了本工具还不支持的函数：{'、'.join(names)}。{detail}。", kind="unsupported", names=names
        )
    if analysis.pool_functions:
        names = list(analysis.pool_functions)
        raise FormulaError(
            f"公式里的股票池条件（{'、'.join(names)}）不通过公式计算，请去掉这部分，改在“股票范围”里设置。",
            kind="unsupported",
            names=names,
        )
    return analysis


class _Evaluator:
    def __init__(
        self,
        panel: Panel,
        params: Mapping[str, float] | None,
        extra_series: Mapping[str, np.ndarray] | None,
    ) -> None:
        self._panel = panel
        self._shape = panel.shape
        self._env: dict[str, np.ndarray] = {}
        self._ignored: set[str] = set()  # 画图语句的名字
        self._params = {k.upper(): float(v) for k, v in (params or {}).items()}
        self._extra: dict[str, np.ndarray] = {}
        for name, series in (extra_series or {}).items():
            arr = np.asarray(series)
            if arr.shape != self._shape:
                raise ValueError(f"extra_series[{name!r}] 的形状 {arr.shape} 与 Panel 的 {self._shape} 不一致")
            self._extra[name.upper()] = arr

    def run(self, program: Program) -> dict[str, np.ndarray]:
        labels = output_labels(program)
        tradable = self._panel.tradable
        result: dict[str, np.ndarray] = {}
        for stmt in program.statements:
            if is_drawing_statement(stmt):
                if stmt.key is not None:
                    self._ignored.add(stmt.key)
                    self._env.pop(stmt.key, None)
                continue
            value = self._eval(stmt.expr)
            if stmt.key is not None:
                self._env[stmt.key] = value
                self._ignored.discard(stmt.key)
            if stmt.index in labels:
                full = np.broadcast_to(value, self._shape)
                if full.dtype == np.bool_:
                    result[labels[stmt.index]] = full & tradable  # 未上市、停牌的格子不出信号
                else:
                    result[labels[stmt.index]] = np.array(full, dtype=np.float64)
        return result

    def _eval(self, node: Expr) -> np.ndarray:
        if isinstance(node, Number):
            return np.asarray(node.value, dtype=np.float64)
        if isinstance(node, Ident):
            return self._lookup(node)
        if isinstance(node, Unary):
            value = as_float(self._eval(node.operand))
            return -value if node.op == "-" else value
        if isinstance(node, BinOp):
            return self._binary(node)
        if isinstance(node, Call):
            return self._call(node)
        if isinstance(node, String):
            raise FormulaError(
                f"第 {node.line} 行写法有误：文字“{node.value}”不能参与计算", kind="syntax", line=node.line
            )
        raise TypeError(f"未知的语法树节点：{type(node).__name__}")

    def _lookup(self, node: Ident) -> np.ndarray:
        key = node.key
        if key in self._env:
            return self._env[key]
        if key in self._params:
            return np.asarray(self._params[key], dtype=np.float64)
        if key in self._extra:
            return self._extra[key]
        if key in BUILTIN_SERIES:
            return np.asarray(getattr(self._panel, BUILTIN_SERIES[key]))
        if key in self._ignored:
            raise FormulaError(
                f"第 {node.line} 行用到了“{node.name}”，但它是一条画图语句，没有可以计算的结果。",
                kind="unsupported", names=[node.name], line=node.line,
            )  # fmt: skip
        raise FormulaError(
            f"第 {node.line} 行用到了没有定义的名字“{node.name}”。请检查是否写错，或者它的定义是否写在了后面。",
            kind="syntax", names=[node.name], line=node.line,
        )  # fmt: skip

    def _binary(self, node: BinOp) -> np.ndarray:
        left, right = self._eval(node.left), self._eval(node.right)
        op = node.op
        if op == "AND":
            return as_bool(left) & as_bool(right)
        if op == "OR":
            return as_bool(left) | as_bool(right)
        a, b = as_float(left), as_float(right)
        with np.errstate(all="ignore"):
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return a / np.where(b == 0, np.nan, b)  # 除以 0 的结果是 NaN
            # 比较：涉及 NaN 一律为 False
            if op == ">":
                return a > b
            if op == "<":
                return a < b
            if op == ">=":
                return a >= b
            if op == "<=":
                return a <= b
            if op == "=":
                return a == b
            if op == "<>":
                return (a != b) & ~(np.isnan(a) | np.isnan(b))
        raise TypeError(f"未知的运算符：{op}")

    def _call(self, node: Call) -> np.ndarray:
        spec = FUNCTIONS[node.key]  # ensure_evaluable 已保证函数存在
        if len(node.args) != len(spec.args):
            raise FormulaError(
                f"第 {node.line} 行写法有误：函数 {node.key} 要 {len(spec.args)} 个参数，这里写了 {len(node.args)} 个",
                kind="syntax", names=[node.key], line=node.line,
            )  # fmt: skip
        args = []
        for kind, arg in zip(spec.args, node.args, strict=True):
            value = self._eval(arg)
            if kind == "s" and value.ndim == 0:
                value = np.broadcast_to(value, self._shape)
            args.append(value)
        try:
            return np.asarray(spec.fn(*args))
        except FormulaError as err:
            if err.line is None:
                err.line = node.line
                err.message = f"第 {node.line} 行：{err.message}"
                err.args = (err.message,)
            raise


def evaluate(
    program: Program,
    panel: Panel,
    params: Mapping[str, float] | None = None,
    extra_series: Mapping[str, np.ndarray] | None = None,
) -> dict[str, np.ndarray]:
    """对公式求值，返回 {输出名: (T, N) 数组}，顺序与公式里的输出语句一致。

    - 布尔输出是 bool 数组，并且在未上市、停牌的格子上强制为 False；数值输出是 float64 数组。
    - 没有名字的输出（裸表达式）叫"输出1""输出2"……，按它是第几个输出编号。
    - params：公式里可以直接使用的命名参数，如 {"N": 12}。
    - extra_series：额外注入的内置序列，如 {"ISLIMITUP": (T, N) bool 数组}。名字大小写不敏感。
    - 名字的查找顺序：公式里定义的变量 → params → extra_series → 内置序列（C、O、H、L、V、AMOUNT）。
    - 求值前先做静态检查：含未来函数抛 kind="lookahead"（优先于其他错误）；
      含不支持的函数或股票池函数（NAMELIKE 等）抛 kind="unsupported"。画图语句整体忽略。
    """
    ensure_evaluable(program)
    return _Evaluator(panel, params, extra_series).run(program)


__all__ = ["MSG_DRAWING_ONLY", "Analysis", "analyze", "ensure_evaluable", "evaluate"]
