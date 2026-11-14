"""通达信公式扩展的接口。全部在 /api/custom/tdx-formula/ 下面。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .data_service import DataService
from .live_service import LiveService
from .ql.core import clock
from .ql.core.errors import FormulaError, QuantLabError
from .ql.data import store as ql_store
from .service import FormulaService
from .tsp_sync import TspSync

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


class RealtimeUpdate(BaseModel):
    interval_seconds: float


class ImportRequest(BaseModel):
    path: str = Field(max_length=500)


class DownloadRequest(BaseModel):
    start: str | None = Field(default=None, max_length=10)


class SourcePolicyUpdate(BaseModel):
    """一个数据来源的限速设置；不传的不改。官方上限只用于校验，不能改。"""

    enabled: bool | None = None
    max_concurrent: int | None = None
    daily_request_cap: int | None = None
    min_interval_seconds: float | None = None
    jitter_seconds: float | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    stop_after_consecutive_failures: int | None = None
    cooldown_minutes_on_reject: int | None = None
    count_pages_as_requests: bool | None = None


class ScheduleUpdate(BaseModel):
    run_time: str | None = Field(default=None, max_length=5)
    auto_sync_tsp: bool | None = None


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


def build_router(background: bool = True) -> APIRouter:
    """background：是否启动每分钟检查一次的后台线程（测试里关掉）。"""
    services: dict[str, tuple[FormulaService, LiveService, DataService]] = {}
    guard = threading.Lock()
    tsp_sync = TspSync()

    def pair(app: Any) -> tuple[FormulaService, LiveService, DataService]:
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
                # 行情放在 TSP 数据目录下的 freecn 文件夹里，「免费公开来源」数据源插件读的也是这一份
                formulas = FormulaService(Path(raw) / "freecn")
                services[key] = (formulas, LiveService(formulas, root), DataService(formulas))
            return services[key]

    def formulas(request: Request) -> FormulaService:
        return pair(request.app)[0]

    def live(request: Request) -> LiveService:
        return pair(request.app)[1]

    def data(request: Request) -> DataService:
        return pair(request.app)[2]

    def run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except QuantLabError as e:
            raise _fail(e) from e

    @asynccontextmanager
    async def lifespan(app: Any):
        """每分钟看一眼：到了设定的时间就更新当天的行情；行情到了新的一天、又有运行中的策略，就生成提醒并推送。"""
        stopping = threading.Event()
        thread = None
        event_loop = asyncio.get_running_loop()
        if background:
            def loop() -> None:
                while not stopping.wait(TICK_SECONDS):
                    try:
                        formula_service, live_service, data_service = pair(app)
                        data_service.tick()
                        live_service.tick()
                        # 行情更新完，TSP 那份还是旧的，就让 TSP 接着同步一次
                        now = clock.now()
                        latest = ql_store.latest_date() if formula_service.has_data() else None
                        data_dir = Path(app.state.repo.store.data_dir)
                        if not data_service.busy() and tsp_sync.due(data_dir, latest, now):
                            tsp_sync.trigger(app, event_loop, latest, now)
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

    # ---------------- 行情的下载、更新和限速 ----------------
    @router.get("/data/sources")
    def data_sources(request: Request) -> dict[str, Any]:
        return run(data(request).sources)

    @router.put("/data/sources/{name}")
    def data_source_update(name: str, body: SourcePolicyUpdate, request: Request) -> dict[str, Any]:
        return run(data(request).update_source, name, body.model_dump())

    @router.get("/data/progress")
    def data_progress(request: Request) -> dict[str, Any]:
        return run(data(request).progress)

    @router.post("/data/download")
    def data_download(body: DownloadRequest, request: Request) -> dict[str, Any]:
        return run(data(request).start_download, body.start)

    @router.post("/data/stop")
    def data_stop(request: Request) -> dict[str, bool]:
        run(data(request).stop_download)
        return {"ok": True}

    @router.post("/data/update")
    def data_update(request: Request) -> dict[str, Any]:
        return run(data(request).start_update)

    @router.post("/data/import")
    def data_import(body: ImportRequest, request: Request) -> dict[str, Any]:
        return run(formulas(request).import_market, body.path)

    @router.get("/data/realtime")
    def data_realtime(request: Request) -> dict[str, Any]:
        return run(data(request).realtime)

    @router.put("/data/realtime")
    def data_realtime_update(body: RealtimeUpdate, request: Request) -> dict[str, Any]:
        return run(data(request).set_realtime_interval, body.interval_seconds)

    @router.get("/data/schedule")
    def data_schedule(request: Request) -> dict[str, Any]:
        return {**run(data(request).schedule), **tsp_sync.info()}

    @router.put("/data/schedule")
    def data_schedule_update(body: ScheduleUpdate, request: Request) -> dict[str, Any]:
        svc = data(request)
        if body.run_time is not None:
            run(svc.set_run_time, body.run_time)
        if body.auto_sync_tsp is not None:
            tsp_sync.set_enabled(body.auto_sync_tsp)
        return {**run(svc.schedule), **tsp_sync.info()}

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
