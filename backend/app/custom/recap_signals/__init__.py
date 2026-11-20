"""复盘信号：每天复盘跑完后，把“持仓复盘”里给出的关键价位变成监控规则，只管下一个交易日。

下一个交易日走完（或者有了更新的复盘）就把旧的删掉，监控中心里永远只有最新一份复盘的信号。
详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.recap-signals"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
