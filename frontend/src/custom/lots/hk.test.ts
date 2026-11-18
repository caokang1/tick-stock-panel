import { describe, expect, it } from 'vitest'
import { fxOf, hkSymbolFrom, isHk } from './hk'

describe('港股持仓', () => {
  it('认得出港股代码', () => {
    expect(isHk('00981.HK')).toBe(true)
    expect(isHk('600519.SH')).toBe(false)
    expect(isHk('981.HK')).toBe(false)
  })

  it('把敲的代码补成标准写法', () => {
    expect(hkSymbolFrom('981.hk')).toBe('00981.HK')
    expect(hkSymbolFrom('00981')).toBe('00981.HK')
    expect(hkSymbolFrom('1211hk')).toBe('01211.HK')
    expect(hkSymbolFrom('600519')).toBeNull()
    expect(hkSymbolFrom('981')).toBeNull()
    expect(hkSymbolFrom('中芯')).toBeNull()
  })

  it('折人民币：A 股乘 1，港股乘汇率，没有汇率算不出', () => {
    expect(fxOf('600519.SH', null)).toBe(1)
    expect(fxOf('00981.HK', 0.85)).toBe(0.85)
    expect(fxOf('00981.HK', null)).toBeNull()
  })
})
