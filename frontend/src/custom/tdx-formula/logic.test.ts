import { describe, expect, it } from 'vitest'
import type { Block, Options, RealPosition, RealTodo, SourcePolicy } from './client'
import {
  DATA_SETTINGS_PATH, FREECN_SOURCE, capText, capabilityLabel, capitalError, conditionFromParsed, conditionName, configFromForm, costFromInput, costInput,
  costText, curveRange, dailyCapText, defaultTab, defaultUniverse, etaText, formFromConfig, fractionOf, frequencyText,
  initialCashError, initialTradeForm, limitsBody, limitsForm, linePath, money, needsCode, needsShares, newRankFactor,
  paramValue, pct, pendingText, percentInput, progressPercent, rankFromForm, rankToForm, runTimeError, sourceState,
  statusLabel, timingFromForm, timingToForm, todayText, tone, toPanelSymbol, tradeFormError, tradeFromForm, tradeInput,
  tradeToForm, withParam,
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

const spec = (key: string, label: string, value: number) => ({ key, label, default: value, min: 1, max: 250, step: 1, unit: '天' })
const block = (id: string, kind: Block['kind'], name: string, params = [spec('n', '天数', 5)]): Block => ({
  id, kind, name, description: '', formula: '', params, source: 'builtin', category: '',
})
const options: Options = {
  rank_blocks: [block('rank_amount', 'rank', '成交额'), block('rank_float_cap', 'rank', '流通市值', [])],
  timing_blocks: [
    block('timing_above_ma', 'timing', '指数在均线上方', [spec('n', '均线天数', 20)]),
    block('timing_ma_cross', 'timing', '指数短均线在长均线上方', [spec('fast', '短均线', 5), spec('slow', '长均线', 20)]),
  ],
  indexes: [{ code: 'sh.000300', name: '沪深300' }, { code: 'sh.000905', name: '中证500' }],
  trade_defaults: { max_positions: 5, position_pct: null, max_hold_days: 5, take_profit: 0.1, stop_loss: 0.05 },
}

describe('策略的表单和保存内容之间的转换', () => {
  it('状态的叫法和默认先看哪一页', () => {
    expect(statusLabel('draft')).toBe('草稿')
    expect(statusLabel('running')).toBe('运行中')
    expect(statusLabel('stopped')).toBe('已停用')
    expect(statusLabel('别的')).toBe('别的')
    expect(defaultTab(0)).toBe('strategy')
    expect(defaultTab(2)).toBe('today')
  })

  it('百分数和小数互换，不带浮点尾巴', () => {
    expect(percentInput(0.07)).toBe('7')
    expect(percentInput(0.125)).toBe('12.5')
    expect(percentInput(null)).toBe('')
    expect(fractionOf('7')).toBe(0.07)
    expect(fractionOf('12.5')).toBe(0.125)
    expect(fractionOf('  ')).toBeNull()
    expect(fractionOf('abc')).toBeNaN()
    expect(paramValue(spec('n', '天数', 5), '')).toBe(5)
    expect(paramValue(spec('n', '天数', 5), '8')).toBe(8)
  })

  it('买卖规则：默认值、留空表示不用、存成小数', () => {
    const form = tradeToForm(null, options.trade_defaults)
    expect(form).toEqual({ max_positions: '5', position_pct: '', max_hold_days: '5', take_profit: '10', stop_loss: '5' })
    expect(tradeFromForm(form)).toEqual({ ok: true, value: options.trade_defaults })
    expect(tradeFromForm({ max_positions: '10', position_pct: '8', max_hold_days: '', take_profit: '', stop_loss: '7.5' }))
      .toEqual({ ok: true, value: { max_positions: 10, position_pct: 0.08, max_hold_days: null, take_profit: null, stop_loss: 0.075 } })
  })

  it('买卖规则：保存时写成 null 的项载入后仍是空，没写的项用默认值', () => {
    const form = tradeToForm({ max_positions: 3, max_hold_days: null, take_profit: null }, options.trade_defaults)
    expect(form).toEqual({ max_positions: '3', position_pct: '', max_hold_days: '', take_profit: '', stop_loss: '5' })
  })

  it('买卖规则：填错时说明原因', () => {
    const base = tradeToForm(null, options.trade_defaults)
    const error = (patch: Partial<typeof base>) => {
      const r = tradeFromForm({ ...base, ...patch })
      return r.ok ? null : r.error
    }
    expect(error({ max_positions: '0' })).toContain('1 到 100')
    expect(error({ max_positions: '2.5' })).toContain('整数')
    expect(error({ max_positions: '' })).toContain('1 到 100')
    expect(error({ position_pct: '120' })).toContain('100%')
    expect(error({ max_hold_days: '501' })).toContain('1 到 500')
    expect(error({ take_profit: '-1' })).toContain('止盈')
    expect(error({ stop_loss: '0' })).toContain('止损')
    expect(error({ max_hold_days: '', take_profit: '', stop_loss: '' })).toContain('至少要有一种卖出方式')
    expect(error({})).toBeNull()
  })

  it('排名：没有因子存成 null；新加的因子取默认参数；权重要大于 0', () => {
    expect(rankFromForm([], options.rank_blocks)).toEqual({ ok: true, value: null })
    const factor = newRankFactor(options.rank_blocks[0])
    expect(factor).toEqual({ block_id: 'rank_amount', params: { n: 5 }, weight: '1', direction: 'desc' })
    expect(rankFromForm([{ ...factor, weight: '2', direction: 'asc' }], options.rank_blocks)).toEqual({
      ok: true, value: { factors: [{ block_id: 'rank_amount', params: { n: 5 }, weight: 2, direction: 'asc' }] },
    })
    const bad = rankFromForm([{ ...factor, weight: '0' }], options.rank_blocks)
    expect(bad.ok ? '' : bad.error).toContain('成交额')
  })

  it('排名：载入时补上缺的参数和默认的方向、权重', () => {
    const rank = { factors: [{ block_id: 'rank_amount' }, { block_id: 'rank_amount', params: { n: 20 }, weight: 3, direction: 'asc' }] }
    expect(rankToForm(rank as never, options.rank_blocks)).toEqual([
      { block_id: 'rank_amount', params: { n: 5 }, weight: '1', direction: 'desc' },
      { block_id: 'rank_amount', params: { n: 20 }, weight: '3', direction: 'asc' },
    ])
    expect(rankToForm(null, options.rank_blocks)).toEqual([])
    // 认不出的因子：保存过的参数原样留着
    expect(rankToForm({ factors: [{ block_id: 'gone', params: { x: 1 }, weight: 1, direction: 'desc' }] }, options.rank_blocks)[0].params)
      .toEqual({ x: 1 })
  })

  it('大盘择时：关着存成 null；打开时带上条件、参数、指数和不满足时怎么办', () => {
    const off = timingToForm(null, options)
    expect(off).toEqual({ enabled: false, block_id: 'timing_above_ma', params: { n: 20 }, index_code: 'sh.000300', on_fail: 'stop_buy' })
    expect(timingFromForm(off)).toBeNull()
    expect(timingFromForm({ ...off, enabled: true })).toEqual({
      block_id: 'timing_above_ma', params: { n: 20 }, index_code: 'sh.000300', on_fail: 'stop_buy',
    })
    const saved = { block_id: 'timing_ma_cross', params: { fast: 10 }, index_code: 'sh.000905', on_fail: 'clear' as const }
    const on = timingToForm(saved, options)
    expect(on).toEqual({ enabled: true, block_id: 'timing_ma_cross', params: { fast: 10, slow: 20 }, index_code: 'sh.000905', on_fail: 'clear' })
    expect(timingFromForm(on)).toEqual({ ...saved, params: { fast: 10, slow: 20 } })
  })

  it('整个策略：保存的内容载入后再保存，不走样', () => {
    const config = {
      conditions: [conditionFromParsed(parsed)],
      universe: { ...defaultUniverse(), min_float_cap: 50 },
      rank: { factors: [{ block_id: 'rank_amount', params: { n: 10 }, weight: 1, direction: 'desc' as const }] },
      trade: { max_positions: 8, position_pct: 0.1, max_hold_days: null, take_profit: 0.15, stop_loss: null },
      timing: { block_id: 'timing_above_ma', params: { n: 60 }, index_code: 'sh.000905', on_fail: 'clear' as const },
      formula: 'XG:C>MA(C,20);',
    }
    expect(configFromForm(formFromConfig(config, options), options.rank_blocks)).toEqual({ ok: true, value: config })
  })

  it('整个策略：空白的新策略用默认规则；没有条件、市值上下限反了时不能保存', () => {
    const blank = formFromConfig({}, options)
    expect(blank.rank).toEqual([])
    expect(blank.timing.enabled).toBe(false)
    expect(blank.trade.max_positions).toBe('5')
    const none = configFromForm(blank, options.rank_blocks)
    expect(none.ok ? '' : none.error).toContain('至少要启用一个选股条件')
    const withCondition = { ...blank, conditions: [conditionFromParsed(parsed)] }
    const ok = configFromForm(withCondition, options.rank_blocks)
    expect(ok.ok && ok.value.rank).toBeNull()
    expect(ok.ok && ok.value.timing).toBeNull()
    const capped = configFromForm({ ...withCondition, universe: { ...blank.universe, min_float_cap: 100, max_float_cap: 50 } }, options.rank_blocks)
    expect(capped.ok ? '' : capped.error).toContain('下限不能比上限大')
  })
})

describe('设置和金额', () => {
  const limit = (min: number, max: number, label: string) => ({ min, max, label })

  it('金额带千分位', () => {
    expect(money(1234567.891)).toBe('1,234,567.89')
    expect(money(1000000, 0)).toBe('1,000,000')
    expect(money(-50.5)).toBe('-50.50')
    expect(money(null)).toBe('—')
  })

  it('交易成本按万分之、千分之、元来填', () => {
    expect(costInput('commission', 0.00025)).toBe('2.5')
    expect(costInput('stamp_tax', 0.0005)).toBe('0.5')
    expect(costInput('min_commission', 5)).toBe('5')
    expect(costText('commission', 0.00025)).toBe('万分之 2.5')
    expect(costText('slippage', 0.001)).toBe('千分之 1')
    expect(costText('min_commission', 5)).toBe('5 元')
    expect(costFromInput('commission', '3', limit(0, 0.003, '佣金比例'))).toEqual({ ok: true, value: 0.0003 })
    expect(costFromInput('slippage', '2', limit(0, 0.01, '滑点'))).toEqual({ ok: true, value: 0.002 })
    const over = costFromInput('commission', '31', limit(0, 0.003, '佣金比例'))
    expect(over.ok ? '' : over.error).toBe('佣金比例要在 万分之 0 到 万分之 30 之间。')
    expect(costFromInput('min_commission', '', limit(0, 50, '单笔最低佣金')).ok).toBe(false)
  })

  it('初始资金和实际投入资金的范围', () => {
    expect(initialCashError('1000000')).toBeNull()
    expect(initialCashError('9999')).toContain('1 万')
    expect(initialCashError('')).not.toBeNull()
    expect(capitalError('1000')).toBeNull()
    expect(capitalError('999')).toContain('1000 元')
    expect(capitalError('abc')).not.toBeNull()
  })
})

const todo: RealTodo = {
  id: 1, strategy_id: 's1', date: '2024-06-03', side: 'buy', code: 'sh.600000', name: '示例', shares: 2000, price: 12,
  reason: '满足：示例条件', status: 'pending', default_date: '2024-06-04',
}
const position: RealPosition = {
  id: 7, code: 'sh.600000', name: '示例', buy_date: '2024-06-04', buy_price: 12.5, shares: 1000, cost: 12505,
  value: 12000, gain: -0.04, hold_days: 2, sell_reason: null,
}

describe('实际持仓的成交记录', () => {
  it('确认提醒时预填提醒里的数，建议 0 股时留空让人填', () => {
    expect(initialTradeForm('confirm-buy', '2024-06-05', todo))
      .toEqual({ code: 'sh.600000', shares: '2000', price: '12', date: '2024-06-04' })
    expect(initialTradeForm('confirm-buy', '2024-06-05', { ...todo, shares: 0 }).shares).toBe('')
  })

  it('修改持仓预填原来的数，卖出预填今天、价格留空，手动添加全空', () => {
    expect(initialTradeForm('edit', '2024-06-10', undefined, position))
      .toEqual({ code: 'sh.600000', shares: '1000', price: '12.5', date: '2024-06-04' })
    const sell = initialTradeForm('sell', '2024-06-10', undefined, position)
    expect(sell.price).toBe('')
    expect(sell.date).toBe('2024-06-10')
    expect(initialTradeForm('add', '2024-06-10')).toEqual({ code: '', shares: '', price: '', date: '2024-06-10' })
  })

  it('哪种用法要填代码、股数', () => {
    expect(needsCode('add')).toBe(true)
    expect(needsCode('edit')).toBe(false)
    expect(needsShares('confirm-buy')).toBe(true)
    expect(needsShares('confirm-sell')).toBe(false)
    expect(needsShares('sell')).toBe(false)
  })

  it('检查填的内容', () => {
    const today = '2024-06-10'
    const ok = { code: '600000', shares: '100', price: '10.5', date: '2024-06-07' }
    expect(tradeFormError('add', ok, today)).toBeNull()
    expect(tradeFormError('add', { ...ok, code: '6000' }, today)).toContain('6 位股票代码')
    expect(tradeFormError('add', { ...ok, shares: '0' }, today)).toContain('股数')
    expect(tradeFormError('add', { ...ok, shares: '100.5' }, today)).toContain('股数')
    expect(tradeFormError('add', { ...ok, price: '' }, today)).toContain('成交价')
    expect(tradeFormError('add', { ...ok, date: '' }, today)).toContain('成交日期')
    expect(tradeFormError('add', { ...ok, date: '2024-06-11' }, today)).toContain('将来')
    // 卖出不用填股数和代码
    expect(tradeFormError('sell', { code: 'sh.600000', shares: '', price: '11', date: today }, today)).toBeNull()
    expect(tradeInput(ok)).toEqual({ code: '600000', shares: 100, price: 10.5, date: '2024-06-07' })
  })

  it('待确认提醒的说明和本地日期', () => {
    expect(pendingText([], '2024-06-04')).toBe('没有待确认的提醒')
    expect(pendingText([todo], '2024-06-03')).toBe('有 1 条待确认')
    expect(pendingText([todo, { ...todo, id: 2, date: '2024-06-04' }], '2024-06-04')).toBe('有 2 条待确认，其中 1 条是以前的')
    expect(todayText(new Date(2024, 0, 5))).toBe('2024-01-05')
  })
})

describe('净值曲线', () => {
  it('取值范围上下留空，全空时给一个能画的范围', () => {
    const [lo, hi] = curveRange([[1, 1.5, null], [0.8, null, 1.2]])
    expect(lo).toBeCloseTo(0.765)
    expect(hi).toBeCloseTo(1.535)
    expect(curveRange([[null], []])).toEqual([0, 1])
    const flat = curveRange([[1, 1]])
    expect(flat[0]).toBeLessThan(1)
    expect(flat[1]).toBeGreaterThan(1)
  })

  it('画成路径，空值处断开', () => {
    expect(linePath([0, 1, 0.5], 0, 1, 100, 50)).toBe('M0.0,50.0L50.0,0.0L100.0,25.0')
    expect(linePath([null, 0, null, 1, 1], 0, 1, 100, 50)).toBe('M25.0,50.0M75.0,0.0L100.0,0.0')
    expect(linePath([], 0, 1, 100, 50)).toBe('')
  })
})

describe('「数据」页', () => {
  const free: SourcePolicy = {
    enabled: true, max_concurrent: 1, daily_request_cap: 0, min_interval_seconds: 0.5, jitter_seconds: 0.3,
    timeout_seconds: 30, max_retries: 2, stop_after_consecutive_failures: 5, cooldown_minutes_on_reject: 10,
    count_pages_as_requests: false, official_daily_limit: null, official_max_concurrent: null,
  }
  const official: SourcePolicy = {
    ...free, daily_request_cap: 20000, min_interval_seconds: 0.3, jitter_seconds: 0.2, timeout_seconds: 60,
    cooldown_minutes_on_reject: 360, count_pages_as_requests: true, official_daily_limit: 50000,
    official_max_concurrent: 1,
  }

  it('行情管理的入口指向设置里的免费公开来源', () => {
    expect(DATA_SETTINGS_PATH).toBe('/settings?tab=data-sources&source=freecn')
    expect(FREECN_SOURCE).toBe('freecn')
  })

  it('还要多久：按小时、分钟说，不知道时不说', () => {
    expect(etaText(null)).toBe('')
    expect(etaText(undefined)).toBe('')
    expect(etaText(NaN)).toBe('')
    expect(etaText(20)).toBe('不到 1 分钟')
    expect(etaText(90)).toBe('约 2 分钟')
    expect(etaText(3600)).toBe('约 1 小时')
    expect(etaText(3 * 3600 + 5 * 60 + 10)).toBe('约 3 小时 5 分钟')
    expect(etaText(3599)).toBe('约 1 小时')
  })

  it('进度条的百分比：总数未知是 0，不超过 100', () => {
    expect(progressPercent(0, 0)).toBe(0)
    expect(progressPercent(5, 0)).toBe(0)
    expect(progressPercent(1, 3)).toBe(33)
    expect(progressPercent(5000, 5000)).toBe(100)
    expect(progressPercent(6, 5)).toBe(100)
  })

  it('来源状态、数据种类的中文名，不认识的原样显示', () => {
    expect(sourceState('ok')).toEqual({ label: '正常', tone: 'accent' })
    expect(sourceState('cooldown')).toEqual({ label: '暂停中', tone: 'warning' })
    expect(sourceState('cap_reached')).toEqual({ label: '已达今日上限', tone: 'danger' })
    expect(sourceState('disabled')).toEqual({ label: '已停用', tone: 'muted' })
    expect(sourceState('别的')).toEqual({ label: '别的', tone: 'muted' })
    expect(capabilityLabel('stock_daily')).toBe('个股日线')
    expect(capabilityLabel('float_shares')).toBe('流通股本')
    expect(capabilityLabel('market_snapshot')).toBe('当日全市场行情')
    expect(capabilityLabel('new_kind')).toBe('new_kind')
    expect(dailyCapText(0)).toBe('不限')
    expect(dailyCapText(20000)).toBe('20,000')
  })

  it('限速表单 → 发给后端的修改：只带可以改的项', () => {
    const form = limitsForm(official)
    expect(form).toEqual({
      enabled: true, daily_request_cap: '20000', min_interval_seconds: '0.3', jitter_seconds: '0.2',
      timeout_seconds: '60', max_retries: '2', stop_after_consecutive_failures: '5', cooldown_minutes_on_reject: '360',
    })
    expect(limitsBody(form, official)).toEqual({
      ok: true,
      value: {
        enabled: true, daily_request_cap: 20000, min_interval_seconds: 0.3, jitter_seconds: 0.2, timeout_seconds: 60,
        max_retries: 2, stop_after_consecutive_failures: 5, cooldown_minutes_on_reject: 360,
      },
    })
    const off = limitsBody({ ...limitsForm(free), enabled: false, min_interval_seconds: ' 1.5 ' }, free)
    expect(off.ok && off.value.enabled).toBe(false)
    expect(off.ok && off.value.min_interval_seconds).toBe(1.5)
    expect(off.ok && Object.keys(off.value)).not.toContain('max_concurrent')
  })

  it('限速表单填错时说明是哪一项', () => {
    const error = (policy: SourcePolicy, changes: Partial<ReturnType<typeof limitsForm>>) => {
      const result = limitsBody({ ...limitsForm(policy), ...changes }, policy)
      return result.ok ? null : result.error
    }
    expect(error(free, {})).toBeNull()
    expect(error(free, { max_retries: '' })).toBe('「失败后的重试次数」还没有填')
    expect(error(free, { timeout_seconds: 'abc' })).toBe('「单次请求超时（秒）」要填数字')
    expect(error(free, { max_retries: '1.5' })).toBe('「失败后的重试次数」要填整数')
    expect(error(free, { min_interval_seconds: '0.05' })).toContain('不能小于 0.1 秒')
    expect(error(free, { min_interval_seconds: '0.1' })).toBeNull()
    expect(error(free, { jitter_seconds: '-1' })).toBe('「随机抖动上限（秒）」不能是负数')
    expect(error(free, { timeout_seconds: '0' })).toBe('「单次请求超时（秒）」必须大于 0')
    expect(error(free, { timeout_seconds: '601' })).toBe('「单次请求超时（秒）」不能超过 600 秒')
    expect(error(free, { stop_after_consecutive_failures: '0' })).toBe('「连续失败多少次后停止」至少为 1')
    expect(error(free, { cooldown_minutes_on_reject: '0' })).toContain('至少为 1')
    expect(error(free, { daily_request_cap: '-1' })).toContain('不能是负数')
    // 没有官方上限的来源：0 表示不限，暂停时间可以短
    expect(error(free, { daily_request_cap: '0', cooldown_minutes_on_reject: '5' })).toBeNull()
  })

  it('有官方上限的来源：每日上限不能不限、不能超过官方的数，暂停不能少于 6 小时', () => {
    const error = (changes: Partial<ReturnType<typeof limitsForm>>) => {
      const result = limitsBody({ ...limitsForm(official), ...changes }, official)
      return result.ok ? null : result.error
    }
    expect(error({ daily_request_cap: '0' })).toContain('不能设为 0')
    expect(error({ daily_request_cap: '50001' })).toContain('不能超过官方每日上限 50000')
    expect(error({ daily_request_cap: '50000' })).toBeNull()
    expect(error({ cooldown_minutes_on_reject: '359' })).toContain('不能小于 360 分钟')
  })

  it('每天自动更新的时间要在允许的范围里', () => {
    expect(runTimeError('18:30', '18:00', '23:30')).toBeNull()
    expect(runTimeError('18:00', '18:00', '23:30')).toBeNull()
    expect(runTimeError('23:30', '18:00', '23:30')).toBeNull()
    expect(runTimeError('17:59', '18:00', '23:30')).toBe('每天自动更新的时间要在 18:00 到 23:30 之间')
    expect(runTimeError('23:31', '18:00', '23:30')).toContain('18:00 到 23:30')
    expect(runTimeError('', '18:00', '23:30')).toBe('请填一个时间，例如 18:30')
  })
})
