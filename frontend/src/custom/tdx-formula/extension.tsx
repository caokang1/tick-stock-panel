import { lazy } from 'react'
import { FunctionSquare } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const TdxFormula = lazy(() => import('./Page').then(m => ({ default: m.TdxFormula })))

const extension: FrontendExtension = {
  id: 'community.tdx-formula',
  apiVersion: 1,
  routes: [
    { id: 'tdx-formula', path: '/tdx-formula', component: TdxFormula },
  ],
  navigation: [
    { id: 'tdx-formula', routeId: 'tdx-formula', label: '通达信公式', icon: FunctionSquare },
  ],
}

export default extension
