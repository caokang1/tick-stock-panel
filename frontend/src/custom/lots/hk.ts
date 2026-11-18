/** 持仓里的港股：报价端点、类型和几个纯计算。港股代码写成 00981.HK。 */
import { ApiError } from '@/lib/api'

export interface HkQuote {
  symbol: string
  name: string
  /** 最新价（港元） */
  last: number
  prev: number | null
  /** 小数，0.01 = 1% */
  change_pct: number | null
  /** 报价时间，2026-11-18 15:38:19 */
  time: string
}

export interface HkQuotes {
  items: Record<string, HkQuote>
  /** 1 港元 = 多少人民币；没取到过是 null */
  rate: number | null
  fetched_at: string | null
  error: string | null
  /** 面板的实时行情开关是不是开着 */
  live: boolean
  /** 开关开着时两次取数之间至少隔多少秒 */
  interval: number
}

export const isHk = (symbol: string) => /^\d{5}\.HK$/i.test(symbol.trim())

/** 搜索框里敲的像港股代码（981、00981、981.hk）→ 00981.HK；不像就是 null */
export function hkSymbolFrom(input: string): string | null {
  const m = input.trim().match(/^(\d{1,5})\.?hk$/i) ?? input.trim().match(/^(\d{5})$/)
  return m ? `${m[1].padStart(5, '0')}.HK` : null
}

/** 一笔持仓折成人民币时乘的数：A 股是 1，港股是汇率（没有汇率时算不出来） */
export function fxOf(symbol: string, rate: number | null | undefined): number | null {
  if (!isHk(symbol)) return 1
  return rate != null && rate > 0 ? rate : null
}

export async function fetchHkQuotes(symbols: string[]): Promise<HkQuotes> {
  const response = await fetch(`/api/custom/hk-quotes/quotes?symbols=${encodeURIComponent(symbols.join(','))}`)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    throw new ApiError(typeof detail === 'string' ? detail : `请求失败（${response.status}）`, response.status)
  }
  return payload as HkQuotes
}

export const hkKeys = {
  quotes: (symbols: string[]) => ['custom', 'lots', 'hk-quotes', symbols.join(',')] as const,
}
