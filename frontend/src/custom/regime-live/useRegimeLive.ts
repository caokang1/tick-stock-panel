/**
 * 市场环境的盘中刷新。
 *
 * 行情每更新一轮，SSE 会让 ['regime-live'] 这条查询重新请求（受“设置 → 实时监控 → 页面实时刷新”
 * 里的“市场环境”开关控制，页面没开着时不请求）。请求让后端把今天这一行重算一遍；
 * 算出了新结果，就让页面上的各块数据重新读取。
 */
import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

export const REGIME_LIVE_KEY = ['regime-live'] as const

export interface RegimeLiveResult { updated: boolean; days?: number; reason?: string }

export async function refreshRegimeLive(): Promise<RegimeLiveResult> {
  try {
    const response = await fetch('/api/custom/regime-live/refresh', { method: 'POST' })
    if (!response.ok) return { updated: false, reason: 'error' }
    return await response.json() as RegimeLiveResult
  } catch {
    return { updated: false, reason: 'error' } // 刷新不成不影响页面，照常显示已有的数据
  }
}

/** 是不是市场环境页面上要跟着重读的数据（不含触发刷新的那条查询自己）。 */
export function isRegimeDataKey(key: unknown): boolean {
  const head = String(key)
  return head.startsWith('regime-') && head !== REGIME_LIVE_KEY[0]
}

export function useRegimeLive(): void {
  const client = useQueryClient()
  const live = useQuery({
    queryKey: REGIME_LIVE_KEY, queryFn: refreshRegimeLive,
    staleTime: Infinity, retry: false, refetchOnWindowFocus: false,
  })
  const { data, dataUpdatedAt } = live
  useEffect(() => {
    if (data?.updated) void client.invalidateQueries({ predicate: query => isRegimeDataKey(query.queryKey[0]) })
  }, [client, data, dataUpdatedAt])
}
