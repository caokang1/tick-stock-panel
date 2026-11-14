"""把每日提醒写成一段文字。发送由扩展交给 TSP 自己的推送通道（飞书、企业微信），这里不发请求。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def format_message(day: str, items: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
    """items：[(策略名, 提醒)]。格式见产品文档 §8 的示例。"""
    month, date = int(day[5:7]), int(day[8:10])
    lines = [f"【通达信公式 {month}月{date}日 收盘提醒】"]
    for name, r in items:
        timing = r.get("timing") or {}
        state = ""
        if timing.get("enabled"):
            state = "（大盘：可以出手）" if timing.get("ok") else "（大盘：建议空仓）"
        lines += ["", f"{name}{state}"]
        buys = r.get("buys") or []
        tracked = bool(r.get("real"))  # 这份提醒是按实际持仓生成的
        real = [b for b in buys if not b.get("reference_only")]
        for b in real:
            hint = f"建议 {b.get('shares', 0)} 股" if tracked else f"排名分 {b.get('score', 0)}"
            lines.append(f"  买入：{b.get('name') or b['code']}（{b['code']}）  {hint}")
            if b.get("reason"):
                lines.append(f"        原因：{b['reason']}")
        if not real:
            lines.append("  今日无买入")
        for b in buys:
            if b.get("reference_only"):
                lines.append(f"  仅供参考：{b.get('name') or b['code']}（{b['code']}）  排名分 {b.get('score', 0)}")
        for s in r.get("sells") or []:
            lines.append(f"  卖出：{s.get('name') or s['code']}（{s['code']}）  原因：{s['reason']}"
                         f"  {'实际' if tracked else '模拟'}盈亏 {_pct(s['gain'])}")
        for note in r.get("notes") or []:
            lines.append(f"  注意：{note}")
        account = r.get("account") or {}
        if account:
            lines.append(f"  {'实际持仓' if tracked else '模拟账户'}：持仓 {len(r.get('positions') or [])} 只，"
                         f"累计 {_pct(account['total_return'])}")
        if r.get("older_pending"):
            lines.append(f"  还有 {r['older_pending']} 条以前的提醒没有确认，请到网页上处理。")
    lines += ["", "提醒是“策略该怎么做”，不是“你的账户该怎么做”。不构成投资建议。"]
    return "\n".join(lines)
