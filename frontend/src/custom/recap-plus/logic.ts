/** “每日复盘”页的纯计算：报告分节、按日期归档、数字的写法、图表坐标。 */
import type { RecapReport } from './client'

export const yi = (v: number | null | undefined, digits = 0): string =>
  v == null || !Number.isFinite(v) ? '—' : `${(v / 1e8).toFixed(digits)}亿`

export const signedYi = (v: number | null | undefined, digits = 0): string =>
  v == null || !Number.isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${(v / 1e8).toFixed(digits)}亿`

export const pct = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(v) ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`

/** 2026-11-16 → 11月16日 */
export const shortDate = (day: string): string => {
  const m = /^\d{4}-(\d{2})-(\d{2})/.exec(day)
  return m ? `${Number(m[1])}月${Number(m[2])}日` : day
}

/** 2026-11-16 → 11-16 */
export const axisDate = (day: string): string => day.slice(5)

/**
 * 每个交易日只留最新生成的那一份报告，新的日期在前。
 * 同一天可能生成过好几次（手动、定时各一次），归档里只看最后那次。
 */
export function latestPerDay(reports: RecapReport[]): RecapReport[] {
  const byDay = new Map<string, RecapReport>()
  for (const r of reports) {
    const kept = byDay.get(r.as_of)
    if (!kept || (r.created_at ?? '') > (kept.created_at ?? '')) byDay.set(r.as_of, r)
  }
  return [...byDay.values()].sort((a, b) => (a.as_of < b.as_of ? 1 : -1))
}

/**
 * 从报告里取出某一节的正文（不含标题）。标题按“### 一、市场全景”这样的写法认，
 * 只看开头的序号（一、二……），所以节名略有出入也能找到。找不到返回空字符串。
 */
export function section(content: string, ordinal: string): string {
  const lines = content.replace(/\r\n/g, '\n').split('\n')
  const isHeading = (line: string) => /^#{1,4}\s*[一二三四五六七八九十]+[、.．]/.test(line)
  const start = lines.findIndex(line => isHeading(line) && new RegExp(`^#{1,4}\\s*${ordinal}[、.．]`).test(line))
  if (start < 0) return ''
  let end = lines.length
  for (let i = start + 1; i < lines.length; i++) {
    if (isHeading(lines[i])) { end = i; break }
  }
  return lines.slice(start + 1, end).join('\n').trim()
}

export interface ChartPoint { x: number; y: number; value: number }

/**
 * 把一组数画进宽 w、高 h 的框里（四周留白 pad）。空值跳过。
 * zero=true 时纵轴从 0 起（柱状图用），否则上下各留一成空隙。
 */
export function scale(values: (number | null)[], w: number, h: number, pad: { l: number; r: number; t: number; b: number },
                      zero = false): { points: (ChartPoint | null)[]; min: number; max: number } {
  const nums = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (!nums.length) return { points: values.map(() => null), min: 0, max: 1 }
  let min = Math.min(...nums), max = Math.max(...nums)
  if (zero) min = Math.min(0, min)
  else {
    const gap = (max - min || Math.abs(max) || 1) * 0.1
    const floor = min >= 0 ? 0 : -Infinity  // 家数、点位这类不会为负的数，纵轴不画到 0 以下
    min = Math.max(floor, min - gap); max += gap
  }
  if (max === min) max = min + 1
  const innerW = w - pad.l - pad.r, innerH = h - pad.t - pad.b
  const step = values.length > 1 ? innerW / (values.length - 1) : 0
  const points = values.map((v, i) => (v == null || !Number.isFinite(v) ? null : {
    x: pad.l + (values.length > 1 ? i * step : innerW / 2),
    y: pad.t + innerH - ((v - min) / (max - min)) * innerH,
    value: v,
  }))
  return { points, min, max }
}
