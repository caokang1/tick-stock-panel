"""从复盘正文里取出“持仓 + 价位 + 该怎么办”，核对后变成监控规则。全是纯函数，不读文件、不联网。

AI 只负责把复盘里已经写明的价位摘出来；摘回来的每一条都要过这里的核对：
是不是持仓、价位是不是真的写在那只股票的复盘里、下一个交易日到不到得了。过不了的直接丢掉。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

RULE_PREFIX = "rs_"
MAX_PER_SYMBOL = 4
MAX_ACTION_CHARS = 60
DIRECTIONS = {"below": ("跌破", "<", "d", "warn"), "above": ("突破", ">", "u", "info")}

SYSTEM_PROMPT = (
    "你是一个严谨的信息摘录工具。用户会给你一份股票持仓复盘和持仓清单，"
    "你只把复盘里明确写出的、需要在下一个交易日盯住的价位摘出来，不做任何自己的判断或计算。"
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_HEADING = re.compile(r"^#{2,4}\s+.*$", re.M)


def holding_section(content: str) -> str:
    """复盘正文里“持仓复盘”那一节（到下一个同级标题为止）。找不到就返回空。"""
    start = re.search(r"^###\s+.*持仓.*$", content, re.M)
    if not start:
        return ""
    rest = content[start.end():]
    end = re.search(r"^###\s+", rest, re.M)
    return rest[: end.start()] if end else rest


def blocks_of(section: str, holdings: list[dict[str, Any]]) -> dict[str, str]:
    """把持仓复盘按小标题切开，对上每只持仓：{代码: 这只股票的那段文字}。"""
    marks = [m for m in _HEADING.finditer(section) if m.group().startswith("####")]
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        body = section[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(section)]
        title = m.group()
        for h in holdings:
            code, name = h["symbol"].split(".")[0], str(h.get("name") or "")
            if h["symbol"] not in out and (code in title or (name and name in title)):
                out[h["symbol"]] = body
                break
    return out


def build_prompt(section: str, holdings: list[dict[str, Any]]) -> str:
    listing = "\n".join(f"- {h['symbol']} {h.get('name') or ''}".rstrip() for h in holdings)
    return (
        "下面是今天收盘后的持仓复盘。请把其中明确写出的“下一个交易日要盯的价位”摘出来。\n\n"
        "要求：\n"
        "1. 只摘复盘原文里写了具体数字的价位（防守线、止损位、突破位、压力位等），数字必须和原文一模一样，不要自己算。\n"
        "2. direction 只有两种：below（跌破这个价要提醒）、above（涨过这个价要提醒）。\n"
        f"3. action 用一句话照复盘原意写出到了这个价该怎么办，不超过 {MAX_ACTION_CHARS} 个字；"
        "如果原文说的是“收盘跌破”，要保留“收盘”两个字。\n"
        "4. 没有给出具体价位的持仓直接跳过；成本价、估值、成交额这些不是要盯的价位，不要摘。\n"
        "5. symbol 只能用下面清单里的代码。\n"
        "6. 只输出一个 JSON 数组，不要任何解释。没有可摘的就输出 []。\n\n"
        '格式：[{"symbol": "000001.SZ", "direction": "below", "price": 9.5, "action": "收盘跌破再减半"}]\n\n'
        f"可以用的持仓代码：\n{listing}\n\n"
        f"持仓复盘原文：\n{section.strip()}\n"
    )


def parse_reply(text: str) -> list[dict[str, Any]]:
    """从 AI 的回复里取出那个 JSON 数组。取不出来就抛 ValueError。"""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("AI 的回复里没有找到信号列表")
    try:
        data = json.loads(text[start: end + 1])
    except ValueError as exc:
        raise ValueError("AI 的回复不是能读懂的格式") from exc
    if not isinstance(data, list):
        raise ValueError("AI 的回复不是列表")
    return [x for x in data if isinstance(x, dict)]


def limit_pct(symbol: str, name: str | None, asset_type: str) -> float:
    """下一个交易日最多能涨跌多少（小数）。拿不准的往宽里算，免得把到得了的价位丢掉。"""
    code = symbol.split(".")[0]
    if asset_type != "stock":
        return 0.20
    if symbol.endswith(".BJ") or code.startswith(("8", "4", "92")):
        return 0.30
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if name and "ST" in name.upper():
        return 0.05
    return 0.10


def _price(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def clean(items: list[dict[str, Any]], holdings: list[dict[str, Any]], blocks: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """核对 AI 摘回来的信号。holdings：[{symbol, name, close, asset_type}]（只有能监控的 A 股和 ETF）。

    返回 (留下的信号, 被丢掉的原因)。留下的每条：{symbol, name, direction, price, action, asset_type}。
    """
    by_symbol = {h["symbol"]: h for h in holdings}
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen: set[tuple[str, str, float]] = set()
    count: dict[str, int] = {}
    for item in items:
        symbol = str(item.get("symbol") or "").strip().upper()
        direction = str(item.get("direction") or "").strip().lower()
        price = _price(item.get("price"))
        h = by_symbol.get(symbol)
        label = f"{(h or {}).get('name') or symbol} {item.get('price')}"
        if h is None:
            dropped.append(f"{label}：不是能监控的持仓")
            continue
        if direction not in DIRECTIONS or price is None:
            dropped.append(f"{label}：方向或价位看不懂")
            continue
        written = {float(n) for n in _NUMBER.findall(blocks.get(symbol, ""))}
        if not any(abs(price - w) < 1e-9 for w in written):
            dropped.append(f"{label}：复盘原文里没有这个价位")
            continue
        close = _price(h.get("close"))
        if close is None:
            dropped.append(f"{label}：没有收盘价，无法核对")
            continue
        if (direction == "below" and close < price) or (direction == "above" and close > price):
            dropped.append(f"{label}：收盘时已经{DIRECTIONS[direction][0]}了")
            continue
        band = limit_pct(symbol, h.get("name"), h.get("asset_type") or "stock") + 0.005
        if not close * (1 - band) <= price <= close * (1 + band):
            dropped.append(f"{label}：离收盘价太远，下一个交易日到不了")
            continue
        key = (symbol, direction, round(price, 4))
        if key in seen or count.get(symbol, 0) >= MAX_PER_SYMBOL:
            continue
        seen.add(key)
        count[symbol] = count.get(symbol, 0) + 1
        action = re.sub(r"\s+", " ", str(item.get("action") or "")).strip()[:MAX_ACTION_CHARS]
        kept.append({"symbol": symbol, "name": h.get("name") or symbol, "direction": direction, "price": price,
                     "action": action, "asset_type": h.get("asset_type") or "stock"})
    return kept, dropped


def to_rules(signals: list[dict[str, Any]], recap_day: str, channels: list[str]) -> list[dict[str, Any]]:
    """信号 → 监控中心的价格规则。规则 id 以 rs_ 开头，本扩展只增删这个前缀的规则。"""
    rules: list[dict[str, Any]] = []
    index: dict[tuple[str, str], int] = {}
    for s in signals:
        word, op, tag, severity = DIRECTIONS[s["direction"]]
        code = re.sub(r"[^a-z0-9]", "", s["symbol"].split(".")[0].lower())
        n = index[(code, tag)] = index.get((code, tag), 0) + 1
        price_text = f"{s['price']:g}"
        action = s["action"] or f"复盘里把 {price_text} 定为关键价位"
        rules.append({
            "id": f"{RULE_PREFIX}{code}_{tag}{n}",
            "name": f"复盘信号 · {s['name']} {word} {price_text}",
            "type": "price",
            "asset_type": s["asset_type"],
            "scope": "symbols",
            "symbols": [s["symbol"]],
            "conditions": [{"field": "close", "op": op, "value": s["price"]}],
            "logic": "and",
            "cooldown_seconds": 86400,
            "severity": severity,
            "message": f"{recap_day[5:]} 复盘：{action}（这条信号只管复盘后的下一个交易日）",
            "webhook_channels": list(channels),
            "enabled": True,
        })
    return rules
