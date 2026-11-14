"""条件积木的数据结构与求值。积木不另写计算逻辑，一律交给 tdx 求值。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql.core import codes as codes_mod
from app.custom.tdx_formula.ql.core import limits
from app.custom.tdx_formula.ql.core.errors import QuantLabError
from app.custom.tdx_formula.ql.core.panel import Panel
from app.custom.tdx_formula.ql.tdx import Program, evaluate, parse, render_template

KIND_CONDITION = "condition"
KIND_RANK = "rank"
KIND_TIMING = "timing"
SOURCE_BUILTIN = "builtin"
SOURCE_PASTED = "pasted"


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    default: float
    min: float
    max: float
    step: float
    unit: str = ""
    grid: tuple[float, ...] = ()  # 自动发现时这个参数试哪几个值；空表示只用默认值

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["grid"] = list(self.grid)
        return d

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> ParamSpec:
        return ParamSpec(
            key=str(d["key"]), label=str(d.get("label", d["key"])), default=float(d["default"]),
            min=float(d.get("min", 0.0)), max=float(d.get("max", 0.0)), step=float(d.get("step", 1.0)),
            unit=str(d.get("unit", "")), grid=tuple(float(x) for x in d.get("grid") or ()),
        )


@dataclass(frozen=True)
class Block:
    id: str
    kind: str
    name: str
    description: str
    formula: str  # 通达信公式模板，参数写成 {key}；最后一条语句是输出
    params: tuple[ParamSpec, ...]
    source: str = SOURCE_BUILTIN
    category: str = ""  # 指标库里的分组，如"均线""量能"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "description": self.description,
            "formula": self.formula, "params": [p.to_dict() for p in self.params], "source": self.source,
            "category": self.category,
        }

    def default_params(self) -> dict[str, float]:
        return {p.key: p.default for p in self.params}


@dataclass(frozen=True)
class Universe:
    exclude_st: bool = True
    exclude_star: bool = True
    exclude_chinext: bool = True
    exclude_bse: bool = True
    exclude_suspended: bool = True
    min_float_cap: float | None = None  # 流通市值不低于多少亿元；None 表示不限
    max_float_cap: float | None = None  # 流通市值不高于多少亿元；None 表示不限
    exclude_industries: tuple[str, ...] = ()  # 不要的行业（按现在的行业分类）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exclude_industries"] = list(self.exclude_industries)
        return d

    @staticmethod
    def from_dict(d: Mapping[str, Any] | None) -> Universe:
        d = d or {}
        base = Universe()
        flags = {k: bool(d.get(k, v)) for k, v in asdict(base).items() if isinstance(v, bool)}
        lo, hi = _cap_limit(d.get("min_float_cap"), "下限"), _cap_limit(d.get("max_float_cap"), "上限")
        if lo is not None and hi is not None and lo > hi:
            raise QuantLabError(f"流通市值的下限（{lo:g} 亿）不能比上限（{hi:g} 亿）大。")
        industries = tuple(dict.fromkeys(str(x) for x in d.get("exclude_industries") or () if str(x).strip()))
        return Universe(**flags, min_float_cap=lo, max_float_cap=hi, exclude_industries=industries)


def _cap_limit(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise QuantLabError(f"流通市值的{label}要填数字（单位：亿元）。") from e
    if not np.isfinite(number) or number < 0:
        raise QuantLabError(f"流通市值的{label}不能是负数。")
    return number


def universe_mask(panel: Panel, u: Universe) -> np.ndarray:
    """(T, N) bool：哪些格子在股票范围内。未上市的格子总是不在范围内。"""
    mask = panel.listed.copy()
    boards = np.array([codes_mod.board_of(str(c)) for c in panel.codes])
    if u.exclude_star:
        mask[:, boards == codes_mod.STAR] = False
    if u.exclude_chinext:
        mask[:, boards == codes_mod.CHINEXT] = False
    if u.exclude_bse:
        mask[:, boards == codes_mod.BSE] = False
    if u.exclude_st:
        mask &= ~panel.is_st
    if u.exclude_suspended:
        mask &= ~panel.suspended
    if u.min_float_cap is not None or u.max_float_cap is not None:
        cap = panel.float_cap / 1e8  # 亿元。流通股本未知的格子是 NaN，下面的比较都不成立，等于不在范围内
        with np.errstate(invalid="ignore"):
            if u.min_float_cap is not None:
                mask &= cap >= u.min_float_cap
            if u.max_float_cap is not None:
                mask &= cap <= u.max_float_cap
    if u.exclude_industries:
        mask[:, np.isin(panel.industry, list(u.exclude_industries))] = False
    return mask


@lru_cache(maxsize=512)
def _parse_cached(text: str) -> Program:
    return parse(text)


def resolve_params(block: Block, params: Mapping[str, float] | None) -> dict[str, float]:
    """用默认值补齐，并检查参数是否在允许的范围内。"""
    given = dict(params or {})
    out: dict[str, float] = {}
    for spec in block.params:
        value = float(given.get(spec.key, spec.default))
        if not np.isfinite(value):
            raise QuantLabError(f"“{block.name}”的参数“{spec.label}”不是有效的数字。")
        if spec.max > spec.min and not (spec.min <= value <= spec.max):
            raise QuantLabError(
                f"“{block.name}”的参数“{spec.label}”填的是 {value:g}，允许的范围是 {spec.min:g} 到 {spec.max:g}。"
            )
        out[spec.key] = value
    return out


def rendered_formula(block: Block, params: Mapping[str, float] | None = None) -> str:
    return render_template(block.formula, resolve_params(block, params))


def _extra_series(text: str, panel: Panel) -> dict[str, np.ndarray]:
    upper = text.upper()
    extra: dict[str, np.ndarray] = {}
    if "ISLIMITUP" in upper:
        extra["ISLIMITUP"] = limits.is_limit_up_close(panel)
    if "ISLIMITDOWN" in upper:
        extra["ISLIMITDOWN"] = limits.is_limit_down_close(panel)
    return extra


def evaluate_block(block: Block, panel: Panel, params: Mapping[str, float] | None = None) -> np.ndarray:
    """求出积木在全市场上的结果。条件类返回 (T, N) bool。"""
    text = rendered_formula(block, params)
    result = evaluate(_parse_cached(text), panel, extra_series=_extra_series(text, panel) or None)
    if not result:
        raise QuantLabError(f"“{block.name}”的公式没有输出。")
    out = list(result.values())[-1]
    if block.kind != KIND_RANK and out.dtype != bool:
        with np.errstate(invalid="ignore"):
            out = np.nan_to_num(out.astype(float), nan=0.0) != 0
    return out
