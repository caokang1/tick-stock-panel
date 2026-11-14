"""指标库：常用技术指标条件、排名因子、大盘择时条件。粘贴公式时也用它来认出公式里已知的写法。"""

from __future__ import annotations

from app.custom.tdx_formula.ql.blocks.library import LIBRARY_BLOCKS
from app.custom.tdx_formula.ql.blocks.model import KIND_RANK, KIND_TIMING, Block, ParamSpec
from app.custom.tdx_formula.ql.core.errors import NotFoundError

_EXTRA_BLOCKS: tuple[Block, ...] = (
    # ---------- 排名因子：输出一个数值，用来给当天选出的股票排先后 ----------
    Block(
        id="rank_amount", kind=KIND_RANK, name="成交额",
        description="近 N 天的平均成交额。成交额大的股票买卖更容易，不容易买不进或卖不出。",
        formula="MA(AMOUNT,{n});",
        params=(ParamSpec("n", "天数", 5, 1, 250, 1, "天"),),
    ),
    Block(
        id="rank_momentum", kind=KIND_RANK, name="近期涨幅",
        description="近 N 天的涨幅。选“从大到小”是追强势股，选“从小到大”是挑涨得少的。",
        formula="C/REF(C,{n})-1;",
        params=(ParamSpec("n", "天数", 20, 1, 250, 1, "天"),),
    ),
    Block(
        id="rank_volatility", kind=KIND_RANK, name="波动大小",
        description="近 N 天每日涨跌幅的波动程度。数值小说明走势平稳，数值大说明起伏剧烈。",
        formula="STD(C/REF(C,1)-1,{n});",
        params=(ParamSpec("n", "天数", 20, 2, 250, 1, "天"),),
    ),
    Block(
        id="rank_volume_ratio", kind=KIND_RANK, name="量比",
        description="今天的成交量是近 N 天平均成交量的多少倍。数值大说明今天明显放量。",
        formula="V/MA(V,{n});",
        params=(ParamSpec("n", "天数", 5, 1, 250, 1, "天"),),
    ),
    Block(
        id="rank_ma_bias", kind=KIND_RANK, name="偏离均线",
        description="收盘价比 N 日均线高出（或低出）多少。数值小说明离均线近或在均线下方。",
        formula="C/MA(C,{n})-1;",
        params=(ParamSpec("n", "均线天数", 20, 2, 250, 1, "日"),),
    ),
    Block(
        id="rank_float_cap", kind=KIND_RANK, name="流通市值",
        description="当天的流通市值。选“从小到大”是优先买小盘股，选“从大到小”是优先买大盘股。",
        formula="LTSZ;", params=(),
    ),
    Block(
        id="rank_turnover", kind=KIND_RANK, name="换手率",
        description="近 N 天的平均换手率。换手率高说明交投活跃。",
        formula="MA(HSL,{n});",
        params=(ParamSpec("n", "天数", 5, 1, 250, 1, "天"),),
    ),
    # ---------- 择时条件：在指数的日线上判断，成立表示“可以出手” ----------
    Block(
        id="timing_above_ma", kind=KIND_TIMING, name="指数在均线上方",
        description="指数收盘价在它的 N 日均线上方时才出手。大盘走弱时自动停手。",
        formula="C>MA(C,{n});",
        params=(ParamSpec("n", "均线天数", 20, 2, 250, 1, "日"),),
    ),
    Block(
        id="timing_ma_cross", kind=KIND_TIMING, name="指数短均线在长均线上方",
        description="指数的短期均线在长期均线上方时才出手，比只看一条均线更稳、反应也更慢。",
        formula="MA(C,{fast})>MA(C,{slow});",
        params=(ParamSpec("fast", "短均线", 5, 1, 120, 1, "日"), ParamSpec("slow", "长均线", 20, 2, 250, 1, "日")),
    ),
    Block(
        id="timing_macd", kind=KIND_TIMING, name="指数 MACD 多头",
        description="指数 MACD 的快线（DIF）在慢线（DEA）上方时才出手。",
        formula="DIF:=EMA(C,{short})-EMA(C,{long});\nDEA:=EMA(DIF,{mid});\nDIF>DEA;",
        params=(
            ParamSpec("short", "短周期", 12, 2, 120, 1, "日"),
            ParamSpec("long", "长周期", 26, 5, 500, 1, "日"),
            ParamSpec("mid", "平滑周期", 9, 2, 120, 1, "日"),
        ),
    ),
)

BUILTIN_BLOCKS: tuple[Block, ...] = LIBRARY_BLOCKS + _EXTRA_BLOCKS
_BY_ID = {b.id: b for b in BUILTIN_BLOCKS}


def all_blocks() -> tuple[Block, ...]:
    return BUILTIN_BLOCKS


def get_block(block_id: str) -> Block:
    block = _BY_ID.get(block_id)
    if block is None:
        raise NotFoundError(f"指标库里没有编号为 {block_id} 的条件。")
    return block
