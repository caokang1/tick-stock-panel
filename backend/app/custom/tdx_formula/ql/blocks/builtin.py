"""指标库：常用技术指标条件。粘贴公式时用它来认出公式里已知的写法，给条件起一个看得懂的名字。"""

from __future__ import annotations

from app.custom.tdx_formula.ql.blocks.library import LIBRARY_BLOCKS
from app.custom.tdx_formula.ql.blocks.model import Block
from app.custom.tdx_formula.ql.core.errors import NotFoundError

BUILTIN_BLOCKS: tuple[Block, ...] = LIBRARY_BLOCKS
_BY_ID = {b.id: b for b in BUILTIN_BLOCKS}


def all_blocks() -> tuple[Block, ...]:
    return BUILTIN_BLOCKS


def get_block(block_id: str) -> Block:
    block = _BY_ID.get(block_id)
    if block is None:
        raise NotFoundError(f"指标库里没有编号为 {block_id} 的条件。")
    return block
