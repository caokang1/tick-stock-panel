"""数据源适配器。每个来源一个文件；外部行情库只在各自的适配器文件里 import。

``default_registry()`` 给出"来源名称 -> 构造函数"的登记表。适配器在第一次用到时才 import 和创建，
所以不联网的测试不会加载任何外部行情库。新增数据来源：写一个适配器，在这里登记，再在配置里排进优先级。
"""

from __future__ import annotations

from collections.abc import Callable

from app.custom.tdx_formula.ql.data.interface import DataSource


def _baostock() -> DataSource:
    from app.custom.tdx_formula.ql.data.sources.baostock_source import BaoStockSource

    return BaoStockSource()


def _akshare_em() -> DataSource:
    from app.custom.tdx_formula.ql.data.sources.akshare_em import AkshareEmSource

    return AkshareEmSource()


def _akshare_sina() -> DataSource:
    from app.custom.tdx_formula.ql.data.sources.akshare_sina import AkshareSinaSource

    return AkshareSinaSource()


def _akshare_tx() -> DataSource:
    from app.custom.tdx_formula.ql.data.sources.akshare_tx import AkshareTxSource

    return AkshareTxSource()


def _tencent_quote() -> DataSource:
    from app.custom.tdx_formula.ql.data.sources.tencent_quote import TencentQuoteSource

    return TencentQuoteSource()


def default_registry() -> dict[str, Callable[[], DataSource]]:
    return {
        "baostock": _baostock,
        "akshare_em": _akshare_em,
        "akshare_sina": _akshare_sina,
        "akshare_tx": _akshare_tx,
        "tencent_quote": _tencent_quote,
    }
