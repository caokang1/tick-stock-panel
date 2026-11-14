"""行情获取与本地存储。其他模块只从这里取用，不感知数据来自哪个来源。

对外的入口：

- ``build_panel(start=None, end=None) -> Panel``
- ``read_index(code) -> DataFrame | None``、``read_securities() -> DataFrame``、``read_calendar() -> ndarray``
- ``data_status() -> DataStatus``
- ``sources_overview() -> list[SourceOverview]``
- ``get_effective_config() -> DataConfig``
- ``update_source_policy(name, changes) -> (DataConfig, 警告列表)``
- ``update_priority(mapping) -> (DataConfig, 警告列表)``
- ``start_history_download(start=None, limit=None) -> dict``、``get_progress() -> dict``、``stop_history_download()``
- ``download_history(start, codes=None, on_progress=None) -> DownloadReport``（同步执行）
- ``update_daily(day=None) -> UpdateReport``
- ``panel_cache_enabled() -> bool``、``panel_cache_bytes() -> int``、``set_panel_cache(enabled)``（行情的硬盘缓存）
"""

from __future__ import annotations

from app.custom.tdx_formula.ql.data.config import DataConfig, update_priority, update_source_policy
from app.custom.tdx_formula.ql.data.download import download_history, update_daily
from app.custom.tdx_formula.ql.data.interface import Capability
from app.custom.tdx_formula.ql.data.limiter import SourcePolicy
from app.custom.tdx_formula.ql.data.models import (
    DataStatus,
    DownloadReport,
    SourceOverview,
    UpdateReport,
)
from app.custom.tdx_formula.ql.data.service import (
    data_status,
    get_effective_config,
    get_progress,
    sources_overview,
    start_history_download,
    stop_history_download,
)
from app.custom.tdx_formula.ql.data.store import (
    build_panel,
    panel_cache_bytes,
    panel_cache_enabled,
    read_calendar,
    read_index,
    read_securities,
    set_panel_cache,
)

__all__ = [
    "Capability",
    "DataConfig",
    "DataStatus",
    "DownloadReport",
    "SourceOverview",
    "SourcePolicy",
    "UpdateReport",
    "build_panel",
    "data_status",
    "download_history",
    "get_effective_config",
    "get_progress",
    "panel_cache_bytes",
    "panel_cache_enabled",
    "read_calendar",
    "read_index",
    "read_securities",
    "set_panel_cache",
    "sources_overview",
    "start_history_download",
    "stop_history_download",
    "update_daily",
    "update_priority",
    "update_source_policy",
]
