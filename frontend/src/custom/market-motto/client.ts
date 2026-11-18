/** “左上角的话”用的端点和类型。 */
import { ApiError } from '@/lib/api'

export interface Motto {
  text: string
  /** 情绪评分 0-100；算不出来时是 null，这时 text 是默认那句 */
  score: number | null
  /** 面板给这个评分的叫法，如“偏冷” */
  label: string | null
  /** 这句话出自语料库里的哪一档；评分没有落在任何一档时是 null */
  band: string | null
  /** 评分对应的日期 */
  as_of: string | null
}

export interface CorpusBand {
  name: string
  low: number
  high: number
  /** 这一档下面有几句话 */
  count: number
}

export interface Corpus {
  text: string
  /** 还在用自带的示例（自己还没保存过） */
  is_default: boolean
  /** 语料库文件在这台电脑上的位置 */
  path: string
  problems: string[]
  notes: string[]
  max_chars: number
  bands: CorpusBand[]
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    throw new ApiError(typeof detail === 'string' ? detail : `请求失败（${response.status}）`, response.status)
  }
  return payload as T
}

const BASE = '/api/custom/market-motto'

export const mottoApi = {
  current: () => call<Motto>(`${BASE}/current`),
  corpus: () => call<Corpus>(`${BASE}/corpus`),
  save: (text: string) => call<Corpus>(`${BASE}/corpus`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
  }),
  reset: () => call<Corpus>(`${BASE}/corpus`, { method: 'DELETE' }),
}

export const mottoKeys = {
  current: ['custom', 'market-motto', 'current'] as const,
  corpus: ['custom', 'market-motto', 'corpus'] as const,
}

/** 评分取不到（后端没起来、还没有数据）时显示的那句 */
export const FALLBACK = '知行合一，贪是原罪'

/** 鼠标移上去时的说明 */
export function hint(motto: Motto | undefined): string {
  if (!motto || motto.score == null) return '还没有情绪评分，显示的是默认的一句。点击查看每日复盘'
  const day = /^\d{4}-(\d{2})-(\d{2})/.exec(motto.as_of ?? '')
  const date = day ? ` · ${Number(day[1])}月${Number(day[2])}日` : ''
  return `情绪评分 ${Math.round(motto.score)} · ${motto.label ?? ''}${date}。点击查看每日复盘`
}
