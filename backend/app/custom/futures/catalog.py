"""页面上展示哪些品种。code 是新浪行情接口里的写法。

kind 决定一行行情怎么解析：
- fin  中金所的股指 / 国债期货（主力连续）
- com  商品期货（主力连续）
- hf   外盘期货和伦敦现货
spot 只有股指期货有：对应现货指数在新浪接口里的代码，用来算升贴水。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    group: str
    kind: str
    spot: str | None = None

    @property
    def query(self) -> str:
        return ("hf_" if self.kind == "hf" else "nf_") + self.code


ITEMS: tuple[Item, ...] = (
    Item("IF0", "沪深300股指", "股指期货", "fin", "sh000300"),
    Item("IH0", "上证50股指", "股指期货", "fin", "sh000016"),
    Item("IC0", "中证500股指", "股指期货", "fin", "sh000905"),
    Item("IM0", "中证1000股指", "股指期货", "fin", "sh000852"),
    Item("AU0", "沪金", "贵金属和有色", "com"),
    Item("AG0", "沪银", "贵金属和有色", "com"),
    Item("CU0", "沪铜", "贵金属和有色", "com"),
    Item("SC0", "原油", "能源、黑色和国债", "com"),
    Item("RB0", "螺纹钢", "能源、黑色和国债", "com"),
    Item("T0", "10年期国债", "能源、黑色和国债", "fin"),
    Item("XAU", "伦敦金", "外盘", "hf"),
    Item("XAG", "伦敦银", "外盘", "hf"),
    Item("GC", "纽约黄金", "外盘", "hf"),
    Item("SI", "纽约白银", "外盘", "hf"),
    Item("HG", "纽约铜", "外盘", "hf"),
    Item("CL", "纽约原油", "外盘", "hf"),
)

BY_CODE = {item.code: item for item in ITEMS}
