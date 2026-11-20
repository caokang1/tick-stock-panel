"""市场环境的盘中刷新：行情更新后把“今天”这一行按当时的数据重算一遍，页面随实时行情变化。

面板原本只在收盘后的管道里算市场环境，盘中停在上一个交易日。详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.regime-live"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
