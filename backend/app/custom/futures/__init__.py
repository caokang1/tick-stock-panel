"""期货行情：一页看股指期货、贵金属、有色、能源黑色、国债和外盘主要品种。只展示，不参与选股和回测。

行情和日线来自新浪财经的公开接口，每个请求都经过免费数据源的限流器（和分钟 K 共用新浪的计数）。
详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.futures"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
