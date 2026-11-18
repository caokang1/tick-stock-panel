"""左上角的话：按当天的市场情绪评分，从语料库里挑一句显示在侧栏左上角。

评分用的是面板自己算的情绪评分（和每日复盘里的是同一个数）。语料库是一个纯文本文件，
放在个人数据目录里，可以直接改，也可以在“设置 → 左上角的话”里改。详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.market-motto"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
