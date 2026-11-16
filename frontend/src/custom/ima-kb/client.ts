/** 腾讯 ima 知识库扩展专用的端点和类型。 */
import { ApiError } from '@/lib/api'

const BASE = '/api/custom/ima-kb'

export interface KnowledgeBase {
  id: string
  name: string
  /** 个人知识库 / 共享知识库；已经选中的那份记录里没有这一项 */
  kind?: string
  /** 里面有多少篇；不知道时是 null */
  count?: number | null
}

export interface ImaConfig {
  /** 凭证有没有填好 */
  configured: boolean
  /** 打了码的 Client ID；API Key 不会从后端返回 */
  client_id_masked: string
  selected: KnowledgeBase[]
  /** AI 分析个股时要不要参考知识库 */
  use_in_ai: boolean
  max_selected: number
}

export interface SearchItem {
  base_id: string
  base_name: string
  media_id: string
  title: string
  /** 命中的片段；只命中标题时是空的 */
  snippet: string
  /** 从标题里认出来的日期（2026-05-18），认不出为空。结果已按它从新到旧排好 */
  date?: string
  /** 一次按几个词搜时，这一条是按哪个词搜到的 */
  matched?: string
}

export interface SearchResult {
  query: string
  items: SearchItem[]
  /** 个别知识库没搜成功时的原因 */
  errors: string[]
}

async function request<T>(endpoint: string, method = 'GET', body?: unknown): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 90_000)
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
      throw new ApiError(typeof detail === 'string' ? detail : `请求失败（${response.status}）`, response.status)
    }
    return payload as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw new Error('等了太久没有结果，请再试一次')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

export const imaApi = {
  config: () => request<ImaConfig>('config'),
  setCredentials: (client_id: string, api_key: string) =>
    request<ImaConfig>('credentials', 'PUT', { client_id, api_key }),
  clearCredentials: () => request<ImaConfig>('credentials', 'DELETE'),
  knowledgeBases: (query: string) =>
    request<{ items: KnowledgeBase[] }>(`knowledge-bases?query=${encodeURIComponent(query)}`),
  setSelected: (selected: KnowledgeBase[]) => request<ImaConfig>('selected', 'PUT', { selected }),
  setUseInAi: (use_in_ai: boolean) => request<ImaConfig>('options', 'PUT', { use_in_ai }),
  search: (query: string) => request<SearchResult>('search', 'POST', { query }),
  stock: (symbol: string) => request<SearchResult>(`stock/${encodeURIComponent(symbol)}`),
}

const ROOT = ['custom', 'ima-kb'] as const
export const imaKeys = {
  config: [...ROOT, 'config'] as const,
  bases: (query: string) => [...ROOT, 'bases', query] as const,
  stock: (symbol: string) => [...ROOT, 'stock', symbol] as const,
}
