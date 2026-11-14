"""股票代码格式与板块判断。统一格式：sh.600000 / sz.000001 / bj.430047。"""

from __future__ import annotations

from app.custom.tdx_formula.ql.core.errors import DataError

MAIN = "main"
CHINEXT = "chinext"  # 创业板
STAR = "star"  # 科创板
BSE = "bse"  # 北交所


def normalize(code: str) -> str:
    """接受 600000 / sh600000 / sh.600000 / 600000.SH 等写法，返回 sh.600000。"""
    c = code.strip().lower().replace("_", ".")
    if "." in c:
        a, b = c.split(".", 1)
        if a in ("sh", "sz", "bj") and b.isdigit():
            return f"{a}.{b}"
        if b in ("sh", "sz", "bj") and a.isdigit():
            return f"{b}.{a}"
    elif c[:2] in ("sh", "sz", "bj") and c[2:].isdigit():
        return f"{c[:2]}.{c[2:]}"
    elif c.isdigit() and len(c) == 6:
        return f"{exchange_of_digits(c)}.{c}"
    raise DataError(f"无法识别的股票代码：{code}")


def exchange_of_digits(digits: str) -> str:
    if digits.startswith("92"):  # 北交所新代码段，要先于"9 开头算上海"判断
        return "bj"
    if digits[0] in ("6", "9") or digits.startswith(("5", "11")):
        return "sh"
    if digits[0] in ("0", "2", "3") or digits.startswith(("15", "16", "12")):
        return "sz"
    if digits[0] in ("4", "8") or digits.startswith("92"):
        return "bj"
    raise DataError(f"无法判断交易所：{digits}")


def digits(code: str) -> str:
    return normalize(code).split(".", 1)[1]


def board_of(code: str) -> str:
    c = normalize(code)
    ex, d = c.split(".", 1)
    if ex == "bj":
        return BSE
    if ex == "sh" and d.startswith(("688", "689")):
        return STAR
    if ex == "sz" and d.startswith(("300", "301", "302")):
        return CHINEXT
    return MAIN
