"""通达信公式选股扩展：粘贴通达信公式，拆成可以调参数的条件，在全市场历史数据上统计信号。

计算内核和行情数据都来自 quant-lab（见本目录的 README.md）；不改动、也不依赖 TSP 的核心数据管道。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.tdx-formula"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
