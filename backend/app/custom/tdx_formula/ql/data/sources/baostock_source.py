"""BaoStock 适配器（含代理隧道）。

职责只有翻译：把统一的请求翻译成 BaoStock 的调用，把结果翻译成统一的列，把"黑名单"错误码翻译成
``SourceRejected``。等待、重试、计数、冷却都在调度层，这里不做。

关于连接方式：BaoStock 用的是自己的 TCP 协议，不会自动走本机的 HTTP 代理。
设置了代理环境变量时，这里先连代理并发起 ``CONNECT 主机:端口``，再在隧道上收发；没有设置时直连。
做法是只替换 ``baostock.util.socketutil`` 这一个模块里用到的 ``socket`` 和 ``send_msg``，
**不改全局的 ``socket.socket``**，所以进程里其他网络连接完全不受影响。
顺带解决了原库的三个问题：没有超时、对方断开连接时死循环、出错只 print 不抛异常。
"""

from __future__ import annotations

import atexit
import contextlib
import io
import logging
import os
import socket
import threading
import time
import types
import zlib
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlparse

import baostock as bs
import baostock.common.contants as bs_cons
import baostock.common.context as bs_context
import baostock.util.socketutil as bs_socketutil
import numpy as np
import pandas as pd

from app.custom.tdx_formula.ql.core.errors import SourceRejected, SourceUnavailable
from app.custom.tdx_formula.ql.data.interface import Capability
from app.custom.tdx_formula.ql.data.models import KIND_INDEX, KIND_STOCK

logger = logging.getLogger(__name__)

_PROXY_ENV = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
_END_MARK = b"<![CDATA[]]>\n"
_SUCCESS = bs_cons.BSERR_SUCCESS
_REJECTED_CODES = frozenset({bs_cons.BSERR_BLACKLIST_USER})  # 10001011：黑名单用户
_NOT_LOGGED_IN = bs_cons.BSERR_NO_LOGIN  # 10001001：服务器认为本连接没有登录
_LOGOUT_TIMEOUT = 5.0  # 登出只是礼貌性通知，不值得久等
_FACTOR_EPOCH = "1990-01-01"  # 复权因子从头取，才能知道起始日之前累计的因子

_SECURITY_KINDS = {"1": KIND_STOCK, "2": KIND_INDEX}


class _SessionLost(SourceUnavailable):
    """服务器那边的登录已经失效。重新登录即可，不需要也不应该再发登出。"""


class _Transport:
    """本模块与 baostock 库之间共享的连接状态。BaoStock 只允许一条连接，所以是模块级的一份。"""

    timeout: float = 60.0
    requests: int = 0
    connect_error: str | None = None
    installed: bool = False


def _proxy_address() -> tuple[str, int] | None:
    for key in _PROXY_ENV:
        value = os.environ.get(key)
        if not value:
            continue
        u = urlparse(value if "://" in value else f"http://{value}")
        if u.scheme in ("http", "https") and u.hostname and u.port:
            return u.hostname, u.port
        logger.warning("代理 %s 不是 HTTP 代理，BaoStock 改为直连", value)
        return None
    return None


class _TunnelSocket(socket.socket):
    """连接时先经 HTTP 代理打隧道（设置了代理时），其余行为与普通 socket 相同。只在本适配器里使用。"""

    def connect(self, address: Any) -> None:
        self.settimeout(_Transport.timeout)
        _Transport.connect_error = None
        try:
            proxy = _proxy_address()
            if proxy is None:
                super().connect(address)
                return
            host, port = address
            super().connect(proxy)
            self.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.recv(4096)
                if not chunk:
                    raise ConnectionError("代理没有响应 CONNECT 请求")
                buf += chunk
            status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 200" not in status:
                raise ConnectionError(f"代理拒绝了连接：{status}")
        except OSError as e:
            _Transport.connect_error = f"{type(e).__name__}: {e}"
            raise


def _send_msg(msg: str) -> str:
    """替换 baostock 的 send_msg：带超时，对方断开时抛异常而不是死循环，每发一次计一次请求。"""
    sock = getattr(bs_context, "default_socket", None)
    if sock is None:
        raise ConnectionError("尚未连接")
    _Transport.requests += 1
    deadline = time.monotonic() + _Transport.timeout
    sock.sendall((msg + "\n").encode("utf-8"))
    received = bytearray()
    while not received.endswith(_END_MARK):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{_Transport.timeout:g} 秒内没有收完数据")
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("对方关闭了连接")
        received += chunk
    head_len = bs_cons.MESSAGE_HEADER_LENGTH
    head = bytes(received[:head_len]).decode()
    parts = head.split(bs_cons.MESSAGE_SPLIT)
    if parts[1] in bs_cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
        body_len = int(parts[2])
        return head + zlib.decompress(bytes(received[head_len:head_len + body_len])).decode()
    return bytes(received).decode()


def _install_transport() -> None:
    if _Transport.installed:
        return
    bs_socketutil.socket = types.SimpleNamespace(  # type: ignore[assignment]
        socket=_TunnelSocket, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM
    )
    bs_socketutil.send_msg = _send_msg
    _Transport.installed = True


def _fmt(d: date) -> str:
    return d.isoformat()


class BaoStockSource:
    name = "baostock"
    capabilities = frozenset({
        Capability.TRADE_CALENDAR, Capability.SECURITIES, Capability.ST_HISTORY, Capability.MARKET_SNAPSHOT,
        Capability.ADJUST_FACTOR, Capability.STOCK_DAILY, Capability.INDEX_DAILY, Capability.INDUSTRY,
        Capability.FLOAT_SHARES,
    })

    def __init__(self) -> None:
        _install_transport()
        self._open = False
        self._lock = threading.Lock()  # 一条连接上不允许两个线程同时收发
        atexit.register(self.close)

    # ---- 会话 ----
    @property
    def is_open(self) -> bool:
        return self._open

    def open(self, timeout: float) -> None:
        with self._lock:
            _Transport.timeout = timeout
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = bs.login()
            except (OSError, ValueError, IndexError, zlib.error) as e:
                self._drop()
                detail = _Transport.connect_error or f"{type(e).__name__}: {e}"
                raise SourceUnavailable(f"BaoStock 登录失败：{detail}") from e
            if result.error_code != _SUCCESS:
                self._drop()
                self._raise_for(result.error_code, result.error_msg, "登录")
            self._open = True

    def close(self) -> None:
        """登出并断开。任务结束或异常退出时都要调用。"""
        with self._lock:
            if self._open:
                self._drop(logout=True)

    def _drop(self, logout: bool = False) -> None:
        self._open = False
        sock = getattr(bs_context, "default_socket", None)
        if sock is None:
            return
        if logout:
            _Transport.timeout = min(_Transport.timeout, _LOGOUT_TIMEOUT)
            try:
                sock.settimeout(_Transport.timeout)
                with contextlib.redirect_stdout(io.StringIO()):
                    bs.logout()
            except (OSError, ValueError, IndexError, zlib.error) as e:
                logger.warning("BaoStock 登出时出错（连接将直接关闭）：%s", e)
        with contextlib.suppress(OSError):  # 连接本来就坏了，关不关都一样
            sock.close()
        bs_context.default_socket = None

    @staticmethod
    def _raise_for(code: str, msg: str, what: str) -> None:
        if code in _REJECTED_CODES:
            raise SourceRejected(f"BaoStock 已限制本机访问（错误码 {code}：{msg}）。按规则暂停，不重试、不绕过")
        if code == _NOT_LOGGED_IN:
            raise _SessionLost(f"BaoStock {what}失败（错误码 {code}：{msg}）")
        raise SourceUnavailable(f"BaoStock {what}失败（错误码 {code}：{msg}）")

    # ---- 取数 ----
    def fetch(self, capability: Capability, **kwargs: Any) -> pd.DataFrame:
        handler = self._handlers().get(capability)
        if handler is None:
            raise SourceUnavailable(f"BaoStock 不提供 {capability.value}")
        with self._lock:
            if not self._open:
                raise SourceUnavailable("BaoStock 尚未登录")
            _Transport.timeout = float(kwargs.pop("timeout", _Transport.timeout))
            sock = getattr(bs_context, "default_socket", None)
            if sock is not None:
                sock.settimeout(_Transport.timeout)
            _Transport.requests = 0
            try:
                df = handler(**kwargs)
            except SourceRejected as e:
                # 已被来源限制：不再发送任何请求，登出也不发
                self._drop(logout=False)
                e.requests = _Transport.requests
                raise
            except _SessionLost as e:
                self._drop(logout=False)  # 服务器已经不认这次登录，登出是多余的一次请求
                e.requests = _Transport.requests
                raise
            except SourceUnavailable as e:
                self._drop(logout=True)  # 服务器正常答复了一个错误：连接是好的，礼貌登出后重来
                e.requests = _Transport.requests  # 登出也经过 _send_msg，已计在内
                raise
            except (OSError, ValueError, IndexError, KeyError, zlib.error) as e:
                # 超时、断线或收到残缺数据：这条连接上的收发已经错位，再发登出只会读到上一次请求的残余数据，
                # 而且迟到的登出可能把随后重新登录的会话一并注销（2026-11-12 全量下载时出现过一次）。直接关闭连接。
                self._drop(logout=False)
                error = SourceUnavailable(f"BaoStock 请求失败：{type(e).__name__}: {e}")
                error.requests = _Transport.requests
                raise error from e
            df.attrs["requests"] = max(1, _Transport.requests)
            return df

    def _handlers(self) -> dict[Capability, Callable[..., pd.DataFrame]]:
        return {
            Capability.TRADE_CALENDAR: self._trade_calendar,
            Capability.SECURITIES: self._securities,
            Capability.ST_HISTORY: self._st_history,
            Capability.MARKET_SNAPSHOT: self._market_snapshot,
            Capability.ADJUST_FACTOR: self._adjust_factor,
            Capability.STOCK_DAILY: self._stock_daily,
            Capability.INDEX_DAILY: self._index_daily,
            Capability.INDUSTRY: self._industry,
            Capability.FLOAT_SHARES: self._float_shares,
        }

    def _query(self, what: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> pd.DataFrame:
        """执行一次查询并取完所有分页。翻页由 baostock 在 ``next()`` 里发起，每页都经过 ``_send_msg`` 计数。"""
        with contextlib.redirect_stdout(io.StringIO()):
            rs = fn(*args, **kwargs)
            if rs.error_code != _SUCCESS:
                self._raise_for(rs.error_code, rs.error_msg, what)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rs.error_code != _SUCCESS:
                self._raise_for(rs.error_code, rs.error_msg, what)
        return pd.DataFrame(rows, columns=list(rs.fields))

    def _trade_calendar(self, start: date, end: date) -> pd.DataFrame:
        raw = self._query("取交易日历", bs.query_trade_dates, start_date=_fmt(start), end_date=_fmt(end))
        raw = raw[raw["is_trading_day"] == "1"]
        return pd.DataFrame({"date": raw["calendar_date"]})

    def _securities(self) -> pd.DataFrame:
        raw = self._query("取证券清单", bs.query_stock_basic)
        raw = raw[raw["type"].isin(list(_SECURITY_KINDS))]
        return pd.DataFrame({
            "code": raw["code"],
            "name": raw["code_name"],
            "list_date": raw["ipoDate"].replace("", None),
            "delist_date": raw["outDate"].replace("", None),
            "kind": raw["type"].map(_SECURITY_KINDS),
        })

    def _k_data(self, what: str, code: str, fields: str, start: date, end: date) -> pd.DataFrame:
        return self._query(
            what, bs.query_history_k_data_plus, code, fields,
            start_date=_fmt(start), end_date=_fmt(end), frequency="d", adjustflag="3",
        )

    def _st_history(self, code: str, start: date, end: date) -> pd.DataFrame:
        raw = self._k_data("取历史 ST 状态", code, "date,code,isST", start, end)
        return pd.DataFrame({"date": raw["date"], "code": raw["code"], "is_st": raw["isST"] == "1"})

    def _stock_daily(self, code: str, start: date, end: date) -> pd.DataFrame:
        fields = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus"
        return self._bars(self._k_data("取个股日线", code, fields, start, end))

    def _index_daily(self, code: str, start: date, end: date) -> pd.DataFrame:
        raw = self._k_data("取指数日线", code, "date,code,open,high,low,close,volume,amount", start, end)
        return raw

    def _market_snapshot(self, day: date) -> pd.DataFrame:
        raw = self._query("取某日全市场行情", bs.query_daily_history_k_AStock, date=_fmt(day))
        if raw.empty:
            return pd.DataFrame(columns=["date", "code", "open", "high", "low", "close", "preclose", "volume",
                                         "amount", "suspended", "is_st"])
        out = self._bars(raw)
        out["is_st"] = (raw["isST"] == "1").to_numpy()
        return out

    @staticmethod
    def _bars(raw: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame({"date": raw["date"], "code": raw["code"]})
        for col in ("open", "high", "low", "close", "preclose", "volume", "amount"):
            out[col] = pd.to_numeric(raw[col], errors="coerce")
        out["suspended"] = (raw["tradestatus"] != "1").to_numpy()
        # 停牌日没有成交：成交量、成交额记 0
        out.loc[out["suspended"], ["volume", "amount"]] = 0.0
        if "turn" in raw.columns:
            # 换手率（%）= 成交量 ÷ 流通股本，反过来就得到当天的流通股本；没有成交的日子算不出来，留空
            turn = pd.to_numeric(raw["turn"], errors="coerce")
            out["float_shares"] = (out["volume"] / (turn / 100.0)).where((turn > 0) & (out["volume"] > 0))
        return out

    def _float_shares(self, code: str, start: date, end: date) -> pd.DataFrame:
        bars = self._stock_daily(code, start, end)
        return bars.loc[bars["float_shares"].notna(), ["date", "code", "float_shares"]]

    def _industry(self) -> pd.DataFrame:
        raw = self._query("取行业分类", bs.query_stock_industry)
        # 来源给的是"J66货币金融服务"这样带编号的名称，去掉前面的编号
        names = raw["industry"].astype(str).str.replace(r"^[A-Z]\d+", "", regex=True).str.strip()
        return pd.DataFrame({"code": raw["code"], "industry": names})

    def _adjust_factor(self, code: str, start: date, end: date) -> pd.DataFrame:
        """BaoStock 给的是每次除权除息日的后复权因子；这里展开成逐日（工作日）的因子。"""
        raw = self._query("取复权因子", bs.query_adjust_factor, code=code, start_date=_FACTOR_EPOCH, end_date=_fmt(end))
        days = pd.bdate_range(start, end).astype("datetime64[s]")
        if raw.empty:
            return pd.DataFrame({"date": days, "code": code, "factor": 1.0})
        events = pd.DataFrame({
            "date": pd.to_datetime(raw["dividOperateDate"]).astype("datetime64[s]"),
            "factor": pd.to_numeric(raw["backAdjustFactor"], errors="coerce"),
        }).dropna().sort_values("date")
        idx = np.searchsorted(events["date"].to_numpy(), days.to_numpy(), side="right") - 1
        factor = np.where(idx >= 0, events["factor"].to_numpy()[np.maximum(idx, 0)], 1.0)
        return pd.DataFrame({"date": days, "code": code, "factor": factor})
