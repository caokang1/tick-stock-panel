"""通达信公式扩展的接口。全部在 /api/custom/tdx-formula/ 下面。"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .live_service import LiveService
from .ql.core.errors import FormulaError, QuantLabError
from .service import FormulaService

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/tdx-formula"
TICK_SECONDS = 60


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


class ConfigRequest(BaseModel):
    data_dir: str = Field(max_length=500)


class StrategyConfig(BaseModel):
    """一个策略的全部内容：选股条件、股票范围、排名、买卖规则、大盘择时；formula 是粘贴的公式原文。"""

    conditions: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    universe: dict[str, Any] | None = None
    rank: dict[str, Any] | None = None
    trade: dict[str, Any] | None = None
    timing: dict[str, Any] | None = None
    formula: str = Field(default="", max_length=20_000)


class StrategyCreate(BaseModel):
    name: str = Field(max_length=60)
    config: StrategyConfig


class StrategyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    config: StrategyConfig | None = None


class BacktestRequest(StrategyConfig):
    strategy_id: str | None = Field(default=None, max_length=40)
    start: str | None = None
    end: str | None = None
    initial_cash: float | None = Field(default=None, ge=10_000, le=1e10)
    benchmark: str | None = Field(default=None, max_length=20)


class RealSwitch(BaseModel):
    enabled: bool
    capital: float | None = None


class RealAnswer(BaseModel):
    done: bool
    price: float | None = None
    shares: int | None = None
    date: str | None = None


class RealPositionIn(BaseModel):
    code: str = Field(default="", max_length=20)
    shares: int
    price: float
    date: str


class RealSellIn(BaseModel):
    price: float
    date: str


class SettingsUpdate(BaseModel):
    costs: dict[str, float | None] | None = None
    initial_cash: float | None = None


def _fail(e: QuantLabError) -> HTTPException:
    detail: Any = e.message
    if isinstance(e, FormulaError):
        # 公式错误多带几项，页面可以据此指出是哪一行、哪个函数
        detail = {"message": e.message, "kind": e.kind, "names": e.names, "line": e.line}
    return HTTPException(status_code=e.http_status, detail=detail)


def build_router(repo_root: Path | None = None, background: bool = True) -> APIRouter:
    """repo_root：本项目的根目录，用来找并排放着的 quant-lab；background：是否启动每分钟检查一次的后台线程。
    两个参数都是给测试用的。"""
    project_root = repo_root or Path(__file__).resolve().parents[4]
    services: dict[str, tuple[FormulaService, LiveService]] = {}
    guard = threading.Lock()

    def pair(app: Any) -> tuple[FormulaService, LiveService]:
        repo = getattr(app.state, "repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="数据仓库还没有就绪，请稍后再试。")
        raw = getattr(getattr(repo, "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            # 数据目录不是一个真正的路径（例如测试里用假对象顶替了数据仓库）：不拿它去建目录
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        key = str(raw)
        with guard:
            if key not in services:
                root = Path(raw) / "user_data" / "extensions" / "tdx_formula"
                formulas = FormulaService(root, project_root)
                services[key] = (formulas, LiveService(formulas, root))
            return services[key]

    def formulas(request: Request) -> FormulaService:
        return pair(request.app)[0]

    def live(request: Request) -> LiveService:
        return pair(request.app)[1]

    def run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except QuantLabError as e:
            raise _fail(e) from e

    @asynccontextmanager
    async def lifespan(app: Any):
        """每分钟看一眼：quant-lab 的行情到了新的一天、又有运行中的策略，就生成当天的提醒并推送。"""
        stopping = threading.Event()
        thread = None
        if background:
            def loop() -> None:
                while not stopping.wait(TICK_SECONDS):
                    try:
                        pair(app)[1].tick()
                    except HTTPException:
                        pass  # 数据仓库还没就绪，下一轮再看
                    except Exception:
                        logger.exception("通达信公式扩展的每日任务出错")

            thread = threading.Thread(target=loop, name="tdx-formula-daily", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stopping.set()
            if thread is not None:
                thread.join(timeout=5)

    router = APIRouter(prefix=PREFIX, tags=["custom-tdx-formula"], lifespan=lifespan)

    # 下面的接口都是同步函数：读全市场数据和统计要几秒到十几秒，FastAPI 会放到线程池里跑，不卡住别的请求
    # ---------------- 行情、公式、信号统计 ----------------
    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        return run(formulas(request).status)

    @router.put("/config")
    def config(body: ConfigRequest, request: Request) -> dict[str, Any]:
        svc = formulas(request)
        run(svc.set_data_dir, body.data_dir)
        return run(svc.status)

    @router.get("/blocks")
    def blocks(request: Request) -> list[dict[str, Any]]:
        return formulas(request).blocks()

    @router.get("/options")
    def options(request: Request) -> dict[str, Any]:
        return live(request).options()

    @router.post("/parse")
    def parse(body: ParseRequest, request: Request) -> dict[str, Any]:
        return run(formulas(request).parse, body.formula)

    @router.post("/stats")
    def stats(body: ConditionsRequest, request: Request) -> dict[str, Any]:
        return run(formulas(request).stats, body.conditions, body.universe, body.start, body.end)

    @router.post("/signals")
    def signals(body: SignalsRequest, request: Request) -> dict[str, Any]:
        return run(formulas(request).signals, body.conditions, body.universe, body.limit, body.offset,
                   body.start, body.end)

    @router.post("/picks")
    def picks(body: ConditionsRequest, request: Request) -> dict[str, Any]:
        return run(formulas(request).picks, body.conditions, body.universe)

    # ---------------- 策略 ----------------
    @router.get("/strategies")
    def strategies(request: Request) -> list[dict[str, Any]]:
        return run(live(request).strategies)

    @router.post("/strategies")
    def create_strategy(body: StrategyCreate, request: Request) -> dict[str, Any]:
        return run(live(request).create, body.name, body.config.model_dump())

    @router.get("/strategies/{strategy_id}")
    def get_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
        return run(live(request).detail, strategy_id)

    @router.put("/strategies/{strategy_id}")
    def update_strategy(strategy_id: str, body: StrategyUpdate, request: Request) -> dict[str, Any]:
        config = body.config.model_dump() if body.config else None
        return run(live(request).update, strategy_id, body.name, config)

    @router.delete("/strategies/{strategy_id}")
    def delete_strategy(strategy_id: str, request: Request) -> dict[str, bool]:
        run(live(request).delete, strategy_id)
        return {"ok": True}

    @router.post("/strategies/{strategy_id}/start")
    def start_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
        return run(live(request).start, strategy_id)

    @router.post("/strategies/{strategy_id}/stop")
    def stop_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
        return run(live(request).stop, strategy_id)

    # ---------------- 组合回测 ----------------
    @router.post("/backtest")
    def backtest(body: BacktestRequest, request: Request) -> dict[str, Any]:
        return run(live(request).backtest, body.model_dump())

    @router.get("/strategies/{strategy_id}/backtest")
    def saved_backtest(strategy_id: str, request: Request) -> dict[str, Any]:
        return run(live(request).saved_backtest, strategy_id)

    # ---------------- 今日提醒、模拟账户 ----------------
    @router.get("/today")
    def today(request: Request) -> dict[str, Any]:
        return run(live(request).today)

    @router.get("/strategies/{strategy_id}/account")
    def account(strategy_id: str, request: Request) -> dict[str, Any]:
        return run(live(request).account, strategy_id)

    @router.post("/daily/run")
    def daily_run(request: Request) -> dict[str, Any]:
        return run(live(request).run_daily)

    # ---------------- 实际持仓 ----------------
    @router.get("/strategies/{strategy_id}/real")
    def get_real(strategy_id: str, request: Request) -> dict[str, Any]:
        return run(live(request).real_view, strategy_id)

    @router.put("/strategies/{strategy_id}/real")
    def put_real(strategy_id: str, body: RealSwitch, request: Request) -> dict[str, Any]:
        return run(live(request).set_real, strategy_id, body.enabled, body.capital)

    @router.post("/strategies/{strategy_id}/real/skip-all")
    def real_skip_all(strategy_id: str, request: Request) -> dict[str, Any]:
        return run(live(request).real_skip_all, strategy_id)

    @router.post("/strategies/{strategy_id}/real/positions")
    def real_add(strategy_id: str, body: RealPositionIn, request: Request) -> dict[str, Any]:
        return run(live(request).real_add, strategy_id, body.model_dump())

    @router.post("/real/todos/{todo_id}")
    def real_answer(todo_id: int, body: RealAnswer, request: Request) -> dict[str, Any]:
        return run(live(request).real_answer, todo_id, body.model_dump())

    @router.put("/real/positions/{position_id}")
    def real_edit(position_id: int, body: RealPositionIn, request: Request) -> dict[str, Any]:
        return run(live(request).real_edit, position_id, body.model_dump())

    @router.post("/real/positions/{position_id}/sell")
    def real_sell(position_id: int, body: RealSellIn, request: Request) -> dict[str, Any]:
        return run(live(request).real_sell, position_id, body.model_dump())

    @router.delete("/real/positions/{position_id}")
    def real_remove(position_id: int, request: Request) -> dict[str, Any]:
        return run(live(request).real_remove, position_id)

    # ---------------- 设置 ----------------
    @router.get("/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        return run(live(request).settings)

    @router.put("/settings")
    def put_settings(body: SettingsUpdate, request: Request) -> dict[str, Any]:
        return run(live(request).update_settings, body.model_dump())

    return router
