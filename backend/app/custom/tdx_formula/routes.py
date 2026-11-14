"""通达信公式扩展的接口。全部在 /api/custom/tdx-formula/ 下面。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .ql.core.errors import FormulaError, QuantLabError
from .service import FormulaService

PREFIX = "/api/custom/tdx-formula"


class ParseRequest(BaseModel):
    formula: str = Field(max_length=20_000)


class ConditionsRequest(BaseModel):
    conditions: list[dict[str, Any]] = Field(max_length=50)
    universe: dict[str, Any] | None = None
    start: str | None = None
    end: str | None = None


class SignalsRequest(ConditionsRequest):
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class SaveRequest(BaseModel):
    id: str | None = Field(default=None, max_length=40)
    name: str = Field(max_length=60)
    formula: str = Field(default="", max_length=20_000)
    conditions: list[dict[str, Any]] = Field(max_length=50)
    universe: dict[str, Any] | None = None


class ConfigRequest(BaseModel):
    data_dir: str = Field(max_length=500)


def _fail(e: QuantLabError) -> HTTPException:
    detail: Any = e.message
    if isinstance(e, FormulaError):
        # 公式错误多带几项，页面可以据此指出是哪一行、哪个函数
        detail = {"message": e.message, "kind": e.kind, "names": e.names, "line": e.line}
    return HTTPException(status_code=e.http_status, detail=detail)


def build_router(repo_root: Path | None = None) -> APIRouter:
    """repo_root：本项目的根目录，用来找并排放着的 quant-lab；测试里可以换成别的目录。"""
    router = APIRouter(prefix=PREFIX, tags=["custom-tdx-formula"])
    services: dict[str, FormulaService] = {}
    project_root = repo_root or Path(__file__).resolve().parents[4]

    def service(request: Request) -> FormulaService:
        repo = getattr(request.app.state, "repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="数据仓库还没有就绪，请稍后再试。")
        raw = getattr(getattr(repo, "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            # 数据目录不是一个真正的路径（例如测试里用假对象顶替了数据仓库）：不拿它去建目录
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        data_dir = Path(raw)
        key = str(data_dir)
        if key not in services:
            root = data_dir / "user_data" / "extensions" / "tdx_formula"
            services[key] = FormulaService(root, project_root)
        return services[key]

    def run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except QuantLabError as e:
            raise _fail(e) from e

    # 下面的接口都是同步函数：读全市场数据和统计要几秒到十几秒，FastAPI 会放到线程池里跑，不卡住别的请求
    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        return run(service(request).status)

    @router.put("/config")
    def config(body: ConfigRequest, request: Request) -> dict[str, Any]:
        svc = service(request)
        run(svc.set_data_dir, body.data_dir)
        return run(svc.status)

    @router.get("/blocks")
    def blocks(request: Request) -> list[dict[str, Any]]:
        return service(request).blocks()

    @router.post("/parse")
    def parse(body: ParseRequest, request: Request) -> dict[str, Any]:
        return run(service(request).parse, body.formula)

    @router.post("/stats")
    def stats(body: ConditionsRequest, request: Request) -> dict[str, Any]:
        return run(service(request).stats, body.conditions, body.universe, body.start, body.end)

    @router.post("/signals")
    def signals(body: SignalsRequest, request: Request) -> dict[str, Any]:
        return run(service(request).signals, body.conditions, body.universe, body.limit, body.offset,
                   body.start, body.end)

    @router.post("/picks")
    def picks(body: ConditionsRequest, request: Request) -> dict[str, Any]:
        return run(service(request).picks, body.conditions, body.universe)

    @router.get("/saved")
    def saved(request: Request) -> list[dict[str, Any]]:
        return service(request).saved()

    @router.post("/saved")
    def save(body: SaveRequest, request: Request) -> dict[str, Any]:
        return run(service(request).save, body.name, body.formula, body.conditions, body.universe, body.id)

    @router.delete("/saved/{item_id}")
    def delete(item_id: str, request: Request) -> dict[str, bool]:
        run(service(request).delete, item_id)
        return {"ok": True}

    return router
