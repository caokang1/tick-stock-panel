"""AKShare 各适配器共用的部分：带超时地调用、把异常翻译成统一的异常、代码格式转换。

这里不等待、不重试（那是调度层的事）。超时的做法：AKShare 的多数函数不接受超时参数，
所以把调用放进一个后台线程，等不到结果就按超时处理。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
import requests
from akshare.exceptions import RateLimitError

from app.custom.tdx_formula.ql.core import codes
from app.custom.tdx_formula.ql.core.errors import DataError, SourceRejected, SourceUnavailable

# 网站明确表示"你被限制了"的 HTTP 状态码
_REJECT_STATUS = frozenset({403, 429, 456})


def compact(d: date) -> str:
    return d.strftime("%Y%m%d")


def prefixed_symbol(code: str) -> str:
    """sh.600000 -> sh600000"""
    return codes.normalize(code).replace(".", "")


def call_akshare(label: str, fn: Callable[..., pd.DataFrame], timeout: float, **kwargs: Any) -> pd.DataFrame:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn(**kwargs)
        except Exception as e:  # noqa: BLE001 —— 在下面统一翻译
            box["error"] = e

    worker = threading.Thread(target=run, name=f"akshare-{label}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise SourceUnavailable(f"{label}：{timeout:g} 秒内没有返回")
    error = box.get("error")
    if error is not None:
        raise translate_error(label, error) from error
    result = box.get("result")
    if not isinstance(result, pd.DataFrame):
        raise SourceUnavailable(f"{label}：返回的不是表格数据")
    return result


def translate_error(label: str, error: Exception) -> DataError:
    if isinstance(error, RateLimitError):
        return SourceRejected(f"{label}：来源限制了访问（{error}）")
    if isinstance(error, requests.HTTPError) and error.response is not None:
        status = error.response.status_code
        if status in _REJECT_STATUS:
            return SourceRejected(f"{label}：来源限制了访问（HTTP {status}）")
    return SourceUnavailable(f"{label}：{type(error).__name__}: {error}")


def in_range(df: pd.DataFrame, start: date, end: date, column: str = "date") -> pd.DataFrame:
    d = pd.to_datetime(df[column])
    return df[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]
