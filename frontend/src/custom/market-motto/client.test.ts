import { describe, expect, it } from 'vitest'
import { hint } from './client'

describe('左上角的话：鼠标移上去的说明', () => {
  it('有评分时写出分数、叫法和日期', () => {
    expect(hint({ text: 'x', score: 33.6, label: '偏冷', band: '偏冷', as_of: '2026-11-18' }))
      .toBe('情绪评分 34 · 偏冷 · 11月18日。点击查看每日复盘')
  })
  it('没有评分、还没取到时说明是默认的一句', () => {
    expect(hint(undefined)).toContain('默认的一句')
    expect(hint({ text: 'x', score: null, label: null, band: null, as_of: null })).toContain('默认的一句')
  })
})
