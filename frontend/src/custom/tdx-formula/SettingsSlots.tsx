import { lazy, Suspense, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { FrontendSlotContextMap } from '@/extensions/types'
import { FREECN_SOURCE } from './logic'

const DataTab = lazy(() => import('./DataTab').then(m => ({ default: m.DataTab })))

/** 从「通达信公式」页点过来时（地址里带 source=freecn），自动选中「免费公开来源」。自己不显示任何东西。 */
export function FreeCnSourceSelector({ selectSource }: FrontendSlotContextMap['settings.data-sources.toolbar']) {
  const [params] = useSearchParams()
  const wanted = params.get('source') === FREECN_SOURCE
  useEffect(() => {
    if (wanted) selectSource(FREECN_SOURCE)
    // 只在进入页面时选一次，之后用户可以自己换别的来源
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted])
  return null
}

/** 「免费公开来源」的详情里：行情的下载、更新、自动更新时间、导入、各来源限速。 */
export function FreeCnDataPanel({ plugin }: FrontendSlotContextMap['settings.data-sources.detail-actions']) {
  if (plugin.name !== FREECN_SOURCE) return null
  return (
    <div className="mt-5 border-t border-border pt-5">
      <Suspense fallback={<p className="text-xs text-muted">正在加载…</p>}>
        <DataTab />
      </Suspense>
    </div>
  )
}
