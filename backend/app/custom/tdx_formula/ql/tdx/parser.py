"""通达信公式的词法分析和递归下降解析。语法见技术设计文档 §4.2。"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.custom.tdx_formula.ql.core.errors import FormulaError
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
from app.custom.tdx_formula.ql.tdx.special import LOOKAHEAD_FUNCS, lookahead_error

# 从中文输入法里粘贴出来的全角标点，按半角处理（字符串里的不动）
_FULLWIDTH = {"；": ";", "，": ",", "（": "(", "）": ")", "：": ":", "＞": ">", "＜": "<", "＝": "="}

_TWO_CHAR_OPS = {":=", ">=", "<=", "<>", "!=", "==", "&&", "||"}
_ONE_CHAR_OPS = set(";,()+-*/><=:")
_COMPARE = {">": ">", "<": "<", ">=": ">=", "<=": "<=", "=": "=", "==": "=", "<>": "<>", "!=": "<>"}


@dataclass(frozen=True)
class _Token:
    kind: str  # "num" "str" "id" "op" "eof"
    text: str
    line: int
    col: int


def _syntax_error(line: int, detail: str) -> FormulaError:
    return FormulaError(f"第 {line} 行写法有误：{detail}", kind="syntax", line=line)


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def _is_ident_part(ch: str) -> bool:
    return ch == "_" or ch.isalnum()


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, line, line_start, size = 0, 1, 0, len(text)
    while i < size:
        ch = _FULLWIDTH.get(text[i], text[i])
        col = i - line_start + 1
        if ch == "\n":
            line += 1
            line_start = i + 1
            i += 1
        elif ch.isspace() or ch == "﻿":
            i += 1
        elif ch == "{":  # 注释，可跨行
            end = text.find("}", i)
            if end < 0:
                raise _syntax_error(line, "注释的“{”没有对应的“}”")
            newlines = text.count("\n", i, end)
            if newlines:
                line += newlines
                line_start = text.rfind("\n", i, end) + 1
            i = end + 1
        elif text.startswith("//", i):  # 行尾注释
            end = text.find("\n", i)
            i = size if end < 0 else end
        elif ch in "'\"":
            end = text.find(text[i], i + 1)
            if end < 0 or "\n" in text[i:end]:
                raise _syntax_error(line, "文字的引号没有配对")
            tokens.append(_Token("str", text[i + 1 : end], line, col))
            i = end + 1
        elif ch.isdigit() or (ch == "." and i + 1 < size and text[i + 1].isdigit()):
            j = i
            while j < size and text[j].isdigit():
                j += 1
            if j < size and text[j] == ".":
                j += 1
                while j < size and text[j].isdigit():
                    j += 1
            tokens.append(_Token("num", text[i:j], line, col))
            i = j
        elif _is_ident_start(ch):
            j = i + 1
            while j < size and _is_ident_part(text[j]):
                j += 1
            tokens.append(_Token("id", text[i:j], line, col))
            i = j
        else:
            nxt = _FULLWIDTH.get(text[i + 1], text[i + 1]) if i + 1 < size else ""
            if ch + nxt in _TWO_CHAR_OPS:
                tokens.append(_Token("op", ch + nxt, line, col))
                i += 2
            elif ch in _ONE_CHAR_OPS:
                tokens.append(_Token("op", ch, line, col))
                i += 1
            else:
                raise _syntax_error(line, f"出现了不认识的符号“{text[i]}”")
    tokens.append(_Token("eof", "", line, size - line_start + 1))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    # ---- 基础 ----
    @property
    def _tok(self) -> _Token:
        return self._tokens[self._pos]

    def _advance(self) -> _Token:
        tok = self._tokens[self._pos]
        if tok.kind != "eof":
            self._pos += 1
        return tok

    def _is_op(self, *ops: str) -> bool:
        return self._tok.kind == "op" and self._tok.text in ops

    def _is_word(self, word: str) -> bool:
        return self._tok.kind == "id" and self._tok.text.upper() == word

    def _unexpected(self) -> FormulaError:
        tok = self._tok
        if tok.kind == "eof":
            return _syntax_error(tok.line, "公式在这里突然结束了，可能少了括号或后半句")
        return _syntax_error(tok.line, f"“{tok.text}”出现在不该出现的位置")

    # ---- 语句 ----
    def program(self, source: str) -> Program:
        statements: list[Statement] = []
        while self._tok.kind != "eof":
            if self._is_op(";"):
                self._advance()
                continue
            statements.append(self._statement(len(statements)))
            if self._tok.kind == "eof":
                break
            if not self._is_op(";"):
                tok = self._tok
                raise _syntax_error(tok.line, f"“{tok.text}”前面可能少了分号“;”或运算符")
        if not statements:
            raise FormulaError("公式是空的，没有可以计算的内容", kind="syntax", line=1)
        return Program(statements=tuple(statements), source=source)

    def _statement(self, index: int) -> Statement:
        first = self._tok
        name: str | None = None
        kind = "output"
        nxt = self._tokens[self._pos + 1] if self._pos + 1 < len(self._tokens) else first
        if first.kind == "id" and nxt.kind == "op" and nxt.text in (":=", ":"):
            name = first.text
            kind = "assign" if nxt.text == ":=" else "output"
            self._advance()
            self._advance()
        expr = self._or()
        if self._is_op(","):  # ,COLORRED ,LINETHICK2 之类的修饰，直接丢弃
            while self._tok.kind != "eof" and not self._is_op(";"):
                self._advance()
        return Statement(name=name, kind=kind, expr=expr, line=first.line, index=index)

    # ---- 表达式，优先级从低到高 ----
    def _or(self) -> Expr:
        left = self._and()
        while self._is_op("||") or self._is_word("OR"):
            tok = self._advance()
            left = BinOp("OR", left, self._and(), tok.line, tok.col)
        return left

    def _and(self) -> Expr:
        left = self._compare()
        while self._is_op("&&") or self._is_word("AND"):
            tok = self._advance()
            left = BinOp("AND", left, self._compare(), tok.line, tok.col)
        return left

    def _compare(self) -> Expr:
        left = self._additive()
        while self._tok.kind == "op" and self._tok.text in _COMPARE:
            tok = self._advance()
            left = BinOp(_COMPARE[tok.text], left, self._additive(), tok.line, tok.col)
        return left

    def _additive(self) -> Expr:
        left = self._multiplicative()
        while self._is_op("+", "-"):
            tok = self._advance()
            left = BinOp(tok.text, left, self._multiplicative(), tok.line, tok.col)
        return left

    def _multiplicative(self) -> Expr:
        left = self._unary()
        while self._is_op("*", "/"):
            tok = self._advance()
            left = BinOp(tok.text, left, self._unary(), tok.line, tok.col)
        return left

    def _unary(self) -> Expr:
        if self._is_op("-", "+"):
            tok = self._advance()
            return Unary(tok.text, self._unary(), tok.line, tok.col)
        return self._primary()

    def _primary(self) -> Expr:
        tok = self._tok
        if tok.kind == "num":
            self._advance()
            return Number(float(tok.text), tok.text, tok.line, tok.col)
        if tok.kind == "str":
            self._advance()
            return String(tok.text, tok.line, tok.col)
        if tok.kind == "id":
            if tok.text.upper() in ("AND", "OR"):
                raise self._unexpected()
            self._advance()
            if self._is_op("("):
                self._advance()
                return Call(tok.text, self._args(tok), tok.line, tok.col)
            return Ident(tok.text, tok.line, tok.col)
        if tok.kind == "op" and tok.text == "(":
            self._advance()
            inner = self._or()
            if not self._is_op(")"):
                raise _syntax_error(tok.line, "括号没有配对，少了“)”")
            self._advance()
            return inner
        raise self._unexpected()

    def _args(self, func: _Token) -> tuple[Expr, ...]:
        args: list[Expr] = []
        if self._is_op(")"):
            self._advance()
            return ()
        while True:
            args.append(self._or())
            if self._is_op(","):
                self._advance()
            elif self._is_op(")"):
                self._advance()
                return tuple(args)
            else:
                raise _syntax_error(func.line, f"函数 {func.text} 的括号没有配对，或参数之间少了逗号")


_COMMENT_RE = re.compile(r"\{[^}]*\}|//[^\n]*")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _scan_lookahead(text: str) -> list[str]:
    """不依赖解析结果，直接在文本里找未来函数的名字。"""
    words = {w.upper() for w in _WORD_RE.findall(_COMMENT_RE.sub(" ", text))}
    return sorted(words & LOOKAHEAD_FUNCS)


# DYNAINFO 取的是"当天的行情"。按日线回看时，当天的最高、最低、现价就是那一天的最高、最低、收盘，
# 所以直接换成对应的内置序列，这样它可以和技术条件写在一起。其余编号（如 DYNAINFO(4)>0）仍按股票池条件处理。
_QUOTE_SERIES = {5: "HIGH", 6: "LOW", 7: "CLOSE"}


def _rewrite_expr(expr: Expr) -> Expr:
    if isinstance(expr, Call):
        if expr.key == "DYNAINFO" and len(expr.args) == 1 and isinstance(expr.args[0], Number):
            series = _QUOTE_SERIES.get(int(expr.args[0].value)) if expr.args[0].is_integer else None
            if series is not None:
                return Ident(series, expr.line, expr.col)
        return replace(expr, args=tuple(_rewrite_expr(a) for a in expr.args))
    if isinstance(expr, Unary):
        return replace(expr, operand=_rewrite_expr(expr.operand))
    if isinstance(expr, BinOp):
        return replace(expr, left=_rewrite_expr(expr.left), right=_rewrite_expr(expr.right))
    return expr


def _rewrite_quotes(program: Program) -> Program:
    return replace(program, statements=tuple(replace(s, expr=_rewrite_expr(s.expr)) for s in program.statements))


def parse(text: str) -> Program:
    """把公式文本解析成语法树。

    写法有误时抛 FormulaError(kind="syntax")，带行号。
    未来函数的判断优先于其他错误：写法有误的公式里如果出现了未来函数，抛的是 kind="lookahead"。
    """
    # 从聊天软件、网页里复制公式时，末尾常常多带一个顿号、逗号之类的标点：不算公式内容，去掉
    text = text.rstrip().rstrip("、，。,").rstrip()
    try:
        return _rewrite_quotes(_Parser(_tokenize(text)).program(text))
    except FormulaError as err:
        names = _scan_lookahead(text)
        if names:
            raise lookahead_error(names) from err
        raise
