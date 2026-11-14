"""给上层（接口层、每日任务）用的入口。函数都很薄，具体实现在各自的文件里。"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from datetime import date
from typing import Any

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.errors import ConflictError, QuantLabError
from app.custom.tdx_formula.ql.data import download, store
from app.custom.tdx_formula.ql.data.config import DataConfig
from app.custom.tdx_formula.ql.data.limiter import STATE_COOLING
from app.custom.tdx_formula.ql.data.lock import is_locked
from app.custom.tdx_formula.ql.data.models import KIND_STOCK, DataStatus, SourceOverview
from app.custom.tdx_formula.ql.data.runtime import current_config, get_runtime

logger = logging.getLogger(__name__)

_job_guard = threading.Lock()
_job_thread: threading.Thread | None = None
_job_stop = threading.Event()
_job_progress: dict[str, Any] = {"running": False}


def get_effective_config() -> DataConfig:
    """生效的数据来源配置（默认配置 + 用户的修改）。``.to_dict()`` 可直接转成接口响应。"""
    return current_config()


def data_status() -> DataStatus:
    """本地行情数据的概况：最新日期、股票数、ST 历史是否齐全、上次更新的结果等。读取很快，不会拼 Panel。"""
    rt = get_runtime()
    progress = store.Progress.load()
    codes = store.list_daily_codes()
    securities = store.read_securities()
    st_done = progress.count_done(store.ITEM_ST, codes)
    shares_done = progress.count_done(store.ITEM_SHARES, codes)
    last = rt.state.last_update()
    return DataStatus(
        has_data=bool(codes) and store.latest_date() is not None,
        first_date=date.fromisoformat(progress.start) if progress.start else None,
        latest_date=store.latest_date(),
        stock_count=len(codes),
        securities_count=int((securities["kind"] == KIND_STOCK).sum()) if len(securities) else 0,
        st_known=bool(codes) and st_done == len(codes),
        st_done=st_done,
        shares_known=bool(codes) and shares_done == len(codes),
        shares_done=shares_done,
        download_start=date.fromisoformat(progress.start) if progress.start else None,
        download_finished=progress.finished,
        last_update_at=last.finished_at if last else None,
        last_update_ok=last.ok if last else None,
        last_update_message=last.message if last else "",
    )


def sources_overview() -> list[SourceOverview]:
    """每个数据来源的状况：生效的限制、状态、今日请求数、冷却剩余时间。

    状态取值：normal / cooling / capped / disabled / halted。
    """
    rt = get_runtime()
    config = current_config()
    result = []
    for name, policy in config.sources.items():
        state = rt.limiter.state_of(name)
        until = rt.limiter.cooldown_until(name)
        remaining = int((until - clock.now()).total_seconds()) if until and state == STATE_COOLING else 0
        declared = [cap.value for cap, names in config.priority.items() if name in names]
        result.append(SourceOverview(
            name=name, policy=asdict(policy), capabilities=declared, state=state,
            today_requests=rt.limiter.today_requests(name), cooldown_until=until,
            cooldown_remaining_seconds=max(0, remaining),
            consecutive_failures=rt.limiter.consecutive_failures(name),
        ))
    return result


def start_history_download(start: date | None = None, limit: int | None = None) -> dict[str, Any]:
    """在后台线程里开始（或继续）历史下载，立即返回当前进度。用 ``get_progress()`` 查看进展。

    ``start`` 默认取配置里的起始日期。已有数据任务在运行时抛 ``ConflictError``。
    """
    global _job_thread
    begin = start or current_config().download.default_start
    with _job_guard:
        if (_job_thread is not None and _job_thread.is_alive()) or is_locked():
            raise ConflictError("已有数据任务在运行，请等它结束后再试")
        _job_stop.clear()
        _job_progress.clear()
        _job_progress.update({
            "running": True, "phase": "准备", "current": "", "total": 0, "done": {}, "failed": 0,
            "started_at": clock.now().isoformat(timespec="seconds"), "finished_at": None, "message": "", "report": None,
        })

        def run() -> None:
            try:
                report = download.download_history(begin, on_progress=_job_progress.update, limit=limit, stop=_job_stop)
                _job_progress.update({"message": report.message, "report": report.to_dict()})
            except QuantLabError as e:
                _job_progress.update({"message": e.message})
            except Exception as e:  # 后台线程不能悄悄死掉：记下来给界面看
                logger.exception("历史下载意外出错")
                _job_progress.update({"message": f"下载意外出错：{type(e).__name__}: {e}"})
            finally:
                _job_progress.update({"running": False, "finished_at": clock.now().isoformat(timespec="seconds")})

        _job_thread = threading.Thread(target=run, name="history-download", daemon=True)
        _job_thread.start()
    return get_progress()


def stop_history_download() -> None:
    """请求停止后台的历史下载（当前这只股票处理完就停）。已下载的部分保留，下次接着下。"""
    _job_stop.set()


def get_progress() -> dict[str, Any]:
    """后台历史下载的进度：running、phase、current、total、done（各项已完成的股票数）、failed、message、report。"""
    return dict(_job_progress)


def wait_history_download(timeout: float | None = None) -> None:
    """等后台下载结束（测试和命令行用）。"""
    thread = _job_thread
    if thread is not None:
        thread.join(timeout)
