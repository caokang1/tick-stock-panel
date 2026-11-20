/** “复盘信号”用的端点和类型。 */
import { ApiError } from '@/lib/api'

export interface RecapSignal {
  symbol: string
  name: string
  direction: 'below' | 'above'
  price: number
  action: string
  rule_id: string
}

export interface RecapSignalStatus {
  enabled: boolean
  /** 这批信号出自哪一天的复盘；还没生成过是 null */
  recap_day: string | null
  generated_at: string | null
  message: string | null
  signals: RecapSignal[]
  /** 复盘里提到、但核对没通过的价位和原因 */
  dropped: string[]
  /** 监控不了的港股持仓 */
  skipped_hk: string[]
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    throw new ApiError(typeof detail === 'string' ? detail : `请求没有成功（${response.status}）`, response.status)
  }
  return payload as T
}

const BASE = '/api/custom/recap-signals'

export const recapSignalKeys = { status: ['recap-signals', 'status'] as const }

export const recapSignalApi = {
  status: () => call<RecapSignalStatus>(`${BASE}/status`),
  run: () => call<RecapSignalStatus>(`${BASE}/run`, { method: 'POST' }),
  setEnabled: (enabled: boolean) => call<RecapSignalStatus>(`${BASE}/options`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }),
  }),
}

/** 一条信号给人看的说法，例如“跌破 26.35”。 */
export function signalText(signal: Pick<RecapSignal, 'direction' | 'price'>): string {
  return `${signal.direction === 'below' ? '跌破' : '突破'} ${signal.price}`
}
