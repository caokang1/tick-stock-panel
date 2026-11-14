/** 页面用到的纯函数：解析结果转成条件、数字的显示。没有界面依赖，方便单独测试。 */
import type {
  Block, Condition, CostKey, Direction, OnFail, Options, ParamSpec, ParsedCondition, RankConfig, RealPosition, RealTodo,
  StrategyConfig, StrategyStatus, TimingConfig, TradeInput, TradeRules, Universe,
} from './client'

export function defaultUniverse(): Universe {
  return {
    exclude_st: true,
    exclude_star: true,
    exclude_chinext: true,
    exclude_bse: true,
    exclude_suspended: true,
    min_float_cap: null,
    max_float_cap: null,
    exclude_industries: [],
  }
}

/** 公式拆出来的一个条件 → 页面上可以勾选、调参数的条件，参数先取公式里原来的数 */
export function conditionFromParsed(parsed: ParsedCondition): Condition {
  const params: Record<string, number> = {}
  for (const p of parsed.params) params[p.key] = p.default
  return {
    pasted: { name: parsed.name, description: parsed.description, formula: parsed.formula, params_spec: parsed.params },
    enabled: true,
    params,
  }
}

export function conditionName(c: Condition): string {
  return c.pasted?.name ?? c.block_id ?? '条件'
}

/** 参数输入框里的字 → 数；清空或填的不是数时用默认值 */
export function paramValue(spec: ParamSpec | undefined, raw: string): number {
  const value = Number(raw)
  return raw.trim() !== '' && Number.isFinite(value) ? value : (spec?.default ?? 0)
}

/** 改一个参数：输入框清空或填的不是数时，回到公式里原来的数 */
export function withParam(c: Condition, key: string, raw: string): Condition {
  const spec = c.pasted?.params_spec.find(p => p.key === key)
  return { ...c, params: { ...c.params, [key]: paramValue(spec, raw) } }
}

/** 0.0123 → "+1.23%"；空值显示成横线 */
export function pct(value: number | null | undefined, digits = 2, signed = true): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const text = (value * 100).toFixed(digits)
  return `${signed && value > 0 ? '+' : ''}${text}%`
}

/** 涨用红、跌用绿（A 股习惯）；0 和空值不上色 */
export function tone(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value === 0) return ''
  return value > 0 ? 'text-bull' : 'text-bear'
}

/** 金额（元）显示成"多少亿" */
export function capText(yuan: number | null | undefined): string {
  if (yuan == null || !Number.isFinite(yuan)) return '—'
  const yi = yuan / 1e8
  return `${yi < 1 ? yi.toFixed(2) : yi < 1000 ? yi.toFixed(1) : Math.round(yi).toLocaleString('en-US')} 亿`
}

/** quant-lab 的代码写法 sh.600000 → 本面板的写法 600000.SH；认不出来的原样返回 */
export function toPanelSymbol(code: string): string {
  const m = /^(sh|sz|bj)\.(\d{6})$/i.exec(code)
  return m ? `${m[2]}.${m[1].toUpperCase()}` : code
}

/** 每天平均出多少个信号的大白话 */
export function frequencyText(f: { avg_per_day: number; per_year: number; total: number }): string {
  if (f.total === 0) return '历史上从来没有出过信号。条件可能太严，放宽参数试试。'
  const perDay = f.avg_per_day >= 1 ? `平均每天 ${f.avg_per_day.toFixed(1)} 个` : `平均每年 ${f.per_year.toFixed(0)} 个`
  const hint = f.avg_per_day > 100 ? '。信号太多，条件可能太松' : f.per_year < 20 ? '。信号很少，统计结果不太可靠' : ''
  return `历史上一共 ${f.total.toLocaleString('en-US')} 个信号，${perDay}${hint}`
}

/** 金额（元），带千分位；空值显示成横线 */
export function money(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

// ---------------- 策略：状态、表单和保存内容之间的转换 ----------------

export const STATUS_LABEL: Record<StrategyStatus, string> = { draft: '草稿', running: '运行中', stopped: '已停用' }

export function statusLabel(status: string): string {
  return STATUS_LABEL[status as StrategyStatus] ?? status
}

/** 打开页面时先看哪一页：有运行中的策略先看今日提醒，否则先去建策略 */
export function defaultTab(runningCount: number): 'today' | 'strategy' {
  return runningCount > 0 ? 'today' : 'strategy'
}

type Parsed<T> = { ok: true; value: T } | { ok: false; error: string }
const fail = (error: string): { ok: false; error: string } => ({ ok: false, error })

/** 输入框里的字 → 数；留空返回 null，填的不是数返回 NaN */
function numberOrNull(raw: string): number | null {
  const text = raw.trim()
  return text === '' ? null : Number(text)
}

/** 小数 → 输入框里的百分数："0.1" 显示成 "10"；null 显示成空 */
export function percentInput(fraction: number | null | undefined): string {
  if (fraction == null || !Number.isFinite(fraction)) return ''
  return String(Number((fraction * 100).toFixed(4)))
}

/** 输入框里的百分数 → 小数；留空返回 null，填的不是数返回 NaN */
export function fractionOf(raw: string): number | null {
  const value = numberOrNull(raw)
  return value == null ? null : Number.isFinite(value) ? Number((value / 100).toFixed(6)) : NaN
}

export function blockDefaults(block: Block | undefined): Record<string, number> {
  const out: Record<string, number> = {}
  for (const p of block?.params ?? []) out[p.key] = p.default
  return out
}

/** 保存下来的参数补上缺的、去掉这个积木没有的 */
function filledParams(block: Block | undefined, saved: Record<string, number> | undefined): Record<string, number> {
  if (!block) return { ...(saved ?? {}) } // 认不出的积木：原样留着，不丢保存过的参数
  const out = blockDefaults(block)
  for (const key of Object.keys(out)) {
    const value = saved?.[key]
    if (typeof value === 'number' && Number.isFinite(value)) out[key] = value
  }
  return out
}

/** 买卖规则在页面上的样子：都是输入框里的字，比例按百分数填，留空表示不用这一条 */
export interface TradeForm {
  max_positions: string
  position_pct: string
  max_hold_days: string
  take_profit: string
  stop_loss: string
}

export const FALLBACK_TRADE: TradeRules = {
  max_positions: 5, position_pct: null, max_hold_days: 5, take_profit: 0.1, stop_loss: 0.05,
}

export function tradeToForm(rules: Partial<TradeRules> | null | undefined, defaults: TradeRules = FALLBACK_TRADE): TradeForm {
  // 和后端一致：保存的规则里没写的项用默认值，写成 null 的才是"不用这一条"
  const r: TradeRules = { ...defaults, ...(rules ?? {}) }
  return {
    max_positions: String(r.max_positions),
    position_pct: percentInput(r.position_pct),
    max_hold_days: r.max_hold_days == null ? '' : String(r.max_hold_days),
    take_profit: percentInput(r.take_profit),
    stop_loss: percentInput(r.stop_loss),
  }
}

export function tradeFromForm(form: TradeForm): Parsed<TradeRules> {
  const maxPositions = numberOrNull(form.max_positions)
  if (maxPositions == null || !Number.isInteger(maxPositions) || maxPositions < 1 || maxPositions > 100) {
    return fail('“最多同时持有几只”要填 1 到 100 之间的整数。')
  }
  const pct = fractionOf(form.position_pct)
  if (pct != null && !(pct > 0 && pct <= 1)) return fail('“每只股票用多少资金”要在 0 到 100% 之间，留空表示平均分配。')
  const hold = numberOrNull(form.max_hold_days)
  if (hold != null && !(Number.isInteger(hold) && hold >= 1 && hold <= 500)) {
    return fail('“最多持有几天”要填 1 到 500 之间的整数，不用这一条就留空。')
  }
  const takeProfit = fractionOf(form.take_profit)
  if (takeProfit != null && !(takeProfit > 0)) return fail('“止盈”要填大于 0 的数，不用这一条就留空。')
  const stopLoss = fractionOf(form.stop_loss)
  if (stopLoss != null && !(stopLoss > 0)) return fail('“止损”要填大于 0 的数，不用这一条就留空。')
  if (hold == null && takeProfit == null && stopLoss == null) {
    return fail('至少要有一种卖出方式：最多持有几天、止盈、止损，三个里填一个。')
  }
  return {
    ok: true,
    value: { max_positions: maxPositions, position_pct: pct, max_hold_days: hold, take_profit: takeProfit, stop_loss: stopLoss },
  }
}

/** 一个排名因子在页面上的样子；权重是输入框里的字 */
export interface RankFactorForm {
  block_id: string
  params: Record<string, number>
  weight: string
  direction: Direction
}

export function newRankFactor(block: Block): RankFactorForm {
  return { block_id: block.id, params: blockDefaults(block), weight: '1', direction: 'desc' }
}

export function rankToForm(rank: Partial<RankConfig> | null | undefined, blocks: Block[]): RankFactorForm[] {
  return (rank?.factors ?? []).map(f => ({
    block_id: f.block_id,
    params: filledParams(blocks.find(b => b.id === f.block_id), f.params),
    weight: String(f.weight ?? 1),
    direction: f.direction === 'asc' ? 'asc' : 'desc',
  }))
}

/** 没有因子时保存成 null（回测时按成交额从大到小取） */
export function rankFromForm(factors: RankFactorForm[], blocks: Block[]): Parsed<RankConfig | null> {
  if (factors.length === 0) return { ok: true, value: null }
  const out: RankConfig = { factors: [] }
  for (const f of factors) {
    const name = blocks.find(b => b.id === f.block_id)?.name ?? f.block_id
    const weight = numberOrNull(f.weight)
    if (weight == null || !Number.isFinite(weight) || weight <= 0) return fail(`排名“${name}”的权重要填大于 0 的数。`)
    out.factors.push({ block_id: f.block_id, params: { ...f.params }, weight, direction: f.direction })
  }
  return { ok: true, value: out }
}

/** 大盘择时在页面上的样子 */
export interface TimingForm {
  enabled: boolean
  block_id: string
  params: Record<string, number>
  index_code: string
  on_fail: OnFail
}

type TimingOptions = Pick<Options, 'timing_blocks' | 'indexes'>

export function timingToForm(timing: Partial<TimingConfig> | null | undefined, options: TimingOptions | undefined): TimingForm {
  const blocks = options?.timing_blocks ?? []
  const block = blocks.find(b => b.id === timing?.block_id) ?? blocks[0]
  const enabled = Boolean(timing?.block_id)
  return {
    enabled,
    block_id: timing?.block_id || block?.id || '',
    params: filledParams(block, enabled ? timing?.params : undefined),
    index_code: timing?.index_code || options?.indexes[0]?.code || 'sh.000300',
    on_fail: timing?.on_fail === 'clear' ? 'clear' : 'stop_buy',
  }
}

/** 没有打开择时保存成 null */
export function timingFromForm(form: TimingForm): TimingConfig | null {
  if (!form.enabled || !form.block_id) return null
  return { block_id: form.block_id, params: { ...form.params }, index_code: form.index_code, on_fail: form.on_fail }
}

export interface StrategyForm {
  formula: string
  conditions: Condition[]
  universe: Universe
  rank: RankFactorForm[]
  trade: TradeForm
  timing: TimingForm
}

/** 页面上填的内容 → 要保存的策略内容；有填错的地方返回说明 */
export function configFromForm(form: StrategyForm, rankBlocks: Block[]): Parsed<StrategyConfig> {
  if (!form.conditions.some(c => c.enabled)) return fail('至少要启用一个选股条件。先粘贴公式并点“解析公式”。')
  const u = form.universe
  if (u.min_float_cap != null && u.max_float_cap != null && u.min_float_cap > u.max_float_cap) {
    return fail('流通市值的下限不能比上限大。')
  }
  const rank = rankFromForm(form.rank, rankBlocks)
  if (!rank.ok) return rank
  const trade = tradeFromForm(form.trade)
  if (!trade.ok) return trade
  return {
    ok: true,
    value: {
      conditions: form.conditions, universe: form.universe, rank: rank.value, trade: trade.value,
      timing: timingFromForm(form.timing), formula: form.formula,
    },
  }
}

/** 保存的策略内容 → 页面上填的内容；旧数据里缺的项用默认值 */
export function formFromConfig(config: Partial<StrategyConfig>, options: Options | undefined): StrategyForm {
  return {
    formula: config.formula ?? '',
    conditions: config.conditions ?? [],
    universe: { ...defaultUniverse(), ...(config.universe ?? {}) },
    rank: rankToForm(config.rank, options?.rank_blocks ?? []),
    trade: tradeToForm(config.trade, options?.trade_defaults),
    timing: timingToForm(config.timing, options),
  }
}

// ---------------- 设置：交易成本的显示单位 ----------------

/** 每项成本在页面上按什么单位填：佣金按万分之几，印花税和滑点按千分之几，最低佣金按元 */
export const COST_UNITS: Record<CostKey, { scale: number; unit: string }> = {
  commission: { scale: 10_000, unit: '万分之' },
  min_commission: { scale: 1, unit: '元' },
  stamp_tax: { scale: 1000, unit: '千分之' },
  slippage: { scale: 1000, unit: '千分之' },
}

export function costInput(key: CostKey, value: number): string {
  return String(Number((value * COST_UNITS[key].scale).toFixed(6)))
}

/** 0.00025 → "万分之 2.5"；5 → "5 元" */
export function costText(key: CostKey, value: number): string {
  const { unit } = COST_UNITS[key]
  return unit === '元' ? `${costInput(key, value)} 元` : `${unit} ${costInput(key, value)}`
}

export function costFromInput(key: CostKey, raw: string, limit: { min: number; max: number; label: string }): Parsed<number> {
  const shown = numberOrNull(raw)
  const range = `${costText(key, limit.min)} 到 ${costText(key, limit.max)}`
  if (shown == null || !Number.isFinite(shown)) return fail(`${limit.label}要填数字（${range}）。`)
  const value = Number((shown / COST_UNITS[key].scale).toFixed(8))
  if (value < limit.min || value > limit.max) return fail(`${limit.label}要在 ${range} 之间。`)
  return { ok: true, value }
}

export const MIN_INITIAL_CASH = 10_000
export const MIN_CAPITAL = 1000
export const MAX_MONEY = 1e10

/** 模拟账户初始资金：1 万到 100 亿 */
export function initialCashError(raw: string): string | null {
  const value = numberOrNull(raw)
  return value != null && Number.isFinite(value) && value >= MIN_INITIAL_CASH && value <= MAX_MONEY
    ? null : '模拟账户初始资金要在 1 万到 100 亿之间。'
}

/** 实际投入的资金：1000 元到 100 亿 */
export function capitalError(raw: string): string | null {
  const value = numberOrNull(raw)
  return value != null && Number.isFinite(value) && value >= MIN_CAPITAL && value <= MAX_MONEY
    ? null : '实际投入的资金要在 1000 元到 100 亿之间。'
}

// ---------------- 实际持仓：记一笔成交的对话框 ----------------

/** 成交记录对话框的几种用法 */
export type TradeMode = 'confirm-buy' | 'confirm-sell' | 'add' | 'edit' | 'sell'

/** 对话框里填的内容，都是输入框里的字 */
export interface TradeDialogForm {
  code: string
  shares: string
  price: string
  date: string
}

export const TRADE_TITLE: Record<TradeMode, string> = {
  'confirm-buy': '确认已买入',
  'confirm-sell': '确认已卖出',
  add: '手动添加一笔持仓',
  edit: '修改这笔持仓',
  sell: '记一笔卖出',
}

/** 这种用法要不要填股票代码、股数 */
export const needsCode = (mode: TradeMode): boolean => mode === 'add'
export const needsShares = (mode: TradeMode): boolean => mode !== 'confirm-sell' && mode !== 'sell'
export const isSellMode = (mode: TradeMode): boolean => mode === 'confirm-sell' || mode === 'sell'

/** 打开对话框时预填的内容 */
export function initialTradeForm(mode: TradeMode, today: string, todo?: RealTodo, position?: RealPosition): TradeDialogForm {
  if (todo) {
    return { code: todo.code, shares: todo.shares > 0 ? String(todo.shares) : '', price: String(todo.price), date: todo.default_date }
  }
  if (position && mode === 'edit') {
    return { code: position.code, shares: String(position.shares), price: String(position.buy_price), date: position.buy_date }
  }
  if (position) return { code: position.code, shares: String(position.shares), price: '', date: today }
  return { code: '', shares: '', price: '', date: today }
}

/** 填的内容有问题时返回说明，没问题返回 null */
export function tradeFormError(mode: TradeMode, form: TradeDialogForm, today: string): string | null {
  if (needsCode(mode) && !/^\d{6}$/.test(form.code.trim())) return '请填 6 位股票代码，例如 600000'
  const shares = numberOrNull(form.shares)
  if (needsShares(mode) && !(shares != null && Number.isInteger(shares) && shares > 0)) return '股数要是大于 0 的整数'
  const price = numberOrNull(form.price)
  if (!(price != null && Number.isFinite(price) && price > 0)) return '请填成交价'
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.date)) return '请选成交日期'
  if (form.date > today) return '成交日期不能是将来的日子'
  return null
}

/** 检查过的表单 → 要提交的数 */
export function tradeInput(form: TradeDialogForm): TradeInput & { code: string } {
  return { code: form.code.trim(), shares: Number(form.shares) || 0, price: Number(form.price), date: form.date }
}

/** 待确认的提醒有几条、其中几条不是最新一天的 */
export function pendingText(todos: Pick<RealTodo, 'date'>[], latest: string | null): string {
  if (todos.length === 0) return '没有待确认的提醒'
  const old = todos.filter(t => latest !== null && t.date < latest).length
  return old > 0 ? `有 ${todos.length} 条待确认，其中 ${old} 条是以前的` : `有 ${todos.length} 条待确认`
}

/** 本地日期，YYYY-MM-DD */
export function todayText(now: Date = new Date()): string {
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

// ---------------- 回测的净值曲线 ----------------

/** 几条曲线合起来的最小值、最大值（上下各留一点空）；全是空值时返回 [0, 1] */
export function curveRange(series: (number | null)[][]): [number, number] {
  let lo = Infinity
  let hi = -Infinity
  for (const values of series) {
    for (const v of values) {
      if (v == null || !Number.isFinite(v)) continue
      if (v < lo) lo = v
      if (v > hi) hi = v
    }
  }
  if (lo > hi) return [0, 1]
  const pad = (hi - lo) * 0.05 || Math.abs(hi) * 0.05 || 0.05
  return [lo - pad, hi + pad]
}

/** 一条曲线 → SVG 路径；空值处断开。x 从 0 到 width，y 向下为小 */
export function linePath(values: (number | null)[], lo: number, hi: number, width: number, height: number): string {
  const n = values.length
  const span = hi - lo || 1
  const parts: string[] = []
  let pen = false
  values.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) { pen = false; return }
    const x = n > 1 ? (i / (n - 1)) * width : width / 2
    const y = height - ((v - lo) / span) * height
    parts.push(`${pen ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`)
    pen = true
  })
  return parts.join('')
}
