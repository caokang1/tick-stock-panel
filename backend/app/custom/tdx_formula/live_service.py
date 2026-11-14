"""策略、组合回测、模拟账户、每日提醒、实际持仓。规则和 quant-lab 一致，数据读 quant-lab 的行情目录。

每天的提醒不是这里去下载行情触发的：quant-lab 自己在收盘后更新行情，这里发现行情到了新的一天，
就把运行中的策略推进到这一天、生成提醒，并经 TSP 的推送通道（飞书、企业微信）发出去。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from .ql import data as ql_data
from .ql import loader
from .ql.backtest import portfolio, result_store, strategy_run, strategy_store
from .ql.backtest.signals import Costs
from .ql.blocks import all_blocks
from .ql.blocks.model import KIND_RANK, KIND_TIMING
from .ql.core import clock, db
from .ql.core.errors import ConflictError, NotFoundError, QuantLabError
from .ql.core.panel import Panel
from .ql.live import engine, push, real, settings, store
from .ql.live import real_store as rs
from .service import FormulaService

logger = logging.getLogger(__name__)

KEY_LAST_DATE = "last_processed_date"
KEY_PUSHED = "last_pushed_date"
Sender = Callable[[str], list[str]]


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


class LiveService:
    def __init__(self, formulas: FormulaService, root: Path) -> None:
        self._formulas = formulas
        self._root = root
        self._run_lock = threading.Lock()
        db.set_path(root / "tdx.db")
        strategy_store.init_schema()
        store.init_schema()

    # ---------------- 公共 ----------------
    def _panel(self, start: str | None = None, end: str | None = None) -> Panel:
        market, _ = self._formulas.market_dir()
        ql_data.set_market_dir(market)
        return self._formulas.panel(start, end)

    def _names(self) -> dict[str, str]:
        market, _ = self._formulas.market_dir()
        return loader.stock_names(market) if market else {}

    def _evaluate(self, block: Any, panel: Panel, params: Mapping[str, float] | None = None) -> np.ndarray:
        return self._formulas._evaluate(block, panel, params)

    def latest_data_date(self) -> str | None:
        """quant-lab 的行情更新到哪一天了。只读它的概况文件，不用把全市场数据读进内存。"""
        market, _ = self._formulas.market_dir()
        if market is None:
            return None
        try:
            meta = json.loads((market / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return meta.get("updated_through") or meta.get("latest_date")

    # ---------------- 可选项 ----------------
    @staticmethod
    def options() -> dict[str, Any]:
        blocks = all_blocks()
        return {
            "rank_blocks": [b.to_dict() for b in blocks if b.kind == KIND_RANK],
            "timing_blocks": [b.to_dict() for b in blocks if b.kind == KIND_TIMING],
            "indexes": [{"code": c, "name": n} for c, n in strategy_run.INDEX_NAMES.items()],
            "trade_defaults": portfolio.TradeRules().to_dict(),
        }

    # ---------------- 策略 ----------------
    def _summary(self, s: strategy_store.Strategy, backtests: Mapping[str, Any]) -> dict[str, Any]:
        saved = backtests.get(s.id)
        brief = None
        if saved:
            summary = saved.get("summary", {})
            brief = {"annual_return": summary.get("annual_return"), "max_drawdown": summary.get("max_drawdown"),
                     "created_at": saved.get("created_at")}
        account = store.get_account(s.id)
        live = None
        if account is not None:
            equity = float(account["state"].get("equity") or account["initial_cash"])
            live = {"total_return": equity / account["initial_cash"] - 1, "started_on": account["started_on"],
                    "last_date": account["last_date"]}
        return {"id": s.id, "name": s.name, "status": s.status, "updated_at": s.updated_at,
                "condition_count": len(s.config.get("conditions", [])), "backtest": brief, "live": live,
                "real_return": real.total_return(s.id)}

    def strategies(self) -> list[dict[str, Any]]:
        backtests = result_store.load_all()
        return [self._summary(s, backtests) for s in strategy_store.list_strategies()]

    @staticmethod
    def detail(strategy_id: str) -> dict[str, Any]:
        s = strategy_store.get_strategy(strategy_id)
        return {"id": s.id, "name": s.name, "status": s.status, "config": s.config,
                "created_at": s.created_at, "updated_at": s.updated_at}

    def create(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        if any(s.name == name.strip() for s in strategy_store.list_strategies()):
            raise QuantLabError(f"已经有一个叫“{name.strip()}”的策略了，请换个名字。")
        return self.detail(strategy_store.create_strategy(name, config=config).id)

    def update(self, strategy_id: str, name: str | None, config: dict[str, Any] | None) -> dict[str, Any]:
        if name is not None and any(
                s.name == name.strip() and s.id != strategy_id for s in strategy_store.list_strategies()):
            raise QuantLabError(f"已经有一个叫“{name.strip()}”的策略了，请换个名字。")
        strategy_store.update_strategy(strategy_id, name=name, config=config)
        return self.detail(strategy_id)

    @staticmethod
    def delete(strategy_id: str) -> None:
        s = strategy_store.get_strategy(strategy_id)
        if s.status == strategy_store.STATUS_RUNNING:
            raise QuantLabError("这个策略正在运行。请先停用，再删除。")
        strategy_store.delete_strategy(strategy_id)

    def start(self, strategy_id: str) -> dict[str, Any]:
        s = strategy_store.get_strategy(strategy_id)
        panel = self._panel()
        engine.start(s, panel, settings.initial_cash(), self._names(), self._evaluate)
        strategy_store.update_strategy(strategy_id, status=strategy_store.STATUS_RUNNING)
        return self.detail(strategy_id)

    def stop(self, strategy_id: str) -> dict[str, Any]:
        strategy_store.get_strategy(strategy_id)
        strategy_store.update_strategy(strategy_id, status=strategy_store.STATUS_STOPPED)
        return self.detail(strategy_id)

    # ---------------- 组合回测 ----------------
    def backtest(self, body: Mapping[str, Any]) -> dict[str, Any]:
        panel = self._panel(body.get("start"), body.get("end"))
        ctx, assumptions = strategy_run.build_context(panel, body, evaluator=self._evaluate, costs=settings.costs())
        initial_cash = float(body.get("initial_cash") or settings.initial_cash())
        result = portfolio.run_backtest(ctx, initial_cash)
        summary = portfolio.summarize(result, panel.dates)
        yearly = summary.pop("yearly")

        code = str(body.get("benchmark") or strategy_run.DEFAULT_BENCHMARK)
        bench = strategy_run.index_close(code, panel.dates)
        valid = np.isfinite(bench)
        base = bench[valid][0] if valid.any() else np.nan
        curve = bench / base
        total = float(bench[valid][-1] / base - 1) if valid.any() else None
        bench_yearly = portfolio.yearly_returns(panel.dates[valid], curve[valid], 1.0) if valid.any() else {}

        names = self._names()

        def trade_row(t: portfolio.Trade) -> dict[str, Any]:
            c = str(panel.codes[t.col])
            return {
                "code": c, "name": names.get(c, ""), "buy_date": str(panel.dates[t.buy_row]),
                "buy_price": _round(t.buy_price, 3),
                "sell_date": None if t.sell_row is None else str(panel.dates[t.sell_row]),
                "sell_price": _round(t.sell_price, 3), "shares": t.shares, "return": _round(t.ret),
                "pnl": _round(t.pnl, 2),
                "hold_days": t.hold_days if not t.open else int(panel.shape[0] - t.buy_row),
                "reason": t.reason, "open": t.open,
            }

        trades = sorted(result.trades, key=lambda t: (t.buy_row, t.col), reverse=True)
        payload = {
            "summary": {
                **{k: (_round(v) if isinstance(v, float) else v) for k, v in summary.items()},
                "benchmark_annual_return": None if total is None else _round(
                    portfolio.annualized(total, int(valid.sum()))),
                "benchmark_total_return": _round(total),
                "ok_day_ratio": None if ctx.timing_ok is None else _round(float(ctx.timing_ok.mean())),
            },
            "curve": {"dates": [str(d) for d in panel.dates],
                      "equity": [_round(v) for v in result.equity / initial_cash],
                      "benchmark": [_round(v) for v in curve]},
            "yearly": [{"year": y, "strategy": _round(r), "benchmark": _round(bench_yearly.get(y))}
                       for y, r in yearly.items()],
            "trades": [trade_row(t) for t in trades[:500]],
            "trade_count": len(trades),
            "range": {"start": str(panel.dates[0]), "end": str(panel.dates[-1])},
            "benchmark": {"code": code, "name": strategy_run.INDEX_NAMES.get(code, code)},
            "assumptions": assumptions,
            "created_at": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        strategy_id = body.get("strategy_id")
        if strategy_id:
            strategy_store.get_strategy(str(strategy_id))
            result_store.save(str(strategy_id), payload)
        return payload

    @staticmethod
    def saved_backtest(strategy_id: str) -> dict[str, Any]:
        strategy_store.get_strategy(strategy_id)
        result = result_store.load(strategy_id)
        if result is None:
            raise NotFoundError("这个策略还没有保存过回测结果。")
        return result

    # ---------------- 今日提醒、模拟账户 ----------------
    def today(self) -> dict[str, Any]:
        latest = self.latest_data_date()
        names = self._names()
        blocks = []
        for s in strategy_store.list_strategies():
            if s.status != strategy_store.STATUS_RUNNING:
                continue
            reminder = store.latest_reminder(s.id)
            tracked = real.payload(s.id, names)
            if tracked is not None and not tracked["enabled"]:
                tracked = None
            blocks.append({
                "strategy_id": s.id, "name": s.name, "reminder": reminder, "real": tracked,
                "behind": bool(reminder and latest and reminder["date"] < latest),
            })
        return {"latest_date": latest, "strategies": blocks, "last_run": store.last_run(),
                "push": self.push_channels()}

    def account(self, strategy_id: str) -> dict[str, Any]:
        s = strategy_store.get_strategy(strategy_id)
        account = store.get_account(strategy_id)
        if account is None:
            raise NotFoundError("这个策略还没有模拟账户。把它设为“运行中”后会自动开一个。")
        days = store.days(strategy_id)
        names = self._names()
        initial = account["initial_cash"]
        equity = days[-1]["equity"] if days else initial
        reminder = store.latest_reminder(strategy_id)
        return {
            "strategy_id": s.id, "name": s.name, "status": s.status, "started_on": account["started_on"],
            "last_date": account["last_date"], "initial_cash": initial, "equity": _round(equity, 2),
            "total_return": _round(equity / initial - 1),
            "curve": {"dates": [d["date"] for d in days], "equity": [_round(d["equity"] / initial) for d in days]},
            "positions": reminder["positions"] if reminder else [],
            "trades": [{"code": t["code"], "name": names.get(t["code"], ""), "buy_date": t["buy_date"],
                        "buy_price": _round(t["buy_price"], 3), "sell_date": t["sell_date"],
                        "sell_price": _round(t["sell_price"], 3), "shares": t["shares"],
                        "return": _round(t["ret"]), "pnl": _round(t["pnl"], 2), "reason": t["reason"]}
                       for t in store.trades(strategy_id)],
        }

    # ---------------- 实际持仓 ----------------
    _REAL_EMPTY: dict[str, Any] = {  # noqa: RUF012 —— 只读的常量
        "enabled": False, "capital": 0.0, "cash": 0.0, "equity": 0.0, "total_return": 0.0, "date": None,
        "timing": {"enabled": False, "ok": True}, "positions": [], "todos": [], "trades": [],
    }

    def real_view(self, strategy_id: str) -> dict[str, Any]:
        strategy_store.get_strategy(strategy_id)
        ql_data.set_market_dir(self._formulas.market_dir()[0])
        return real.payload(strategy_id, self._names()) or dict(self._REAL_EMPTY)

    def _real_refreshed(self, strategy_id: str) -> dict[str, Any]:
        s = strategy_store.get_strategy(strategy_id)
        panel = self._panel()
        real.refresh(s, panel, engine.build_context(s, panel, self._evaluate))
        return self.real_view(strategy_id)

    def set_real(self, strategy_id: str, enabled: bool, capital: float | None) -> dict[str, Any]:
        s = strategy_store.get_strategy(strategy_id)
        if not enabled:
            real.disable(strategy_id)
            return self.real_view(strategy_id)
        if capital is None:
            existing = rs.get_account(strategy_id)
            if existing is None:
                raise QuantLabError("请填写这个策略实际投入了多少资金。")
            capital = existing["capital"]
        real.enable(s, capital)
        return self._real_refreshed(strategy_id)

    def real_answer(self, todo_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        panel = self._panel()
        sid = real.answer_todo(todo_id, bool(body.get("done")), panel, body.get("price"), body.get("shares"),
                               body.get("date"))
        return self._real_refreshed(sid)

    def real_skip_all(self, strategy_id: str) -> dict[str, Any]:
        real.skip_all(strategy_id)
        return self._real_refreshed(strategy_id)

    def real_add(self, strategy_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        real.add_position(strategy_id, str(body.get("code") or ""), body.get("shares"), body.get("price"),
                          body.get("date"), self._panel())
        return self._real_refreshed(strategy_id)

    def real_edit(self, position_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        ql_data.set_market_dir(self._formulas.market_dir()[0])
        return self._real_refreshed(real.edit_position(position_id, body.get("shares"), body.get("price"),
                                                       body.get("date")))

    def real_sell(self, position_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        ql_data.set_market_dir(self._formulas.market_dir()[0])
        return self._real_refreshed(real.sell_position(position_id, body.get("price"), body.get("date")))

    def real_remove(self, position_id: int) -> dict[str, Any]:
        return self._real_refreshed(real.remove_position(position_id))

    # ---------------- 设置 ----------------
    def settings(self) -> dict[str, Any]:
        base = Costs()
        return {
            "costs": settings.costs_dict(),
            "cost_limits": {k: {"min": lo, "max": hi, "label": label}
                            for k, (lo, hi, label) in settings.COST_LIMITS.items()},
            "cost_defaults": {k: getattr(base, k) for k in settings.COST_LIMITS},
            "initial_cash": settings.initial_cash(),
            "push": self.push_channels(),
        }

    def update_settings(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        if changes.get("costs"):
            settings.set_costs(changes["costs"])
            self._formulas.clear_cache()
        if changes.get("initial_cash") is not None:
            settings.set_initial_cash(changes["initial_cash"])
        return self.settings()

    # ---------------- 推送：用 TSP 自己配置的飞书、企业微信 ----------------
    @staticmethod
    def push_channels() -> dict[str, bool]:
        try:
            from app.services import preferences
            return {"feishu": bool(preferences.get_feishu_webhook_url()),
                    "wecom": bool(preferences.get_wecom_webhook_url())}
        except Exception:
            logger.exception("读取推送设置失败")
            return {"feishu": False, "wecom": False}

    @staticmethod
    def _send(text: str) -> list[str]:
        """经 TSP 的推送通道发出一条文字消息。返回发到了哪些渠道。"""
        from app.services import preferences, webhook_adapter
        title, _, body = text.partition("\n")
        sent: list[str] = []
        feishu = preferences.get_feishu_webhook_url()
        if feishu and webhook_adapter.send_feishu(feishu, title, body, preferences.get_feishu_webhook_secret()):
            sent.append("飞书")
        wecom = preferences.get_wecom_webhook_url()
        if wecom and webhook_adapter.send_wecom(wecom, title, body):
            sent.append("企业微信")
        return sent

    # ---------------- 每日任务 ----------------
    def is_due(self) -> bool:
        latest = self.latest_data_date()
        if latest is None or latest <= str(store.get_setting(KEY_LAST_DATE, "") or ""):
            return False
        return any(s.status == strategy_store.STATUS_RUNNING for s in strategy_store.list_strategies())

    def run_daily(self, sender: Sender | None = None) -> dict[str, Any]:
        """把运行中的策略推进到行情的最新一天，生成提醒并推送。同一时间只允许一个在跑。"""
        if not self._run_lock.acquire(blocking=False):
            raise ConflictError("每日任务正在运行，请等它结束。")
        try:
            run_id = store.start_run(clock.today().isoformat())
            messages: list[str] = []
            ok, data_date, pushed = True, None, False
            try:
                db.backup(clock.today())  # 推进模拟账户之前先留一份备份
            except (OSError, sqlite3.Error) as e:
                logger.warning("业务库没有备份成：%s", e)
            try:
                panel = self._panel()
                data_date = str(panel.dates[-1])
                names = self._names()
                tracked = rs.enabled_ids()
                running = [s for s in strategy_store.list_strategies()
                           if s.status == strategy_store.STATUS_RUNNING]
                for s in running:
                    try:
                        ctx = engine.build_context(s, panel, self._evaluate)
                        engine.advance(s, panel, names, self._evaluate, ctx)
                        if s.id in tracked:
                            real.refresh(s, panel, ctx)
                    except QuantLabError as e:
                        ok = False
                        logger.error("策略 %s 没能生成提醒：%s", s.name, e.message)
                        messages.append(f"策略“{s.name}”今天没能生成提醒：{e.message}")
                messages.insert(0, f"已处理到 {data_date}，共 {len(running)} 个运行中的策略。")
                store.set_setting(KEY_LAST_DATE, data_date)
                if running and store.get_setting(KEY_PUSHED, "") != data_date:
                    items = []
                    for s in running:
                        reminder = real.as_reminder(s.id, data_date, names) or store.get_reminder(s.id, data_date)
                        if reminder is not None:
                            items.append((s.name, reminder))
                    if items:
                        try:
                            channels = (sender or self._send)(push.format_message(data_date, items))
                        except Exception as e:
                            logger.exception("推送失败")
                            channels = []
                            messages.append(f"推送出了问题：{e}")
                        if channels:
                            pushed = True
                            store.set_setting(KEY_PUSHED, data_date)
                            messages.append(f"已推送到{'、'.join(channels)}。")
                        else:
                            messages.append("没有推送：还没有在 TSP 的设置里配置飞书或企业微信的机器人地址。")
            except QuantLabError as e:
                ok = False
                messages.append(e.message)
            message = " ".join(messages)
            store.finish_run(run_id, ok, data_date, pushed, message)
            return {"ok": ok, "data_date": data_date, "pushed": pushed, "message": message}
        finally:
            self._run_lock.release()

    def tick(self) -> None:
        """后台线程定时调用：行情到了新的一天、又有运行中的策略时，跑一次每日任务。"""
        if self.is_due():
            with contextlib.suppress(ConflictError):  # 已经有一次在跑：下一轮再看
                self.run_daily()
