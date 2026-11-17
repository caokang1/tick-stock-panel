"""把通达信公式策略的结果送进 TSP 的因子库和信号库。

TSP 自带的信号只认“字段 比较 数值”，通达信公式翻译不过去。所以公式照旧由这里的引擎计算，
把结果写成 TSP 的一张“扩展数据”时序表（``data/ext_data/tdx_<策略编号>``）：

- ``xg``：策略的选股结果（全部条件同时满足并且在股票范围内），1 = 选中、0 = 没选中；
- ``c1``、``c2``……：有多个条件时，每个条件各自的结果；
- ``sell``：策略设了卖出条件时，卖出条件的结果；
- 其余字段：条件最后一句里直接用到的公式变量（例如 `M5:=MA(C,5);` 里的 M5），原样的数值。

TSP 会把这张表的数值字段登记成因子（因子库里“扩展数据”一组），这里再替用户在信号库里各建一条
“字段 == 1”的信号，并生成一条 TSP 自己的策略（``custom_tdx_<策略编号>``：选股结果为 1 时买入），
于是 TSP 的“策略”“回测”“监控中心”都能直接选用。

注意：结果只在每天行情更新之后算一次，盘中看到的是上一个交易日的值；只保留最近 ``HISTORY_DAYS``
个交易日，TSP 回测更早的日期时这些字段是空的（等于没有信号）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .ql.backtest import strategy_store
from .ql.blocks import Universe, enabled_conditions, rendered_formula
from .ql.blocks.model import _extra_series, universe_mask
from .ql.core import clock
from .ql.core.errors import NotFoundError, QuantLabError
from .ql.core.panel import Panel
from .ql.live import store as live_store
from .ql.tdx import evaluate, parse
from .ql.tdx import inspect as tinspect

logger = logging.getLogger(__name__)

PREFIX = "tdx_"
HISTORY_DAYS = 750  # 送进 TSP 的交易日数（大约三年）
MAX_FACTORS = 12  # 一个策略最多送多少个数值变量，免得表太大
KEY_ENABLED = "bridge_strategies"  # 开了“送进面板”的策略编号
KEY_STATE = "bridge_state"  # 每个策略上一次送的情况
_RESERVED = re.compile(r"^(xg|sell|c\d+|symbol|date)$")

Evaluator = Callable[..., np.ndarray]


class Hooks:
    """写完以后要 TSP 那边配合做的事。默认什么都不做（测试里、或者 TSP 的策略引擎还没起来时）。"""

    def after(self) -> None:
        """丢掉 TSP 里按旧数据算好的缓存。"""

    def save_strategy(self, strategy_id: str, code: str, name: str, description: str) -> str | None:
        """把生成的 TSP 策略存进去；存不成返回原因，成了返回 None。"""
        return "面板的策略引擎还没有就绪"

    def delete_strategy(self, strategy_id: str) -> None:
        """把生成的 TSP 策略删掉；本来就没有时不算错。"""


@dataclass(frozen=True)
class Field:
    name: str  # 表里的字段名：小写字母、数字、下划线
    label: str  # 给人看的名字
    values: np.ndarray  # (天数, 股票数) float32，没有值的格子是 NaN
    signal: str | None = None  # 要在信号库里建信号时，是 "entry" 还是 "exit"


def table_id(strategy_id: str) -> str:
    """策略编号 → 扩展表的编号（也是因子、信号名字的前缀）。"""
    return PREFIX + (re.sub(r"[^a-z0-9]", "", strategy_id.lower())[:16] or "x")


def panel_strategy_id(strategy_id: str) -> str:
    """生成的 TSP 策略的编号。TSP 要求用户策略以 custom_ 开头。"""
    return "custom_" + table_id(strategy_id)


def column(strategy_id: str, field: str) -> str:
    """字段在 TSP 里的列名（因子库、信号条件里看到的就是它）。"""
    return f"ext_{table_id(strategy_id)}_{field}"


def _flag(values: np.ndarray, rows: slice) -> np.ndarray:
    return np.asarray(values[rows], dtype=np.float32)


def _factor_values(block: Any, params: Mapping[str, float], panel: Panel) -> dict[str, np.ndarray]:
    """条件最后一句里直接用到的公式变量各自的数值；布尔变量不算。"""
    text = rendered_formula(block, params)
    program = parse(text)
    if not program.statements:
        return {}
    last = program.statements[-1]
    names = sorted(n for n in tinspect.referenced_names(last.expr)
                   if tinspect.find_definition(program, n, before=last) is not None)
    if not names:
        return {}
    probes = {f"QLOUT{i}": name for i, name in enumerate(names, 1)}
    probe_text = text.rstrip() + "\n" + "\n".join(f"{out}:{name};" for out, name in probes.items())
    result = evaluate(parse(probe_text), panel, extra_series=_extra_series(text, panel) or None)
    upper = {str(k).upper(): v for k, v in result.items()}
    return {name: upper[out] for out, name in probes.items() if out in upper and upper[out].dtype != bool}


def compute(strategy: strategy_store.Strategy, panel: Panel, evaluator: Evaluator,
            days: int = HISTORY_DAYS) -> tuple[list[Field], np.ndarray]:
    """算出要送的全部字段，返回（字段，最近 days 个交易日的日期）。"""
    config = strategy.config
    conds = enabled_conditions(config.get("conditions") or [])
    if not conds:
        raise QuantLabError("这个策略还没有启用任何选股条件，没有东西可以送进面板。")
    rows = slice(max(panel.shape[0] - days, 0), None)
    signal = universe_mask(panel, Universe.from_dict(config.get("universe"))) & panel.tradable
    parts = []
    for block, params in conds:
        value = evaluator(block, panel, params)
        parts.append((block, params, value))
        signal = signal & value
    fields = [Field("xg", "选股结果（1=选中）", _flag(signal, rows), "entry")]
    if len(parts) > 1:
        for i, (block, _, value) in enumerate(parts, 1):
            fields.append(Field(f"c{i}", f"{block.name}（1=满足）", _flag(value & panel.tradable, rows), "entry"))
    sells = enabled_conditions((config.get("trade") or {}).get("sell_conditions") or [])
    if sells:
        sell = np.zeros(panel.shape, dtype=bool)
        for block, params in sells:
            sell |= evaluator(block, panel, params)
        fields.append(Field("sell", "卖出条件（1=满足）", _flag(sell & panel.tradable, rows), "exit"))

    seen = {f.name for f in fields}
    count = 0
    for block, params, _ in parts:
        for name, value in _factor_values(block, params, panel).items():
            if count >= MAX_FACTORS:
                break
            key = name.lower() if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name) else f"v{count + 1}"
            if _RESERVED.match(key):
                key += "_v"
            if key in seen:
                continue  # 几个条件共用同一个变量，只送一次
            seen.add(key)
            count += 1
            with np.errstate(invalid="ignore"):
                data = np.where(panel.tradable[rows], value[rows], np.nan).astype(np.float32)
            data[~np.isfinite(data)] = np.nan
            fields.append(Field(key, name, data))
    return fields, panel.dates[rows]


def _symbols(panel: Panel) -> np.ndarray:
    from app.plugins.freecn.provider import to_panel_symbol

    return np.array([to_panel_symbol(str(code)) for code in panel.codes], dtype=object)


def write_table(data_dir: Path, strategy: strategy_store.Strategy, fields: list[Field], dates: np.ndarray,
                panel: Panel) -> int:
    """把字段写成 TSP 的扩展时序表：先写到旁边的新目录里，全部写好再整体换上去。返回写了多少天。"""
    import polars as pl

    from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, _invalidate_ext_derived
    from app.services.fs_utils import atomic_write_parquet

    tid = table_id(strategy.id)
    store = ExtConfigStore(data_dir)
    previous = store.get(tid)
    config = ExtConfig(
        id=tid, label=f"公式·{strategy.name}"[:24], mode="timeseries",
        fields=[ExtField(f.name, "float", f.label[:24]) for f in fields],
        description="由“通达信公式”页自动生成和更新，请不要手工改动；要停掉，到通达信公式页关掉“送进面板”。",
        created_at=previous.created_at if previous else None,
    )
    store.upsert(config)

    base = data_dir / "ext_data" / tid
    fresh, live, old = base / "timeseries.new", base / "timeseries", base / "timeseries.old"
    shutil.rmtree(fresh, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    symbols = _symbols(panel)
    tradable = panel.tradable[panel.shape[0] - len(dates):]
    written = 0
    for r, day in enumerate(dates):
        mask = tradable[r]
        if not mask.any():
            continue
        frame = pl.DataFrame({"symbol": symbols[mask].tolist(),
                              **{f.name: f.values[r][mask].astype(np.float64) for f in fields}})
        frame = frame.with_columns(pl.col(f.name).fill_nan(None) for f in fields)
        part = fresh / f"date={str(day)[:10]}"
        part.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(frame, part / "part.parquet")
        written += 1
    if live.exists():
        live.rename(old)
    if fresh.exists():
        fresh.rename(live)
    shutil.rmtree(old, ignore_errors=True)
    _invalidate_ext_derived(data_dir)
    return written


def signal_id(strategy_id: str, field: str) -> str:
    return f"{table_id(strategy_id)}_{field}"


def ensure_signals(data_dir: Path, strategy: strategy_store.Strategy, fields: list[Field]) -> list[str]:
    """给选股结果、各个条件、卖出条件各建一条“字段 == 1”的信号。已经有的不动（用户可能改过名字）。"""
    from app.factors import ext_factors
    from app.strategy import custom_signals

    ext_factors.ensure_synced(data_dir)
    wanted = {signal_id(strategy.id, f.name): f for f in fields if f.signal}
    names = []
    for sid, f in wanted.items():
        suffix = "选中" if f.name == "xg" else "卖出" if f.name == "sell" else f.label.split("（")[0]
        names.append(sid)
        if custom_signals.load_one(data_dir, sid) is not None:
            continue
        sig = {
            "id": sid, "name": f"公式·{strategy.name}·{suffix}"[:40], "kind": f.signal, "timeframe": "daily",
            "enabled": True, "created_at": datetime.now().isoformat(timespec="seconds"),
            "conditions": [{"left": column(strategy.id, f.name), "op": "==", "right": "1",
                            "leftDays": 0, "rightDays": 0}],
        }
        custom_signals.validate(sig)
        custom_signals.save_one(data_dir, sig)
    # 条件数变少以后，多出来的那几条信号一起去掉
    prefix = table_id(strategy.id) + "_"
    for existing in custom_signals.load_all(data_dir):
        if str(existing.get("id", "")).startswith(prefix) and existing["id"] not in wanted:
            custom_signals.delete_one(data_dir, existing["id"])
    return names


def strategy_code(strategy: strategy_store.Strategy, fields: list[Field]) -> str:
    """生成一条 TSP 的策略：选股结果为 1 的那天出买入信号；卖出条件、止损、最长持有天数照搬公式策略里的设置。"""
    from .ql.backtest.portfolio import TradeRules

    rules = TradeRules.from_dict(strategy.config.get("trade"))
    has_sell = any(f.name == "sell" for f in fields)
    xg, sell = column(strategy.id, "xg"), column(strategy.id, "sell")
    entry_id = "csg_" + signal_id(strategy.id, "xg")
    exit_ids = ["csg_" + signal_id(strategy.id, "sell")] if has_sell else []
    stop = -abs(rules.stop_loss) if rules.stop_loss else None
    hold = int(rules.max_hold_days) if rules.max_hold_days else 10
    meta = {
        "id": panel_strategy_id(strategy.id), "name": f"公式·{strategy.name}"[:40],
        "description": "由“通达信公式”页自动生成：公式选中的那天买入。请不要手改，改公式请到通达信公式页。",
        "tags": ["通达信公式"], "asset_types": ["stock"], "timeframes": ["1d"], "params": [],
        "scoring": {"amount": 1.0}, "order_by": "score", "descending": True, "limit": 100,
    }
    exit_expr = f"np.nan_to_num(matrix_feature(market, {sell!r}), nan=0.0) > 0.5" if has_sell \
        else "np.zeros(market.shape, dtype=bool)"
    fields_expr = repr(sorted({"close", "amount", xg} | ({sell} if has_sell else set())))
    return f'''"""通达信公式策略（自动生成，请勿手改）。"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {json.dumps(meta, ensure_ascii=False, indent=4).replace(": true", ": True").replace(": false", ": False")}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = {[entry_id]!r}
EXIT_SIGNALS = {exit_ids!r}
STOP_LOSS = {stop!r}
MAX_HOLD_DAYS = {hold!r}


class TdxFormulaMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({fields_expr})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 1

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        del params
        entry = np.nan_to_num(matrix_feature(market, {xg!r}), nan=0.0) > 0.5
        exit_ = {exit_expr}
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids={(entry_id,)!r},
            exit_signal_ids={tuple(exit_ids)!r},
        )


MATRIX_STRATEGY = TdxFormulaMatrixStrategy()
'''


def remove(data_dir: Path, strategy_id: str) -> None:
    """把这个策略送过去的表和信号都去掉。"""
    from app.services.ext_data import ExtConfigStore
    from app.strategy import custom_signals

    prefix = table_id(strategy_id) + "_"
    for existing in custom_signals.load_all(data_dir):
        if str(existing.get("id", "")).startswith(prefix):
            custom_signals.delete_one(data_dir, existing["id"])
    ExtConfigStore(data_dir).delete(table_id(strategy_id))


def _fingerprint(strategy: strategy_store.Strategy) -> str:
    text = json.dumps({"name": strategy.name, "config": strategy.config}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class Bridge:
    """哪些策略要送进面板、上一次送的情况，以及每天行情更新后重新送一次。"""

    def __init__(self, panel_of: Callable[[], Panel], evaluator: Evaluator) -> None:
        self._panel_of = panel_of
        self._evaluator = evaluator
        self._lock = threading.Lock()  # 同一时间只算一个
        self._busy: set[str] = set()

    # ---------------- 设置 ----------------
    @staticmethod
    def enabled_ids() -> list[str]:
        return [str(x) for x in (live_store.get_setting(KEY_ENABLED, []) or [])]

    @staticmethod
    def _states() -> dict[str, dict[str, Any]]:
        return dict(live_store.get_setting(KEY_STATE, {}) or {})

    @staticmethod
    def _save_state(strategy_id: str, state: dict[str, Any] | None) -> None:
        states = Bridge._states()
        if state is None:
            states.pop(strategy_id, None)
        else:
            states[strategy_id] = state
        live_store.set_setting(KEY_STATE, states)

    def view(self) -> list[dict[str, Any]]:
        """每个策略一行：开没开、送了哪些字段和信号、送到哪一天。"""
        enabled, states = set(self.enabled_ids()), self._states()
        out = []
        for s in strategy_store.list_strategies():
            state = states.get(s.id, {})
            out.append({
                "strategy_id": s.id, "name": s.name, "enabled": s.id in enabled, "busy": s.id in self._busy,
                "table": table_id(s.id), "through": state.get("through"), "days": state.get("days"),
                "fields": state.get("fields", []), "signals": state.get("signals", []),
                "message": state.get("message"), "ok": state.get("ok"), "updated_at": state.get("updated_at"),
                "panel_strategy": state.get("panel_strategy"),
                "panel_strategy_problem": state.get("panel_strategy_problem"),
            })
        return out

    def set_enabled(self, strategy_id: str, on: bool, data_dir: Path, hooks: Hooks | None = None) -> None:
        strategy = strategy_store.get_strategy(strategy_id)  # 没有这个策略时这里报错
        ids = [x for x in self.enabled_ids() if x != strategy.id]
        if on:
            enabled_conditions(strategy.config.get("conditions") or [])  # 条件写得不对，现在就报出来
            ids.append(strategy.id)
        live_store.set_setting(KEY_ENABLED, ids)
        if not on:
            hooks = hooks or Hooks()
            hooks.delete_strategy(panel_strategy_id(strategy.id))  # 先删策略：它引用着下面要删的信号
            remove(data_dir, strategy.id)
            self._save_state(strategy.id, None)
            hooks.after()

    # ---------------- 送 ----------------
    def export(self, strategy: strategy_store.Strategy, data_dir: Path, hooks: Hooks | None = None,
               panel: Panel | None = None) -> dict[str, Any]:
        """算一遍并写进 TSP。算不出来时把原因记下来，不往外抛。"""
        hooks = hooks or Hooks()
        with self._lock:
            self._busy.add(strategy.id)
            state: dict[str, Any] = {"fingerprint": _fingerprint(strategy),
                                     "updated_at": clock.now().strftime("%Y-%m-%d %H:%M")}
            try:
                panel = panel if panel is not None else self._panel_of()
                fields, dates = compute(strategy, panel, self._evaluator)
                days = write_table(data_dir, strategy, fields, dates, panel)
                signals = ensure_signals(data_dir, strategy, fields)
                hooks.after()  # 新的字段和信号要先让 TSP 认到，下面存策略时才通得过它的检查
                problem = hooks.save_strategy(
                    panel_strategy_id(strategy.id), strategy_code(strategy, fields), f"公式·{strategy.name}"[:40],
                    "由“通达信公式”页自动生成：公式选中的那天买入。")
                state.update(panel_strategy=None if problem else panel_strategy_id(strategy.id),
                             panel_strategy_problem=problem)
                state.update(ok=True, through=str(panel.dates[-1])[:10], days=days, signals=signals,
                             fields=[{"name": f.name, "label": f.label, "column": column(strategy.id, f.name),
                                      "is_signal": bool(f.signal)} for f in fields],
                             message=f"已送到 {str(panel.dates[-1])[:10]}，最近 {days} 个交易日。")
            except QuantLabError as e:
                state.update(ok=False, through=str(self._latest() or ""), message=f"没有送成：{e.message}")
            except Exception as e:
                logger.exception("把策略 %s 送进面板时出错", strategy.id)
                state.update(ok=False, through=str(self._latest() or ""),
                             message=f"没有送成：{type(e).__name__}: {e}")
            finally:
                self._busy.discard(strategy.id)
            self._save_state(strategy.id, state)
        hooks.after()
        return state

    def _latest(self) -> str | None:
        from .ql.data import store as ql_store

        day = ql_store.latest_date()
        return day.isoformat() if day else None

    def pending(self) -> list[strategy_store.Strategy]:
        """开了开关、但行情有了新的一天或者策略改过、还没重新送的那些。"""
        latest = self._latest()
        if latest is None:
            return []
        states = self._states()
        out = []
        for sid in self.enabled_ids():
            try:
                strategy = strategy_store.get_strategy(sid)
            except NotFoundError:
                continue
            state = states.get(sid, {})
            if state.get("through") != latest or state.get("fingerprint") != _fingerprint(strategy):
                out.append(strategy)
        return out

    def cleanup(self, data_dir: Path, hooks: Hooks | None = None) -> bool:
        """策略已经删掉的，把它的表和信号也去掉。有清理时返回 True。"""
        known = {s.id for s in strategy_store.list_strategies()}
        gone = [sid for sid in self.enabled_ids() if sid not in known]
        for sid in gone:
            (hooks or Hooks()).delete_strategy(panel_strategy_id(sid))
            remove(data_dir, sid)
            self._save_state(sid, None)
        if gone:
            live_store.set_setting(KEY_ENABLED, [x for x in self.enabled_ids() if x in known])
        return bool(gone)

    def tick(self, data_dir: Path, hooks: Hooks | None = None) -> None:
        """后台线程每分钟调用一次。"""
        changed = self.cleanup(data_dir, hooks)
        todo = self.pending()
        if todo:
            panel = self._panel_of()
            for strategy in todo:
                self.export(strategy, data_dir, hooks, panel=panel)
        elif changed and hooks:
            hooks.after()
