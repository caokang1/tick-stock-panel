"""公式的语法树。所有节点不可变，带行号和列号（都从 1 开始）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Number:
    value: float
    text: str  # 原文写法，如 "1.098"
    line: int
    col: int

    @property
    def is_integer(self) -> bool:
        return float(self.value).is_integer()


@dataclass(frozen=True)
class String:
    value: str
    line: int
    col: int


@dataclass(frozen=True)
class Ident:
    name: str  # 原文写法
    line: int
    col: int

    @property
    def key(self) -> str:
        """查找用的名字：大小写不敏感，统一成大写。"""
        return self.name.upper()


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Expr, ...]
    line: int
    col: int

    @property
    def key(self) -> str:
        return self.name.upper()


@dataclass(frozen=True)
class Unary:
    op: str  # "-" 或 "+"
    operand: Expr
    line: int
    col: int


@dataclass(frozen=True)
class BinOp:
    op: str  # "OR" "AND" ">" "<" ">=" "<=" "=" "<>" "+" "-" "*" "/"
    left: Expr
    right: Expr
    line: int
    col: int


Expr = Number | String | Ident | Call | Unary | BinOp


@dataclass(frozen=True)
class Statement:
    name: str | None  # 裸表达式没有名字
    kind: str  # "assign"（名字:=表达式，中间变量）或 "output"（名字:表达式 / 裸表达式）
    expr: Expr
    line: int
    index: int  # 在 Program.statements 里的位置

    @property
    def key(self) -> str | None:
        return None if self.name is None else self.name.upper()

    @property
    def is_output(self) -> bool:
        return self.kind == "output"


@dataclass(frozen=True)
class Program:
    statements: tuple[Statement, ...]
    source: str
