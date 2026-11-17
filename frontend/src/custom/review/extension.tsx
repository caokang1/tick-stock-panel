import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'

export const ReviewPage = lazy(() => import('./Page').then(m => ({ default: m.ReviewPage })))

export const DragonTigerCard = lazy(() => import('./Page').then(m => ({ default: m.DragonTigerCard })))

const extension: FrontendExtension = {
  id: 'official.review',
  apiVersion: 1,
  routes: [
    { id: 'review', path: '/review', component: ReviewPage },
  ],
  // 左侧不再单独放入口：生成复盘从“每日复盘”页右上角的按钮进来，两个入口并成一个
}

export default extension
