"""每日汇总：当天的定时任务都跑完以后，把“每日复盘 + 系统快照”做成一个网页，并把摘要和链接推到飞书。

网页存在本机的个人数据目录里，不上传到任何地方。详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.daily-digest"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
