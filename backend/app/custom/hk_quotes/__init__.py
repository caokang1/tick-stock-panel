"""港股报价：给“持仓提醒”里登记的港股补上名称、现价和港元兑人民币汇率。只展示，不参与选股和回测。

报价来自新浪财经的公开接口，每个请求都经过免费数据源的限流器（和分钟 K、期货共用新浪的计数）。
详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.hk-quotes"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
