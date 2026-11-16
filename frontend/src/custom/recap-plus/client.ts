/** “每日复盘”页用的端点和类型。走势数据来自 recap-plus 扩展，报告归档用的是面板复盘自己的接口。 */
import { ApiError } from '@/lib/api'

export interface TrendDay {
  date: string
  /** 沪深两市个股成交额合计（元） */
  amount: number | null
  amount_change?: number
  limit_up: number
  limit_down: number
  up: number | null
  down: number | null
  flat: number | null
  sh_close: number | null
  /** 上证指数当日涨跌幅（小数） */
  sh_change: number | null
}

export interface MarginDay {
  date: string
  rzye: number | null
  rzmre: number | null
  rzjme: number | null
  rqye: number | null
  rzrqye: number | null
}

export interface Dashboard {
  as_of: string | null
  days: TrendDay[]
  /** 全市场两融，旧的在前；交易所次日披露，所以最新一天通常比行情晚一个交易日 */
  margin: MarginDay[]
  margin_error: string | null
}

export interface RecapReport {
  id: string
  as_of: string
  content: string
  summary: string
  emotion_score?: string | number | null
  emotion_label?: string
  created_at?: string
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

export const recapApi = {
  dashboard: () => get<Dashboard>('/api/custom/recap-plus/dashboard'),
  reports: () => get<{ reports: RecapReport[] }>('/api/market-recap/reports').then(r => r.reports ?? []),
}

export const recapKeys = {
  dashboard: ['custom', 'recap-plus', 'dashboard'] as const,
  reports: ['custom', 'recap-plus', 'reports'] as const,
}
