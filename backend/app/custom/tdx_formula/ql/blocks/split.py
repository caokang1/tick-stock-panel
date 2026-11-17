"""把粘贴的通达信公式拆成几个可勾选、可调参数的条件。规则见技术设计文档 §5.3。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.custom.tdx_formula.ql.blocks.builtin import BUILTIN_BLOCKS
from app.custom.tdx_formula.ql.blocks.model import (
    KIND_CONDITION,
    SOURCE_PASTED,
    Block,
    ParamSpec,
    Universe,
    rendered_formula,
)
from app.custom.tdx_formula.ql.core.errors import FormulaError
from app.custom.tdx_formula.ql.tdx import analyze, parse
from app.custom.tdx_formula.ql.tdx import inspect as tinspect
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
from app.custom.tdx_formula.ql.tdx.special import MSG_DRAWING_ONLY, lookahead_error

_ALIASES = {"CLOSE": "C", "OPEN": "O", "HIGH": "H", "LOW": "L", "VOL": "V", "AMO": "AMOUNT"}


@dataclass(frozen=True)
class SplitResult:
    conditions: tuple[Block, ...]
    universe: Universe
    warnings: tuple[str, ...]


def _canonical(expr: Expr, program: Program, before: Statement | None, depth: int = 0) -> str:
    """把中间变量全部展开、数字抹掉后的规范写法，用来判断两个条件的结构是否相同。"""
    if depth > 50:
        return "?"
    if isinstance(expr, Number):
        return "#"
    if isinstance(expr, String):
        return repr(expr.value)
    if isinstance(expr, Ident):
        definition = tinspect.find_definition(program, expr.name, before=before)
        if definition is not None:
            return _canonical(definition.expr, program, definition, depth + 1)
        return _ALIASES.get(expr.key, expr.key)
    if isinstance(expr, Call):
        return expr.key + "(" + ",".join(_canonical(a, program, before, depth) for a in expr.args) + ")"
    if isinstance(expr, Unary):
        return expr.op + _canonical(expr.operand, program, before, depth)
    if isinstance(expr, BinOp):
        return "(" + _canonical(expr.left, program, before, depth) + expr.op + \
            _canonical(expr.right, program, before, depth) + ")"
    return "?"


@lru_cache(maxsize=1)
def _builtin_shapes() -> dict[str, Block]:
    shapes: dict[str, Block] = {}
    for block in BUILTIN_BLOCKS:
        if block.kind != KIND_CONDITION:
            continue
        program = parse(rendered_formula(block))
        last = program.statements[-1]
        shapes[_canonical(last.expr, program, last)] = block
    return shapes


def _strip_parens(text: str) -> str:
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for i, ch in enumerate(text):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(text) - 1:
                return text
        text = text[1:-1]
    return text


def _param_spec(key: str, lit: tinspect.NumericLiteral) -> ParamSpec:
    value = float(lit.value)
    if lit.in_period and lit.is_integer:
        return ParamSpec(key, f"周期（原值 {lit.text}）", value, 1.0, max(value * 3, 10.0), 1.0, "")
    lo, hi = sorted((0.0, value * 3))
    return ParamSpec(key, f"数值（原值 {lit.text}）", value, lo, hi, abs(value) / 100, "")


def _structural_numbers(statements: list[Statement], final: Expr) -> set[int]:
    """不值得做成可调参数的数字：REF 的回看天数（改了就不是原来的形态了）。"""
    skip: set[int] = set()
    for expr in [st.expr for st in statements] + [final]:
        for node in tinspect.walk(expr):
            if isinstance(node, Call) and node.key == "REF" and len(node.args) >= 2:
                skip.update(id(n) for n in tinspect.walk(node.args[1]) if isinstance(n, Number))
    return skip


def _parameterize(statements: list[Statement], final: Expr) -> tuple[str, list[ParamSpec]]:
    """把值得调的数字换成 {p1}、{p2}……。数字 0 和 REF 的回看天数保持原样。"""
    skip = _structural_numbers(statements, final)
    placeholders: dict[Number, str] = {}
    specs: list[ParamSpec] = []
    for lit in tinspect.numeric_literals([*statements, final]):
        if float(lit.value) == 0 or id(lit.node) in skip:
            continue
        key = f"p{len(specs) + 1}"
        placeholders[lit.node] = key
        specs.append(_param_spec(key, lit))
    return tinspect.render_statements(statements, placeholders, final=final), specs


_LOGIC_OPS = frozenset({"AND", "OR"})
_COMPARE_OPS = frozenset({">", "<", ">=", "<=", "=", "<>"})
_POOL_FUNCS = frozenset({"NAMELIKE", "CODELIKE", "DYNAINFO", "FINANCE", "INBLOCK"})


def _is_pure_pool(expr: Expr, program: Program, ctx: Statement | None, depth: int = 0) -> bool:
    """这个判断是不是只由股票池函数和逻辑运算组成（不含任何技术条件）。"""
    if depth > 50:
        return False
    if isinstance(expr, Number | String):
        return True
    if isinstance(expr, Ident):
        definition = tinspect.find_definition(program, expr.name, before=ctx)
        return definition is not None and _is_pure_pool(definition.expr, program, definition, depth + 1)
    if isinstance(expr, Call):
        if expr.key in _POOL_FUNCS:
            return True
        return expr.key == "NOT" and all(_is_pure_pool(a, program, ctx, depth) for a in expr.args)
    if isinstance(expr, Unary):
        return _is_pure_pool(expr.operand, program, ctx, depth)
    if isinstance(expr, BinOp):
        return expr.op in (_LOGIC_OPS | _COMPARE_OPS) and _is_pure_pool(expr.left, program, ctx, depth) \
            and _is_pure_pool(expr.right, program, ctx, depth)
    return False


def _apply_pool(expr: Expr, program: Program, ctx: Statement | None, negated: bool, flags: dict[str, bool],
                warnings: list[str], depth: int = 0) -> None:
    """把股票池写法转成股票范围设置。只认“排除某类股票”的写法；认不出的写进 warnings 并忽略。"""
    if depth > 50:
        return
    if isinstance(expr, Ident):
        definition = tinspect.find_definition(program, expr.name, before=ctx)
        if definition is not None:
            _apply_pool(definition.expr, program, definition, negated, flags, warnings, depth + 1)
        return
    if isinstance(expr, Call) and expr.key == "NOT" and expr.args:
        _apply_pool(expr.args[0], program, ctx, not negated, flags, warnings, depth)
        return
    if isinstance(expr, BinOp) and expr.op in _LOGIC_OPS:
        _apply_pool(expr.left, program, ctx, negated, flags, warnings, depth)
        _apply_pool(expr.right, program, ctx, negated, flags, warnings, depth)
        return

    shown = tinspect.render_expr(expr)
    if isinstance(expr, BinOp) and expr.op in _COMPARE_OPS:
        left, right = expr.left, expr.right
        is_open_positive = (
            isinstance(left, Call) and left.key == "DYNAINFO" and left.args and isinstance(left.args[0], Number)
            and int(left.args[0].value) == 4 and isinstance(right, Number) and right.value == 0
            and expr.op in (">", "<>")
        )
        if is_open_positive and not negated:
            flags["exclude_suspended"] = True
            return
        warnings.append(f"公式里的股票池写法 {shown} 暂时识别不了，已忽略。请在“股票范围”里手动设置。")
        return
    if isinstance(expr, Call) and expr.key in ("NAMELIKE", "CODELIKE"):
        arg = expr.args[0] if expr.args else None
        text = arg.value if isinstance(arg, String) else None
        flag: str | None = None
        if expr.key == "NAMELIKE" and text is not None and "ST" in text.upper():
            flag = "exclude_st"
        elif expr.key == "CODELIKE" and text is not None and text.startswith(("688", "689")):
            flag = "exclude_star"
        elif expr.key == "CODELIKE" and text is not None and text.startswith("30"):
            flag = "exclude_chinext"
        elif expr.key == "CODELIKE" and text is not None and text.startswith(("8", "4", "92")):
            flag = "exclude_bse"
        if flag is not None and negated:
            flags[flag] = True
            return
        if flag is not None:
            warnings.append(
                f"公式里的 {shown} 是“只选这一类股票”的写法，本工具的股票范围目前只支持“排除某类股票”，已忽略这一项。"
            )
            return
    warnings.append(f"公式里的股票池写法 {shown} 暂时识别不了，已忽略。请在“股票范围”里手动设置。")


def _classify_terms(program: Program, out: Statement) -> list[tuple[Expr, Statement]]:
    """把最后一个输出沿 AND 拆开，分出技术条件和股票池条件。

    一个判断里同时有技术条件和股票池函数时：如果它本身是用 AND 连起来的，就继续往里拆；
    拆不开的直接报错，不能悄悄丢掉其中的技术条件。
    """
    result: list[tuple[Expr, Statement]] = []
    pending: list[tuple[Expr, Statement]] = [(t, out) for t in tinspect.split_top_level_and(out.expr)]
    guard = 0
    while pending:
        guard += 1
        if guard > 500:
            raise FormulaError("公式的条件嵌套太深，无法拆分。", kind="unsupported")
        term, ctx = pending.pop(0)
        if not tinspect.uses_pool_functions(program, term, before=ctx) or _is_pure_pool(term, program, ctx):
            result.append((term, ctx))
            continue
        inner, inner_ctx = term, ctx
        if isinstance(term, Ident):
            definition = tinspect.find_definition(program, term.name, before=ctx)
            if definition is not None:
                inner, inner_ctx = definition.expr, definition
        parts = tinspect.split_top_level_and(inner)
        if len(parts) > 1:
            pending = [(p, inner_ctx) for p in parts] + pending
            continue
        raise FormulaError(
            f"公式里有一个判断把股票池条件（如 NAMELIKE、CODELIKE）和技术条件混在了一起：{tinspect.render_expr(term)}。"
            "本工具需要把两者分开处理，请把它改写成用 AND 连接的两部分，例如“技术条件 AND NOT(NAMELIKE('ST'))”。",
            kind="unsupported",
        )
    return result


def split_formula(text: str) -> SplitResult:
    program = parse(text)
    analysis = analyze(program)
    if analysis.lookahead:
        raise lookahead_error(list(analysis.lookahead))
    if analysis.unsupported:
        names = "、".join(analysis.unsupported)
        raise FormulaError(
            f"公式里用到了暂不支持的函数：{names}。", kind="unsupported", names=list(analysis.unsupported)
        )

    out = tinspect.last_boolean_output(program)
    if out is None:
        how = ("请在公式最后加一行选股条件，写成“XG:条件;”，条件里可以直接用公式前面定义的名字，"
               "例如 XG:C>MA(C,5); 。指标里画图语句的第一个参数（什么时候画）通常就是现成的条件。")
        if analysis.drawing_only:
            raise FormulaError(f"{MSG_DRAWING_ONLY}，无法用来选股。{how}", kind="unsupported")
        raise FormulaError(
            f"这条公式只输出数值（是一条指标公式），没有“成立或不成立”的选股条件，无法用来选股。{how}",
            kind="unsupported",
        )

    warnings: list[str] = []
    flags = {"exclude_st": False, "exclude_star": False, "exclude_chinext": False, "exclude_bse": False,
             "exclude_suspended": True}
    conditions: list[Block] = []
    shapes = _builtin_shapes()

    for term, ctx in _classify_terms(program, out):
        if tinspect.uses_pool_functions(program, term, before=ctx):
            _apply_pool(term, program, ctx, False, flags, warnings)
            continue
        index = len(conditions) + 1
        deps = tinspect.dependency_closure(program, term, before=ctx)
        template, specs = _parameterize(deps, term)
        name = term.name if isinstance(term, Ident) else f"条件{index}"
        matched = shapes.get(_canonical(term, program, ctx))
        if matched is not None:
            description = matched.description
        else:
            own = tinspect.find_definition(program, term.name, before=ctx) if isinstance(term, Ident) else None
            shown = tinspect.render_expr(own.expr if own is not None else term)
            description = "按公式计算：" + _strip_parens(shown)
        conditions.append(Block(
            id=f"pasted_{index}", kind=KIND_CONDITION, name=name, description=description,
            formula=template if template.rstrip().endswith(";") else template.rstrip() + ";",
            params=tuple(specs), source=SOURCE_PASTED,
        ))

    if not conditions:
        raise FormulaError("公式里只有股票池过滤，没有技术条件，无法用来选股。", kind="unsupported")
    if analysis.drawing_count:
        warnings.append(f"公式里有 {analysis.drawing_count} 条画图语句，对选股没有影响，已忽略。")
    return SplitResult(tuple(conditions), Universe.from_dict(flags), tuple(warnings))
