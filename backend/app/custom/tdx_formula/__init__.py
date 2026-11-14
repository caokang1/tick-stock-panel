"""通达信公式选股扩展：粘贴通达信公式做成策略，回测、每日买卖提醒、实际持仓。

行情由扩展自己从免费的公开来源按限速规则下载到 data/freecn（见本目录的 README.md），
同一份数据经「免费公开来源」数据源插件（app/plugins/freecn）也可以交给 TSP 的核心功能使用。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.tdx-formula"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
