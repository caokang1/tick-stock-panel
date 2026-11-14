"""行情的下载和更新：全部来自免费的公开来源（新浪、腾讯、东方财富经 AKShare，以及 BaoStock），带限速。

限速是为了遵守这些来源的使用规则、避免被限制访问：所有请求排队一个一个发、请求之间留间隔、每天有上限、
被拒绝后自动暂停。数值可以在页面上调，但不能超过来源官方公布的上限。
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Any

from .ql import data as ql_data
from .ql.core import clock
from .ql.core.errors import ConflictError, DataError, QuantLabError
from .ql.data import config as data_config
from .ql.data import store as ql_store
from .ql.live import store as live_store
from .service import FormulaService

logger = logging.getLogger(__name__)

SOURCE_LABELS = {
    "baostock": "BaoStock（证券宝）", "akshare_em": "东方财富", "akshare_sina": "新浪财经", "akshare_tx": "腾讯财经",
    "tencent_quote": "腾讯实时行情",
}
_STATE_MAP = {"normal": "ok", "cooling": "cooldown", "capped": "cap_reached", "disabled": "disabled",
              "halted": "cooldown"}
KEY_RUN_TIME = "data_update_time"
DEFAULT_RUN_TIME = "18:30"
LATEST_RUN_TIME = time(23, 30)
RETRY_MINUTES = 30
MISSING_LIBS = "还没有安装取数用的库（akshare、baostock）。请到 TSP 的“设置 → 数据源”里给“免费公开来源”点安装，或者手动安装后重启。"


def libraries_ready() -> bool:
    import importlib.util
    return all(importlib.util.find_spec(name) is not None for name in ("akshare", "baostock"))


class DataService:
    def __init__(self, formulas: FormulaService) -> None:
        self._formulas = formulas
        self._lock = threading.Lock()
        self._update: dict[str, Any] = {"running": False, "message": None, "at": None}
        self._last_attempt: datetime | None = None

    # ---------------- 数据来源和限速 ----------------
    def sources(self) -> dict[str, Any]:
        defaults = data_config.parse(data_config.load_default_raw()).to_dict()["sources"]
        config = ql_data.get_effective_config().to_dict()
        items = []
        for overview in ql_data.sources_overview():
            o = overview.to_dict()
            message: str | None = None
            if o["state"] == "cooling":
                minutes = max(1, int((o.get("cooldown_remaining_seconds") or 0) // 60))
                message = f"这个来源暂时限制了访问，约 {minutes} 分钟后自动恢复。"
            elif o["state"] == "halted":
                message = "连续多次请求失败，本次任务里已停用这个来源；下次任务会重新尝试。"
            elif o["state"] == "capped":
                message = "今天的请求次数已达到上限，明天自动继续。"
            elif o["state"] == "disabled":
                message = "已停用。"
            until = o.get("cooldown_until")
            items.append({
                "name": o["name"], "label": SOURCE_LABELS.get(o["name"], o["name"]),
                "state": _STATE_MAP.get(o["state"], "ok"), "state_message": message,
                "cooldown_until": str(until).replace("T", " ")[:19] if until else None,
                "today_requests": int(o["today_requests"]), "policy": o["policy"],
                "defaults": defaults.get(o["name"], o["policy"]),
            })
        return {"sources": items, "priority": config["priority"], "libraries_ready": libraries_ready()}

    def realtime(self) -> dict[str, Any]:
        """实时行情两轮全市场快照之间的间隔（秒），和允许的范围。"""
        return {"interval_seconds": ql_data.get_effective_config().realtime_interval,
                "interval_min": data_config.REALTIME_INTERVAL_MIN, "interval_max": data_config.REALTIME_INTERVAL_MAX,
                "interval_default": data_config.parse(data_config.load_default_raw()).realtime_interval}

    def set_realtime_interval(self, seconds: float) -> dict[str, Any]:
        data_config.update_realtime_interval(seconds)
        return self.realtime()

    def update_source(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        _, warnings = ql_data.update_source_policy(name, {k: v for k, v in changes.items() if v is not None})
        return {**self.sources(), "warnings": warnings}

    # ---------------- 下载和更新 ----------------
    def _history_progress(self) -> dict[str, Any]:
        p = ql_data.get_progress()
        running = bool(p.get("running"))
        total = int(p.get("total") or 0)
        counts = p.get("done") or {}
        done = int(min(counts.values())) if counts else 0  # 一只股票要下几样数据，进度取最慢的那一样
        eta: float | None = None
        started = p.get("started_at")
        if running and started and done > 0 and total > done:
            try:
                elapsed = (clock.now() - datetime.fromisoformat(str(started))).total_seconds()
                eta = max(0.0, elapsed / done * (total - done))
            except ValueError:
                eta = None
        phase = p.get("phase")
        return {"running": running, "kind": "download" if (running or p.get("finished_at")) else None,
                "stage": str(phase) if phase else None, "done": done, "total": total, "eta_seconds": eta,
                "message": p.get("message") or None}

    def progress(self) -> dict[str, Any]:
        with self._lock:
            if self._update["running"]:
                return {"running": True, "kind": "update", "stage": "当日行情", "done": 0, "total": 0,
                        "eta_seconds": None, "message": "正在更新当天的行情……"}
            last, at = self._update["message"], self._update["at"]
        progress = self._history_progress()
        if progress["running"] or not last:
            return progress
        # 下载和更新都跑过时，显示后跑完的那一个的结果
        finished = str(ql_data.get_progress().get("finished_at") or "")
        if progress["kind"] is None or (at is not None and at.isoformat() >= finished):
            progress = {"running": False, "kind": "update", "stage": None, "done": 0, "total": 0,
                        "eta_seconds": None, "message": last}
        return progress

    def busy(self) -> bool:
        with self._lock:
            if self._update["running"]:
                return True
        return bool(ql_data.get_progress().get("running"))

    @staticmethod
    def _need_libraries() -> None:
        if not libraries_ready():
            raise DataError(MISSING_LIBS)

    def start_download(self, start: str | None) -> dict[str, Any]:
        self._need_libraries()
        with self._lock:
            if self._update["running"]:
                raise ConflictError("正在更新当天的行情，请等它结束后再开始下载。")
        day: date | None = None
        if start:
            try:
                day = date.fromisoformat(start)
            except ValueError as e:
                raise QuantLabError(f"开始日期的格式应为 2015-01-01 这样，收到的是：{start}") from e
        ql_data.start_history_download(day)
        self._formulas.clear_cache()
        return self.progress()

    @staticmethod
    def stop_download() -> None:
        ql_data.stop_history_download()

    def run_update(self) -> str:
        """把行情补到最新一个已经入库的交易日（同步执行）。返回一句结果说明。"""
        self._need_libraries()
        if ql_data.get_progress().get("running"):
            raise ConflictError("历史数据正在下载，下载完成后会包含最新的行情，不需要再手动更新。")
        with self._lock:
            if self._update["running"]:
                raise ConflictError("已经有一次更新在进行中。")
            self._update.update(running=True, message=None)
        message = ""
        try:
            report = ql_data.update_daily()
            message = str(getattr(report, "message", None) or "当日行情更新完成。")
        except QuantLabError as e:
            message = f"更新没有成功：{e.message}"
        except Exception:
            logger.exception("当日行情更新出现未预期的错误")
            message = "更新时出现了未预期的错误，详情见后端日志。"
        finally:
            self._formulas.clear_cache()
            with self._lock:
                self._update.update(running=False, message=message, at=clock.now())
        return message

    def start_update(self) -> dict[str, Any]:
        self._need_libraries()
        if self.busy():
            raise ConflictError("已经有下载或更新在进行中。")
        threading.Thread(target=self._quiet_update, name="tdx-formula-update", daemon=True).start()
        return {"running": True, "kind": "update", "stage": "当日行情", "done": 0, "total": 0,
                "eta_seconds": None, "message": "正在更新当天的行情……"}

    def _quiet_update(self) -> None:
        try:
            self.run_update()
        except QuantLabError as e:
            logger.warning("当日行情没有更新：%s", e.message)

    # ---------------- 每天自动更新的时间 ----------------
    @staticmethod
    def earliest_run_time() -> time:
        """最早可以设到几点：数据来源要到这个钟点之后才有当天完整的行情（收盘是 15:00，入库更晚）。"""
        return time(int(ql_data.get_effective_config().download.data_ready_hour), 0)

    def parse_run_time(self, text: str) -> time:
        try:
            hour, minute = (int(x) for x in str(text).strip().split(":"))
            value = time(hour, minute)
        except (ValueError, TypeError) as e:
            raise QuantLabError(f"时间的格式应为 时:分，例如 18:30。收到的是：{text}") from e
        lo = self.earliest_run_time()
        if not lo <= value <= LATEST_RUN_TIME:
            raise QuantLabError(
                f"每天自动更新的时间要在 {lo:%H:%M} 到 {LATEST_RUN_TIME:%H:%M} 之间。股市 15:00 收盘，"
                f"数据来源要到 {lo:%H:%M} 之后才有当天完整的行情，更早拿不到当天的数据；太晚则可能跨到第二天。")
        return value

    def run_at(self) -> time:
        try:
            return self.parse_run_time(str(live_store.get_setting(KEY_RUN_TIME, DEFAULT_RUN_TIME)))
        except QuantLabError:
            return self.parse_run_time(DEFAULT_RUN_TIME)

    def set_run_time(self, text: str) -> None:
        live_store.set_setting(KEY_RUN_TIME, self.parse_run_time(text).strftime("%H:%M"))

    def schedule(self) -> dict[str, Any]:
        return {"run_time": self.run_at().strftime("%H:%M"),
                "run_time_min": self.earliest_run_time().strftime("%H:%M"),
                "run_time_max": LATEST_RUN_TIME.strftime("%H:%M")}

    def update_due(self, now: datetime) -> bool:
        """现在该不该自动更新一次：到了设定的时间、今天的行情还没有、离上次尝试已经过了一阵子。"""
        if not self._formulas.has_data() or not libraries_ready() or self.busy():
            return False
        if now.weekday() >= 5 or now.time() < self.run_at():
            return False
        through = ql_store.updated_through() or ql_store.latest_date()
        if through is not None and through >= now.date():
            return False
        return self._last_attempt is None or now - self._last_attempt >= timedelta(minutes=RETRY_MINUTES)

    def tick(self) -> None:
        now = clock.now()
        if self.update_due(now):
            self._last_attempt = now
            self._quiet_update()
