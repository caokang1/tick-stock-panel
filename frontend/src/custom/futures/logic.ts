/** “期货”页的纯计算：分组、数字的写法、K 线图的坐标。 */
import type { Bar, FutureQuote } from './client'

/** 按后端给的顺序分组，组的先后就是品种第一次出现的先后。 */
export function grouped(items: FutureQuote[]): { group: string; rows: FutureQuote[] }[] {
  const out: { group: string; rows: FutureQuote[] }[] = []
  for (const item of items) {
    const found = out.find(g => g.group === item.group)
    if (found) found.rows.push(item)
    else out.push({ group: item.group, rows: [item] })
  }
  return out
}

/** 一个价位该留几位小数：上万不留，上千一位，上百两位，再小三位。涨跌额也按最新价的位数来写。 */
export const digitsOf = (v: number): number =>
  Math.abs(v) >= 10000 ? 0 : Math.abs(v) >= 1000 ? 1 : Math.abs(v) >= 100 ? 2 : 3

/** 价格小数位跟着价位走：国债、外盘银铜这类小数字多留几位。 */
export function price(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const digits = digitsOf(v)
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export const signed = (v: number | null | undefined, digits = 2): string =>
  v == null || !Number.isFinite(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}`

export const pct = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(v) ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(2)}%`

/** 手数：过万写成“x.x万” */
export const lots = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(v) ? '—' : v >= 10000 ? `${(v / 10000).toFixed(1)}万` : String(Math.round(v))

export interface Candle {
  x: number
  width: number
  yOpen: number
  yClose: number
  yHigh: number
  yLow: number
  up: boolean
}

/** 把日线换成画布上的蜡烛。pad 是四周留白；全是同一个价时把区间撑开一点，免得除以零。 */
export function candles(bars: Bar[], w: number, h: number, pad: { l: number; r: number; t: number; b: number }) {
  if (!bars.length) return { items: [] as Candle[], min: 0, max: 0, slot: 0 }
  let min = Math.min(...bars.map(b => b.low))
  let max = Math.max(...bars.map(b => b.high))
  if (max === min) { max += 1; min -= 1 }
  const slot = (w - pad.l - pad.r) / bars.length
  const y = (v: number) => pad.t + (h - pad.t - pad.b) * (1 - (v - min) / (max - min))
  const width = Math.max(1, Math.min(9, slot * 0.7))
  const items = bars.map((b, i) => ({
    x: pad.l + slot * (i + 0.5), width,
    yOpen: y(b.open), yClose: y(b.close), yHigh: y(b.high), yLow: y(b.low),
    up: b.close >= b.open,
  }))
  return { items, min, max, slot }
}
