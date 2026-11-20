"""复盘信号的定时检查和接口。

每分钟看一眼：
1. 手头的信号是不是该作废了（复盘之后已经又走完一个交易日）→ 删掉；
2. 有没有一份新的收盘后复盘还没生成过信号 → 让 AI 摘价位、核对、写成监控规则。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import extract

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/recap-signals"
DEFAULT_CONFIG: dict[str, Any] = {"enabled": True}
MARKET_CLOSE = time(15, 0)
TICK_SECONDS = 60
MAX_ATTEMPTS = 3
STALE_DAYS = 10  # 行情一直没更新时的兜底：复盘过了这么多天，信号也作废

Ask = Callable[[str, str], str]      # (系统提示, 用户内容) → AI 的回复
Reload = Callable[[Any], None]       # 规则变了以后让监控引擎重新读一遍

_lock = threading.Lock()


class OptionsIn(BaseModel):
    enabled: bool


def root_of(data_dir: Path) -> Path:
    return data_dir / "user_data" / "extensions" / "recap_signals"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config(root: Path) -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **_read_json(root / "config.json")}


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def pick_recap(reports: list[dict[str, Any]], latest: date | None) -> dict[str, Any] | None:
    """行情最新那一天、并且是那天收盘以后生成的复盘里最新的一份。盘中生成的不算。"""
    if latest is None:
        return None
    close = datetime.combine(latest, MARKET_CLOSE)
    good = []
    for r in reports:
        if str(r.get("as_of") or "")[:10] != latest.isoformat() or not r.get("content") or not r.get("id"):
            continue
        try:
            created = datetime.fromisoformat(str(r.get("created_at") or "")).replace(tzinfo=None)
        except ValueError:
            continue
        if created >= close:
            good.append((created, r))
    return max(good, key=lambda x: x[0])[1] if good else None


def expired(state: dict[str, Any], latest: date | None, now: datetime) -> bool:
    """手头的信号该不该作废：复盘之后又有一个交易日走完了（那天的行情已经进来，而且已经收盘）。"""
    recap_day = _day(state.get("recap_day"))
    if recap_day is None or not state.get("rule_ids"):
        return False
    if now.date() > recap_day + timedelta(days=STALE_DAYS):
        return True
    if latest is None or latest <= recap_day:
        return False
    return latest < now.date() or now.time() >= MARKET_CLOSE


def ask_ai(system: str, user: str) -> str:
    from app.services import ai_provider

    if not ai_provider.ai_configured():
        raise RuntimeError("面板里还没有配置 AI")
    return asyncio.run(ai_provider.generate_ai_text(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0, max_tokens=3000, timeout=180.0,
    ))


def reload_engine(app: Any) -> None:
    from app.api.monitor_rules import _sync_engine

    _sync_engine(SimpleNamespace(app=app))


def own_rules(data_dir: Path) -> list[dict[str, Any]]:
    from app.strategy import monitor_rules

    return [r for r in monitor_rules.load_all(data_dir) if str(r.get("id") or "").startswith(extract.RULE_PREFIX)]


def replace_rules(data_dir: Path, rules: list[dict[str, Any]]) -> bool:
    """把监控中心里本扩展的规则（rs_ 开头）换成这一批。别的规则不碰。返回有没有改动。"""
    from app.strategy import monitor_rules

    ready = []
    for rule in rules:
        rule = monitor_rules.normalize(rule)
        monitor_rules.validate(rule)
        ready.append(rule)
    old = own_rules(data_dir)
    for rule in old:
        monitor_rules.delete_one(data_dir, rule["id"])
    for rule in ready:
        monitor_rules.save_one(data_dir, rule)
    return bool(old or ready)


def holdings_for(repo: Any, data_dir: Path, day: date) -> tuple[list[dict[str, Any]], list[str]]:
    """能放进监控的持仓（A 股和 ETF）及它们那天的收盘价；港股监控不了，单独列出来。"""
    from app.custom.daily_digest.snapshot import _quotes
    from app.custom.recap_plus.context import read_holdings

    symbols = [h["symbol"] for h in read_holdings(data_dir)]
    hk = [s for s in symbols if s.endswith(".HK")]
    local = [s for s in symbols if not s.endswith(".HK")]
    quotes = _quotes(repo, day, set(local))
    out = []
    for symbol in local:
        q = quotes.get(symbol) or {}
        try:
            asset = repo.resolve_asset_type(symbol)
        except Exception:  # noqa: BLE001 - 认不出品种就按个股处理
            asset = "stock"
        out.append({"symbol": symbol, "name": q.get("name") or symbol, "close": q.get("price"),
                    "asset_type": asset if asset in ("stock", "etf") else "stock"})
    return out, hk


def generate(app: Any, recap: dict[str, Any], now: datetime, ask: Ask, reload: Reload) -> dict[str, Any]:
    """用这份复盘生成信号并写进监控中心。返回新的状态。AI 调不通、回复读不懂时抛异常，旧信号保持不动。"""
    from app.services import preferences

    repo = app.state.repo
    data_dir = Path(repo.store.data_dir)
    recap_day = str(recap["as_of"])[:10]
    holdings, hk = holdings_for(repo, data_dir, date.fromisoformat(recap_day))
    section = extract.holding_section(str(recap["content"]))
    signals: list[dict[str, Any]] = []
    dropped: list[str] = []
    if section.strip() and holdings:
        items = extract.parse_reply(ask(extract.SYSTEM_PROMPT, extract.build_prompt(section, holdings)))
        signals, dropped = extract.clean(items, holdings, extract.blocks_of(section, holdings))
    rules = extract.to_rules(signals, recap_day, preferences.get_webhook_default_channels())
    if replace_rules(data_dir, rules):
        reload(app)
    if rules:
        message = f"{now:%m-%d %H:%M} 按 {recap_day} 的复盘生成了 {len(rules)} 条信号，下一个交易日有效"
    elif not section.strip():
        message = f"{now:%m-%d %H:%M} {recap_day} 的复盘里没有“持仓复盘”一节，没有生成信号"
    else:
        message = f"{now:%m-%d %H:%M} {recap_day} 的复盘没有给出下一个交易日够得着的价位，没有生成信号"
    return {
        "recap_id": recap["id"], "recap_day": recap_day, "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "rule_ids": [r["id"] for r in rules],
        "signals": [{**s, "rule_id": r["id"]} for s, r in zip(signals, rules, strict=True)],
        "dropped": dropped, "skipped_hk": hk, "message": message,
    }


def clear(app: Any, root: Path, state: dict[str, Any], message: str, reload: Reload) -> dict[str, Any]:
    data_dir = Path(app.state.repo.store.data_dir)
    if replace_rules(data_dir, []):
        reload(app)
    state = {"recap_id": state.get("recap_id"), "recap_day": state.get("recap_day"), "rule_ids": [],
             "signals": [], "message": message}
    _write_json(root / "state.json", state)
    return state


def tick(app: Any, now: datetime, ask: Ask = ask_ai, reload: Reload = reload_engine, force: bool = False) -> str | None:
    """每分钟看一眼。做了事就返回一句说明，否则 None。force：不管生成过没有，按最新的复盘重来一遍。"""
    from app.services import market_recap_reports

    repo = getattr(app.state, "repo", None)
    raw = getattr(getattr(repo, "store", None), "data_dir", None)
    if not isinstance(raw, (str, Path)):
        return None
    root = root_of(Path(raw))
    with _lock:
        if not load_config(root)["enabled"]:
            return None
        state = _read_json(root / "state.json")
        latest = repo.latest_enriched_date()
        done = None
        if expired(state, latest, now):
            state = clear(app, root, state, f"{now:%m-%d %H:%M} {state.get('recap_day')} 的复盘信号已过有效期，已清除", reload)
            done = state["message"]
            logger.info("复盘信号：%s", done)
        recap = pick_recap(market_recap_reports.list_reports(), latest)
        if recap is None:
            if force:
                raise LookupError("还没有最新交易日收盘后的复盘，先生成复盘再来。")
            return done
        if not force:
            if recap["id"] == state.get("recap_id"):
                return done
            if state.get("attempt_id") == recap["id"] and int(state.get("attempts", 0)) >= MAX_ATTEMPTS:
                return done
        try:
            state = generate(app, recap, now, ask, reload)
        except Exception as exc:
            attempts = int(state.get("attempts", 0)) + 1 if state.get("attempt_id") == recap["id"] else 1
            state = {**state, "attempt_id": recap["id"], "attempts": attempts,
                     "message": f"{now:%m-%d %H:%M} 生成信号没有成功（第 {attempts} 次）：{exc}"}
            _write_json(root / "state.json", state)
            logger.exception("复盘信号：生成失败")
            if force:
                raise
            return state["message"]
        _write_json(root / "state.json", state)
        logger.info("复盘信号：%s", state["message"])
        return state["message"]


def build_router(background: bool = True) -> APIRouter:
    @asynccontextmanager
    async def lifespan(app: Any):
        stopping = threading.Event()
        thread = None
        if background:
            def loop() -> None:
                while not stopping.wait(TICK_SECONDS):
                    try:
                        tick(app, datetime.now())
                    except Exception:
                        logger.exception("复盘信号的定时检查出错")

            thread = threading.Thread(target=loop, name="recap-signals", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stopping.set()
            if thread is not None:
                thread.join(timeout=2)

    router = APIRouter(prefix=PREFIX, tags=["custom-recap-signals"], lifespan=lifespan)

    def data_dir_of(request: Request) -> Path:
        raw = getattr(getattr(getattr(request.app.state, "repo", None), "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        return Path(raw)

    def view(request: Request) -> dict[str, Any]:
        data_dir = data_dir_of(request)
        root = root_of(data_dir)
        state = _read_json(root / "state.json")
        live = {r["id"] for r in own_rules(data_dir)}
        signals = [s for s in state.get("signals") or [] if s.get("rule_id") in live]
        return {"enabled": load_config(root)["enabled"], "recap_day": state.get("recap_day"),
                "generated_at": state.get("generated_at"), "message": state.get("message"),
                "signals": signals, "dropped": state.get("dropped") or [], "skipped_hk": state.get("skipped_hk") or []}

    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        return view(request)

    @router.post("/run")
    def run(request: Request) -> dict[str, Any]:
        """按最新的收盘后复盘重新生成一遍（会调用一次 AI）。"""
        data_dir_of(request)
        try:
            tick(request.app, datetime.now(), force=True)
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"生成信号没有成功：{exc}") from exc
        return view(request)

    @router.put("/options")
    def options(body: OptionsIn, request: Request) -> dict[str, Any]:
        """开关。关掉时把已经生成的信号一并清掉。"""
        root = root_of(data_dir_of(request))
        with _lock:
            _write_json(root / "config.json", {**load_config(root), "enabled": body.enabled})
            if not body.enabled:
                clear(request.app, root, {}, f"{datetime.now():%m-%d %H:%M} 已关闭，信号已清除", reload_engine)
        return view(request)

    return router
