import { lazy } from 'react'
import { LibraryBig } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { StockKnowledgeTab } from './StockTab'

export const ImaKnowledge = lazy(() => import('./Page').then(m => ({ default: m.ImaKnowledge })))

const extension: FrontendExtension = {
  id: 'community.ima-kb',
  apiVersion: 1,
  routes: [
    { id: 'ima-kb', path: '/ima-kb', component: ImaKnowledge },
  ],
  navigation: [
    { id: 'ima-kb', routeId: 'ima-kb', label: '知识库', icon: LibraryBig },
  ],
  slots: [
    { name: 'stock-preview.tabs', id: 'ima-kb.stock', label: '知识库', component: StockKnowledgeTab },
  ],
}

export default extension
