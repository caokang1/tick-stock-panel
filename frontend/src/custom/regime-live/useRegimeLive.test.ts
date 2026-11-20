import { describe, expect, it } from 'vitest'
import { SSE_INVALIDATE_PREFIXES } from '@/lib/queryKeys'
import { REGIME_LIVE_KEY, isRegimeDataKey } from './useRegimeLive'

describe('市场环境盘中刷新', () => {
  it('行情推送会触发刷新这条查询', () => {
    expect(SSE_INVALIDATE_PREFIXES).toContain(REGIME_LIVE_KEY[0])
  })

  it('重算后只重读页面数据，不重读触发刷新的查询自己（否则会循环）', () => {
    expect(isRegimeDataKey('regime-history')).toBe(true)
    expect(isRegimeDataKey('regime-latest')).toBe(true)
    expect(isRegimeDataKey('regime-mainline')).toBe(true)
    expect(isRegimeDataKey(REGIME_LIVE_KEY[0])).toBe(false)
    expect(isRegimeDataKey('overview-market')).toBe(false)
  })

  it('这个前缀不会误伤别的市场环境查询', () => {
    for (const key of ['regime-history', 'regime-latest', 'regime-states', 'regime-coverage', 'regime-phases', 'regime-mainline']) {
      expect(key.startsWith(REGIME_LIVE_KEY[0])).toBe(false)
    }
  })
})
