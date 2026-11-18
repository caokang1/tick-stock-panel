"""语料库：一段纯文本，按评分分档，每档下面一行一句话。

    # 井号开头的是备注
    冰点（0-30）
    现金也是仓位
    少看盘，多复盘

    偏冷（30-45）
    轻仓试错，错了就走

档位那一行是“名称（下限-上限）”，评分落在 下限 ≤ 评分 < 上限 的用这一档（上限写 100 时含 100）。
同一天同一档固定挑同一句，第二天换一句。
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field

FALLBACK = "知行合一，贪是原罪"
MAX_CHARS = 14  # 侧栏那一行大约放得下这么多字，再长会被截断
MAX_TEXT = 200_000

DEFAULT_TEXT = """\
# 左上角的话。一档一段：第一行是“名称（下限-上限）”，下面一行一句。
# 评分是面板的情绪评分（0 到 100）。井号开头的行是备注，空行随便加。
# 每句最好不超过 14 个字，再长在侧栏里显示不全。

冰点（0-30）
冰点不抄底，等止跌信号
现金也是仓位
少看盘，多复盘

偏冷（30-45）
轻仓试错，错了就走
只做看得懂的那一笔
不急，等市场给信号

震荡（45-55）
高抛低吸，不追不杀
震荡市，利润要落袋
控制仓位，进退都有路

偏暖（55-70）
顺势而为，拿住强的
回踩是机会，别追高
有赚钱效应，更要守纪律

强势（70-100）
人声鼎沸时，想想退路
越涨越要盯紧止盈
知行合一，贪是原罪
"""

_HEADER = re.compile(r"^(.+?)\s*[（(]\s*(\d+(?:\.\d+)?)\s*[-~－—–到至]\s*(\d+(?:\.\d+)?)\s*[)）]\s*$")


@dataclass
class Band:
    name: str
    low: float
    high: float
    lines: list[str] = field(default_factory=list)

    def holds(self, score: float) -> bool:
        return self.low <= score < self.high or (self.high >= 100 and score == self.high)


@dataclass
class Corpus:
    bands: list[Band]
    problems: list[str]  # 挡住保存的问题
    notes: list[str]     # 只是提醒，不挡保存

    def band_of(self, score: float) -> Band | None:
        return next((b for b in self.bands if b.lines and b.holds(score)), None)

    def pick(self, score: float, day: str) -> tuple[str, Band | None]:
        """评分和日期 → 一句话。同一天同一档固定是同一句；没有对应的档时给默认那句。"""
        band = self.band_of(score)
        if band is None:
            return FALLBACK, None
        index = zlib.crc32(f"{day}|{band.name}".encode()) % len(band.lines)
        return band.lines[index], band


def parse(text: str) -> Corpus:
    bands: list[Band] = []
    problems: list[str] = []
    notes: list[str] = []
    current: Band | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = _HEADER.match(line)
        if header:
            low, high = float(header.group(2)), float(header.group(3))
            if low >= high or high > 100:
                problems.append(f"第 {number} 行“{line}”：分数范围要写成 小-大，而且不超过 100。")
                current = None
                continue
            current = Band(header.group(1).strip(), low, high)
            bands.append(current)
        elif current is None:
            problems.append(f"第 {number} 行“{line[:20]}”前面还没有档位。档位写成“冰点（0-30）”这样。")
        else:
            current.lines.append(line)
            if len(line) > MAX_CHARS:
                notes.append(f"“{line[:MAX_CHARS]}…”有 {len(line)} 个字，侧栏里只显示得下约 {MAX_CHARS} 个。")

    for band in bands:
        if not band.lines:
            notes.append(f"“{band.name}”这一档下面还没有话，评分落在这里时会显示默认那句。")
    ordered = sorted(bands, key=lambda b: b.low)
    for before, after in zip(ordered, ordered[1:]):
        if after.low < before.high:
            problems.append(f"“{before.name}”和“{after.name}”的分数范围重叠了。")
        elif after.low > before.high:
            notes.append(f"{before.high:g} 到 {after.low:g} 分之间没有档位，评分落在这里时会显示默认那句。")
    if not any(b.lines for b in bands) and not problems:
        problems.append("至少要有一个档位，并且下面有一句话。")
    return Corpus(bands, problems, notes)
