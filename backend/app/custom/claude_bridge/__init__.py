"""Claude 转接口：把本机的 claude 命令行包装成“OpenAI 兼容接口”，供面板的 AI 功能使用。

只接受本机的请求并要求转接口令；调用命令行时关掉它的全部工具。详见 bridge.py 开头的说明。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.claude-bridge"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .bridge import build_router

    registrar.include_router(build_router())
