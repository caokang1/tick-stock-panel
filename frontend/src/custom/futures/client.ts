/** “期货”页用的端点和类型。 */
import { ApiError } from '@/lib/api'

export interface FutureQuote {
  code: string
  name: string
  group: string
  last: number
  open: number | null
  high: number | null
  low: number | null
  /** 昨结算价，涨跌按它算 */
  prev: number | null
  change: number | null
  /** 小数，0.01 = 1% */
  change_pct: number | null
  /** 成交量（手）；外盘没有 */
  volume: number | null
  /** 持仓量（手）；外盘没有 */
  open_interest: number | null
  /** 行情时间，2026-11-18 11:08:36 */
  time: string
  /** 现货指数最新价；只有股指期货有 */
  spot: number | null
  /** 升贴水 = 期货 − 现货 */
  basis: number | null
  basis_pct: number | null
}

export interface Quotes {
  items: FutureQuote[]
  /** 上次成功取到的时间；从没取到过是 null */
  fetched_at: string | null
  /** 这次没取到时的原因（这时 items 是上次的） */
  error: string | null
  /** 两次取数之间至少隔多少秒：和股票的全市场快照是同一个设置 */
  interval: number
  /** 面板的实时行情开关是不是开着；关着时不联网，items 是上一次存下来的 */
  live: boolean
}

export interface Bar {
  date: string
  open: number
  high: number
  low: number
  close: number
}

export interface Kline {
  code: string
  name: string
  bars: Bar[]
  fetched_at: string | null
  error: string | null
  live: boolean
}

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    throw new ApiError(typeof detail === 'string' ? detail : `请求失败（${response.status}）`, response.status)
  }
  return payload as T
}

export const futuresApi = {
  quotes: () => get<Quotes>('/api/custom/futures/quotes'),
  kline: (code: string) => get<Kline>(`/api/custom/futures/kline?code=${encodeURIComponent(code)}`),
}

export const futuresKeys = {
  quotes: ['custom', 'futures', 'quotes'] as const,
  kline: (code: string) => ['custom', 'futures', 'kline', code] as const,
}
