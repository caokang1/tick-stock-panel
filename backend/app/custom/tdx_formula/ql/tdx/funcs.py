"""通达信函数库。见技术设计文档 §4.1。

约定：
- 输入输出都是 numpy 数组，形状 (T,) 或 (T, N)，沿第 0 轴（时间）计算，对列向量化。
- 每个函数的第 t 行只使用第 t 行及之前的数据（有截断测试保证）。
- 数值窗口函数（MA、SUM、HHV、STD 等）：窗口不足 n 根或窗口内有 NaN 时结果为 NaN。
- 条件窗口函数（COUNT、EXIST、BARSLAST 等）：NaN 当作"不成立"，窗口不足 n 根时按已有的根数算。
- 条件可以是 bool 数组，也可以是数值数组（非 0 且不是 NaN 为成立）。
- 周期 n 一般是固定的整数；REF、SUM、COUNT、EXIST、EVERY、HHV、LLV、HHVBARS、LLVBARS 的 n 也可以是数组。
- 不修改传入的数组。
"""

# ruff: noqa: N802 —— 函数名与通达信公式里的写法保持一致（MA、REF、CROSS……），所以是大写

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from app.custom.tdx_formula.ql.core.errors import FormulaError

Array = np.ndarray
Period = Any  # int、float 或数组


# ---------------------------------------------------------------- 内部工具


def _f(x: Any) -> Array:
    a = np.asarray(x)
    return a if a.dtype == np.float64 else a.astype(np.float64)


def _b(x: Any) -> Array:
    """转成"是否成立"：bool 原样返回；数值非 0 且不是 NaN 为成立。"""
    a = np.asarray(x)
    if a.dtype == np.bool_:
        return a
    a = a.astype(np.float64, copy=False)
    return (a != 0) & ~np.isnan(a)


as_float = _f
as_bool = _b


def _is_array(n: Period) -> bool:
    return np.ndim(n) > 0


def _period(n: Period, name: str, minimum: int = 0) -> int:
    if _is_array(n):
        raise FormulaError(
            f"{name} 的周期必须是一个固定的数字，不能是随 K 线变化的序列", kind="unsupported", names=[name]
        )
    value = float(n)
    if not np.isfinite(value) or value < minimum:
        raise FormulaError(f"{name} 的周期必须是不小于 {minimum} 的数字，现在是 {value:g}", kind="syntax")
    return int(value)


def _shift(x: Array, k: int, fill: Any) -> Array:
    """整体向后移 k 行（第 t 行取到第 t-k 行），前 k 行填 fill。k >= 0。"""
    if k == 0:
        return x
    out = np.empty_like(x)
    out[:k] = fill
    out[k:] = x[: max(len(x) - k, 0)]
    return out


def _rows(x: Array) -> Array:
    """行号 0..T-1，形状可与 x 广播。"""
    return np.arange(len(x)).reshape((len(x),) + (1,) * (x.ndim - 1))


def _doubling(n: int, base: Any, combine: Callable[[Any, Any, int], Any], shift: Callable[[Any, int], Any]) -> Any:
    """用倍增法得到"最近 n 根"的窗口状态，只需要 O(log n) 次整表运算。

    base 是窗口长度为 1 的状态；combine(近的一段, 远的一段, 近的一段的长度) 合并相邻两段；
    shift(状态, k) 把状态向后移 k 行。
    """
    result: Any = None
    rsize = 0
    block, bsize = base, 1
    while True:
        if n & bsize:
            result = block if result is None else combine(result, shift(block, rsize), rsize)
            rsize += bsize
        if bsize * 2 > n:
            return result
        block = combine(block, shift(block, bsize), bsize)
        bsize *= 2


def _var_window(x: Array, n: Period) -> tuple[Array, Array]:
    """可变周期的公共处理：返回 (整数周期, 该格子是否有效)。周期不是正数或历史不足的格子无效。"""
    nf = np.broadcast_to(_f(n), x.shape)
    valid = np.isfinite(nf) & (nf >= 1)
    ni = np.where(valid, nf, 1).astype(np.int64)
    valid = valid & (ni <= _rows(x) + 1)
    return np.where(valid, ni, 1), valid


def _var_sum(x: Array, n: Period) -> Array:
    ni, valid = _var_window(x, n)
    nan = np.isnan(x)
    pad = np.zeros((1, *x.shape[1:]))
    cs = np.concatenate([pad, np.cumsum(np.where(nan, 0.0, x), axis=0)], axis=0)
    bad = np.concatenate([pad, np.cumsum(nan, axis=0, dtype=np.float64)], axis=0)
    hi = _rows(x) + 1
    lo = np.broadcast_to(hi - ni, x.shape)
    total = cs[1:] - np.take_along_axis(cs, lo, axis=0)
    nbad = bad[1:] - np.take_along_axis(bad, lo, axis=0)
    return np.where(valid & (nbad == 0), total, np.nan)


def _var_extreme(x: Array, n: Period, highest: bool, want_bars: bool) -> Array:
    """可变周期的最高/最低值（或其位置）。逐根回看，回看次数等于最大的周期。"""
    ni, valid = _var_window(x, n)
    best = x.copy()
    bars = np.zeros(x.shape, dtype=np.int32)
    bad = np.isnan(x)
    kmax = min(int(ni.max()) if ni.size else 1, len(x))
    for k in range(1, kmax):
        sh = _shift(x, k, np.nan)
        active = ni > k
        bad |= active & np.isnan(sh)
        take = active & ((sh > best) if highest else (sh < best))
        best = np.where(take, sh, best)
        bars = np.where(take, k, bars)
    out = bars.astype(np.float64) if want_bars else best
    return np.where(valid & ~bad, out, np.nan)


def _window_sums(x: Array, n: int) -> tuple[Array, Array, Array]:
    """最近 n 根的 (Σx, Σ(行号·x), 是否无效)。"""
    nan = np.isnan(x)
    xz = np.where(nan, 0.0, x)
    cs = np.cumsum(xz, axis=0)
    ics = np.cumsum(xz * _rows(x), axis=0)
    nbad = np.cumsum(nan, axis=0, dtype=np.int32)
    invalid = (nbad - _shift(nbad, n, 0)) > 0
    invalid[: n - 1] = True
    return cs - _shift(cs, n, 0.0), ics - _shift(ics, n, 0.0), invalid


def _recursive(x: Any, w_new: float, w_old: float, den: float) -> Array:
    """y[t] = (w_new*x[t] + w_old*y[t-1]) / den，从每一列第一个非 NaN 值起算。"""
    x = _f(x)
    out = np.empty_like(x)
    prev = np.full(x.shape[1:], np.nan)
    for t in range(len(x)):
        xt = x[t]
        cur = (w_new * xt + w_old * prev) / den
        cur = np.where(np.isnan(prev), xt, cur)
        cur = np.where(np.isnan(xt), prev, cur)  # 起算后遇到 NaN：沿用上一个值
        out[t] = cur
        prev = cur
    return out


# ---------------------------------------------------------------- 引用


def REF(x: Any, n: Period) -> Array:
    """n 根之前的值。数值序列取不到时为 NaN，条件序列取不到时为不成立。"""
    a = np.asarray(x)
    if a.dtype != np.bool_:
        a = _f(a)
    fill: Any = False if a.dtype == np.bool_ else np.nan
    if not _is_array(n):
        return _shift(a, _period(n, "REF"), fill)
    nf = np.broadcast_to(_f(n), a.shape)
    valid = np.isfinite(nf) & (nf >= 0)
    src = _rows(a) - np.where(valid, nf, 0).astype(np.int64)
    valid = valid & (src >= 0)
    out = np.take_along_axis(a, np.where(valid, src, 0), axis=0)
    return np.where(valid, out, fill)


# ---------------------------------------------------------------- 统计


def SUM(x: Any, n: Period) -> Array:
    """最近 n 根的和。n=0 表示从第一根累计到当前。"""
    x = _f(x)
    if _is_array(n):
        return _var_sum(x, n)
    n = _period(n, "SUM")
    nan = np.isnan(x)
    cs = np.cumsum(np.where(nan, 0.0, x), axis=0)
    if n == 0:
        return np.where(np.logical_or.accumulate(~nan, axis=0), cs, np.nan)
    out = cs - _shift(cs, n, 0.0)
    nbad = np.cumsum(nan, axis=0, dtype=np.int32)
    out[(nbad - _shift(nbad, n, 0)) > 0] = np.nan
    out[: n - 1] = np.nan
    return out


def COUNT(cond: Any, n: Period) -> Array:
    """最近 n 根里条件成立的根数。n=0 表示从第一根累计。"""
    c = _b(cond)
    if _is_array(n):
        return _var_sum(c.astype(np.float64), n)
    n = _period(n, "COUNT")
    cs = np.cumsum(c, axis=0, dtype=np.int32)
    if n > 0:
        cs = cs - _shift(cs, n, 0)
    return cs.astype(np.float64)


def MA(x: Any, n: Period) -> Array:
    """简单移动平均，不足 n 根时为 NaN。"""
    x = _f(x)
    n = _period(n, "MA")
    if n == 0:
        seen = np.cumsum(~np.isnan(x), axis=0, dtype=np.int32)
        with np.errstate(invalid="ignore", divide="ignore"):
            return SUM(x, 0) / np.where(seen > 0, seen, np.nan)
    return SUM(x, n) / n


def EMA(x: Any, n: Period) -> Array:
    """指数移动平均：y[t] = (2*x[t] + (n-1)*y[t-1]) / (n+1)。"""
    n = _period(n, "EMA", 1)
    return _recursive(x, 2.0, n - 1.0, n + 1.0)


def SMA(x: Any, n: Period, m: Period) -> Array:
    """y[t] = (m*x[t] + (n-m)*y[t-1]) / n。"""
    n = _period(n, "SMA", 1)
    if _is_array(m):
        raise FormulaError("SMA 的权重必须是一个固定的数字", kind="unsupported", names=["SMA"])
    mv = float(m)
    if not 0 < mv <= n:
        raise FormulaError(f"SMA 的权重必须大于 0 且不大于周期 {n}，现在是 {mv:g}", kind="syntax")
    return _recursive(x, mv, n - mv, float(n))


def WMA(x: Any, n: Period) -> Array:
    """加权移动平均，权重从远到近为 1..n。"""
    x = _f(x)
    n = _period(n, "WMA", 1)
    s, i, invalid = _window_sums(x, n)
    start = _rows(x) - n + 1
    out = (i - (start - 1) * s) / (n * (n + 1) / 2)
    out[invalid] = np.nan
    return out


def SLOPE(x: Any, n: Period) -> Array:
    """最近 n 根做线性回归的斜率。"""
    x = _f(x)
    n = _period(n, "SLOPE", 1)
    s, i, invalid = _window_sums(x, n)
    start = _rows(x) - n + 1
    sk = n * (n - 1) / 2
    skk = (n - 1) * n * (2 * n - 1) / 6
    den = n * skk - sk * sk
    if den == 0:
        return np.full(x.shape, np.nan)
    out = (n * (i - start * s) - sk * s) / den
    out[invalid] = np.nan
    return out


def STD(x: Any, n: Period) -> Array:
    """估算标准差（除以 n-1）。"""
    x = _f(x)
    n = _period(n, "STD", 1)
    if n < 2:
        return np.full(x.shape, np.nan)
    # 先减去每列第一个有效值再求和，减小大数相减的误差；这个基准只取决于过去的数据
    nan = np.isnan(x)
    first = np.take_along_axis(x, np.argmax(~nan, axis=0)[None, ...], axis=0)
    d = x - first
    s1 = SUM(d, n)
    s2 = SUM(d * d, n)
    var = np.maximum(s2 - s1 * s1 / n, 0.0) / (n - 1)
    out = np.sqrt(var)
    out[HHV(x, n) == LLV(x, n)] = 0.0  # 窗口内的值全部相同，标准差严格为 0
    return out


def AVEDEV(x: Any, n: Period) -> Array:
    """平均绝对偏差。"""
    x = _f(x)
    n = _period(n, "AVEDEV", 1)
    mean = MA(x, n)
    acc = np.zeros(x.shape)
    for k in range(n):
        acc += np.abs(_shift(x, k, np.nan) - mean)
    return acc / n


# ---------------------------------------------------------------- 极值


def _extreme(x: Any, n: Period, name: str, highest: bool) -> Array:
    x = _f(x)
    if _is_array(n):
        return _var_extreme(x, n, highest, want_bars=False)
    n = _period(n, name)
    if n == 0:
        return (np.fmax if highest else np.fmin).accumulate(x, axis=0)
    op = np.maximum if highest else np.minimum
    return _doubling(n, x, lambda near, far, _r: op(near, far), lambda s, k: _shift(s, k, np.nan))


def _extreme_bars(x: Any, n: Period, name: str, highest: bool) -> Array:
    x = _f(x)
    if _is_array(n):
        return _var_extreme(x, n, highest, want_bars=True)
    n = _period(n, name)
    if n == 0:
        running = (np.fmax if highest else np.fmin).accumulate(x, axis=0)
        return BARSLAST(x == running)
    op = np.maximum if highest else np.minimum

    def combine(near: tuple[Array, Array], far: tuple[Array, Array], r: int) -> tuple[Array, Array]:
        take = (far[0] > near[0]) if highest else (far[0] < near[0])  # 相同时取离现在近的
        return op(near[0], far[0]), np.where(take, far[1] + r, near[1])

    def shift(s: tuple[Array, Array], k: int) -> tuple[Array, Array]:
        return _shift(s[0], k, np.nan), _shift(s[1], k, 0)

    value, bars = _doubling(n, (x, np.zeros(x.shape, dtype=np.int32)), combine, shift)
    return np.where(np.isnan(value), np.nan, bars)


def HHV(x: Any, n: Period) -> Array:
    """最近 n 根的最高值。n=0 表示从第一根到当前。"""
    return _extreme(x, n, "HHV", True)


def LLV(x: Any, n: Period) -> Array:
    """最近 n 根的最低值。n=0 表示从第一根到当前。"""
    return _extreme(x, n, "LLV", False)


def HHVBARS(x: Any, n: Period) -> Array:
    """最近 n 根的最高值出现在几根之前（当天为 0）。"""
    return _extreme_bars(x, n, "HHVBARS", True)


def LLVBARS(x: Any, n: Period) -> Array:
    """最近 n 根的最低值出现在几根之前（当天为 0）。"""
    return _extreme_bars(x, n, "LLVBARS", False)


# ---------------------------------------------------------------- 逻辑


def EXIST(cond: Any, n: Period) -> Array:
    """最近 n 根里条件是否成立过。"""
    return COUNT(cond, n) > 0


def EVERY(cond: Any, n: Period) -> Array:
    """最近 n 根里条件是否一直成立。不足 n 根时为不成立。"""
    c = _b(cond)
    if _is_array(n):
        return COUNT(c, n) == np.trunc(_f(n))
    n = _period(n, "EVERY")
    if n == 0:
        return COUNT(c, 0) == _rows(c) + 1
    return COUNT(c, n) == n


def CROSS(a: Any, b: Any) -> Array:
    """a 上穿 b：上一根 a < b，这一根 a > b。"""
    fa, fb = np.broadcast_arrays(_f(a), _f(b))
    return (_shift(fa, 1, np.nan) < _shift(fb, 1, np.nan)) & (fa > fb)


def LONGCROSS(a: Any, b: Any, n: Period) -> Array:
    """a 在之前 n 根一直小于 b，这一根上穿 b。"""
    n = _period(n, "LONGCROSS", 1)
    fa, fb = np.broadcast_arrays(_f(a), _f(b))
    return _shift(EVERY(fa < fb, n), 1, False) & (fa > fb)


def BARSLAST(cond: Any) -> Array:
    """上一次条件成立到现在的根数，成立当天为 0，从未成立为 NaN。"""
    c = _b(cond)
    rows = _rows(c)
    last = np.maximum.accumulate(np.where(c, rows, -1), axis=0)
    return np.where(last < 0, np.nan, rows - last)


def BARSSINCEN(cond: Any, n: Period) -> Array:
    """最近 n 根里第一次条件成立到现在的根数。n 根里没有成立过为 NaN。"""
    c = _b(cond)
    n = _period(n, "BARSSINCEN")
    rows = _rows(c)
    if n == 0:
        first = np.minimum.accumulate(np.where(c, rows, len(c)), axis=0)
        return np.where(first >= len(c), np.nan, rows - first)
    base = np.where(c, 0, -1).astype(np.int32)
    dist = _doubling(
        n,
        base,
        lambda near, far, r: np.where(far >= 0, far + r, near),
        lambda s, k: _shift(s, k, -1),
    )
    return np.where(dist < 0, np.nan, dist)


def FILTER(cond: Any, n: Period) -> Array:
    """条件成立后，其后 n 根内的成立被忽略。"""
    c = _b(cond)
    n = _period(n, "FILTER")
    out = np.zeros(c.shape, dtype=bool)
    until = np.full(c.shape[1:], -1, dtype=np.int64)  # 屏蔽到第几行（含）
    for t in range(len(c)):
        keep = c[t] & (t > until)
        out[t] = keep
        until = np.where(keep, t + n, until)
    return out


def LAST(cond: Any, a: Period, b: Period) -> Array:
    """从 a 根前到 b 根前条件一直成立。a=0 表示从第一根算起，b=0 表示到当前。"""
    c = _b(cond)
    a = _period(a, "LAST")
    b = _period(b, "LAST")
    if a == 0:
        return _shift(EVERY(c, 0), b, False)
    if a < b:
        raise FormulaError(f"LAST 的第 2 个参数（{a}）不能小于第 3 个参数（{b}）", kind="syntax")
    return _shift(EVERY(c, a - b + 1), b, False)


def IF(cond: Any, a: Any, b: Any) -> Array:
    """条件成立取 a，否则取 b。a、b 都是条件时结果也是条件，否则是数值。"""
    aa, bb = np.asarray(a), np.asarray(b)
    if not (aa.dtype == np.bool_ and bb.dtype == np.bool_):
        aa, bb = _f(aa), _f(bb)
    return np.where(_b(cond), aa, bb)


def NOT(x: Any) -> Array:
    return ~_b(x)


def BETWEEN(x: Any, a: Any, b: Any) -> Array:
    """x 在 a 和 b 之间（含两端，a、b 不分大小）。"""
    fx, fa, fb = _f(x), _f(a), _f(b)
    return ((fa <= fx) & (fx <= fb)) | ((fb <= fx) & (fx <= fa))


def RANGE(x: Any, a: Any, b: Any) -> Array:
    """a < x < b。"""
    fx = _f(x)
    return (_f(a) < fx) & (fx < _f(b))


def UPNDAY(x: Any, n: Period) -> Array:
    """连续 n 根上涨。"""
    fx = _f(x)
    return EVERY(fx > _shift(fx, 1, np.nan), _period(n, "UPNDAY", 1))


def DOWNNDAY(x: Any, n: Period) -> Array:
    """连续 n 根下跌。"""
    fx = _f(x)
    return EVERY(fx < _shift(fx, 1, np.nan), _period(n, "DOWNNDAY", 1))


def NDAY(a: Any, b: Any, n: Period) -> Array:
    """连续 n 根 a > b。"""
    fa, fb = np.broadcast_arrays(_f(a), _f(b))
    return EVERY(fa > fb, _period(n, "NDAY", 1))


# ---------------------------------------------------------------- 数学


def ABS(x: Any) -> Array:
    return np.abs(_f(x))


def MAX(a: Any, b: Any) -> Array:
    return np.maximum(_f(a), _f(b))


def MIN(a: Any, b: Any) -> Array:
    return np.minimum(_f(a), _f(b))


def SQRT(x: Any) -> Array:
    fx = _f(x)
    with np.errstate(invalid="ignore"):
        return np.sqrt(np.where(fx >= 0, fx, np.nan))


def POW(a: Any, b: Any) -> Array:
    with np.errstate(all="ignore"):
        out = np.power(_f(a), _f(b))
    return np.where(np.isfinite(out), out, np.nan)


def LN(x: Any) -> Array:
    fx = _f(x)
    with np.errstate(all="ignore"):
        return np.log(np.where(fx > 0, fx, np.nan))


def LOG(x: Any) -> Array:
    """以 10 为底的对数。"""
    fx = _f(x)
    with np.errstate(all="ignore"):
        return np.log10(np.where(fx > 0, fx, np.nan))


def ROUND(x: Any) -> Array:
    """四舍五入到整数（0.5 向远离 0 的方向进位）。"""
    fx = _f(x)
    return np.sign(fx) * np.floor(np.abs(fx) + 0.5)


def INTPART(x: Any) -> Array:
    """取整数部分（向 0 截断）。"""
    return np.trunc(_f(x))


def MOD(a: Any, b: Any) -> Array:
    """a 除以 b 的余数。除数为 0 时为 NaN。"""
    fa, fb = _f(a), _f(b)
    with np.errstate(all="ignore"):
        return np.fmod(fa, np.where(fb == 0, np.nan, fb))
