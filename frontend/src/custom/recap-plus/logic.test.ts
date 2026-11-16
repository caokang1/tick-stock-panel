import { describe, expect, it } from 'vitest'
import { axisDate, isNew, latestPerDay, pct, previousClose, scale, section, shortDate, signedYi, yi } from './logic'
import type { RecapReport } from './client'

const report = (as_of: string, created_at: string, content = ''): RecapReport => ({ id: created_at, as_of, content, summary: '', created_at })

describe('每日复盘页的计算', () => {
  it('数字和日期的写法', () => {
    expect(yi(1.4843e12)).toBe('14843亿')
    expect(signedYi(-1.712e11)).toBe('-1712亿')
    expect(signedYi(2.5e10, 1)).toBe('+250.0亿')
    expect(pct(-0.0087)).toBe('-0.87%')
    expect([yi(null), pct(undefined)]).toEqual(['—', '—'])
    expect([shortDate('2026-11-06'), axisDate('2026-11-06')]).toEqual(['11月6日', '11-06'])
  })

  it('同一天只留最后生成的那份，新的日期在前', () => {
    const kept = latestPerDay([report('2026-11-13', 'a'), report('2026-11-16', 'b'), report('2026-11-16', 'c'), report('2026-11-12', 'd')])
    expect(kept.map(r => [r.as_of, r.id])).toEqual([['2026-11-16', 'c'], ['2026-11-13', 'a'], ['2026-11-12', 'd']])
  })

  it('按序号取出报告里的一节', () => {
    const content = '# 标题\n\n### 一、市场全景\n甲\n乙\n\n### 二、成交额\n丙\n\n### 六、数据质量自查\n丁'
    expect(section(content, '一')).toBe('甲\n乙')
    expect(section(content, '六')).toBe('丁')
    expect(section(content, '五')).toBe('')
  })

  it('公告新不新：上一个工作日收盘之后披露的算新', () => {
    expect(previousClose(new Date(2026, 10, 16, 19, 0))).toBe('2026-11-13 15:00:00') // 周一往前是周五
    expect(previousClose(new Date(2026, 10, 17, 9, 0))).toBe('2026-11-16 15:00:00')
    expect(isNew('2026-11-13 18:04:08', '2026-11-13 15:00:00')).toBe(true)
    expect(isNew('2026-11-13 09:00:00', '2026-11-13 15:00:00')).toBe(false)
  })

  it('图表坐标：空值跳过，柱状图从 0 起', () => {
    const pad = { l: 10, r: 10, t: 10, b: 10 }
    const line = scale([1, null, 3], 120, 60, pad)
    expect(line.points[1]).toBeNull()
    expect(line.points[0]!.x).toBe(10)
    expect(line.points[2]!.x).toBe(110)
    expect(line.points[0]!.y).toBeGreaterThan(line.points[2]!.y) // 数值大的在上面
    const bars = scale([5, 10], 120, 60, pad, true)
    expect(bars.min).toBe(0)
    expect(bars.points[1]!.y).toBe(10)
    expect(scale([null, null], 120, 60, pad).points).toEqual([null, null])
  })
})
