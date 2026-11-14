"""把策略配置里的条件解析成积木，并合成全市场信号。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql.blocks.builtin import get_block
from app.custom.tdx_formula.ql.blocks.model import (
    KIND_CONDITION,
    SOURCE_PASTED,
    Block,
    ParamSpec,
    Universe,
    evaluate_block,
    resolve_params,
    universe_mask,
)
from app.custom.tdx_formula.ql.core.errors import QuantLabError
from app.custom.tdx_formula.ql.core.panel import Panel


def resolve_condition(cond: Mapping[str, Any]) -> tuple[Block, dict[str, float]]:
    """策略配置里的一个条件 -> (积木, 参数)。两种写法：引用指标库 block_id，或内嵌粘贴的公式 pasted。"""
    if cond.get("block_id"):
        block = get_block(str(cond["block_id"]))
        if block.kind != KIND_CONDITION:
            raise QuantLabError(f"“{block.name}”不是选股条件，不能放在条件列表里。")
    elif cond.get("pasted"):
        p = cond["pasted"]
        formula = str(p.get("formula", "")).strip()
        if not formula:
            raise QuantLabError("粘贴的条件里没有公式内容。")
        block = Block(
            id="pasted", kind=KIND_CONDITION, name=str(p.get("name") or "粘贴的条件"),
            description=str(p.get("description") or ""), formula=formula,
            params=tuple(ParamSpec.from_dict(s) for s in p.get("params_spec", [])), source=SOURCE_PASTED,
        )
    else:
        raise QuantLabError("条件的格式不对：既没有指标库编号，也没有粘贴的公式。")
    return block, resolve_params(block, cond.get("params"))


def enabled_conditions(conditions: Sequence[Mapping[str, Any]]) -> list[tuple[Block, dict[str, float]]]:
    return [resolve_condition(c) for c in conditions if c.get("enabled", True)]


def combine_signals(panel: Panel, conditions: Sequence[tuple[Block, Mapping[str, float]]], universe: Universe,
                    evaluator=evaluate_block) -> np.ndarray:
    """全部条件同时满足，并且在股票范围内。没有条件时结果全为 False。

    evaluator 可替换，接口层用它接入带缓存的求值。
    """
    if not conditions:
        return np.zeros(panel.shape, dtype=bool)
    signal = universe_mask(panel, universe) & panel.tradable
    for block, params in conditions:
        signal = signal & evaluator(block, panel, params)
    return signal
