"""历史下载（断点续传）与每日增量。流程见技术设计 §7.5。

历史下载的顺序：交易日历、证券清单、指数、每只股票的日线和复权因子、每只股票的 ST 历史。
日线与复权因子、ST 历史分别由两个线程串行处理（各自访问的来源不同；同一个来源任何时候都只有一个请求，
由限流器保证）。每完成一只股票的一项数据就保存一次进度。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.custom.tdx_formula.ql.core import clock
from app.custom.tdx_formula.ql.core.errors import DataError, QuantLabError
from app.custom.tdx_formula.ql.data import store
from app.custom.tdx_formula.ql.data.config import DataConfig
from app.custom.tdx_formula.ql.data.interface import Capability, NoSourceAvailable
from app.custom.tdx_formula.ql.data.lock import job_lock
from app.custom.tdx_formula.ql.data.models import DownloadReport, UpdateReport
from app.custom.tdx_formula.ql.data.router import Router
from app.custom.tdx_formula.ql.data.runtime import current_config, get_runtime
from app.custom.tdx_formula.ql.data.store import (
    DONE,
    ITEM_DAILY,
    ITEM_FACTOR,
    ITEM_SHARES,
    ITEM_ST,
    Progress,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]
_BAR_VALUE_COLUMNS = ("open", "high", "low", "close", "preclose", "volume", "amount")


def effective_end(config: DataConfig) -> date:
    """最近一个"数据已经齐了"的自然日：过了配置的入库时间取今天，否则取昨天。"""
    now = clock.now()
    return now.date() if now.hour >= config.download.data_ready_hour else now.date() - timedelta(days=1)


# ======================= 历史下载 =======================
def download_history(
    start: date,
    codes: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
    *,
    limit: int | None = None,
    stop: threading.Event | None = None,
    router: Router | None = None,
) -> DownloadReport:
    """下载从 ``start`` 起的历史数据。可以反复运行：已完成的跳过，没完成的接着下。

    ``codes``：只下载这些股票（默认是证券清单里这段时间上市过的全部股票）；``limit``：只取前 N 只。
    已有任务在运行时抛 ``ConflictError``。
    """
    with job_lock():
        rt = get_runtime()
        router = router or rt.router
        router.limiter.begin_task()
        started_at, t0 = clock.now(), time.monotonic()
        report = DownloadReport(start=start)
        try:
            # 下载期间数据文件一直在变，这时每读一次行情都重写一遍硬盘缓存是白费功夫
            store.pause_disk_cache(True)
            _run_history(router, start, codes, limit, on_progress, stop or threading.Event(), report)
        except QuantLabError as e:
            report.message = e.message
            logger.error("历史下载中断：%s", e.message)
        finally:
            store.pause_disk_cache(False)
            router.close()
            report.seconds = round(time.monotonic() - t0, 1)
            rt.state.log_update("history", started_at, clock.now(), report.finished, report.message)
        logger.info("历史下载结束：%s", report.message)
        return report


def _run_history(
    router: Router, start: date, codes: list[str] | None, limit: int | None,
    on_progress: ProgressCallback | None, stop: threading.Event, report: DownloadReport,
) -> None:
    config = current_config()
    end = effective_end(config)
    progress = Progress.load()
    if progress.start is not None and start.isoformat() < progress.start:
        logger.info("起始日期比上次更早（%s -> %s），个股数据重新下载", progress.start, start)
        progress.stocks.clear()
        progress.indices.clear()
    if progress.start is None or start.isoformat() < progress.start:
        progress.start = start.isoformat()
    progress.finished = False
    progress.save()

    notes: list[str] = []

    def notify(phase: str, current: str = "") -> None:
        if on_progress is not None:
            on_progress({
                "phase": phase, "current": current, "total": report.total,
                "done": {item: progress.count_done(item, targets) for item in store.ITEMS},
                "failed": len(report.failed),
            })

    targets: list[str] = []

    # ---- 交易日历、证券清单 ----
    notify("交易日历")
    try:
        cal = router.fetch(Capability.TRADE_CALENDAR, start=start, end=end)
        store.write_calendar(cal["date"])
        progress.calendar = True
    except NoSourceAvailable as e:
        if not progress.calendar:
            raise DataError(f"取不到交易日历，下载无法开始。{e.message}") from e
        notes.append("交易日历这次没能更新，沿用已有的")
    notify("证券清单")
    try:
        store.write_securities(router.fetch(Capability.SECURITIES))
        progress.securities = True
    except NoSourceAvailable as e:
        if not progress.securities and codes is None:
            raise DataError(f"取不到证券清单，下载无法开始。{e.message}") from e
        notes.append("证券清单这次没能更新，沿用已有的")
    _refresh_industry(router)
    progress.save()

    targets = list(codes) if codes is not None else store.stock_codes_in_range(store.read_securities(), start, end)
    if limit is not None:
        targets = targets[:limit]
    report.total = len(targets)

    # ---- 指数 ----
    for index_code in config.download.indices:
        if stop.is_set() or progress.indices.get(index_code) == DONE:
            continue
        notify("指数", index_code)
        try:
            df = router.fetch(Capability.INDEX_DAILY, code=index_code, start=start, end=end, accept_empty=False)
            store.merge_index(index_code, df)
            progress.indices[index_code] = DONE
        except NoSourceAvailable as e:
            report.failed[f"{index_code}/index"] = e.message
        progress.save()

    # ---- 个股：日线 + 复权因子、ST 历史 ----
    lock = threading.Lock()
    crashes: list[BaseException] = []

    def fetched(item: str) -> None:
        with lock:
            report.fetched[item] = report.fetched.get(item, 0) + 1

    def failed(code: str, item: str, reason: str) -> None:
        with lock:
            report.failed[f"{code}/{item}"] = reason
        progress.mark(code, item, f"failed: {reason}"[:300])

    def price_worker() -> None:
        for code in targets:
            if stop.is_set():
                return
            try:
                if not progress.is_done(code, ITEM_DAILY):
                    notify("日线", code)
                    source, has_factor, has_shares = _download_bars(router, code, start, end)
                    extra = {"end": end.isoformat(), "source": source}
                    progress.mark(code, ITEM_DAILY, **extra)
                    fetched(ITEM_DAILY)
                    if has_factor:
                        progress.mark(code, ITEM_FACTOR)
                    if has_shares:
                        progress.mark(code, ITEM_SHARES)  # 日线里顺带有流通股本，不用再取一次
                if not progress.is_done(code, ITEM_FACTOR):
                    _download_factor(router, code, start, end, progress.stocks.get(code, {}).get("source"))
                    progress.mark(code, ITEM_FACTOR)
                    fetched(ITEM_FACTOR)
                if not progress.is_done(code, ITEM_SHARES):
                    # 早期版本下载的数据里没有流通股本：再取一次日线，只把流通股本这一列补进去
                    notify("流通股本", code)
                    # 来源都答复了、但都没有这只股票的流通股本（多半是很早退市的）：记为完成并注明，不再反复去取。
                    # 来源暂时不可用时会抛异常，走下面的失败分支，下次再试。
                    found = _download_shares(router, code, start, end)
                    progress.mark(code, ITEM_SHARES, shares_note="" if found else "各来源都没有这只股票的流通股本")
                    fetched(ITEM_SHARES)
            except NoSourceAvailable as e:
                if e.blocked:
                    with lock:
                        notes.append(f"日线、复权因子和流通股本暂停在 {code}：{e.message}")
                    return
                failed(code, _pending_item(progress, code), e.message)
            except QuantLabError as e:
                failed(code, _pending_item(progress, code), e.message)

    def st_worker() -> None:
        for code in targets:
            if stop.is_set():
                return
            if progress.is_done(code, ITEM_ST):
                continue
            try:
                notify("ST 历史", code)
                df = router.fetch(Capability.ST_HISTORY, code=code, start=start, end=end)
                if not df.empty:
                    store.merge_daily(code, df[["date", "is_st"]])
                progress.mark(code, ITEM_ST)
                fetched(ITEM_ST)
            except NoSourceAvailable as e:
                if e.blocked:
                    with lock:
                        notes.append(f"ST 历史暂停在 {code}：{e.message}")
                    return
                failed(code, ITEM_ST, e.message)
            except QuantLabError as e:
                failed(code, ITEM_ST, e.message)

    def guarded(fn: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except BaseException as e:  # noqa: BLE001 —— 线程里的意外错误带回主线程再抛
                crashes.append(e)
                stop.set()
        return run

    workers = [
        threading.Thread(target=guarded(price_worker), name="history-price"),
        threading.Thread(target=guarded(st_worker), name="history-st"),
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    if crashes:
        progress.save()
        raise crashes[0]

    report.done = {item: progress.count_done(item, targets) for item in store.ITEMS}
    indices_done = all(progress.indices.get(i) == DONE for i in config.download.indices)
    report.finished = (
        not stop.is_set() and indices_done and all(report.done[item] == len(targets) for item in store.ITEMS)
    )
    if codes is None and limit is None:
        progress.finished = report.finished
    progress.save()
    notify("结束")
    if stop.is_set():
        notes.append("任务被手动停止")
    summary = (
        f"共 {len(targets)} 只股票：日线完成 {report.done[ITEM_DAILY]}，复权因子完成 {report.done[ITEM_FACTOR]}，"
        f"ST 历史完成 {report.done[ITEM_ST]}，流通股本完成 {report.done[ITEM_SHARES]}，失败 {len(report.failed)} 项。"
    )
    summary += "全部完成。" if report.finished else "还有没完成的，下次运行会接着下。"
    report.message = summary + ("".join(f" {n}。" for n in notes) if notes else "")


def _pending_item(progress: Progress, code: str) -> str:
    """价格这条线上，这只股票现在卡在哪一项。"""
    for item in (ITEM_DAILY, ITEM_FACTOR):
        if not progress.is_done(code, item):
            return item
    return ITEM_SHARES


def _refresh_industry(router: Router) -> None:
    """更新行业分类。取不到不影响别的数据，沿用已有的。"""
    try:
        df = router.fetch(Capability.INDUSTRY)
    except NoSourceAvailable as e:
        logger.warning("行业分类这次没能更新，沿用已有的：%s", e.message)
        return
    if not df.empty:
        store.write_industry(df)


def _download_bars(router: Router, code: str, start: date, end: date) -> tuple[str, bool, bool]:
    """取一只股票的不复权日线并写入文件。返回（来源名称, 是否顺带得到了复权因子, 是否顺带得到了流通股本）。"""
    df = router.fetch(Capability.STOCK_DAILY, code=code, start=start, end=end, accept_empty=False)
    source = str(df.attrs.get("source", ""))
    if df.empty:
        return source, True, True  # 这段时间没有任何行情（例如一直停牌），没有可复权、可补的东西
    bars = df.loc[~df["suspended"]].drop(columns=["suspended", "code"])
    has_factor = "factor" in bars.columns and bool(bars["factor"].notna().all())
    has_shares = "float_shares" in bars.columns and bool(bars["float_shares"].notna().any())
    if has_factor:
        bars = fill_preclose(bars)
    bars = bars.assign(source=source)
    store.merge_daily(code, bars)
    return source, has_factor, has_shares


def _download_shares(router: Router, code: str, start: date, end: date) -> bool:
    """给已有的日线补上流通股本。只动这一列，价格等别的数据不碰。返回来源有没有提供。"""
    existing = store.read_daily(code)
    if existing is None:
        return False
    have = existing.loc[existing["close"].notna(), "date"]
    if have.empty:
        return False
    df = router.fetch(Capability.FLOAT_SHARES, code=code, start=start, end=end, accept_empty=False)
    # 只补到已有行情的日子上：文件里"有日期、没有收盘价"的行表示停牌，不能凭空多出这样的行
    shares = df.loc[df["float_shares"].notna() & df["date"].isin(have), ["date", "float_shares"]]
    if shares.empty:
        return False
    store.merge_daily(code, shares)
    return True


def _download_factor(router: Router, code: str, start: date, end: date, prefer: str | None) -> None:
    """取复权因子，按日期对到已有的日线上；来源没给前收盘价的，顺便用因子推出来。"""
    existing = store.read_daily(code)
    if existing is None:
        return
    bars = existing.loc[existing["close"].notna(), ["date", "close", "preclose"]].sort_values("date")
    if bars.empty:
        return
    factors = router.fetch(Capability.ADJUST_FACTOR, code=code, start=start, end=end, prefer=prefer)
    if factors.empty:
        raise DataError(f"{code} 的复权因子是空的")
    joined = pd.merge_asof(bars, factors[["date", "factor"]].sort_values("date"), on="date", direction="backward")
    if joined["factor"].isna().any():
        # 最早的几天早于因子表的第一天：沿用第一天的因子
        joined["factor"] = joined["factor"].bfill()
    store.merge_daily(code, fill_preclose(joined)[["date", "factor", "preclose"]])


def fill_preclose(bars: pd.DataFrame) -> pd.DataFrame:
    """来源没给前收盘价的行，用"上一根的收盘价 × 上一根的因子 ÷ 当天的因子"推出（四舍五入到分）。

    没有除权除息时因子不变，结果就是上一根的收盘价；除权除息日得到的是除权参考价。第一行没有上一根，保持为空。
    """
    out = bars.sort_values("date").copy()
    derived = (out["close"].shift(1) * out["factor"].shift(1) / out["factor"]).round(2)
    out["preclose"] = out["preclose"].where(out["preclose"].notna(), derived)
    return out


# ======================= 每日增量 =======================
def update_daily(day: date | None = None, *, router: Router | None = None) -> UpdateReport:
    """把本地数据补到 ``day``（默认是最近一个数据已入库的交易日）。

    用"某日全市场"能力一次取回一天，追加到各股票文件；中间缺的交易日会依次补上。
    已有任务在运行时抛 ``ConflictError``；还没下载过历史数据时抛 ``DataError``。
    """
    with job_lock():
        rt = get_runtime()
        router = router or rt.router
        router.limiter.begin_task()
        started_at = clock.now()
        report = UpdateReport()
        try:
            _run_update(router, day, report)
        except QuantLabError as e:
            report.ok = False
            report.message = e.message
            logger.error("每日更新失败：%s", e.message)
            raise
        finally:
            router.close()
            rt.state.log_update("daily", started_at, clock.now(), report.ok, report.message)
        logger.info("每日更新结束：%s", report.message)
        return report


def _run_update(router: Router, day: date | None, report: UpdateReport) -> None:
    config = current_config()
    progress = Progress.load()
    base = store.updated_through() or progress.history_end()
    if base is None:
        raise DataError("本地还没有历史数据，请先运行历史下载")
    target = day or effective_end(config)
    if target <= base:
        report.ok, report.message = True, f"数据已经是最新的（到 {base}）"
        return

    logger.info("每日更新：从 %s 补到 %s", base, target)
    try:
        cal = router.fetch(Capability.TRADE_CALENDAR, start=base, end=target)
        store.write_calendar(cal["date"])
    except NoSourceAvailable as e:
        raise DataError(f"取不到交易日历，无法确定要补哪几天。{e.message}") from e
    calendar = store.read_calendar()
    days = [d.astype(object) for d in calendar[(calendar > np.datetime64(base)) & (calendar <= np.datetime64(target))]]
    if not days:
        store.update_meta(updated_through=target.isoformat())
        report.ok, report.message = True, f"{base} 之后到 {target} 没有交易日，数据已经是最新的"
        return
    if len(days) > config.download.max_catchup_days:
        raise DataError(
            f"本地数据停在 {base}，到 {target} 缺了 {len(days)} 个交易日，超过一次最多补的 "
            f"{config.download.max_catchup_days} 天。请重新运行历史下载"
        )
    try:
        store.write_securities(router.fetch(Capability.SECURITIES))
    except NoSourceAvailable as e:
        logger.warning("证券清单这次没能更新，沿用已有的：%s", e.message)
    _refresh_industry(router)

    notes: list[str] = []
    for d in days:
        snapshot = router.fetch(Capability.MARKET_SNAPSHOT, day=d)
        if snapshot.empty:
            notes.append(f"{d} 的行情来源还没有入库，稍后再试")
            break
        changed = _apply_snapshot(snapshot, d)
        store.update_meta(updated_through=d.isoformat())
        report.days.append(d)
        report.stocks = int((~snapshot["suspended"]).sum())
        report.factor_changed.extend(changed)
        logger.info("已写入 %s：%d 只股票，其中 %d 只当天除权除息", d, report.stocks, len(changed))

    if report.days:
        for index_code in config.download.indices:
            try:
                df = router.fetch(
                    Capability.INDEX_DAILY, code=index_code, start=base, end=report.days[-1], accept_empty=False
                )
                store.merge_index(index_code, df)
            except NoSourceAvailable as e:
                notes.append(f"指数 {index_code} 没能更新")
                logger.warning("指数 %s 没能更新：%s", index_code, e.message)

    report.ok = len(report.days) == len(days)
    if report.days:
        report.message = (
            f"已补上 {len(report.days)} 个交易日（到 {report.days[-1]}），最后一天 {report.stocks} 只股票有成交。"
        )
    report.message += "".join(f"{n}。" for n in notes)


def _apply_snapshot(snapshot: pd.DataFrame, day: date) -> list[str]:
    """把某天的全市场行情追加到各股票文件。返回当天复权因子发生变化（除权除息）的股票。

    复权因子的接续：前收盘价等于上一根的收盘价时因子不变；不相等说明当天除权除息，
    新因子 = 旧因子 × 上一根收盘价 ÷ 当天的前收盘价（除权参考价）。这与历史因子的口径一致。
    """
    changed: list[str] = []
    stamp = pd.Timestamp(day)
    source = str(snapshot.attrs.get("source", ""))
    records: list[dict[str, Any]] = snapshot.to_dict("records")  # type: ignore[assignment]
    for row in records:
        code = str(row["code"])
        existing = store.read_daily(code)
        if existing is not None and bool(((existing["date"] >= stamp) & existing["close"].notna()).any()):
            continue  # 这一天（或更晚）的行情已经有了，不重复写
        new: dict[str, Any] = {"date": stamp, "is_st": bool(row["is_st"])}
        close, preclose = float(row["close"]), float(row["preclose"])
        if not row["suspended"] and not np.isnan(close):
            factor = 1.0
            if existing is not None:
                bars = existing[existing["close"].notna()]
                if len(bars):
                    last_close = float(bars["close"].iloc[-1])
                    factor = float(bars["factor"].iloc[-1])
                    if not np.isnan(preclose) and preclose > 0 and store.price_changed(last_close, preclose):
                        factor = factor * last_close / preclose
                        changed.append(code)
            new.update({c: float(row[c]) for c in _BAR_VALUE_COLUMNS})
            shares = row.get("float_shares")
            if shares is not None and np.isfinite(shares) and shares > 0:
                new["float_shares"] = float(shares)
            new["factor"] = factor
            new["source"] = source
        store.merge_daily(code, pd.DataFrame([new]))
    return changed
