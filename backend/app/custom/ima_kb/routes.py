"""ima 知识库扩展的接口。都在 /api/custom/ima-kb 下面，走面板自己的访问校验。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .service import ImaError, ImaService

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/ima-kb"
_services: dict[str, ImaService] = {}
_guard = threading.Lock()
_names: dict[str, tuple[float, dict[str, str]]] = {}


class Credentials(BaseModel):
    client_id: str = Field(max_length=200)
    api_key: str = Field(max_length=400)


class Base(BaseModel):
    id: str = Field(max_length=200)
    name: str = Field(default="", max_length=200)


class Selection(BaseModel):
    selected: list[Base] = Field(max_length=50)


class Options(BaseModel):
    use_in_ai: bool


class SearchRequest(BaseModel):
    query: str = Field(max_length=200)
    base_ids: list[str] | None = Field(default=None, max_length=50)


def data_dir_of(app: Any) -> Path | None:
    raw = getattr(getattr(getattr(app.state, "repo", None), "store", None), "data_dir", None)
    return Path(raw) if isinstance(raw, (str, Path)) else None


def service_of(app: Any) -> ImaService:
    data_dir = data_dir_of(app)
    if data_dir is None:
        raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
    with _guard:
        key = str(data_dir)
        if key not in _services:
            _services[key] = ImaService(data_dir / "user_data" / "extensions" / "ima_kb")
        return _services[key]


def stock_name(data_dir: Path, symbol: str) -> str | None:
    """股票代码 → 名称，读面板自己的证券清单（读一次后记在内存里，清单文件变了才重读）。"""
    path = data_dir / "instruments" / "instruments.parquet"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    cached = _names.get(str(path))
    if cached is None or cached[0] != stamp:
        try:
            import polars as pl

            frame = pl.read_parquet(path, columns=["symbol", "name"])
            table = dict(zip(frame["symbol"].to_list(), frame["name"].to_list(), strict=True))
        except Exception as e:  # noqa: BLE001 —— 读不到名称就按代码搜，不算错误
            logger.info("读取证券清单没有成功：%s", e)
            return None
        cached = (stamp, {str(k): str(v) for k, v in table.items() if v})
        _names[str(path)] = cached
    return cached[1].get(symbol)


def ai_context(app: Any, prompt: str) -> str:
    """给别的扩展（AI 转接口）用：这段提示词如果是针对某一只股票的，返回知识库里的参考材料，否则返回空。"""
    data_dir = data_dir_of(app)
    if data_dir is None:
        return ""
    try:
        return service_of(app).context_for(prompt, lambda symbol: stock_name(data_dir, symbol))
    except Exception as e:  # noqa: BLE001 —— 参考材料拿不到不能影响原来的 AI 调用
        logger.warning("准备知识库参考材料时出错：%s", e)
        return ""


def build_router() -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["custom-ima-kb"])

    def run(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except ImaError as e:
            raise HTTPException(status_code=e.status, detail=e.message) from e

    @router.get("/config")
    def config(request: Request) -> dict[str, Any]:
        return service_of(request.app).config()

    @router.put("/credentials")
    def set_credentials(body: Credentials, request: Request) -> dict[str, Any]:
        return run(service_of(request.app).set_credentials, body.client_id, body.api_key)

    @router.delete("/credentials")
    def clear_credentials(request: Request) -> dict[str, Any]:
        return service_of(request.app).clear_credentials()

    @router.get("/knowledge-bases")
    def knowledge_bases(request: Request, query: str = "") -> dict[str, Any]:
        return {"items": run(service_of(request.app).knowledge_bases, query[:100])}

    @router.put("/selected")
    def set_selected(body: Selection, request: Request) -> dict[str, Any]:
        return run(service_of(request.app).set_selected, [b.model_dump() for b in body.selected])

    @router.put("/options")
    def set_options(body: Options, request: Request) -> dict[str, Any]:
        return service_of(request.app).set_use_in_ai(body.use_in_ai)

    @router.post("/search")
    def search(body: SearchRequest, request: Request) -> dict[str, Any]:
        return run(service_of(request.app).search, body.query, body.base_ids)

    @router.get("/stock/{symbol}")
    def stock(symbol: str, request: Request) -> dict[str, Any]:
        """个股详情里用：按这只股票的名称（没有名称就按代码）在选中的知识库里搜索。"""
        data_dir = data_dir_of(request.app)
        name = stock_name(data_dir, symbol.upper()) if data_dir else None
        return run(service_of(request.app).search, name or symbol.split(".")[0])

    return router
