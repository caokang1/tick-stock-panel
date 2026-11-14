import { describe, expect, it } from 'vitest'
import {
  capText, conditionFromParsed, conditionName, defaultUniverse, frequencyText, pct, tone, toPanelSymbol, withParam,
} from './logic'

const parsed = {
  name: '站上均线',
  description: '收盘价高于均线',
  formula: 'C>MA(C,{p1});',
  params: [{ key: 'p1', label: '周期（原值 20）', default: 20, min: 1, max: 60, step: 1, unit: '' }],
}

describe('通达信公式页面的纯函数', () => {
  it('解析出的条件默认启用，参数取公式里原来的数', () => {
    const c = conditionFromParsed(parsed)
    expect(c.enabled).toBe(true)
    expect(c.params).toEqual({ p1: 20 })
    expect(c.pasted?.params_spec).toHaveLength(1)
    expect(conditionName(c)).toBe('站上均线')
  })

  it('改参数；清空或填的不是数时回到原来的数', () => {
    const c = conditionFromParsed(parsed)
    expect(withParam(c, 'p1', '30').params.p1).toBe(30)
    expect(withParam(c, 'p1', '').params.p1).toBe(20)
    expect(withParam(c, 'p1', 'abc').params.p1).toBe(20)
    expect(c.params.p1).toBe(20) // 不改动原来的对象
  })

  it('百分比和涨跌颜色', () => {
    expect(pct(0.0123)).toBe('+1.23%')
    expect(pct(-0.004)).toBe('-0.40%')
    expect(pct(0.456, 1, false)).toBe('45.6%')
    expect(pct(null)).toBe('—')
    expect(tone(0.01)).toBe('text-bull')
    expect(tone(-0.01)).toBe('text-bear')
    expect(tone(0)).toBe('')
    expect(tone(null)).toBe('')
  })

  it('流通市值和代码写法', () => {
    expect(capText(346_380_736_000)).toBe('3,464 亿')
    expect(capText(5_230_000_000)).toBe('52.3 亿')
    expect(capText(null)).toBe('—')
    expect(toPanelSymbol('sh.600000')).toBe('600000.SH')
    expect(toPanelSymbol('bj.430047')).toBe('430047.BJ')
    expect(toPanelSymbol('600000')).toBe('600000')
  })

  it('信号频率的说明', () => {
    expect(frequencyText({ avg_per_day: 0, per_year: 0, total: 0 })).toContain('从来没有出过信号')
    expect(frequencyText({ avg_per_day: 12.34, per_year: 3000, total: 35000 })).toBe('历史上一共 35,000 个信号，平均每天 12.3 个')
    expect(frequencyText({ avg_per_day: 0.02, per_year: 5, total: 60 })).toContain('信号很少')
    expect(frequencyText({ avg_per_day: 300, per_year: 70000, total: 800000 })).toContain('条件可能太松')
  })

  it('默认的股票范围', () => {
    expect(defaultUniverse()).toMatchObject({ exclude_st: true, min_float_cap: null, exclude_industries: [] })
  })
})
