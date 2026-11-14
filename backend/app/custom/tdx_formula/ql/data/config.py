"""数据来源配置：读默认配置和用户配置，合并，校验护栏，保存用户的修改。

默认值在 ``backend/config/data_sources.default.toml``，用户的修改在 ``data/config/data_sources.toml``
（只保存改过的项），读取时后者覆盖前者。护栏的规则见本文件的 validate。
"""

from __future__ import annotations

import copy
import math
import os
import threading
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path
from typing import Any

from app.custom.tdx_formula.ql.core.config import get_settings
from app.custom.tdx_formula.ql.core.errors import ConfigError
from app.custom.tdx_formula.ql.data.interface import Capability
from app.custom.tdx_formula.ql.data.limiter import SourcePolicy

_WARN_RATIO = 0.8  # 超过官方上限的这个比例时给出警告

_LABELS = {
    "enabled": "是否启用",
    "max_concurrent": "同时最多几个连接",
    "daily_request_cap": "每日请求上限",
    "min_interval_seconds": "两次请求的最小间隔（秒）",
    "jitter_seconds": "随机抖动上限（秒）",
    "timeout_seconds": "单次请求超时（秒）",
    "max_retries": "失败后的重试次数",
    "stop_after_consecutive_failures": "连续失败多少次后停止",
    "cooldown_minutes_on_reject": "被限制后暂停多久（分钟）",
    "count_pages_as_requests": "翻页是否计入请求数",
    "official_daily_limit": "官方每日上限",
    "official_max_concurrent": "官方允许的并发数",
}
_BOOL_FIELDS = {"enabled", "count_pages_as_requests"}
_INT_FIELDS = {
    "max_concurrent", "daily_request_cap", "max_retries", "stop_after_consecutive_failures",
    "cooldown_minutes_on_reject",
}
_FLOAT_FIELDS = {"min_interval_seconds", "jitter_seconds", "timeout_seconds"}
_OPTIONAL_INT_FIELDS = {"official_daily_limit", "official_max_concurrent"}
_POLICY_FIELDS = tuple(f.name for f in fields(SourcePolicy))


@dataclass(frozen=True)
class DownloadOptions:
    default_start: date
    indices: tuple[str, ...]
    max_catchup_days: int
    data_ready_hour: int  # 每天几点（北京时间）之后，当天的日线才算已经入库


@dataclass(frozen=True)
class DataConfig:
    sources: dict[str, SourcePolicy]
    priority: dict[Capability, tuple[str, ...]]
    download: DownloadOptions
    panel_cache: bool = True  # 是否把拼好的全市场行情在硬盘上另存一份，下次启动直接读

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": {name: asdict(p) for name, p in self.sources.items()},
            "priority": {cap.value: list(names) for cap, names in self.priority.items()},
            "download": {
                "default_start": self.download.default_start.isoformat(),
                "indices": list(self.download.indices),
                "max_catchup_days": self.download.max_catchup_days,
                "data_ready_hour": self.download.data_ready_hour,
            },
            "storage": {"panel_cache": self.panel_cache},
        }


_io_lock = threading.Lock()


# ---------------- 读取与合并 ----------------
def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件 {path} 的格式有误：{e}") from e


def load_default_raw() -> dict[str, Any]:
    path = get_settings().default_sources_config
    if not path.exists():
        raise ConfigError(f"找不到默认配置文件：{path}")
    return _read_toml(path)


def load_user_raw() -> dict[str, Any]:
    path = get_settings().user_sources_config
    return _read_toml(path) if path.exists() else {}


def merge_raw(default: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """用户配置覆盖默认配置：sources 逐来源逐项覆盖；priority、download 逐项覆盖。"""
    merged = copy.deepcopy(default)
    for section, value in user.items():
        if section == "sources" and isinstance(value, dict):
            target = merged.setdefault("sources", {})
            for name, items in value.items():
                if isinstance(items, dict):
                    target.setdefault(name, {}).update(items)
                else:
                    target[name] = items
        elif isinstance(value, dict):
            merged.setdefault(section, {}).update(value)
        else:
            merged[section] = value
    return merged


def parse(raw: dict[str, Any]) -> DataConfig:
    """把合并后的原始配置转成 ``DataConfig``。类型不对、缺项、有不认识的项时抛 ``ConfigError``。"""
    unknown = set(raw) - {"sources", "priority", "download", "storage"}
    if unknown:
        raise ConfigError(f"配置里有不认识的部分：{', '.join(sorted(unknown))}")
    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, dict) or not sources_raw:
        raise ConfigError("配置里没有任何数据来源（sources）")
    sources = {name: _parse_policy(name, items) for name, items in sources_raw.items()}

    priority_raw = raw.get("priority", {})
    if not isinstance(priority_raw, dict):
        raise ConfigError("来源优先级（priority）的格式不对")
    valid_caps = {c.value for c in Capability}
    priority: dict[Capability, tuple[str, ...]] = {}
    for cap_name, names in priority_raw.items():
        if cap_name not in valid_caps:
            raise ConfigError(f"来源优先级里有不认识的能力：{cap_name}。可用的有：{', '.join(sorted(valid_caps))}")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ConfigError(f"能力 {cap_name} 的来源优先级必须是来源名称的列表")
        priority[Capability(cap_name)] = tuple(names)

    storage = raw.get("storage", {})
    if not isinstance(storage, dict) or set(storage) - {"panel_cache"}:
        raise ConfigError("存储设置（storage）里只能有 panel_cache 一项")
    panel_cache = storage.get("panel_cache", True)
    if not isinstance(panel_cache, bool):
        raise ConfigError(f"存储设置里的 panel_cache 必须是 true 或 false，当前值：{panel_cache!r}")
    return DataConfig(sources=sources, priority=priority, download=_parse_download(raw.get("download", {})),
                      panel_cache=panel_cache)


def _parse_policy(name: str, items: Any) -> SourcePolicy:
    if not isinstance(items, dict):
        raise ConfigError(f"数据来源 {name} 的配置格式不对")
    unknown = set(items) - set(_POLICY_FIELDS)
    if unknown:
        raise ConfigError(f"数据来源 {name} 有不认识的配置项：{', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for key in _POLICY_FIELDS:
        label = f"数据来源 {name} 的「{_LABELS[key]}」（{key}）"
        if key in _OPTIONAL_INT_FIELDS:
            v = items.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise ConfigError(f"{label}必须是整数，当前值：{v!r}")
            values[key] = v
            continue
        if key not in items:
            raise ConfigError(f"{label}没有设置")
        v = items[key]
        if key in _BOOL_FIELDS:
            if not isinstance(v, bool):
                raise ConfigError(f"{label}必须是 true 或 false，当前值：{v!r}")
        elif key in _INT_FIELDS:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError(f"{label}必须是整数，当前值：{v!r}")
        elif isinstance(v, bool) or not isinstance(v, int | float):
            raise ConfigError(f"{label}必须是数字，当前值：{v!r}")
        else:
            v = float(v)
        values[key] = v
    return SourcePolicy(**values)


def _parse_download(raw: Any) -> DownloadOptions:
    if not isinstance(raw, dict):
        raise ConfigError("下载设置（download）的格式不对")
    unknown = set(raw) - {"default_start", "indices", "max_catchup_days", "data_ready_hour"}
    if unknown:
        raise ConfigError(f"下载设置里有不认识的配置项：{', '.join(sorted(unknown))}")
    try:
        start_raw = raw["default_start"]
        start = start_raw if isinstance(start_raw, date) else date.fromisoformat(str(start_raw))
        indices = raw["indices"]
        catchup = raw["max_catchup_days"]
        ready_hour = raw["data_ready_hour"]
    except KeyError as e:
        raise ConfigError(f"下载设置缺少配置项：{e.args[0]}") from e
    except ValueError as e:
        bad = raw.get("default_start")
        raise ConfigError(f"下载设置的起始日期写法有误：{bad!r}，应为 2015-01-01 这样的格式") from e
    if not isinstance(indices, list) or not all(isinstance(i, str) for i in indices):
        raise ConfigError("下载设置里的指数列表（indices）必须是代码的列表")
    if isinstance(catchup, bool) or not isinstance(catchup, int) or catchup < 1:
        raise ConfigError(f"下载设置里的最多补几个交易日（max_catchup_days）必须是正整数，当前值：{catchup!r}")
    if isinstance(ready_hour, bool) or not isinstance(ready_hour, int) or not 0 <= ready_hour <= 23:
        raise ConfigError(f"下载设置里的数据入库时间（data_ready_hour）必须是 0 到 23 的整数，当前值：{ready_hour!r}")
    return DownloadOptions(
        default_start=start, indices=tuple(indices), max_catchup_days=catchup, data_ready_hour=ready_hour
    )


# ---------------- 护栏 ----------------
_MIN_INTERVAL_FLOOR = 0.1  # 任何来源的请求间隔都不得小于这个值（秒）
_TIMEOUT_CEILING = 600.0
_OFFICIAL_COOLDOWN_FLOOR = 360  # 有官方限制的来源，被拒绝后至少暂停这么久（分钟）
def validate(config: DataConfig) -> list[str]:
    """校验护栏。违反时抛 ``ConfigError``（写明哪一项、当前值、允许的范围）；返回警告列表（可以保存，但要提示）。"""
    warnings: list[str] = []
    for name, p in config.sources.items():
        def label(key: str, name: str = name) -> str:
            return f"数据来源 {name} 的「{_LABELS[key]}」（{key}）"

        for key in ("min_interval_seconds", "jitter_seconds", "timeout_seconds"):
            value = getattr(p, key)
            if not math.isfinite(value):
                raise ConfigError(f"{label(key)}必须是一个有限的数字，当前值：{value}")
        if p.min_interval_seconds < _MIN_INTERVAL_FLOOR:
            raise ConfigError(
                f"{label('min_interval_seconds')}不能小于 {_MIN_INTERVAL_FLOOR:g} 秒，"
                f"当前值：{p.min_interval_seconds:g}。请求之间必须留出间隔，这一条不能关闭"
            )
        if p.timeout_seconds > _TIMEOUT_CEILING:
            raise ConfigError(
                f"{label('timeout_seconds')}不能超过 {_TIMEOUT_CEILING:g} 秒，当前值：{p.timeout_seconds:g}")
        if p.max_concurrent < 1:
            raise ConfigError(f"{label('max_concurrent')}至少为 1，当前值：{p.max_concurrent}")
        if p.daily_request_cap < 0:
            raise ConfigError(f"{label('daily_request_cap')}不能是负数，当前值：{p.daily_request_cap}（0 表示不限）")
        if p.jitter_seconds < 0:
            raise ConfigError(f"{label('jitter_seconds')}不能是负数，当前值：{p.jitter_seconds:g}")
        if p.timeout_seconds <= 0:
            raise ConfigError(f"{label('timeout_seconds')}必须大于 0，当前值：{p.timeout_seconds:g}")
        if p.max_retries < 0:
            raise ConfigError(f"{label('max_retries')}不能是负数，当前值：{p.max_retries}")
        if p.stop_after_consecutive_failures < 1:
            raise ConfigError(
                f"{label('stop_after_consecutive_failures')}至少为 1，当前值：{p.stop_after_consecutive_failures}"
            )
        if p.cooldown_minutes_on_reject < 1:
            raise ConfigError(
                f"{label('cooldown_minutes_on_reject')}至少为 1，当前值：{p.cooldown_minutes_on_reject}。"
                "被来源限制后必须暂停，这一条不能关闭"
            )
        if p.official_daily_limit is not None:
            limit = p.official_daily_limit
            if not p.count_pages_as_requests:
                raise ConfigError(
                    f"{label('count_pages_as_requests')}不能关闭：这个来源官方规定了每日上限，"
                    "一次查询会分多页取回，不把翻页计入就可能在不知不觉中超过上限"
                )
            if p.cooldown_minutes_on_reject < _OFFICIAL_COOLDOWN_FLOOR:
                raise ConfigError(
                    f"{label('cooldown_minutes_on_reject')}不能小于 {_OFFICIAL_COOLDOWN_FLOOR} 分钟，"
                    f"当前值：{p.cooldown_minutes_on_reject}。这个来源被限制后官方至少冻结 6 小时，"
                    "提前重试只会让限制时间变得更长"
                )
            if p.daily_request_cap == 0:
                raise ConfigError(
                    f"{label('daily_request_cap')}不能设为 0（不限）：这个来源官方规定每日最多 {limit} 次，"
                    f"请设为 1 到 {limit} 之间的数"
                )
            if p.daily_request_cap > limit:
                raise ConfigError(
                    f"{label('daily_request_cap')}当前值 {p.daily_request_cap}，超过了官方每日上限 {limit}。"
                    f"超过会被对方限制访问，允许的最大值是 {limit}"
                )
            if p.daily_request_cap > limit * _WARN_RATIO:
                warnings.append(
                    f"{label('daily_request_cap')}当前值 {p.daily_request_cap}，已超过官方每日上限 {limit} 的 "
                    f"{_WARN_RATIO:.0%}（{int(limit * _WARN_RATIO)}），余量很小，有被限制访问的风险"
                )
        if p.official_max_concurrent is not None and p.max_concurrent > p.official_max_concurrent:
            raise ConfigError(
                f"{label('max_concurrent')}当前值 {p.max_concurrent}，"
                f"超过了官方允许的并发数 {p.official_max_concurrent}。允许的最大值是 {p.official_max_concurrent}"
            )
    for cap, names in config.priority.items():
        for n in names:
            if n not in config.sources:
                raise ConfigError(
                    f"能力 {cap.value} 的来源优先级里有不认识的来源：{n}。已登记的来源：{', '.join(config.sources)}"
                )
        if len(set(names)) != len(names):
            raise ConfigError(f"能力 {cap.value} 的来源优先级里有重复的来源")
    return warnings


# ---------------- 对外 ----------------
def load_config() -> DataConfig:
    """读取生效的配置（默认配置 + 用户的修改），并校验护栏。"""
    config = parse(merge_raw(load_default_raw(), load_user_raw()))
    validate(config)
    return config


def config_warnings() -> list[str]:
    return validate(load_config())


def update_source_policy(name: str, changes: dict[str, Any]) -> tuple[DataConfig, list[str]]:
    """修改一个来源的若干配置项并保存到用户配置。返回（生效的配置, 警告列表）。

    违反护栏时抛 ``ConfigError``，什么都不保存。官方上限两项只用于校验，不允许通过这里修改。
    """
    with _io_lock:
        default = load_default_raw()
        if name not in default.get("sources", {}):
            raise ConfigError(f"没有这个数据来源：{name}")
        for key in changes:
            if key not in _POLICY_FIELDS:
                raise ConfigError(f"数据来源 {name} 没有这个配置项：{key}")
            if key in _OPTIONAL_INT_FIELDS:
                raise ConfigError(f"「{_LABELS[key]}」（{key}）是来源官方公布的限制，只用于校验，不能修改")
        user = load_user_raw()
        user.setdefault("sources", {}).setdefault(name, {}).update(changes)
        return _validate_and_save(default, user)


def update_priority(mapping: dict[str, list[str]]) -> tuple[DataConfig, list[str]]:
    """修改若干能力的来源优先级并保存到用户配置。键是能力名（如 ``stock_daily``），值是来源名称的列表。"""
    with _io_lock:
        default = load_default_raw()
        user = load_user_raw()
        user.setdefault("priority", {}).update({str(k): list(v) for k, v in mapping.items()})
        return _validate_and_save(default, user)


def update_panel_cache(enabled: bool) -> DataConfig:
    """打开或关闭"把拼好的全市场行情在硬盘上另存一份"，保存到用户配置。"""
    with _io_lock:
        user = load_user_raw()
        user.setdefault("storage", {})["panel_cache"] = bool(enabled)
        return _validate_and_save(load_default_raw(), user)[0]


def _validate_and_save(default: dict[str, Any], user: dict[str, Any]) -> tuple[DataConfig, list[str]]:
    config = parse(merge_raw(default, user))
    warnings = validate(config)
    path = get_settings().user_sources_config
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump_toml(user), encoding="utf-8")
    os.replace(tmp, path)
    return config, warnings


def dump_toml(raw: dict[str, Any]) -> str:
    """把用户配置写成 TOML。只支持本配置用到的结构：表、标量、字符串列表。"""
    lines = ["# 用户对数据来源配置的修改。这里只保存改过的项，其余取默认配置。", ""]

    def emit_table(header: str, table: dict[str, Any]) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        if scalars:
            lines.append(f"[{header}]")
            lines.extend(f"{k} = {_toml_value(v)}" for k, v in scalars.items())
            lines.append("")
        for k, v in table.items():
            if isinstance(v, dict):
                emit_table(f"{header}.{k}", v)

    for section, value in raw.items():
        if isinstance(value, dict):
            emit_table(section, value)
    return "\n".join(lines)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, date):
        return f'"{v.isoformat()}"'
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ConfigError(f"配置里有无法保存的值：{v!r}")
