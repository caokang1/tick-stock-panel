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
from app.custom.tdx_formula.ql.data.interface import SourceNoData

# 网站明确表示"你被限制了"的 HTTP 状态码
_REJECT_STATUS = frozenset({403, 429, 456})
# AKShare 拿到回应、但解析不出想要的表时抛的错
_NO_DATA_ERRORS = (KeyError, IndexError, ValueError, TypeError, AttributeError)


def compact(d: date) -> str:
    return d.strftime("%Y%m%d")


def prefixed_symbol(code: str) -> str:
    """sh.600000 -> sh600000"""
    return codes.normalize(code).replace(".", "")


_in_flight: dict[str, threading.Thread] = {}  # 每个来源上一次还没返回的调用
_in_flight_guard = threading.Lock()


def call_akshare(
    source: str, label: str, fn: Callable[..., pd.DataFrame], timeout: float, **kwargs: Any
) -> pd.DataFrame:
    """带超时地调用一次 AKShare。``source`` 是来源名称，同一个来源任何时候只有一个调用在途。

    超时后原来的调用没法中途取消，可能还在等对方回话。这时不发新的请求（否则同一个来源上就有两个请求了），
    直接按"来源不可用"处理，等它自己结束。
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn(**kwargs)
        except Exception as e:  # noqa: BLE001 —— 在下面统一翻译
            box["error"] = e

    worker = threading.Thread(target=run, name=f"akshare-{label}", daemon=True)
    with _in_flight_guard:
        previous = _in_flight.get(source)
        if previous is not None and previous.is_alive():
            raise SourceUnavailable(f"{label}：上一次超时的请求还没有结束，先不发新的请求")
        _in_flight[source] = worker
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
    if isinstance(error, _NO_DATA_ERRORS) and not isinstance(error, requests.RequestException):
        # 请求发出去了、也有回应，只是内容里没有要的东西（AKShare 解析时出错）。再问一遍也一样
        return SourceNoData(f"{label}：来源没有给出这项数据（{type(error).__name__}: {error}）")
    return SourceUnavailable(f"{label}：{type(error).__name__}: {error}")


def in_range(df: pd.DataFrame, start: date, end: date, column: str = "date") -> pd.DataFrame:
    d = pd.to_datetime(df[column])
    return df[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]
