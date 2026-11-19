"""把一份“每日汇总”数据排成网页和飞书卡片。这里全是纯函数：不读文件、不联网。"""

from __future__ import annotations

import base64
import html
import re
from typing import Any

Digest = dict[str, Any]

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.、)]\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return _LINK.sub(r'<a href="\2">\1</a>', out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def md_to_html(text: str) -> str:
    """复盘报告用到的那部分 Markdown：标题、表格、列表、引用、分隔线、加粗。其余原样当段落。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level = min(len(heading.group(1)) + 1, 5)  # 页面标题占了 h1
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            i += 1
        elif line.strip() in {"---", "***", "___"}:
            out.append("<hr>")
            i += 1
        elif "|" in line and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            head = "".join(f"<th>{_inline(c)}</th>" for c in _cells(line))
            i += 2
            body = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                body.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(lines[i])) + "</tr>")
                i += 1
            out.append(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')
        elif _BULLET.match(line) or _NUMBERED.match(line):
            pattern, tag = (_BULLET, "ul") if _BULLET.match(line) else (_NUMBERED, "ol")
            items = []
            while i < len(lines) and pattern.match(lines[i]):
                items.append(f"<li>{_inline(pattern.match(lines[i]).group(1))}</li>")  # type: ignore[union-attr]
                i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
        elif line.lstrip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(_inline(lines[i].lstrip()[1:].strip()))
                i += 1
            out.append(f"<blockquote>{'<br>'.join(quote)}</blockquote>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not _HEADING.match(lines[i]) and not _BULLET.match(lines[i]) \
                    and not _NUMBERED.match(lines[i]) and not lines[i].lstrip().startswith(">") \
                    and not ("|" in lines[i] and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1])):
                para.append(_inline(lines[i].strip()))
                i += 1
            out.append(f"<p>{'<br>'.join(para)}</p>")
    return "\n".join(out)


# ---------------- 数字的写法 ----------------


def pct(value: Any, digits: int = 2) -> str:
    """涨跌幅（小数，0.0123 → +1.23%）。"""
    return "—" if value is None else f"{value * 100:+.{digits}f}%"


def money(value: Any) -> str:
    if value is None:
        return "—"
    sign, v = ("-" if value < 0 else ""), abs(value)
    if v >= 1e12:
        return f"{sign}{v / 1e12:.2f}万亿"
    if v >= 1e8:
        return f"{sign}{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{sign}{v / 1e4:.2f}万"
    return f"{sign}{v:.0f}"


def num(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _tone(value: Any) -> str:
    """A 股习惯：涨红跌绿。"""
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def _span(text: str, value: Any) -> str:
    return f'<span class="{_tone(value)}">{html.escape(text)}</span>'


# ---------------- 网页 ----------------

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--line:#e3e5ea;--text:#1d2129;--muted:#7a8191;--up:#d93026;--down:#0b8f4d;--accent:#2f6bff}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--line:#2c3038;--text:#e6e8ec;--muted:#8d94a3;--up:#ff5b50;--down:#2fbf71;--accent:#6c97ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1080px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0 2px}
h2{font-size:17px;margin:0 0 10px;padding-left:8px;border-left:3px solid var(--accent)}
h3{font-size:15px;margin:16px 0 6px}h4,h5{font-size:14px;margin:12px 0 4px}
.sub{color:var(--muted);font-size:12px;margin-bottom:12px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.tile{border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.tile .k{color:var(--muted);font-size:12px}.tile .v{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .n{font-size:12px;color:var(--muted)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:500}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--muted)}
.ok{color:var(--down)}.bad{color:var(--up)}
.muted{color:var(--muted)}
blockquote{margin:8px 0;padding:6px 12px;border-left:3px solid var(--line);color:var(--muted)}
code{background:var(--bg);padding:1px 4px;border-radius:4px}
hr{border:0;border-top:1px solid var(--line);margin:14px 0}
.recap p{margin:6px 0}.recap td,.recap th{white-space:normal}
footer{color:var(--muted);font-size:12px;text-align:center;padding:8px 0 24px}
"""


def _table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    """headers：(列名, 是否右对齐)。rows 里的格子已经是拼好的 HTML。"""
    head = "".join(f'<th class="{"r" if right else ""}">{html.escape(name)}</th>' for name, right in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="{"r" if headers[j][1] else ""}">{cell}</td>' for j, cell in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _tile(key: str, value: str, note: str = "") -> str:
    extra = f'<div class="n">{note}</div>' if note else ""
    return f'<div class="tile"><div class="k">{html.escape(key)}</div><div class="v">{value}</div>{extra}</div>'


def _market(m: dict[str, Any]) -> str:
    tiles = [
        _tile(str(x.get("name") or x.get("symbol")), _span(num(x.get("last")), x.get("pct")), _span(pct(x.get("pct")), x.get("pct")))
        for x in m.get("indices") or []
    ]
    b = m.get("breadth") or {}
    tiles += [
        _tile("上涨 / 平 / 下跌", f'<span class="up">{b.get("up", 0)}</span> / {b.get("flat", 0)} / <span class="down">{b.get("down", 0)}</span>',
              f'中位涨跌 {_span(pct(b.get("median_pct")), b.get("median_pct"))}'),
        _tile("全市场成交额", html.escape(money(m.get("amount_total")))),
        _tile("涨停 / 跌停", f'<span class="up">{m.get("limit_up", 0)}</span> / <span class="down">{m.get("limit_down", 0)}</span>',
              f'最高 {m.get("max_boards", 0)} 连板'),
        _tile("情绪评分", html.escape(f'{num(m.get("emotion_score"), 0)}'), html.escape(str(m.get("emotion_label") or ""))),
    ]
    lists = []
    for title, key, value_of in (
        ("总市值前十", "cap", lambda r: money(r.get("market_cap"))),
        ("成交额前十", "turnover", lambda r: money(r.get("amount"))),
        ("换手率前十", "active", lambda r: f'{num(r.get("turnover_rate"), 1)}%'),
    ):
        rows = [[html.escape(str(r.get("name") or r.get("symbol"))), html.escape(value_of(r)),
                 _span(pct(r.get("change_pct")), r.get("change_pct"))] for r in m.get(key) or []]
        lists.append(f"<div><h3>{title}</h3>{_table([('股票', False), ('数值', True), ('涨跌', True)], rows)}</div>")
    rows = [[html.escape(str(r.get("name"))), html.escape(money(r.get("amount"))),
             html.escape(f'{num(r.get("share_pct"), 1)}%'), _span(pct(r.get("avg_pct")), r.get("avg_pct"))]
            for r in m.get("industry") or []]
    lists.insert(1, f"<div><h3>行业成交额前十</h3>{_table([('行业', False), ('成交额', True), ('占比', True), ('平均涨跌', True)], rows)}</div>")
    return f'<section><h2>市场快照</h2><div class="grid">{"".join(tiles)}</div><div class="cols">{"".join(lists)}</div></section>'


def _holdings(h: dict[str, Any]) -> str:
    rows = []
    for r in h.get("rows") or []:
        unit = "HK$" if r.get("currency") == "HKD" else ""
        pnl_text = money(r.get("pnl")) + (f'（{pct(r.get("pnl_pct"))}）' if r.get("pnl_pct") is not None else "")
        rows.append([
            (html.escape(str(r["name"])) + " " if r.get("name") else "") + f'<span class="muted">{html.escape(r["symbol"])}</span>',
            html.escape(f'{r.get("qty", 0):g}'),
            html.escape(unit + num(r.get("cost"), 3)) if r.get("cost") else "—",
            html.escape(unit + num(r.get("price"), 3)) if r.get("price") is not None else "—",
            _span(pct(r.get("change_pct")), r.get("change_pct")),
            html.escape(money(r.get("value"))),
            _span(pnl_text, r.get("pnl")) if r.get("pnl") is not None else "—",
        ])
    if not rows:
        return '<section><h2>我的持仓</h2><p class="muted">“持仓提醒”里还没有登记持仓。</p></section>'
    total = (f'<p>合计市值 <strong>{html.escape(money(h.get("total_value")))}</strong> 元，'
             f'浮动盈亏 {_span(money(h.get("total_pnl")), h.get("total_pnl"))} 元。'
             f'<span class="muted">{html.escape(str(h.get("note") or ""))}</span></p>')
    table = _table([("股票", False), ("数量", True), ("成本", True), ("现价", True), ("当日涨跌", True),
                    ("市值(元)", True), ("浮动盈亏(元)", True)], rows)
    return f"<section><h2>我的持仓</h2>{total}{table}</section>"


def _alerts(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return '<section><h2>今天的监控信号</h2><p class="muted">今天没有触发任何信号。</p></section>'
    rows = [[html.escape(str(a.get("time") or "")), html.escape(f'{a.get("name") or ""} {a.get("symbol") or ""}'.strip()),
             html.escape(str(a.get("rule_name") or "")), html.escape(num(a.get("price"))),
             html.escape(str(a.get("message") or ""))] for a in alerts]
    table = _table([("时间", False), ("股票", False), ("规则", False), ("触发价", True), ("说明", False)], rows)
    return f"<section><h2>今天的监控信号（{len(alerts)} 条）</h2>{table}</section>"


def _tasks(tasks: list[dict[str, Any]]) -> str:
    rows = [[html.escape(str(t.get("name"))),
             f'<span class="{"ok" if t.get("ok") else "bad"}">{"正常" if t.get("ok") else "注意"}</span>',
             html.escape(str(t.get("text") or ""))] for t in tasks]
    return f"<section><h2>今天的定时任务</h2>{_table([('任务', False), ('状态', False), ('情况', False)], rows)}</section>"


def page(d: Digest) -> str:
    recap = d.get("recap")
    if recap and recap.get("content"):
        recap_html = (f'<section class="recap"><h2>每日复盘</h2>'
                      f'<div class="sub">生成于 {html.escape(str(recap.get("created_at") or ""))}</div>'
                      f'{md_to_html(str(recap["content"]))}</section>')
    else:
        recap_html = '<section><h2>每日复盘</h2><p class="muted">今天的复盘没有生成，下面只有系统快照。</p></section>'
    day = html.escape(str(d.get("day")))
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{day} 每日汇总</title><style>{CSS}</style></head><body><main>"
        f'<h1>{day} 每日汇总</h1><div class="sub">生成于 {html.escape(str(d.get("generated_at")))} · 行情截至 {day} 收盘</div>'
        f'{_tasks(d.get("tasks") or [])}{_market(d.get("market") or {})}{_holdings(d.get("holdings") or {})}'
        f'{_alerts(d.get("alerts") or [])}{recap_html}'
        "<footer>本页由本机的行情面板生成，只保存在这台电脑上。</footer></main></body></html>"
    )


# ---------------- 飞书卡片 ----------------


def card(d: Digest, link: str, with_file: bool = False, file_error: str | None = None,
         mailed: bool | None = None) -> tuple[str, str]:
    """（副标题，卡片正文）。正文是飞书卡片认的那种 Markdown：不支持表格，所以快照写成几行字。"""
    m, h = d.get("market") or {}, d.get("holdings") or {}
    b = m.get("breadth") or {}
    indices = "、".join(f'{x.get("name")} {pct(x.get("pct"))}' for x in m.get("indices") or [])
    if with_file:
        lines = ["完整网页（复盘全文 + 系统快照）已作为文件发在上一条消息里，手机和电脑都能打开。",
                 f"[在装了面板的电脑上也可以直接点这里打开]({link})"]
    else:
        lines = [f"[打开完整网页（复盘全文 + 系统快照）]({link})", "（链接要在装了面板的这台电脑上打开）"]
        if file_error:
            lines.append(f"网页文件没有发出去：{file_error}")
    if mailed:
        lines.insert(0, "完整网页已发到你的邮箱：正文可以直接看，附件可以用浏览器打开。")
    elif mailed is False:
        lines.append("邮件没有发出去，请到“设置 → 实时监控”检查邮箱设置。")
    lines += [
        "",
        "**市场**",
        indices,
        f'上涨 {b.get("up", 0)} / 下跌 {b.get("down", 0)}，成交 {money(m.get("amount_total"))}，'
        f'涨停 {m.get("limit_up", 0)} / 跌停 {m.get("limit_down", 0)}，情绪 {num(m.get("emotion_score"), 0)}（{m.get("emotion_label") or "—"}）',
    ]
    top = (m.get("industry") or [])[:3]
    if top:
        lines.append("成交最集中的行业：" + "、".join(f'{r.get("name")} {money(r.get("amount"))}（占{num(r.get("share_pct"), 1)}%）' for r in top))
    if h.get("rows"):
        lines += ["", "**持仓**", f'合计市值 {money(h.get("total_value"))}，浮动盈亏 {money(h.get("total_pnl"))}']
        lines += [f'{r.get("name") or r["symbol"]}：{pct(r.get("change_pct"))}' +
                  (f'，盈亏 {money(r.get("pnl"))}' if r.get("pnl") is not None else "") for r in h["rows"]]
    alerts = d.get("alerts") or []
    lines += ["", f"**今天的监控信号**：{len(alerts)} 条" if alerts else "**今天的监控信号**：没有触发"]
    lines += [f'{a.get("time")} {a.get("name") or a.get("symbol")}：{a.get("rule_name") or a.get("message")}' for a in alerts[:8]]
    problems = [t for t in d.get("tasks") or [] if not t.get("ok")]
    if problems:
        lines += ["", "**需要留意**"] + [f'{t.get("name")}：{t.get("text")}' for t in problems]
    recap = d.get("recap")
    lines += ["", "---", ""]
    lines.append(str(recap["content"]) if recap and recap.get("content") else "今天的复盘没有生成。")
    subtitle = f'{d.get("day")} · 情绪 {m.get("emotion_label") or "—"}'
    return subtitle, "\n".join(lines)


# ---------------- 截图版网页、邮件正文 ----------------

SHOTS_CSS = """
.nav{position:sticky;top:0;z-index:2;display:flex;flex-wrap:wrap;gap:6px;padding:8px 0;background:var(--bg)}
.nav a{padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--text);text-decoration:none;font-size:13px}
.shot{padding:10px}
.shot img{display:block;width:100%;height:auto;border-radius:6px}
main.wide{max-width:1280px}
"""


def shots_page(d: Digest, shots: list[dict[str, Any]]) -> str:
    """把各个页面的截图排成一个网页：图片直接嵌在文件里，离开这台电脑也能打开。"""
    day = html.escape(str(d.get("day")))
    nav, body = [], []
    for i, shot in enumerate(shots):
        title = html.escape(str(shot["title"]))
        nav.append(f'<a href="#s{i}">{title}</a>')
        if shot.get("image"):
            data = base64.b64encode(shot["image"]).decode("ascii")
            inner = f'<img alt="{title}" src="data:image/jpeg;base64,{data}">'
        else:
            inner = f'<p class="muted">这一页没有截下来：{html.escape(str(shot.get("error") or "未知原因"))}</p>'
        body.append(f'<section class="shot" id="s{i}"><h2>{title}</h2>{inner}</section>')
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{day} 每日汇总</title><style>{CSS}{SHOTS_CSS}</style></head><body><main class=\"wide\">"
        f'<h1>{day} 每日汇总</h1><div class="sub">生成于 {html.escape(str(d.get("generated_at")))} · 以下是面板各页面当时的样子</div>'
        f'<div class="nav">{"".join(nav)}</div>{"".join(body)}'
        "<footer>本页由本机的行情面板生成。</footer></main></body></html>"
    )


def brief(d: Digest, titles: list[str]) -> str:
    """邮件正文：几行字说清当天的情况，其余看附件。"""
    m, h = d.get("market") or {}, d.get("holdings") or {}
    b = m.get("breadth") or {}
    lines = [
        "、".join(f'{x.get("name")} {pct(x.get("pct"))}' for x in m.get("indices") or []),
        f'上涨 {b.get("up", 0)} / 下跌 {b.get("down", 0)}，成交 {money(m.get("amount_total"))}，'
        f'涨停 {m.get("limit_up", 0)} / 跌停 {m.get("limit_down", 0)}，情绪 {num(m.get("emotion_score"), 0)}（{m.get("emotion_label") or "—"}）',
    ]
    if h.get("rows"):
        lines.append(f'持仓市值 {money(h.get("total_value"))}，浮动盈亏 {money(h.get("total_pnl"))}')
    lines += [f'注意：{t.get("name")}，{t.get("text")}' for t in d.get("tasks") or [] if not t.get("ok")]
    lines.append(f'详情见附件（{"、".join(titles)}）。' if titles else "详情见附件。")
    return "\n".join(line for line in lines if line)


def brief_html(text: str) -> str:
    rows = "".join(f'<p style="margin:0 0 8px">{html.escape(line)}</p>' for line in text.split("\n") if line)
    return f'<div style="font:15px/1.7 -apple-system,PingFang SC,Microsoft YaHei,sans-serif;color:#1d2129">{rows}</div>'
