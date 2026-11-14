"""行业和概念：从东方财富数据中心取“每只股票属于哪些板块”，写进 TSP 的行业表和概念表。

TSP 的「行业分析」「概念分析」读的是两张扩展数据表（ext_hy_ths、ext_gn_ths），出厂时配的来源是 fuyao
（付费）。这里换一个免费的来源把这两张表填上：

- 来源是数据中心的“个股所属板块”报表，一页 500 行，全市场约 200 页，一页一次请求，逐个经限流器发出
  （和财务数据共用“东方财富”的间隔和计数）。任何一页失败，这一次就整体作废，不写半截数据。
- 行业取东方财富行业分类的二级（如“白酒”），没有二级的退到一级，再退到三级；名称末尾表示级别的
  Ⅰ/Ⅱ/Ⅲ 去掉。TSP 那一列的名字仍叫“所属同花顺行业”（表里写死的），内容是东方财富的行业。
- 概念只取来源标了“精确”的题材概念（带入选理由的那些），多个用分号隔开；指数成份、融资融券、
  大盘股这类风格标签不算概念。
- 只有“现在属于哪些板块”，没有历史；每次取回来按当天的日期存一份快照。

概念成分变得很慢，默认每周更新一次（距上次成功满 7 天后的第一个周末，或者满 10 天）。
同花顺自己的成分股页面对程序访问返回 403，没有去绕。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .ql.core import clock

logger = logging.getLogger(__name__)

SOURCE = "akshare_em"
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPT_F10_CORETHEME_BOARDTYPE"
COLUMNS = "SECUCODE,SECURITY_NAME_ABBR,BOARD_NAME,IS_PRECISE,BOARD_TYPE,BOARD_LEVEL"
PAGE_SIZE = 500
MAX_PAGES = 600  # 正常约 200 页；超过这个数说明接口不对劲，停下
MIN_ROWS = 20_000  # 少于这么多行说明没取全，不写
CONCEPT_TABLE, INDUSTRY_TABLE = "ext_gn_ths", "ext_hy_ths"
CONCEPT_COLUMN, INDUSTRY_COLUMN = "所属概念", "所属同花顺行业"
REFRESH_DAYS = 7
OVERDUE_DAYS = 10
RETRY_HOURS = 6
_LEVEL_SUFFIX = re.compile(r"[ⅠⅡⅢ]$")
_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

Fetch = Callable[[dict[str, str], float], tuple[int, str]]
Progress = Callable[[int, int], Any]


def _http_get(params: dict[str, str], timeout: float) -> tuple[int, str]:
    import requests

    response = requests.get(URL, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    return response.status_code, response.text


_fetch: Fetch = _http_get


def set_fetch(fetch: Fetch | None) -> None:
    """换掉真正发请求的函数（测试用）；传 None 恢复。"""
    global _fetch
    _fetch = fetch or _http_get


# ---------------- 取数 ----------------
def download(on_page: Progress | None = None) -> list[dict[str, Any]]:
    """把全市场的“个股所属板块”逐页取回来。来源限制访问或不可用时向上抛扩展的数据错误。"""
    from .ql.core.errors import SourceRejected, SourceUnavailable
    from .ql.data.runtime import current_config, get_runtime

    limiter = get_runtime().limiter
    timeout = float(current_config().sources[SOURCE].timeout_seconds)
    rows: list[dict[str, Any]] = []
    page, pages = 1, 1
    while page <= pages:
        if page > MAX_PAGES:
            raise SourceUnavailable(f"东方财富的板块数据页数异常（超过 {MAX_PAGES} 页），已停止")
        params = {
            "reportName": REPORT, "columns": COLUMNS, "pageNumber": str(page), "pageSize": str(PAGE_SIZE),
            "sortColumns": "SECUCODE,BOARD_CODE", "sortTypes": "1,1", "source": "HSF10", "client": "PC",
        }

        def one(params: dict[str, str] = params) -> dict[str, Any]:
            try:
                status, text = _fetch(params, timeout)
            except Exception as e:
                raise SourceUnavailable(f"东方财富板块数据请求失败：{type(e).__name__}: {e}") from e
            if status in (403, 429):
                raise SourceRejected(f"东方财富限制了访问（HTTP {status}）")
            if status != 200:
                raise SourceUnavailable(f"东方财富板块数据返回了 HTTP {status}")
            try:
                body = json.loads(text)
            except ValueError as e:
                raise SourceUnavailable("东方财富板块数据返回的不是预期的格式") from e
            result = body.get("result") if isinstance(body, dict) else None
            if not isinstance(result, dict) or not result.get("data"):
                raise SourceUnavailable("东方财富板块数据返回了空结果")
            return result

        result = limiter.call(SOURCE, one)
        rows.extend(r for r in result["data"] if isinstance(r, dict))
        pages = int(result.get("pages") or 1)
        if on_page is not None:
            on_page(page, pages)
        page += 1
    return rows


# ---------------- 整理成两张表 ----------------
def build_tables(rows: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """原始行 →（概念表的行, 行业表的行）。每只股票各一行。"""
    names: dict[str, str] = {}
    concepts: dict[str, list[str]] = {}
    industries: dict[str, dict[str, str]] = {}
    for r in rows:
        symbol, board = str(r.get("SECUCODE") or ""), str(r.get("BOARD_NAME") or "").strip()
        if not _SYMBOL.match(symbol) or not board:
            continue
        names.setdefault(symbol, str(r.get("SECURITY_NAME_ABBR") or ""))
        kind = r.get("BOARD_TYPE")
        if kind == "行业":
            industries.setdefault(symbol, {}).setdefault(str(r.get("BOARD_LEVEL") or ""), _LEVEL_SUFFIX.sub("", board))
        elif kind is None and str(r.get("IS_PRECISE")) == "1":
            found = concepts.setdefault(symbol, [])
            if board not in found:
                found.append(board)
    concept_rows = [{"股票代码": s, "股票简称": names[s], CONCEPT_COLUMN: ";".join(sorted(found))}
                    for s, found in sorted(concepts.items())]
    industry_rows = []
    for symbol, levels in sorted(industries.items()):
        name = levels.get("2") or levels.get("1") or levels.get("3")
        if name:
            industry_rows.append({"股票代码": symbol, "股票简称": names[symbol], INDUSTRY_COLUMN: name})
    return concept_rows, industry_rows


# ---------------- 写进 TSP 的表 ----------------
def _write_tables(app: Any, loop: asyncio.AbstractEventLoop, data_dir: Path, day: date,
                  concept_rows: list[dict[str, str]], industry_rows: list[dict[str, str]]) -> None:
    """走 TSP 自己的扩展数据写入通道（和它的定时拉取共用同一把写锁），再刷新查询视图。"""
    from app.services.ext_data import ExtConfigStore, rows_to_parquet, run_ext_data_io

    store = ExtConfigStore(data_dir)
    for table, rows in ((CONCEPT_TABLE, concept_rows), (INDUSTRY_TABLE, industry_rows)):
        config = store.get(table)
        if config is None:
            raise RuntimeError(f"TSP 里没有 {table} 这张表（可能被删掉了），没有写入。")
        job = run_ext_data_io(rows_to_parquet, rows, config, data_dir, snapshot_date=day)
        asyncio.run_coroutine_threadsafe(job, loop).result(timeout=300)
        _stop_factory_pull(store, config)
    relabel(data_dir)
    try:
        from types import SimpleNamespace

        from app.api.ext_data import _refresh_views

        asyncio.run_coroutine_threadsafe(asyncio.to_thread(_refresh_views, SimpleNamespace(app=app)), loop) \
            .result(timeout=120)
    except Exception as e:  # noqa: BLE001 —— 视图刷新失败不影响已经写好的数据，重启后也会重新注册
        logger.warning("板块数据已写入，但刷新查询视图没有成功：%s", e)
    scheduler = getattr(app.state, "pull_scheduler", None)
    if scheduler is not None:
        try:
            scheduler.refresh(data_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("通知 TSP 的定时拉取刷新配置没有成功：%s", e)


# 表里那一列的内部名字（“所属同花顺行业”）不能改：TSP 的监控通知等地方按这个名字取数。
# 给人看的名称和说明可以改，改成和实际来源一致。只改还是出厂写法的，用户自己改过的不动。
_LABELS = {
    INDUSTRY_TABLE: {"field": INDUSTRY_COLUMN, "label": "所属行业",
                     "description": "东方财富行业分类（二级），由“免费公开来源”约每周更新一次"},
    CONCEPT_TABLE: {"field": CONCEPT_COLUMN, "label": "所属概念",
                    "description": "东方财富题材概念，由“免费公开来源”约每周更新一次"},
}


def relabel(data_dir: Path) -> None:
    """把两张表给人看的名称和说明改成和实际来源一致。只在这两张表已经由这里填过数据之后才改。"""
    from app.services.ext_data import ExtConfigStore

    store = ExtConfigStore(data_dir)
    for table, wanted in _LABELS.items():
        config = store.get(table)
        if config is None:
            continue
        changed = False
        for f in config.fields:
            if f.name == wanted["field"] and f.label in (f.name, "") and f.label != wanted["label"]:
                f.label, changed = wanted["label"], True
        if "fuyao" in str(config.description or "").lower():
            config.description, changed = wanted["description"], True
        if changed:
            store.upsert(config, keep_strategy_cache=True)


def _stop_factory_pull(store: Any, config: Any) -> None:
    """这张表改由这里来填以后，把出厂配的“每天去 fuyao 拉取”关掉（没有 fuyao 的 Key 时它每天都在报错）。

    只关还指向 fuyao 的那一个；用户自己改过地址的不动。
    """
    pull = getattr(config, "pull", None)
    if pull is None or not getattr(pull, "enabled", False) or "fuyao" not in str(getattr(pull, "url", "")).lower():
        return
    pull.enabled = False
    store.upsert(config, keep_strategy_cache=True)
    logger.info("%s 改由免费公开来源更新，已关闭它原来每天去 fuyao 的拉取", config.id)


# ---------------- 什么时候更新、现在什么状态 ----------------
class BoardService:
    def __init__(self, root: Path) -> None:
        self._path = root / "boards" / "state.json"
        self._lock = threading.Lock()
        self._running = False
        self._progress: tuple[int, int] = (0, 0)
        self._last_attempt: datetime | None = None
        self._relabeled = False

    def ensure_labels(self, data_dir: Path) -> None:
        """已经取过板块数据的，把表的显示名称对齐一次（每次启动只做一次）。"""
        if self._relabeled or not self._state().get("updated_at"):
            return
        self._relabeled = True
        try:
            relabel(data_dir)
        except Exception as e:  # noqa: BLE001 —— 改显示名称失败不影响数据
            logger.warning("调整行业表、概念表的显示名称没有成功：%s", e)

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, **changes: Any) -> None:
        state = {**self._state(), **changes}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    def info(self) -> dict[str, Any]:
        state = self._state()
        with self._lock:
            running, (done, total) = self._running, self._progress
        return {
            "running": running, "page": done, "pages": total,
            "updated_at": state.get("updated_at"), "stocks_with_industry": state.get("stocks_with_industry"),
            "stocks_with_concept": state.get("stocks_with_concept"), "industries": state.get("industries"),
            "concepts": state.get("concepts"), "message": state.get("message"),
            "refresh_days": REFRESH_DAYS,
        }

    def due(self, now: datetime) -> bool:
        """现在该不该自动更新：从没成功过；或满 7 天后的第一个周末；或已经 10 天没更新。失败后隔几小时再试。"""
        with self._lock:
            if self._running:
                return False
            if self._last_attempt is not None and now - self._last_attempt < timedelta(hours=RETRY_HOURS):
                return False
        updated = self._state().get("updated_at")
        if not updated:
            return True
        try:
            age = now - datetime.fromisoformat(str(updated))
        except ValueError:
            return True
        if age >= timedelta(days=OVERDUE_DAYS):
            return True
        return age >= timedelta(days=REFRESH_DAYS) and now.weekday() >= 5

    def start(self, app: Any, loop: asyncio.AbstractEventLoop, data_dir: Path) -> bool:
        """在后台线程里更新一次。已经在更新时返回 False。"""
        with self._lock:
            if self._running:
                return False
            self._running, self._progress, self._last_attempt = True, (0, 0), clock.now()
        threading.Thread(target=self._run, args=(app, loop, data_dir), name="tdx-formula-boards", daemon=True).start()
        return True

    def _run(self, app: Any, loop: asyncio.AbstractEventLoop, data_dir: Path) -> None:
        def on_page(done: int, total: int) -> None:
            with self._lock:
                self._progress = (done, total)

        try:
            rows = download(on_page)
            if len(rows) < MIN_ROWS:
                raise RuntimeError(f"只取回 {len(rows)} 行，明显不全，这一次没有写入。")
            concept_rows, industry_rows = build_tables(rows)
            _write_tables(app, loop, data_dir, clock.today(), concept_rows, industry_rows)
            now = clock.now()
            self._save(
                updated_at=now.isoformat(timespec="seconds"),
                stocks_with_industry=len(industry_rows), stocks_with_concept=len(concept_rows),
                industries=len({r[INDUSTRY_COLUMN] for r in industry_rows}),
                concepts=len({c for r in concept_rows for c in r[CONCEPT_COLUMN].split(";") if c}),
                message=f"{now:%m-%d %H:%M} 更新成功。",
            )
            logger.info("板块数据已更新：行业 %d 只股票，概念 %d 只股票", len(industry_rows), len(concept_rows))
        except Exception as e:  # noqa: BLE001 —— 后台任务：把原因记下来给页面看，不让线程带着异常退出
            reason = getattr(e, "message", None) or f"{type(e).__name__}: {e}"
            self._save(message=f"{clock.now():%m-%d %H:%M} 更新没有成功：{reason}")
            logger.warning("板块数据更新没有成功：%s", reason)
        finally:
            with self._lock:
                self._running = False
