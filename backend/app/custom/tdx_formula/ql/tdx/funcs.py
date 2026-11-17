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
    """可变周期的最高/最低值（或其位置）。

    做法：依次算出“最近 1、2、4、8…根”的极值；周期为 n 的格子，用不超过 n 的最大那一档，
    取“从当前往前”和“从窗口最早一根往后”两段的较优者，两段合起来正好盖住整个窗口。
    这样整表运算的次数只和最大周期的对数有关，周期上千也不会慢。
    窗口里有 NaN 时结果为 NaN；数值相同时取离现在近的那一根。
    """
    ni, valid = _var_window(x, n)
    op = np.maximum if highest else np.minimum  # 遇到 NaN 结果就是 NaN
    flat = x.reshape(len(x), -1)  # 统一按二维处理，最后再变回原来的形状
    ni2 = np.broadcast_to(ni, x.shape).reshape(flat.shape)
    out = np.full(flat.shape, np.nan)
    level = np.frexp(ni2.astype(np.float64))[1] - 1  # 不超过 n 的最大的 2 的幂是 2**level
    level[~np.broadcast_to(valid, x.shape).reshape(flat.shape)] = -1
    best = flat
    bars = np.zeros(flat.shape, dtype=np.int32) if want_bars else None
    span = 1
    for k in range(int(level.max()) + 1 if level.size else 0):
        if k > 0:
            half = span // 2
            far = _shift(best, half, np.nan)
            if bars is not None:
                take = (far > best) if highest else (far < best)
                bars = np.where(take, _shift(bars, half, 0) + half, bars)
            best = op(best, far)
        r, c = np.nonzero(level == k)  # 只处理周期落在这一档的格子
        if len(r):
            back = ni2[r, c] - span  # 第二段的终点在 back 根之前
            near, far = best[r, c], best[r - back, c]
            if bars is None:
                out[r, c] = op(near, far)
            else:
                take = (far > near) if highest else (far < near)
                picked = np.where(take, bars[r - back, c] + back, bars[r, c]).astype(np.float64)
                out[r, c] = np.where(np.isnan(op(near, far)), np.nan, picked)
        span *= 2
    return out.reshape(x.shape)


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


def DMA(x: Any, a: Any) -> Array:
    """动态移动平均：y[t] = a[t]*x[t] + (1-a[t])*y[t-1]，a 可以每根 K 线都不同。

    a 按通达信的做法限制在 0 到 1 之间（大于 1 当作 1，即直接取当天的值）。
    a 未知（NaN）的那一根沿用上一个值。从每一列第一个非 NaN 的 x 起算。
    """
    x = _f(x)
    alpha = np.clip(np.broadcast_to(_f(a), x.shape), 0.0, 1.0)
    out = np.empty_like(x)
    prev = np.full(x.shape[1:], np.nan)
    for t in range(len(x)):
        xt, at = x[t], alpha[t]
        cur = at * xt + (1.0 - at) * prev
        cur = np.where(np.isnan(prev), xt, cur)
        cur = np.where(np.isnan(xt) | np.isnan(at), prev, cur)
        out[t] = cur
        prev = cur
    return out


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


# ---------------------------------------------------------------- 数学（补充）


def EXP(x: Any) -> Array:
    with np.errstate(all="ignore"):
        return np.exp(_f(x))


def SIN(x: Any) -> Array:
    return np.sin(_f(x))


def COS(x: Any) -> Array:
    return np.cos(_f(x))


def TAN(x: Any) -> Array:
    return np.tan(_f(x))


def ASIN(x: Any) -> Array:
    with np.errstate(all="ignore"):
        return np.arcsin(_f(x))


def ACOS(x: Any) -> Array:
    with np.errstate(all="ignore"):
        return np.arccos(_f(x))


def ATAN(x: Any) -> Array:
    return np.arctan(_f(x))


def CEILING(x: Any) -> Array:
    return np.ceil(_f(x))


def FLOOR(x: Any) -> Array:
    return np.floor(_f(x))


def SIGN(x: Any) -> Array:
    """正数 1、负数 -1、零 0。"""
    return np.sign(_f(x))


def FRACPART(x: Any) -> Array:
    """小数部分（和 INTPART 相对，符号跟着原数）。"""
    v = _f(x)
    return v - np.trunc(v)


def REVERSE(x: Any) -> Array:
    """相反数。"""
    return -_f(x)


def IFN(cond: Any, a: Any, b: Any) -> Array:
    """条件不成立取 a，否则取 b（和 IF 相反）。"""
    return IF(cond, b, a)


# ---------------------------------------------------------------- 均线、统计（补充）


def MEMA(x: Any, n: Period) -> Array:
    """平滑移动平均：y[t] = (x[t] + (n-1)*y[t-1]) / n，即 SMA(x, n, 1)。"""
    return SMA(x, n, 1)


def _moments(x: Any, n: Period, name: str) -> tuple[Array, Array, int, Array]:
    """最近 n 根的 (Σd, Σd², n, x)，d 是减去每列第一个有效值之后的数（减小大数相减的误差）。"""
    x = _f(x)
    n = _period(n, name, 1)
    nan = np.isnan(x)
    first = np.take_along_axis(x, np.argmax(~nan, axis=0)[None, ...], axis=0)
    d = x - first
    return SUM(d, n), SUM(d * d, n), n, x


def DEVSQ(x: Any, n: Period) -> Array:
    """最近 n 根的偏差平方和。"""
    s1, s2, n, _ = _moments(x, n, "DEVSQ")
    return np.maximum(s2 - s1 * s1 / n, 0.0)


def VARP(x: Any, n: Period) -> Array:
    """总体方差（除以 n）。"""
    s1, s2, n, _ = _moments(x, n, "VARP")
    return np.maximum(s2 - s1 * s1 / n, 0.0) / n


def VAR(x: Any, n: Period) -> Array:
    """估算方差（除以 n-1）。"""
    s1, s2, n, x = _moments(x, n, "VAR")
    if n < 2:
        return np.full(x.shape, np.nan)
    return np.maximum(s2 - s1 * s1 / n, 0.0) / (n - 1)


def STDP(x: Any, n: Period) -> Array:
    """总体标准差（除以 n）。"""
    return np.sqrt(VARP(x, n))


def FORCAST(x: Any, n: Period) -> Array:
    """最近 n 根做线性回归，回归线在当前这一根上的值。"""
    n = _period(n, "FORCAST", 1)
    if n < 2:
        return _f(x).copy()
    slope = SLOPE(x, n)
    return MA(x, n) + slope * (n - 1) / 2.0


# ---------------------------------------------------------------- K 线计数（补充）


def BARSCOUNT(x: Any) -> Array:
    """从第一根有效数据到现在的根数（第一根为 1）；还没有有效数据时为 NaN。"""
    valid = ~np.isnan(_f(x))
    seen = np.logical_or.accumulate(valid, axis=0)
    first = np.argmax(seen, axis=0)[None, ...]
    return np.where(seen, _rows(valid) - first + 1.0, np.nan)


def BARSSINCE(cond: Any) -> Array:
    """第一次条件成立到现在的根数，成立当天为 0；还没成立过为 NaN。"""
    c = _b(cond)
    seen = np.logical_or.accumulate(c, axis=0)
    first = np.argmax(seen, axis=0)[None, ...]
    return np.where(seen, (_rows(c) - first).astype(np.float64), np.nan)


def BARSLASTCOUNT(cond: Any) -> Array:
    """到当前为止条件连续成立了多少根；当前不成立为 0。"""
    c = _b(cond)
    rows = _rows(c)
    last_false = np.maximum.accumulate(np.where(c, -1, rows), axis=0)
    return (rows - last_false).astype(np.float64)


def VALUEWHEN(cond: Any, x: Any) -> Array:
    """最近一次条件成立时 x 的值；还没成立过为 NaN。"""
    c = _b(cond)
    x = np.broadcast_to(_f(x), c.shape)
    rows = _rows(c)
    last = np.maximum.accumulate(np.where(c, rows, -1), axis=0)
    picked = np.take_along_axis(x, np.maximum(last, 0), axis=0)
    return np.where(last < 0, np.nan, picked)


def SUMBARS(x: Any, a: Any) -> Array:
    """往前累加 x，加到不小于 a 需要多少根；全部加完也不够时为 NaN（通达信此时取已有的根数）。"""
    x = _f(x)
    target = np.broadcast_to(_f(a), x.shape)
    cs = np.cumsum(np.where(np.isnan(x), 0.0, x), axis=0)
    pad = np.zeros((1, *x.shape[1:]))
    before = np.concatenate([pad, cs[:-1]], axis=0)  # before[t] = 第 t 根之前的累计
    out = np.full(x.shape, np.nan)
    # 要找最大的 s 使 cs[t] - before[s] >= a，即 before[s] <= cs[t] - a；before 对非负的 x 单调不减，按列二分
    flat_before = before.reshape(len(x), -1)
    flat_need = (cs - target).reshape(len(x), -1)
    flat_out = out.reshape(len(x), -1)
    for col in range(flat_before.shape[1]):
        b = flat_before[:, col]
        if np.any(np.diff(b) < 0):
            continue  # x 里有负数时累计不单调，这一列不算
        pos = np.searchsorted(b, flat_need[:, col], side="right") - 1
        t = np.arange(len(x))
        ok = (pos >= 0) & (pos <= t) & ~np.isnan(flat_need[:, col])
        flat_out[ok, col] = (t - pos + 1)[ok]
    return flat_out.reshape(x.shape)


def EXISTR(cond: Any, a: Period, b: Period) -> Array:
    """从 a 根前到 b 根前条件成立过。"""
    c = _b(cond)
    a = _period(a, "EXISTR")
    b = _period(b, "EXISTR")
    if a < b:
        raise FormulaError(f"EXISTR 的第 2 个参数（{a}）不能小于第 3 个参数（{b}）", kind="syntax")
    return _shift(COUNT(c, a - b + 1) > 0, b, False)


# ---------------------------------------------------------------- 筹码分布（估算）


CHIP_BINS = 160


def chip_distribution(high: Any, low: Any, turnover_pct: Any, query: Any, mode: str) -> Array:
    """按换手率估算筹码分布，并回答一个问题。详见 chip_pass。"""
    return chip_pass(high, low, turnover_pct, [(mode, query)])[0]


def chip_pass(high: Any, low: Any, turnover_pct: Any, requests: list[tuple[str, Any]]) -> list[Array]:
    """按换手率估算筹码分布，一趟算完若干个问题（筹码的演变只算一遍，所以一次问几个比分开问快得多）。

    每个问题是 (mode, query)：
    mode="winner"：query 是价格，结果是这个价位以下的筹码占比（0 到 1），即获利盘比例。
    mode="cost"：query 是百分比（0 到 100），结果是“这么多比例的筹码成本在它以下”的那个价位。

    估算方法（公开的通行做法，通达信自己的算法没有公开，数值会有出入）：
    每天换手的那部分筹码，按当天最低到最高价均匀铺开；原来的筹码按 (1 - 换手率) 留下。
    没有换手率或没有行情的日子（停牌、未上市、流通股本未知）筹码不变；还没有任何筹码时结果为 NaN。
    每只股票用它自己历史上的最低到最高价划成等比的价格格子来记筹码，格子有粗细，结果是近似值。
    """
    high, low = _f(high), _f(low)
    shape = high.shape
    hi2, lo2 = high.reshape(len(high), -1), low.reshape(len(low), -1)
    rate = np.clip(np.broadcast_to(_f(turnover_pct), shape).reshape(hi2.shape) / 100.0, 0.0, 1.0)
    asks = [(mode, np.broadcast_to(_f(query), shape).reshape(hi2.shape)) for mode, query in requests]
    t_count, n = hi2.shape
    with np.errstate(all="ignore"):
        floor = np.log(np.nanmin(np.where(lo2 > 0, lo2, np.nan), axis=0)) - 0.02
        ceil = np.log(np.nanmax(np.where(hi2 > 0, hi2, np.nan), axis=0)) + 0.02
        width = np.where(np.isfinite(ceil - floor) & (ceil > floor), (ceil - floor) / CHIP_BINS, 1.0)
        floor = np.where(np.isfinite(floor), floor, 0.0)
        # 每天的最低、最高价各落在第几个格子上（带小数），一次算好
        pos_lo = ((np.log(lo2) - floor) / width).astype(np.float32)
        pos_hi = np.maximum(((np.log(hi2) - floor) / width).astype(np.float32), pos_lo + 1e-3)
        trade = np.isfinite(pos_lo) & np.isfinite(pos_hi) & np.isfinite(rate) & (rate > 0)
        pos_q = [((np.log(np.maximum(q, 1e-12)) - floor) / width).astype(np.float32) if mode == "winner" else None
                 for mode, q in asks]
    bins = np.arange(CHIP_BINS, dtype=np.float32)[None, :]
    chips = np.zeros((n, CHIP_BINS), dtype=np.float32)
    has = np.zeros(n, dtype=bool)
    outs = [np.full(hi2.shape, np.nan) for _ in asks]
    buf_a = np.empty((n, CHIP_BINS), dtype=np.float32)
    buf_b = np.empty((n, CHIP_BINS), dtype=np.float32)
    rows = np.arange(n)

    for t in range(t_count):
        active = trade[t]
        if active.any():
            # 当天成交的筹码在各个格子里的份额：[最低, 最高] 均匀铺开。没成交的股票份额全为 0
            pl = np.where(active, pos_lo[t], 0.0).astype(np.float32)
            ph = np.where(active, pos_hi[t], 1.0).astype(np.float32)
            np.subtract(ph[:, None], bins, out=buf_a)
            np.clip(buf_a, 0.0, 1.0, out=buf_a)
            np.subtract(pl[:, None], bins, out=buf_b)
            np.clip(buf_b, 0.0, 1.0, out=buf_b)
            buf_a -= buf_b
            r = rate[t]
            add = np.where(active, np.where(has, r, 1.0), 0.0) / (ph - pl)  # 第一天：全部筹码都是当天的
            keep = np.where(active, np.where(has, 1.0 - r, 0.0), 1.0)
            buf_a *= add.astype(np.float32)[:, None]
            chips *= keep.astype(np.float32)[:, None]
            chips += buf_a
            has |= active
        if not has.any():
            continue
        cum = np.cumsum(chips, axis=1, out=buf_b)  # 每个格子及以下的筹码合计；各个问题都从它上面取数
        for (mode, q), pos, out in zip(asks, pos_q, outs, strict=True):
            ok = has & np.isfinite(q[t])
            if not ok.any():
                continue
            if mode == "winner":
                # 价位落在第 k 个格子的 frac 处：下面的格子全算，这个格子按比例算
                p = np.where(ok, pos[t], 0.0)
                k = np.clip(np.floor(p), 0, CHIP_BINS - 1).astype(np.int64)
                frac = np.clip(p - k, 0.0, 1.0)
                below = np.where(k > 0, cum[rows, np.maximum(k - 1, 0)], 0.0)
                out[t] = np.where(ok, np.clip(below + chips[rows, k] * frac, 0.0, 1.0), np.nan)
            else:
                target = np.clip(np.where(ok, q[t], 0.0) / 100.0, 0.0, 1.0) * cum[:, -1]
                k = np.minimum((cum < target[:, None]).sum(axis=1), CHIP_BINS - 1)  # 第一个累计达到目标的格子
                before = np.where(k > 0, cum[rows, np.maximum(k - 1, 0)], 0.0)
                inside = chips[rows, k]
                frac = np.where(inside > 0, (target - before) / np.where(inside > 0, inside, 1.0), 0.0)
                out[t] = np.where(ok, np.exp(floor + (k + np.clip(frac, 0.0, 1.0)) * width), np.nan)
    return [out.reshape(shape) for out in outs]
