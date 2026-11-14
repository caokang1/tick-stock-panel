"""全市场日线的二维表示：行是交易日，列是股票。约定见技术设计文档 §2。"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date

import numpy as np

from app.custom.tdx_formula.ql.core.errors import DataError

_MATRIX_FIELDS = (
    "open", "high", "low", "close", "volume", "amount",
    "raw_open", "raw_high", "raw_low", "raw_close", "raw_preclose",
    "listed", "suspended", "is_st", "float_shares",
)


@dataclass(frozen=True)
class Panel:
    dates: np.ndarray  # (T,) datetime64[D]，升序
    codes: np.ndarray  # (N,) str
    open: np.ndarray  # 以下为 (T, N) float64，后复权
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    raw_open: np.ndarray  # 不复权
    raw_high: np.ndarray
    raw_low: np.ndarray
    raw_close: np.ndarray
    raw_preclose: np.ndarray
    listed: np.ndarray  # (T, N) bool
    suspended: np.ndarray  # (T, N) bool
    is_st: np.ndarray  # (T, N) bool
    st_known: bool = True
    # 下面两项是后来加的，可以不给：不给时流通股本全部未知，行业全部为空
    float_shares: np.ndarray = None  # type: ignore[assignment]  # (T, N) float，当天的流通股本（股），未知为 NaN
    industry: np.ndarray = None  # type: ignore[assignment]  # (N,) str，现在所属的行业，未知为 ""

    def __post_init__(self) -> None:
        shape = (len(self.dates), len(self.codes))
        if self.float_shares is None:
            object.__setattr__(self, "float_shares", np.full(shape, np.nan))
        if self.industry is None:
            object.__setattr__(self, "industry", np.full(len(self.codes), "", dtype="U1"))
        if self.industry.shape != (len(self.codes),):
            raise DataError(f"Panel.industry 的长度 {self.industry.shape} 与股票数 {len(self.codes)} 不一致")
        for name in _MATRIX_FIELDS:
            arr = getattr(self, name)
            if arr.shape != shape:
                raise DataError(f"Panel.{name} 的形状 {arr.shape} 与 (交易日数, 股票数) = {shape} 不一致")

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.dates), len(self.codes))

    @property
    def float_cap(self) -> np.ndarray:
        """流通市值（元）= 不复权收盘价 × 当天的流通股本。流通股本未知的格子为 NaN。"""
        return self.raw_close * self.float_shares

    @property
    def turnover(self) -> np.ndarray:
        """换手率（%）= 成交量 ÷ 流通股本 × 100。流通股本未知的格子为 NaN。"""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.float_shares > 0, self.volume / self.float_shares * 100.0, np.nan)

    @property
    def tradable(self) -> np.ndarray:
        """当天已上市且没有停牌。"""
        return self.listed & ~self.suspended

    def index_of(self, code: str) -> int:
        hit = np.nonzero(self.codes == code)[0]
        if len(hit) == 0:
            raise DataError(f"数据里没有这只股票：{code}")
        return int(hit[0])

    def slice_dates(self, start: date | None = None, end: date | None = None) -> Panel:
        lo = 0 if start is None else int(np.searchsorted(self.dates, np.datetime64(start, "D"), side="left"))
        hi = len(self.dates) if end is None else int(np.searchsorted(self.dates, np.datetime64(end, "D"), side="right"))
        changes: dict[str, np.ndarray] = {"dates": self.dates[lo:hi]}
        for name in _MATRIX_FIELDS:
            changes[name] = getattr(self, name)[lo:hi]
        return replace(self, **changes)  # type: ignore[arg-type]

    def head(self, n_rows: int) -> Panel:
        """只保留前 n_rows 个交易日（截断测试用）。"""
        changes: dict[str, np.ndarray] = {"dates": self.dates[:n_rows]}
        for name in _MATRIX_FIELDS:
            changes[name] = getattr(self, name)[:n_rows]
        return replace(self, **changes)  # type: ignore[arg-type]


def single_series_panel(dates: np.ndarray, code: str, open_: np.ndarray, high: np.ndarray, low: np.ndarray,
                        close: np.ndarray, volume: np.ndarray, amount: np.ndarray) -> Panel:
    """把一条日线（例如一个指数）包装成只有一列的 Panel，这样可以直接用公式求值。"""
    def col(a: np.ndarray) -> np.ndarray:
        return np.asarray(a, dtype=float).reshape(-1, 1)

    t = len(dates)
    c = col(close)
    preclose = np.vstack([c[:1], c[:-1]]) if t else c
    flags = np.zeros((t, 1), dtype=bool)
    return Panel(
        dates=np.asarray(dates), codes=np.array([code]),
        open=col(open_), high=col(high), low=col(low), close=c, volume=col(volume), amount=col(amount),
        raw_open=col(open_), raw_high=col(high), raw_low=col(low), raw_close=c, raw_preclose=preclose,
        listed=np.isfinite(c), suspended=flags, is_st=flags.copy(),
    )


def matrix_field_names() -> tuple[str, ...]:
    return _MATRIX_FIELDS


def all_field_names() -> tuple[str, ...]:
    return tuple(f.name for f in fields(Panel))
