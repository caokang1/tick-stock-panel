/**
 * 看板上的“持仓公告”小组件：持仓股票近三天的上市公司正式公告，点标题打开公告原文。
 *
 * 持仓读自“持仓提醒”；公告来自东方财富的公告列表（后端半小时内不重复请求）。
 * 上一个交易日收盘之后挂出来的标“新”。
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileText, RefreshCw } from 'lucide-react'
import { recapApi, recapKeys } from './client'
import { isNew, previousClose } from './logic'

const DAYS = 3

export function HoldingNoticesWidget() {
  const client = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  // 看板开着的时候每半小时自己再看一次
  const query = useQuery({ queryKey: recapKeys.notices, queryFn: () => recapApi.notices(false, DAYS), staleTime: 5 * 60_000, refetchInterval: 30 * 60_000 })
  const data = query.data
  const items = data?.items ?? []
  const since = previousClose(new Date())
  const fresh = items.filter(n => isNew(n.published_at, since)).length

  const refresh = async () => {
    setRefreshing(true)
    try {
      client.setQueryData(recapKeys.notices, await recapApi.notices(true, DAYS))
    } catch {
      await query.refetch()
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <div className="mb-1.5 flex shrink-0 items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <FileText className="h-3.5 w-3.5 shrink-0 text-accent" />
          <h2 className="text-xs font-semibold text-foreground">持仓公告</h2>
          <span className="truncate font-mono text-[10px] text-muted">近 {DAYS} 天{fresh > 0 ? ` · 新 ${fresh}` : ''}</span>
        </div>
        <button
          onClick={refresh} disabled={refreshing} aria-label="刷新持仓公告"
          title={data?.fetched_at ? `更新于 ${data.fetched_at.replace('T', ' ').slice(5, 16)}，点击刷新` : '刷新'}
          className="inline-flex h-5 w-5 cursor-pointer items-center justify-center rounded text-muted transition-colors hover:bg-accent/10 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RefreshCw className={`h-3 w-3 ${refreshing ? 'animate-spin' : ''}`} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {data?.error && (
          <p role="alert" className="mb-1 rounded border border-warning/30 bg-warning/10 px-2 py-1 text-[10px] text-warning">
            这次没有取到最新公告{items.length > 0 ? '，下面是上次取到的' : ''}
          </p>
        )}
        {query.isLoading ? <p className="py-4 text-center text-[11px] text-muted">正在读取……</p>
          : query.isError ? <p role="alert" className="py-4 text-center text-[11px] text-danger">{(query.error as Error).message}</p>
            : items.length === 0 ? <p className="py-4 text-center text-[11px] text-muted">近 {DAYS} 天持仓没有新公告</p>
              : (
                <ul className="space-y-1">
                  {items.map(n => (
                    <li key={`${n.code}:${n.id}`}>
                      <a
                        href={n.url} target="_blank" rel="noopener noreferrer" title={`${n.title}${n.kind ? `（${n.kind}）` : ''}`}
                        className="block rounded-md border border-transparent bg-elevated/40 px-1.5 py-1 transition-colors hover:border-accent/40 hover:bg-elevated/70"
                      >
                        <div className="flex items-center gap-1.5 text-[10px]">
                          <span className="font-medium text-foreground">{n.name}</span>
                          {isNew(n.published_at, since) && <span className="rounded bg-bull/15 px-1 font-semibold text-bull">新</span>}
                          <span className="ml-auto shrink-0 font-mono tabular-nums text-muted">{n.published_at.slice(5)}</span>
                        </div>
                        <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-secondary">{n.title}</div>
                      </a>
                    </li>
                  ))}
                </ul>
              )}
      </div>
    </section>
  )
}
