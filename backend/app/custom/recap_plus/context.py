"""把复盘要用的补充材料拼成一段文字，接在面板自带的市场数据后面交给 AI。

本机数据（日线、股票名称和股本、行业和概念归属、持仓提醒里登记的持仓）直接读文件；
两融、主力资金流向、估值要联网，见 remote.py。哪一块没有取到，就在材料里明说，让 AI 不要编。
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from . import notices, remote

logger = logging.getLogger(__name__)

HISTORY_DAYS = 25
TOP_N = 10
MAX_HOLDINGS = 30
MAX_PEERS_PER_STOCK = 5


# ---------------- 小工具 ----------------


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def yi(value: Any, digits: int = 2) -> str:
    v = _num(value)
    return "—" if v is None else f"{v / 1e8:.{digits}f} 亿"


def wan(value: Any) -> str:
    v = _num(value)
    if v is None:
        return "—"
    return f"{v / 1e8:+.2f} 亿" if abs(v) >= 1e8 else f"{v / 1e4:+.2f} 万"


def amt(value: Any) -> str:
    """不带正负号的金额（余额、买入额这类本来就不为负的数）。"""
    v = _num(value)
    if v is None:
        return "—"
    return f"{v / 1e8:.2f} 亿" if abs(v) >= 1e8 else f"{v / 1e4:.2f} 万"


def pct(value: Any) -> str:
    v = _num(value)
    return "—" if v is None else f"{v * 100:+.2f}%"


def load_config(root: Path) -> dict[str, Any]:
    """本机的设置：peers = {持仓代码: [对标股票代码, ...]}。文件不存在或写坏了就当没有。"""
    try:
        data = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"peers": {}}
    peers = data.get("peers") if isinstance(data, dict) else None
    clean: dict[str, list[str]] = {}
    if isinstance(peers, dict):
        for key, value in peers.items():
            if isinstance(value, list):
                clean[str(key).upper()] = [str(s).upper() for s in value if isinstance(s, str)][:MAX_PEERS_PER_STOCK]
    return {"peers": clean}


# ---------------- 读本机数据 ----------------


def _read_days(data_dir: Path, as_of: str) -> list[tuple[str, Any]]:
    """到 as_of 为止最近若干个交易日的日线（旧的在前）。每项是 (日期, 以 symbol 为索引的表)。"""
    import pandas as pd

    folder = data_dir / "kline_daily_enriched"
    days = sorted(p.name[5:] for p in folder.glob("date=*") if p.name[5:] <= as_of)[-HISTORY_DAYS:]
    out = []
    for day in days:
        files = sorted((folder / f"date={day}").glob("*.parquet"))
        if not files:
            continue
        frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        out.append((day, frame.drop_duplicates("symbol").set_index("symbol")))
    return out


def _read_instruments(data_dir: Path) -> Any:
    import pandas as pd

    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["name", "total_shares"])
    return pd.read_parquet(path).drop_duplicates("symbol").set_index("symbol")


def _read_board(data_dir: Path, table: str) -> dict[str, str]:
    """行业 / 概念归属表 → {symbol: 归属}。表里除了代码和名称以外的那一列就是归属。"""
    import pandas as pd

    path = data_dir / "ext_data" / table / "part.parquet"
    if not path.exists():
        return {}
    frame = pd.read_parquet(path)
    value_cols = [c for c in frame.columns if c not in ("股票代码", "股票简称", "symbol", "code")]
    if "symbol" not in frame.columns or not value_cols:
        return {}
    return {str(s): str(v) for s, v in zip(frame["symbol"], frame[value_cols[0]], strict=False) if v}


def read_holdings(data_dir: Path) -> list[dict[str, Any]]:
    """“持仓提醒”里登记的持仓，同一只股票的几笔合成一笔（数量相加，成本按数量加权）。"""
    from app.strategy import lots

    merged: dict[str, dict[str, float]] = {}
    for lot in lots.load_all(data_dir):
        symbol = str(lot.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        qty, cost = _num(lot.get("qty")) or 0.0, _num(lot.get("cost_price")) or 0.0
        item = merged.setdefault(symbol, {"qty": 0.0, "amount": 0.0, "costed": 0.0})
        item["qty"] += qty
        if qty > 0 and cost != 0:  # 成本可以是负数（落袋盈利超过本金）
            item["amount"] += qty * cost
            item["costed"] += qty
        elif cost != 0 and item["costed"] == 0:
            item["amount"], item["costed"] = cost, 1.0  # 没填数量时也把成本留下
    out = []
    for symbol, item in list(merged.items())[:MAX_HOLDINGS]:
        cost = item["amount"] / item["costed"] if item["costed"] > 0 else 0.0
        out.append({"symbol": symbol, "qty": item["qty"], "cost": cost})
    return out


# ---------------- 各块材料 ----------------


def _change(today: Any, prev: Any) -> Any:
    """当天相对前一交易日的涨跌幅（小数）。前一天没有这只股票的为空。"""
    return (today["close"] / prev["close"].reindex(today.index) - 1.0) if prev is not None else None


def _market_blocks(today: Any, prev: Any, names: Any, industry: dict[str, str]) -> list[str]:
    lines: list[str] = []
    change = _change(today, prev)

    def name(symbol: str) -> str:
        return str(names["name"].get(symbol, symbol)) if "name" in names else symbol

    def chg(symbol: str) -> str:
        return pct(change.get(symbol)) if change is not None else "—"

    # 成交额：分沪深
    def split(frame: Any) -> tuple[float, float]:
        sh = float(frame.loc[frame.index.str.endswith(".SH"), "amount"].sum())
        sz = float(frame.loc[frame.index.str.endswith(".SZ"), "amount"].sum())
        return sh, sz

    sh, sz = split(today)
    lines += ["## 成交额口径（沪深两市个股合计，不含北交所、ETF、债券）",
              f"- 沪市 {yi(sh)} + 深市 {yi(sz)} = {yi(sh + sz)}"]
    if prev is not None:
        psh, psz = split(prev)
        total, before = sh + sz, psh + psz
        if before > 0:
            lines.append(f"- 前一交易日 {yi(before)}，变化 {wan(total - before)}（{pct(total / before - 1)}）")

    top = today.sort_values("amount", ascending=False).head(TOP_N)
    lines += ["", f"## 成交额前{TOP_N}", "| 个股 | 成交额 | 涨跌幅 | 行业 |", "|---|---|---|---|"]
    lines += [f"| {name(s)} | {yi(r['amount'])} | {chg(s)} | {industry.get(s, '—')} |" for s, r in top.iterrows()]

    if "total_shares" in names and "raw_close" in today:
        cap = (today["raw_close"] * names["total_shares"].reindex(today.index)).dropna().sort_values(ascending=False)
        if len(cap):
            lines += ["", f"## 总市值前{TOP_N}（收盘价 × 总股本）", "| 个股 | 总市值 | 涨跌幅 |", "|---|---|---|"]
            lines += [f"| {name(s)} | {yi(v, 0)} | {chg(s)} |" for s, v in cap.head(TOP_N).items()]

    # 行业涨跌停分布
    ups = today[today.get("consecutive_limit_ups", 0) > 0] if "consecutive_limit_ups" in today else today.iloc[0:0]
    downs = (today[today.get("consecutive_limit_downs", 0) > 0]
             if "consecutive_limit_downs" in today else today.iloc[0:0])
    groups: dict[str, dict[str, list[str]]] = {}
    for s, r in ups.sort_values("consecutive_limit_ups", ascending=False).iterrows():
        boards = int(r["consecutive_limit_ups"])
        label = f"{name(s)}{f'({boards}板)' if boards > 1 else ''}"
        groups.setdefault(industry.get(s, "未归类"), {"up": [], "down": []})["up"].append(label)
    for s in downs.index:
        groups.setdefault(industry.get(s, "未归类"), {"up": [], "down": []})["down"].append(name(s))
    lines += ["", f"## 行业涨跌停分布（东方财富行业分类，不是申万一级；涨停 {len(ups)} 家，跌停 {len(downs)} 家）"]
    if groups:
        lines += ["| 行业 | 涨停 | 跌停 | 涨停代表 | 跌停代表 |", "|---|---|---|---|---|"]
        ordered = sorted(groups.items(), key=lambda kv: -(len(kv[1]["up"]) + len(kv[1]["down"])))
        rest: list[str] = []
        for ind, g in ordered:
            if len(g["up"]) + len(g["down"]) < 2:  # 只有一家的行业合成一行，表格不至于太长
                rest.append(f"{ind}（{'涨停 ' + g['up'][0] if g['up'] else '跌停 ' + g['down'][0]}）")
                continue
            lines.append(f"| {ind} | {len(g['up'])} | {len(g['down'])} | {'、'.join(g['up'][:4]) or '—'} | "
                         f"{'、'.join(g['down'][:4]) or '—'} |")
        if rest:
            lines.append(f"\n只有一家涨停或跌停的行业：{'、'.join(rest)}")
    else:
        lines.append("（当天没有涨停或跌停的股票）")
    return lines


def _margin_block(rows: list[dict[str, Any]] | None, error: str | None) -> list[str]:
    lines = ["", "## 全市场两融（交易所次日披露，注意数据日期）"]
    if not rows:
        return [*lines, f"（没有取到：{error or '来源没有返回数据'}。这一节请直接说明数据缺失，不要编数字。）"]
    latest = rows[0]
    lines.append(f"- 数据日期 {latest['date']}：融资余额 {yi(latest['rzye'])}，融资买入额 {yi(latest['rzmre'])}，"
                 f"融资净买入 {wan(latest['rzjme'])}，融券余额 {yi(latest['rqye'])}，两融余额 {yi(latest['rzrqye'])}")
    if len(rows) > 1 and _num(rows[1].get("rzye")) and _num(latest.get("rzye")):
        before = rows[1]
        lines.append(f"- 前一日 {before['date']}：融资余额 {yi(before['rzye'])}，"
                     f"环比 {pct(_num(latest['rzye']) / _num(before['rzye']) - 1)}；融资买入额 {yi(before['rzmre'])}")
    return lines


def _flow_text(rows: list[dict[str, Any]] | None, as_of: str) -> str:
    if not rows:
        return "没有取到"
    rows = [r for r in rows if r["date"] <= as_of]
    if not rows:
        return "没有取到"
    last = rows[-1]

    def total(n: int) -> str:
        return wan(sum(r["main"] for r in rows[-n:])) if len(rows) >= n else "—"

    return (f"{last['date']} 主力净流入 {wan(last['main'])}（超大单 {wan(last['huge'])}、大单 {wan(last['large'])}、"
            f"中单 {wan(last['medium'])}、小单 {wan(last['small'])}）；近5日 {total(5)}、近10日 {total(10)}、近20日 {total(20)}")


def _stock_margin_text(rows: list[dict[str, Any]] | None) -> str:
    if rows is None:
        return "没有取到"
    if not rows:
        return "不是两融标的，或来源没有它的数据"
    latest = rows[0]
    text = (f"数据日期 {latest['date']}：融资余额 {yi(latest['rzye'])}，当日融资买入 {amt(latest['rzmre'])}，"
            f"融资净买入 {wan(latest['rzjme'])}（近5日 {wan(latest.get('rzjme5d'))}、近10日 {wan(latest.get('rzjme10d'))}），"
            f"融券余额 {amt(latest['rqye'])}")
    if _num(latest.get("rzyezb")) is not None:
        text += f"，融资余额占流通市值 {_num(latest['rzyezb']):.2f}%"
    if len(rows) > 1 and _num(rows[1].get("rzye")) and _num(latest.get("rzye")):
        text += f"，融资余额环比 {pct(_num(latest['rzye']) / _num(rows[1]['rzye']) - 1)}"
    return text


def read_hk_quotes(data_dir: Path) -> dict[str, Any]:
    """港股报价扩展存在本机的最近一次报价和汇率；这里只读文件，不联网。"""
    from app.custom.hk_quotes.routes import read_saved

    return read_saved(data_dir / "user_data" / "extensions" / "hk_quotes" / "quotes.json")


def _hk_holding(h: dict[str, Any], hk: dict[str, Any]) -> tuple[list[str], float, float]:
    """一只港股持仓的几行，以及折成人民币的市值和成本（算不出来的是 0）。面板没有港股的 K 线和资金数据。"""
    s = h["symbol"]
    quote, rate = (hk.get("items") or {}).get(s), _num(hk.get("rate"))
    if not quote or not _num(quote.get("last")):
        return ["", f"### {s}（港股）", "- 还没有取到这只港股的报价"], 0.0, 0.0
    last = float(quote["last"])
    lines = ["", f"### {quote.get('name') or s}（{s}，港股，价格单位港元）"]
    position = f"- 持仓：{h['qty']:g} 股"
    value = cost = 0.0
    if rate:
        value = h["qty"] * last * rate
        position += f"，市值折人民币 {value / 1e4:.2f} 万（1 港元 = {rate:.4f} 人民币）"
    if h["cost"] != 0:
        position += f"，成本 {h['cost']:g} 港元"
        if h["cost"] > 0:
            position += f"，浮动盈亏 {pct(last / h['cost'] - 1)}"
        if rate:
            cost = h["qty"] * h["cost"] * rate
            position += f"（折人民币 {wan(value - cost)}）"
    else:
        position += "，成本没有登记"
    lines.append(position)
    change = _num(quote.get("change_pct"))
    lines.append(f"- 报价：{last:g} 港元，较前收 {pct(change) if change is not None else '—'}，"
                 f"报价时间 {quote.get('time') or '—'}（面板没有港股的 K 线、资金和估值数据）")
    return lines, value, cost


def _holding_blocks(holdings: list[dict[str, Any]], days: list[tuple[str, Any]], names: Any,
                    industry: dict[str, str], concept: dict[str, str], peers: dict[str, list[str]],
                    net: dict[str, Any], as_of: str, hk: dict[str, Any] | None = None) -> list[str]:
    today = days[-1][1]
    prev = days[-2][1] if len(days) > 1 else None
    change = _change(today, prev)

    def name(symbol: str) -> str:
        return str(names["name"].get(symbol, symbol)) if "name" in names else symbol

    def back(symbol: str, n: int) -> str:
        if len(days) <= n or symbol not in today.index or symbol not in days[-1 - n][1].index:
            return "—"
        return pct(today.at[symbol, "close"] / days[-1 - n][1].at[symbol, "close"] - 1)

    lines = ["", f"## 持仓明细（共 {len(holdings)} 只，来自“持仓提醒”里登记的数量和成本）"]
    total_value = total_cost = 0.0
    for h in holdings:
        s = h["symbol"]
        if s.endswith(".HK"):
            block, value, cost = _hk_holding(h, hk or {})
            lines += block
            total_value, total_cost = total_value + value, total_cost + cost
            continue
        lines += ["", f"### {name(s)}（{s}）"]
        if s not in today.index:
            lines.append("- 当天没有这只股票的行情（停牌或数据缺失）")
            continue
        row = today.loc[s]
        close = _num(row.get("raw_close")) or _num(row.get("close")) or 0.0
        value = h["qty"] * close
        lines.append(f"- 所属：行业 {industry.get(s, '—')}；概念 {concept.get(s, '—')}")
        position = f"- 持仓：{h['qty']:g} 股，市值 {value / 1e4:.2f} 万"
        if h["cost"] != 0:
            position += f"，成本 {h['cost']:g}，浮动盈亏 {wan(h['qty'] * (close - h['cost']))}"
            # 成本为负时比例没有意义，只给金额
            position += f"（{pct(close / h['cost'] - 1)}）" if h["cost"] > 0 else "（成本为负，已收回全部本金）"
            total_cost += h["qty"] * h["cost"]
        else:
            position += "，成本没有登记"
        total_value += value
        lines.append(position)
        closes = [d[1].at[s, "raw_close"] for d in days if s in d[1].index and "raw_close" in d[1]]
        amounts = [d[1].at[s, "amount"] for d in days[-6:-1] if s in d[1].index]
        line = (f"- 行情：收盘 {close:.2f}，当日 {pct(change.get(s)) if change is not None else '—'}，"
                f"成交额 {yi(row.get('amount'))}，换手 {_num(row.get('turnover_rate')) or 0:.2f}%；"
                f"近5日 {back(s, 5)}、近10日 {back(s, 10)}、近20日 {back(s, 20)}")
        if amounts and sum(amounts) > 0:
            line += f"；成交额是前5日均值的 {float(row['amount']) / (sum(amounts) / len(amounts)):.2f} 倍"
        if closes:
            line += f"；近{len(closes)}日收盘最高 {max(closes):.2f}、最低 {min(closes):.2f}"
        lines.append(line)
        val = (net.get("valuation") or {}).get(s) or {}
        if val:
            pe = _num(val.get("pe_ttm"))
            pe_text = "亏损（市盈率为负）" if pe is not None and pe <= 0 else (f"{pe:.2f} 倍" if pe is not None else "—")
            lines.append(f"- 估值：市盈率(TTM) {pe_text}，市净率 {_num(val.get('pb')) or 0:.2f} 倍，"
                         f"总市值 {_num(val.get('total_mv_yi')) or 0:.2f} 亿，流通市值 {_num(val.get('float_mv_yi')) or 0:.2f} 亿")
        lines.append(f"- 主力资金：{_flow_text((net.get('flow') or {}).get(s), as_of)}")
        lines.append(f"- 两融：{_stock_margin_text((net.get('stock_margin') or {}).get(s))}")
        if peers.get(s):
            shown = [f"{name(p)} {pct(change.get(p)) if change is not None and p in today.index else '无行情'}"
                     for p in peers[s]]
            lines.append(f"- 对标股票当日：{'、'.join(shown)}")
    if total_value > 0:
        summary = f"\n持仓合计市值 {total_value / 1e4:.2f} 万"
        if total_cost > 0:
            summary += f"，登记成本合计 {total_cost / 1e4:.2f} 万"
        lines.append(summary)
    return lines


def build(data_dir: Path, root: Path, as_of: str) -> str:
    """复盘的补充材料。没有当天日线时返回空字符串（复盘照常进行，只是没有这部分）。"""
    days = _read_days(data_dir, as_of)
    if not days or days[-1][0] != as_of:
        return ""
    today = days[-1][1]
    prev = days[-2][1] if len(days) > 1 else None
    names = _read_instruments(data_dir)
    industry = _read_board(data_dir, "ext_hy_ths")
    concept = _read_board(data_dir, "ext_gn_ths")
    holdings = read_holdings(data_dir)
    peers = load_config(root)["peers"]
    held = [h["symbol"] for h in holdings]
    others = [p for s in held for p in peers.get(s, [])]
    # 港股没有面板里的资金、两融、估值数据，不拿去问
    net = remote.collect(root, as_of, [s for s in held if not s.endswith(".HK")], others)

    lines = [f"以下是 {as_of} 收盘后的补充材料。所有数字都来自这里，没有给出的数据请写“未取得”，不要推测或编造。", ""]
    lines += _market_blocks(today, prev, names, industry)
    lines += _margin_block(net.get("market_margin"), net["errors"].get("全市场两融"))
    if holdings:
        lines += _holding_blocks(holdings, days, names, industry, concept, peers, net, as_of,
                                 read_hk_quotes(data_dir))
    else:
        lines += ["", "## 持仓明细", "（“持仓提醒”里还没有登记持仓，持仓复盘一节请写“未登记持仓”。）"]
    if holdings:
        from app.custom.tdx_formula.ql.core import clock

        lines += notices.recap_block(root, held, as_of, clock.now().replace(tzinfo=None))
    if net["errors"]:
        missing = "；".join(f"{k}：{v}" for k, v in net["errors"].items())
        lines += ["", f"数据缺口：{missing}。东方财富有一项没取到之后，同一来源后面的请求就不再发了。"]
    return "\n".join(lines)
