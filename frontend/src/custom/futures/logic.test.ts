import { describe, expect, it } from 'vitest'
import type { FutureQuote } from './client'
import { candles, digitsOf, grouped, lots, pct, price, signed } from './logic'

const quote = (code: string, group: string): FutureQuote => ({
  code, name: code, group, last: 1, open: 1, high: 1, low: 1, prev: 1, change: 0, change_pct: 0,
  volume: null, open_interest: null, time: '', spot: null, basis: null, basis_pct: null,
})

describe('期货页的计算', () => {
  it('按第一次出现的先后分组', () => {
    const out = grouped([quote('IF0', '股指期货'), quote('AU0', '贵金属和有色'), quote('IH0', '股指期货')])
    expect(out.map(g => g.group)).toEqual(['股指期货', '贵金属和有色'])
    expect(out[0].rows.map(r => r.code)).toEqual(['IF0', 'IH0'])
  })

  it('价格的小数位跟着价位走，空值写成横线', () => {
    expect(price(99850)).toBe('99,850')
    expect(price(3912.6)).toBe('3,912.6')
    expect(price(819.86)).toBe('819.86')
    expect(price(45.943)).toBe('45.943')
    expect(price(null)).toBe('—')
  })

  it('涨跌带正负号，手数过万写成万', () => {
    expect(signed(-1510, digitsOf(99830))).toBe('-1510')
    expect(signed(6.2)).toBe('+6.20')
    expect(signed(-44.53, 1)).toBe('-44.5')
    expect(pct(-0.0158)).toBe('-1.58%')
    expect(pct(null)).toBe('—')
    expect(lots(105351)).toBe('10.5万')
    expect(lots(9876)).toBe('9876')
  })

  it('蜡烛落在画布里，高的在上面', () => {
    const pad = { l: 10, r: 10, t: 10, b: 10 }
    const { items } = candles([
      { date: '2026-11-17', open: 10, high: 12, low: 9, close: 11 },
      { date: '2026-11-18', open: 11, high: 11, low: 8, close: 9 },
    ], 220, 120, pad)
    expect(items).toHaveLength(2)
    expect(items[0].up).toBe(true)
    expect(items[1].up).toBe(false)
    expect(items[0].yHigh).toBe(10)
    expect(items[1].yLow).toBe(110)
    expect(items[0].x).toBeLessThan(items[1].x)
  })

  it('没有数据、全是同一个价都不出错', () => {
    expect(candles([], 100, 100, { l: 0, r: 0, t: 0, b: 0 }).items).toEqual([])
    const flat = candles([{ date: 'd', open: 5, high: 5, low: 5, close: 5 }], 100, 100, { l: 0, r: 0, t: 0, b: 0 })
    expect(Number.isFinite(flat.items[0].yClose)).toBe(true)
  })
})
