import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import type { FrontendSlotContextMap } from '@/extensions/types'
import { imaApi, imaKeys } from './client'
import { Results } from './Results'

/** 个股详情里的“知识库”标签：按这只股票的名称在选中的知识库里搜索 */
export function StockKnowledgeTab({ symbol }: FrontendSlotContextMap['stock-preview.tabs']) {
  const config = useQuery({ queryKey: imaKeys.config, queryFn: imaApi.config, staleTime: 60_000 })
  const ready = Boolean(config.data?.configured && config.data.selected.length > 0)
  const found = useQuery({
    queryKey: imaKeys.stock(symbol), queryFn: () => imaApi.stock(symbol),
    enabled: ready, staleTime: 5 * 60_000, retry: false,
  })

  return (
    <div className="h-full overflow-y-auto p-4">
      {config.isLoading ? <p className="text-xs text-muted">正在加载…</p>
        : !ready ? (
          <p className="text-xs text-secondary">
            还没有接好 ima 知识库。到
            <Link className="mx-1 text-accent hover:underline" to="/ima-kb">“知识库”页</Link>
            填凭证、勾选要用的知识库，这里就会显示和这只股票相关的内容。
          </p>
        ) : found.isLoading ? <p className="text-xs text-muted">正在知识库里搜索…</p>
          : found.error ? (
            <div className="space-y-2">
              <p role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                没有搜成功：{found.error instanceof Error ? found.error.message : '出错了'}
              </p>
              <button className="cursor-pointer text-xs text-accent hover:underline" onClick={() => void found.refetch()}>再试一次</button>
            </div>
          ) : found.data && <Results result={found.data} bases={config.data?.selected.length ?? 0} />}
    </div>
  )
}
