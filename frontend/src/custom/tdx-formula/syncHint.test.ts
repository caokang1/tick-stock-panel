import { describe, expect, it } from 'vitest'
import { beijingNow, expectedDay, syncHint } from './syncHint'

describe('立即同步旁的提示', () => {
  it('算出最近一个数据已经可以下载的工作日', () => {
    expect(expectedDay('2026-11-18', '16:00', '16:30')).toBe('2026-11-17')
    expect(expectedDay('2026-11-18', '16:30', '16:30')).toBe('2026-11-18')
    expect(expectedDay('2026-11-16', '09:00', '16:30')).toBe('2026-11-13') // 周一早上 → 上周五
    expect(expectedDay('2026-11-15', '20:00', '16:30')).toBe('2026-11-13') // 周日
  })

  it('收盘后、还没到能下载的时间：说明今天的数据几点才有', () => {
    const text = syncHint('2026-11-17', '2026-11-18', '15:44', '16:30', '16:30')
    expect(text).toContain('16:30 之后才能下载')
    expect(text).toContain('2026-11-17')
  })

  it('到了时间还没下载：指到“更新到最新”', () => {
    expect(syncHint('2026-11-17', '2026-11-18', '17:00', '16:30', '16:30')).toContain('更新到最新')
    expect(syncHint('2026-11-12', '2026-11-16', '09:00', '16:30', '16:30')).toContain('只到 2026-11-12')
  })

  it('已经是最新的、周末、没有数据：不提示', () => {
    expect(syncHint('2026-11-18', '2026-11-18', '17:00', '16:30', '16:30')).toBeNull()
    expect(syncHint('2026-11-13', '2026-11-15', '10:00', '16:30', '16:30')).toBeNull()
    expect(syncHint(null, '2026-11-18', '17:00', '16:30', '16:30')).toBeNull()
  })

  it('取北京时间', () => {
    expect(beijingNow(new Date('2026-11-18T08:05:00Z'))).toEqual({ today: '2026-11-18', clock: '16:05' })
    expect(beijingNow(new Date('2026-11-18T17:30:00Z'))).toEqual({ today: '2026-11-19', clock: '01:30' })
  })
})
