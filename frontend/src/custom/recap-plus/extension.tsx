import { lazy } from 'react'
import { CalendarDays } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const DailyReview = lazy(() => import('./Page').then(m => ({ default: m.DailyReview })))

const extension: FrontendExtension = {
  id: 'community.recap-plus',
  apiVersion: 1,
  routes: [
    { id: 'daily-review', path: '/daily-review', component: DailyReview },
  ],
  navigation: [
    { id: 'daily-review', routeId: 'daily-review', label: '每日复盘', icon: CalendarDays },
  ],
}

export default extension
