"""在语法树上做静态查看和改写的工具，供公式拆分（blocks/split.py）使用。全部不求值。

典型用法（把粘贴的公式拆成几个条件）::

    program = parse(text)
    out = last_boolean_output(program)                 # (a) 最后一个布尔输出语句
    for term in split_top_level_and(out.expr):         # (b) 沿顶层 AND 拆开
        deps = dependency_closure(program, term, before=out)   # (c) 依赖的中间变量定义，按原顺序
        template, literals = parameterize(deps, final=term)    # (d)(e) 数字换成 {p1}、{p2}……
        formula = render_template(template, {lit.key: lit.value for lit in literals})
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from app.custom.tdx_formula.ql.tdx.ast import (
    BinOp,
    Call,
    Expr,
    Ident,
    Number,
    Program,
    Statement,
    String,
    Unary,
)
from app.custom.tdx_formula.ql.tdx.registry import FUNCTIONS
from app.custom.tdx_formula.ql.tdx.special import POOL_FUNCS, is_drawing_func

# 由调用方注入、结果是条件的内置序列（见技术设计文档 §5.1）
DEFAULT_BOOL_NAMES: frozenset[str] = frozenset({"ISLIMITUP", "ISLIMITDOWN"})

_BOOL_POOL_FUNCS = frozenset({"NAMELIKE", "CODELIKE", "INBLOCK"})
_COMPARE_OPS = frozenset({">", "<", ">=", "<=", "=", "<>"})


# ---------------------------------------------------------------- 遍历


def walk(expr: Expr) -> Iterator[Expr]:
    """按原文从左到右的顺序，依次给出表达式里的每个节点（包括它自己）。"""
    yield expr
    if isinstance(expr, Call):
        for arg in expr.args:
            yield from walk(arg)
    elif isinstance(expr, Unary):
        yield from walk(expr.operand)
    elif isinstance(expr, BinOp):
        yield from walk(expr.left)
        yield from walk(expr.right)


def called_functions(expr: Expr) -> set[str]:
    """表达式里直接调用的函数名（大写）。"""
    return {node.key for node in walk(expr) if isinstance(node, Call)}


def referenced_names(expr: Expr) -> set[str]:
    """表达式里直接出现的名字（大写），不含函数名。"""
    return {node.key for node in walk(expr) if isinstance(node, Ident)}


def is_drawing_statement(statement: Statement) -> bool:
    """整条语句是不是一个画图函数调用（求值时整体忽略）。"""
    return isinstance(statement.expr, Call) and is_drawing_func(statement.expr.name)


def live_statements(program: Program) -> frozenset[int]:
    """真正参与结果的语句（按位置）：非画图的输出语句，以及它们直接或间接用到的定义。

    指标公式里常有一大段只给画图用的中间变量（提示文字、颜色条件等）。这些语句不影响任何输出，
    所以不求值，里面用到的不支持的函数、未来函数也不算数。
    """
    live: set[int] = set()
    needed: set[str] = set()
    for stmt in reversed(program.statements):
        if is_drawing_statement(stmt):
            continue
        wanted = stmt.is_output or (stmt.key is not None and stmt.key in needed)
        if not wanted:
            continue
        live.add(stmt.index)
        if stmt.key is not None:
            needed.discard(stmt.key)  # 后面用到的就是这一条定义；再往前的同名定义只供更早的语句用
        for node in walk(stmt.expr):
            if isinstance(node, Ident):
                needed.add(node.key)
    return frozenset(live)


def output_labels(program: Program) -> dict[int, str]:
    """{语句位置: 输出名}。只含非画图的输出语句；没有名字的输出叫"输出1""输出2"……（按输出的次序编号）。"""
    labels: dict[int, str] = {}
    for stmt in program.statements:
        if stmt.is_output and not is_drawing_statement(stmt):
            labels[stmt.index] = stmt.name if stmt.name is not None else f"输出{len(labels) + 1}"
    return labels


def find_definition(program: Program, name: str, before: Statement | None = None) -> Statement | None:
    """名字对应的定义语句（`名字:=…` 或 `名字:…`）。同名多次定义时取 before 之前最近的一条。"""
    limit = len(program.statements) if before is None else before.index
    key = name.upper()
    for stmt in reversed(program.statements[:limit]):
        if stmt.key == key:
            return stmt
    return None


# ---------------------------------------------------------------- (a) 最后一个布尔输出


def is_boolean_expr(
    expr: Expr,
    program: Program,
    before: Statement | None = None,
    extra_bool_names: Iterable[str] = DEFAULT_BOOL_NAMES,
) -> bool:
    """静态判断表达式的结果是不是条件（布尔）。与 evaluate 实际得到的数组类型一致。

    before：表达式所在的语句，用来确定名字指向哪一条定义；不给则按公式末尾算。
    """
    extra = {n.upper() for n in extra_bool_names}

    def check(node: Expr, limit: Statement | None) -> bool:
        if isinstance(node, BinOp):
            return node.op in ("AND", "OR") or node.op in _COMPARE_OPS
        if isinstance(node, Call):
            key = node.key
            if key in _BOOL_POOL_FUNCS:
                return True
            if key == "REF" and node.args:
                return check(node.args[0], limit)
            if key == "IF" and len(node.args) == 3:
                return check(node.args[1], limit) and check(node.args[2], limit)
            spec = FUNCTIONS.get(key)
            return spec is not None and spec.returns_bool
        if isinstance(node, Ident):
            definition = find_definition(program, node.name, limit)
            if definition is not None:
                return check(definition.expr, definition)
            return node.key in extra
        return False

    return check(expr, before)


def last_boolean_output(
    program: Program, extra_bool_names: Iterable[str] = DEFAULT_BOOL_NAMES
) -> Statement | None:
    """最后一个结果是条件的输出语句（画图语句不算）。没有则返回 None。"""
    for stmt in reversed(program.statements):
        if (stmt.is_output and not is_drawing_statement(stmt)
                and is_boolean_expr(stmt.expr, program, stmt, extra_bool_names)):
            return stmt
    return None


# ---------------------------------------------------------------- (b) 沿顶层 AND 拆开


def split_top_level_and(expr: Expr) -> list[Expr]:
    """把 `a AND b AND c` 拆成 [a, b, c]。括号只影响结合顺序的 AND 也会拆开；不是 AND 链时返回 [expr]。"""
    if isinstance(expr, BinOp) and expr.op == "AND":
        return split_top_level_and(expr.left) + split_top_level_and(expr.right)
    return [expr]


# ---------------------------------------------------------------- (c) 依赖闭包


def dependency_closure(program: Program, target: str | Expr, before: Statement | None = None) -> list[Statement]:
    """target 依赖的全部定义语句，按原公式里的顺序排列。

    target 是名字时：结果包含这个名字自己的定义，以及它直接或间接用到的定义。
    target 是表达式时：结果是表达式直接或间接用到的定义（不含表达式本身）。
    before：target 所在的语句，名字只会指向它之前的定义；不给则按公式末尾算。
    内置序列、参数等没有定义语句的名字不在结果里。
    """
    found: dict[int, Statement] = {}

    def visit_expr(expr: Expr, limit: Statement | None) -> None:
        for node in walk(expr):
            if isinstance(node, Ident):
                visit_name(node.name, limit)

    def visit_name(name: str, limit: Statement | None) -> None:
        definition = find_definition(program, name, limit)
        if definition is None or definition.index in found:
            return
        found[definition.index] = definition
        visit_expr(definition.expr, definition)

    if isinstance(target, str):
        visit_name(target, before)
    else:
        visit_expr(target, before)
    return [found[i] for i in sorted(found)]


def uses_pool_functions(program: Program, target: str | Expr, before: Statement | None = None) -> bool:
    """target 自己或它依赖的定义里，有没有用到股票池函数（NAMELIKE、CODELIKE、DYNAINFO 等）。"""
    exprs = [stmt.expr for stmt in dependency_closure(program, target, before)]
    if not isinstance(target, str):
        exprs.append(target)
    return any(called_functions(expr) & POOL_FUNCS for expr in exprs)


def pool_functions_used(program: Program, target: str | Expr, before: Statement | None = None) -> set[str]:
    """target 自己或它依赖的定义里用到了哪些股票池函数。"""
    exprs = [stmt.expr for stmt in dependency_closure(program, target, before)]
    if not isinstance(target, str):
        exprs.append(target)
    used: set[str] = set()
    for expr in exprs:
        used |= called_functions(expr) & POOL_FUNCS
    return used


# ---------------------------------------------------------------- (d) 数字字面量


@dataclass(frozen=True)
class NumericLiteral:
    key: str  # 按出现顺序编号：p1、p2……
    node: Number  # 语法树里的节点，可直接作为 render_* 的 placeholders 的键
    value: float
    text: str  # 原文写法
    line: int
    col: int
    statement_index: int | None  # 所在语句在原公式里的位置；来自单独传入的表达式时为 None
    in_period: bool  # 是否处在某个函数的周期参数位置（如 MA(C,5) 里的 5、HHV(H,2*3+1) 里的 2、3、1）
    is_integer: bool


def _literals_in(expr: Expr, in_period: bool) -> Iterator[tuple[Number, bool]]:
    if isinstance(expr, Number):
        yield expr, in_period
    elif isinstance(expr, Call):
        spec = FUNCTIONS.get(expr.key)
        positions = spec.period_positions if spec is not None else ()
        for i, arg in enumerate(expr.args):
            yield from _literals_in(arg, i in positions)
    elif isinstance(expr, Unary):
        yield from _literals_in(expr.operand, in_period)
    elif isinstance(expr, BinOp):
        yield from _literals_in(expr.left, in_period)
        yield from _literals_in(expr.right, in_period)


def numeric_literals(items: Iterable[Statement | Expr], prefix: str = "p") -> list[NumericLiteral]:
    """按顺序列出若干语句（或表达式）里的全部数字字面量。"""
    out: list[NumericLiteral] = []
    for item in items:
        expr, index = (item.expr, item.index) if isinstance(item, Statement) else (item, None)
        for node, in_period in _literals_in(expr, False):
            out.append(
                NumericLiteral(
                    key=f"{prefix}{len(out) + 1}", node=node, value=node.value, text=node.text,
                    line=node.line, col=node.col, statement_index=index,
                    in_period=in_period, is_integer=node.is_integer,
                )
            )  # fmt: skip
    return out


# ---------------------------------------------------------------- (e) 还原成公式文本

_PRECEDENCE = {"OR": 1, "AND": 2, "+": 4, "-": 4, "*": 5, "/": 5}
_UNARY_PRECEDENCE = 6


def _precedence(expr: Expr) -> int:
    if isinstance(expr, BinOp):
        return _PRECEDENCE.get(expr.op, 3)
    if isinstance(expr, Unary):
        return _UNARY_PRECEDENCE
    return 7


def render_expr(expr: Expr, placeholders: Mapping[Number, str] | None = None) -> str:
    """把表达式还原成公式文本。placeholders 里的数字节点写成 `{键}`，其余数字保持原文写法。"""

    def wrap(node: Expr, minimum: int) -> str:
        text = render(node)
        return f"({text})" if _precedence(node) < minimum else text

    def render(node: Expr) -> str:
        if isinstance(node, Number):
            if placeholders is not None and node in placeholders:
                return "{" + placeholders[node] + "}"
            return node.text
        if isinstance(node, String):
            quote = '"' if "'" in node.value else "'"
            return f"{quote}{node.value}{quote}"
        if isinstance(node, Ident):
            return node.name
        if isinstance(node, Call):
            return f"{node.name}({','.join(render(arg) for arg in node.args)})"
        if isinstance(node, Unary):
            return node.op + wrap(node.operand, _UNARY_PRECEDENCE)
        level = _precedence(node)
        op = f" {node.op} " if node.op in ("AND", "OR") else node.op
        return wrap(node.left, level) + op + wrap(node.right, level + 1)

    return render(expr)


def render_statement(statement: Statement, placeholders: Mapping[Number, str] | None = None) -> str:
    body = render_expr(statement.expr, placeholders)
    if statement.name is None:
        return f"{body};"
    return f"{statement.name}{':=' if statement.kind == 'assign' else ':'}{body};"


def render_statements(
    statements: Sequence[Statement],
    placeholders: Mapping[Number, str] | None = None,
    final: Expr | None = None,
) -> str:
    """把若干语句还原成公式文本，一条一行。final 不为空时在最后加一条裸表达式作为输出。注释和画图修饰不保留。"""
    lines = [render_statement(stmt, placeholders) for stmt in statements]
    if final is not None:
        lines.append(render_expr(final, placeholders) + ";")
    return "\n".join(lines)


def parameterize(
    statements: Sequence[Statement], final: Expr | None = None, prefix: str = "p"
) -> tuple[str, list[NumericLiteral]]:
    """把语句（加上可选的最后一条输出表达式）里的每个数字换成 `{p1}`、`{p2}`……

    返回 (公式模板, 数字字面量列表)。用各字面量的原值填回模板，得到与原公式等价的公式。
    只想替换其中一部分数字时，用 numeric_literals 挑选后自己调 render_statements。
    """
    items: list[Statement | Expr] = list(statements)
    if final is not None:
        items.append(final)
    literals = numeric_literals(items, prefix)
    template = render_statements(statements, {lit.node: lit.key for lit in literals}, final)
    return template, literals


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def format_number(value: float) -> str:
    """把数字写成公式里的样子：整数不带小数点。"""
    return str(np.format_float_positional(float(value), trim="-"))


def render_template(formula_template: str, params: Mapping[str, float]) -> str:
    """把公式模板里的 `{键}` 换成 params 里对应的数字。params 里没有的 `{…}` 原样保留（公式里它是注释）。"""

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        return format_number(params[key]) if key in params else match.group(0)

    return _PLACEHOLDER_RE.sub(fill, formula_template)
