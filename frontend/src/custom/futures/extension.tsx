import { lazy } from 'react'
import { CandlestickChart } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const Futures = lazy(() => import('./Page').then(m => ({ default: m.Futures })))

const extension: FrontendExtension = {
  id: 'community.futures',
  apiVersion: 1,
  routes: [
    { id: 'futures', path: '/futures', component: Futures },
  ],
  navigation: [
    { id: 'futures', routeId: 'futures', label: '期货', icon: CandlestickChart },
  ],
}

export default extension
