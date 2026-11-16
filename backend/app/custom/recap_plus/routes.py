"""复盘补充数据的接口，以及给面板复盘用的入口 recap_context。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from . import context, notices, trend

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/recap-plus"


def _root(data_dir: Path) -> Path:
    return data_dir / "user_data" / "extensions" / "recap_plus"


def recap_context(data_dir: Any, as_of: Any) -> str:
    """面板的复盘在拼材料时调用。任何失败都返回空字符串，不影响原来的复盘。"""
    try:
        if not data_dir or not as_of:
            return ""
        root = Path(data_dir)
        # 限流器的状态放在免费来源的数据目录里；这里确保它已经指向这份数据
        from app.custom.tdx_formula.ql.core import config as ql_config

        if not ql_config.is_configured():
            ql_config.set_data_dir(root / "freecn")
        return context.build(root, _root(root), str(as_of)[:10])
    except Exception:  # 补充材料出什么问题都不能让复盘失败
        logger.exception("准备复盘补充材料没有成功")
        return ""


def build_router() -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["custom-recap-plus"])

    @router.get("/preview")
    def preview(request: Request, as_of: str | None = None) -> dict[str, Any]:
        """看一眼会交给 AI 的补充材料（会按需联网取两融和资金流向，当天取过的不重复取）。"""
        repo = getattr(request.app.state, "repo", None)
        raw = getattr(getattr(repo, "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        day = as_of
        if not day:
            latest = repo.latest_enriched_date() if hasattr(repo, "latest_enriched_date") else None
            day = str(latest)[:10] if latest else None
        if not day:
            raise HTTPException(status_code=409, detail="本机还没有日线数据。")
        text = recap_context(raw, day)
        return {"as_of": day, "chars": len(text), "context": text}

    @router.get("/dashboard")
    def dashboard(request: Request, as_of: str | None = None) -> dict[str, Any]:
        """“每日复盘”页首页的数据：近 15 个交易日的走势和两融序列。"""
        raw = getattr(getattr(getattr(request.app.state, "repo", None), "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        data_dir = Path(raw)
        from app.custom.tdx_formula.ql.core import config as ql_config

        if not ql_config.is_configured():
            ql_config.set_data_dir(data_dir / "freecn")
        return trend.build(data_dir, _root(data_dir), as_of)

    @router.get("/notices")
    def holding_notices(request: Request, refresh: bool = False) -> dict[str, Any]:
        """持仓股票的最新上市公司公告。半小时内取过的直接用存下来的；refresh=true 时重新取。"""
        raw = getattr(getattr(getattr(request.app.state, "repo", None), "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        data_dir = Path(raw)
        from app.custom.tdx_formula.ql.core import clock
        from app.custom.tdx_formula.ql.core import config as ql_config

        if not ql_config.is_configured():
            ql_config.set_data_dir(data_dir / "freecn")
        held = [h["symbol"] for h in context.read_holdings(data_dir)]
        return notices.latest(_root(data_dir), held, clock.now().replace(tzinfo=None), force=refresh)

    return router
