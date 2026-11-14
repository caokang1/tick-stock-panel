/** 通达信公式扩展专用的端点和类型。 */
import { ApiError } from '@/lib/api'

export interface ParamSpec {
  key: string
  label: string
  default: number
  min: number
  max: number
  step: number
  unit: string
}

/** 一个选股条件：粘贴公式拆出来的带着自己的公式（pasted），指标库里的只记编号（block_id） */
export interface Condition {
  pasted?: { name: string; description: string; formula: string; params_spec: ParamSpec[] }
  block_id?: string
  enabled: boolean
  params: Record<string, number>
}

export interface Universe {
  exclude_st: boolean
  exclude_star: boolean
  exclude_chinext: boolean
  exclude_bse: boolean
  exclude_suspended: boolean
  /** 流通市值的下限、上限，单位亿元；null 表示不限 */
  min_float_cap: number | null
  max_float_cap: number | null
  exclude_industries: string[]
}

export interface ParsedCondition {
  name: string
  description: string
  formula: string
  params: ParamSpec[]
}

export interface ParseResult {
  conditions: ParsedCondition[]
  universe: Universe
  warnings: string[]
}

export interface Status {
  configured: boolean
  data_dir: string
  found_by: string
  saved_data_dir: string
  has_data: boolean
  first_date: string | null
  latest_date: string | null
  stock_count: number
  float_shares_ready: boolean
  costs: { commission: number; stamp_tax: number; slippage: number }
}

export interface StatRow {
  entry: 'next_open' | 'same_close'
  hold: number
  count: number
  win_rate: number | null
  avg_return: number | null
  median_return: number | null
  avg_drawdown: number | null
  worst_drawdown: number | null
  skipped_unbuyable: number
}

export interface StatsResult {
  frequency: { avg_per_day: number; empty_day_ratio: number; per_year: number; total: number }
  stats: StatRow[]
  range: { start: string; end: string }
  st_known: boolean
}

export interface SignalItem {
  date: string
  code: string
  name: string
  returns: Record<string, number | null>
}

export interface Pick {
  code: string
  name: string
  close: number
  change: number | null
  float_cap: number | null
  industry: string
}

export interface Saved {
  id: string
  name: string
  formula: string
  conditions: Condition[]
  universe: Universe
  updated_at: string
}

export interface ConditionsBody {
  conditions: Condition[]
  universe: Universe
}

const BASE = '/api/custom/tdx-formula'

async function request<T>(endpoint: string, method = 'GET', body?: unknown, timeoutMs = 120_000): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${BASE}/${endpoint}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = payload?.detail
      // 公式错误的 detail 是一个对象（带行号和函数名），其余是一句话
      const message = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map(v => v.msg).join('；')
        : typeof detail?.message === 'string' ? detail.message
        : `请求失败 (${response.status})`
      throw new ApiError(response.status === 401 ? `未登录 (401)：${message}` : message, response.status)
    }
    return payload as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('等了太久没有结果。全市场数据第一次读取要十几秒，请再试一次。', 0)
    }
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

export const tdxApi = {
  status: () => request<Status>('status'),
  setDataDir: (data_dir: string) => request<Status>('config', 'PUT', { data_dir }),
  parse: (formula: string) => request<ParseResult>('parse', 'POST', { formula }),
  stats: (body: ConditionsBody) => request<StatsResult>('stats', 'POST', body),
  signals: (body: ConditionsBody, limit = 50) =>
    request<{ total: number; items: SignalItem[] }>('signals', 'POST', { ...body, limit }),
  picks: (body: ConditionsBody) => request<{ date: string; items: Pick[] }>('picks', 'POST', body),
  saved: () => request<Saved[]>('saved'),
  save: (body: ConditionsBody & { id?: string; name: string; formula: string }) =>
    request<Saved>('saved', 'POST', body),
  remove: (id: string) => request<{ ok: boolean }>(`saved/${encodeURIComponent(id)}`, 'DELETE'),
}

export const tdxKeys = {
  status: ['custom', 'tdx-formula', 'status'] as const,
  saved: ['custom', 'tdx-formula', 'saved'] as const,
}
