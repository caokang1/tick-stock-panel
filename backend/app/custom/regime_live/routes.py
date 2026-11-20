"""市场环境盘中刷新的接口。

页面开着、并且在“设置 → 实时监控 → 页面实时刷新”里打开了“市场环境”时，每来一轮新行情，
前端会调一次 /refresh：这里用面板自带的“增量计算”把行情变过的日子（盘中就是今天）的市场环境重算一遍。
收盘后的管道照常会再算一次，以收盘数据为准，盘中的临时结果不会留下来。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/regime-live"
MIN_GAP_SECONDS = 30.0  # 两次重算之间至少隔这么久；间隔内直接返回上一次的结果

Compute = Callable[[Any, Path], dict[str, int]]


def compute(repo: Any, data_dir: Path) -> dict[str, int]:
    """重算行情变过的日子（盘中就是今天）的市场环境。返回重算了几天。

    只动市场环境这张表：它自带“行情文件比结果新就重算”的判断，收盘后的管道会用收盘数据再覆盖一次。
    概念/行业主线没有这个判断（当天算过就不再算），盘中写进去会把临时结果留到收盘以后，所以这里不碰。
    """
    from app.api.regime import invalidate_regime_cache
    from app.services import regime_builder

    rows = regime_builder.compute_regime_incremental(repo, data_dir)
    days = 0 if rows.is_empty() else rows.height
    if days:
        invalidate_regime_cache()
    return {"days": days}


class Refresher:
    """把重算串起来：同一时间只算一次，算得太勤就直接给上一次的结果，管道在跑时不动。"""

    def __init__(self, run: Compute = compute, busy: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic, min_gap: float = MIN_GAP_SECONDS) -> None:
        self._run, self._busy, self._clock, self._min_gap = run, busy or pipeline_busy, clock, min_gap
        self._lock = threading.Lock()
        self._at: float | None = None

    def refresh(self, repo: Any, data_dir: Path) -> dict[str, Any]:
        if self._busy():
            return {"updated": False, "reason": "busy"}
        if not self._lock.acquire(blocking=False):
            return {"updated": False, "reason": "running"}
        try:
            now = self._clock()
            if self._at is not None and now - self._at < self._min_gap:
                return {"updated": False, "reason": "recent"}
            self._at = now
            done = self._run(repo, data_dir)
            return {"updated": bool(done["days"]), **done}
        finally:
            self._lock.release()


def pipeline_busy() -> bool:
    """收盘后的管道或数据修复在跑时不重算，免得和它同时写同一个文件。"""
    from app.services.pipeline_jobs import job_store

    return job_store.active_id() is not None


def build_router(refresher: Refresher | None = None) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["custom-regime-live"])
    worker = refresher or Refresher()

    @router.post("/refresh")
    def refresh(request: Request) -> dict[str, Any]:
        repo = getattr(request.app.state, "repo", None)
        raw = getattr(getattr(repo, "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        try:
            return worker.refresh(repo, Path(raw))
        except Exception as exc:
            logger.exception("市场环境盘中刷新失败")
            raise HTTPException(status_code=500, detail=f"市场环境没有算出来：{exc}") from exc

    return router
