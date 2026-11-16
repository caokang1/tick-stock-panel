"""复盘补充数据：给面板的 AI 复盘多准备几块材料（榜单、行业涨跌停分布、两融、持仓明细）。

面板自带的复盘只给 AI 指数、涨跌家数、板块排名这些全市场数据。这个扩展在它后面再接一段：
成交额 / 总市值前十、各行业的涨停跌停家数、全市场两融，以及“持仓提醒”里每只股票的
行情、盈亏、估值、主力资金流向、两融和对标股票。详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.recap-plus"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
