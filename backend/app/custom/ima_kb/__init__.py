"""腾讯 ima 知识库：在面板里搜索自己的知识库，并在 AI 分析个股时把搜到的片段作为参考材料。

用的是 ima 官方的开放接口（凭证在 https://ima.qq.com/agent-interface 申请），只读，不往知识库里写东西。
详见本目录的 README.md。
"""

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "community.ima-kb"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())
