/** 通达信公式扩展专用的端点和类型。 */
import { ApiError } from '@/lib/api'

export interface ParamSpec {
  key: string
  label: string
  default: number
  min: number
  max: number
  step: number
  unit: string
  grid?: number[]
}

/** 一个选股条件：粘贴公式拆出来的带着自己的公式（pasted），指标库里的只记编号（block_id） */
export interface Condition {
  pasted?: { name: string; description: string; formula: string; params_spec: ParamSpec[] }
  block_id?: string
  enabled: boolean
  params: Record<string, number>
}

export interface Universe {
  exclude_st: boolean
  exclude_star: boolean
  exclude_chinext: boolean
  exclude_bse: boolean
  exclude_suspended: boolean
  /** 流通市值的下限、上限，单位亿元；null 表示不限 */
  min_float_cap: number | null
  max_float_cap: number | null
  exclude_industries: string[]
}

export interface ParsedCondition {
  name: string
  description: string
  formula: string
  params: ParamSpec[]
}

export interface ParseResult {
  conditions: ParsedCondition[]
  universe: Universe
  warnings: string[]
}

export interface Status {
  has_data: boolean
  /** 行情存在本机的哪个文件夹 */
  data_dir: string
  first_date: string | null
  latest_date: string | null
  stock_count: number
  /** 历史行情是否已经全部下完 */
  download_finished: boolean
  st_known: boolean
  float_shares_ready: boolean
  costs: { commission: number; stamp_tax: number; slippage: number }
}

/** 一个数据来源的限速设置 */
export interface SourcePolicy {
  enabled: boolean
  max_concurrent: number
  /** 0 表示不限 */
  daily_request_cap: number
  min_interval_seconds: number
  jitter_seconds: number
  timeout_seconds: number
  max_retries: number
  stop_after_consecutive_failures: number
  cooldown_minutes_on_reject: number
  count_pages_as_requests: boolean
  official_daily_limit: number | null
  official_max_concurrent: number | null
}

/** 页面上可以改的限速项（并发数、翻页是否计数和官方上限不在这里改） */
export interface SourcePolicyUpdate {
  enabled: boolean
  daily_request_cap: number
  min_interval_seconds: number
  jitter_seconds: number
  timeout_seconds: number
  max_retries: number
  stop_after_consecutive_failures: number
  cooldown_minutes_on_reject: number
}

export type SourceState = 'ok' | 'cooldown' | 'cap_reached' | 'disabled'

export interface DataSourceInfo {
  name: string
  label: string
  state: SourceState
  state_message: string | null
  cooldown_until: string | null
  today_requests: number
  policy: SourcePolicy
  defaults: SourcePolicy
}

export interface DataSources {
  sources: DataSourceInfo[]
  /** 每一类数据依次试哪些来源 */
  priority: Record<string, string[]>
  /** 取数用的两个库（akshare、baostock）是否已经装好 */
  libraries_ready: boolean
  /** 只在保存限速设置后返回：可以保存、但需要提醒的事 */
  warnings?: string[]
}

export interface DataProgress {
  running: boolean
  kind: 'download' | 'update' | null
  stage: string | null
  done: number
  total: number
  eta_seconds: number | null
  message: string | null
}

export interface DataSchedule {
  run_time: string
  run_time_min: string
  run_time_max: string
  /** 行情更新完以后，是否自动让 TSP 接着同步一次 */
  auto_sync_tsp: boolean
  /** 最近一次自动让 TSP 同步的情况；还没有过时为 null */
  tsp_sync_message: string | null
}

/** 行业和概念（写进 TSP 的行业表、概念表）的更新情况 */
export interface BoardsInfo {
  running: boolean
  page: number
  pages: number
  updated_at: string | null
  stocks_with_industry: number | null
  stocks_with_concept: number | null
  industries: number | null
  concepts: number | null
  message: string | null
  refresh_days: number
}

/** 实时行情：两轮全市场快照之间隔多少秒 */
export interface RealtimeSetting {
  interval_seconds: number
  interval_min: number
  interval_max: number
  interval_default: number
}

export interface StatRow {
  entry: 'next_open' | 'same_close'
  hold: number
  count: number
  win_rate: number | null
  avg_return: number | null
  median_return: number | null
  avg_drawdown: number | null
  worst_drawdown: number | null
  skipped_unbuyable: number
}

export interface StatsResult {
  frequency: { avg_per_day: number; empty_day_ratio: number; per_year: number; total: number }
  stats: StatRow[]
  range: { start: string; end: string }
  st_known: boolean
}

export interface SignalItem {
  date: string
  code: string
  name: string
  returns: Record<string, number | null>
}

export interface Pick {
  code: string
  name: string
  close: number
  change: number | null
  float_cap: number | null
  industry: string
}

/** 指标库里的一个积木：排名因子、择时条件都是它 */
export interface Block {
  id: string
  kind: 'condition' | 'rank' | 'timing'
  name: string
  description: string
  formula: string
  params: ParamSpec[]
  source: string
  category: string
}

/** 买卖规则。比例都是小数：0.1 表示 10%；null 表示不用这一条（position_pct 为 null 表示均分） */
export interface TradeRules {
  max_positions: number
  position_pct: number | null
  max_hold_days: number | null
  take_profit: number | null
  stop_loss: number | null
}

export type Direction = 'desc' | 'asc'
export type OnFail = 'stop_buy' | 'clear'

export interface RankFactor {
  block_id: string
  params: Record<string, number>
  weight: number
  direction: Direction
}

export interface RankConfig {
  factors: RankFactor[]
}

export interface TimingConfig {
  block_id: string
  params: Record<string, number>
  index_code: string
  on_fail: OnFail
}

/** 一个策略的全部内容；formula 是粘贴的公式原文 */
export interface StrategyConfig {
  conditions: Condition[]
  universe: Universe | null
  rank: RankConfig | null
  trade: TradeRules | null
  timing: TimingConfig | null
  formula: string
}

export interface Options {
  rank_blocks: Block[]
  timing_blocks: Block[]
  indexes: { code: string; name: string }[]
  trade_defaults: TradeRules
}

export type StrategyStatus = 'draft' | 'running' | 'stopped'

export interface StrategySummary {
  id: string
  name: string
  status: StrategyStatus
  updated_at: string
  condition_count: number
  backtest: { annual_return: number | null; max_drawdown: number | null; created_at: string | null } | null
  live: { total_return: number; started_on: string; last_date: string | null } | null
  real_return: number | null
}

export interface StrategyDetail {
  id: string
  name: string
  status: StrategyStatus
  config: Partial<StrategyConfig>
  created_at: string
  updated_at: string
}

export interface BacktestSummary {
  annual_return: number | null
  total_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  win_rate: number | null
  trade_count: number
  avg_hold_days: number | null
  final_equity: number | null
  initial_cash: number | null
  benchmark_annual_return: number | null
  benchmark_total_return: number | null
  /** 大盘择时成立的日子占多少；没有设置择时为 null */
  ok_day_ratio: number | null
}

export interface BacktestTrade {
  code: string
  name: string
  buy_date: string
  buy_price: number | null
  sell_date: string | null
  sell_price: number | null
  shares: number
  return: number | null
  pnl: number | null
  hold_days: number
  reason: string
  /** 回测结束时还持有 */
  open: boolean
}

export interface BacktestResult {
  summary: BacktestSummary
  /** equity、benchmark 都是净值（起点为 1）；指数还没有数据的日子为 null */
  curve: { dates: string[]; equity: (number | null)[]; benchmark: (number | null)[] }
  yearly: { year: number; strategy: number | null; benchmark: number | null }[]
  trades: BacktestTrade[]
  trade_count: number
  range: { start: string; end: string }
  benchmark: { code: string; name: string }
  assumptions: string[]
  created_at: string
}

export interface TimingState {
  enabled: boolean
  ok: boolean
}

export interface ReminderBuy {
  code: string
  name: string
  rank: number
  score: number
  reason: string
  reference_only: boolean
}

export interface ReminderSell {
  code: string
  name: string
  reason: string
  gain: number
}

export interface PaperPosition {
  code: string
  name: string
  buy_date: string
  buy_price: number
  shares: number
  cost: number
  value: number
  gain: number
  hold_days: number
}

/** 模拟账户某一天收盘后生成的提醒 */
export interface Reminder {
  date: string
  timing: TimingState
  buys: ReminderBuy[]
  sells: ReminderSell[]
  notes: string[]
  positions: PaperPosition[]
  candidate_count: number
  account: { equity: number; cash: number; initial_cash: number; total_return: number; started_on: string }
}

export interface RealTodo {
  id: number
  strategy_id: string
  date: string
  side: 'buy' | 'sell'
  code: string
  name: string
  /** 建议股数；0 表示资金不够买一手 */
  shares: number
  /** 提醒那天的收盘价 */
  price: number
  reason: string
  status: string
  default_date: string
}

export interface RealPosition {
  id: number
  code: string
  name: string
  buy_date: string
  buy_price: number
  shares: number
  cost: number
  value: number
  gain: number
  /** 0 表示行情还没有更新到买入那天 */
  hold_days: number
  sell_reason: string | null
}

export interface RealTrade {
  id: number
  code: string
  name: string
  buy_date: string
  buy_price: number
  sell_date: string
  sell_price: number
  shares: number
  return: number
  pnl: number
  reason: string
}

/** 实际持仓的全部内容。每个实际持仓的接口都返回最新的这一份 */
export interface RealView {
  enabled: boolean
  capital: number
  cash: number
  equity: number
  total_return: number
  date: string | null
  timing: TimingState
  positions: RealPosition[]
  todos: RealTodo[]
  trades: RealTrade[]
}

export interface PushChannels {
  feishu: boolean
  wecom: boolean
}

export interface LastRun {
  due_date: string
  started_at: string
  finished_at: string | null
  ok: boolean | null
  data_date: string | null
  pushed: boolean
  message: string | null
}

export interface TodayStrategy {
  strategy_id: string
  name: string
  reminder: Reminder | null
  real: RealView | null
  /** 这份提醒比最新的行情旧 */
  behind: boolean
}

export interface Today {
  latest_date: string | null
  strategies: TodayStrategy[]
  last_run: LastRun | null
  push: PushChannels
}

export interface DailyRunResult {
  ok: boolean
  data_date: string | null
  pushed: boolean
  message: string
}

export type CostKey = 'commission' | 'min_commission' | 'stamp_tax' | 'slippage'

export interface Settings {
  costs: Record<CostKey, number>
  cost_limits: Record<CostKey, { min: number; max: number; label: string }>
  cost_defaults: Record<CostKey, number>
  initial_cash: number
  push: PushChannels
}

export interface TradeInput {
  shares: number
  price: number
  date: string
}

export interface ConditionsBody {
  conditions: Condition[]
  universe: Universe
}

const BASE = '/api/custom/tdx-formula'
const seg = encodeURIComponent

async function request<T>(endpoint: string, method = 'GET', body?: unknown, timeoutMs = 120_000): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${BASE}/${endpoint}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = payload?.detail
      // 公式错误的 detail 是一个对象（带行号和函数名），其余是一句话
      const message = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map(v => v.msg).join('；')
        : typeof detail?.message === 'string' ? detail.message
        : `请求失败 (${response.status})`
      throw new ApiError(response.status === 401 ? `未登录 (401)：${message}` : message, response.status)
    }
    return payload as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('等了太久没有结果。全市场数据第一次读取要十几秒，请再试一次。', 0)
    }
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

export const tdxApi = {
  status: () => request<Status>('status'),

  dataSources: () => request<DataSources>('data/sources'),
  updateDataSource: (name: string, body: SourcePolicyUpdate) =>
    request<DataSources>(`data/sources/${seg(name)}`, 'PUT', body),
  dataProgress: () => request<DataProgress>('data/progress'),
  /** 开始或继续下载历史行情；start 不填时从默认的起始日期开始 */
  startDownload: (start?: string) => request<DataProgress>('data/download', 'POST', { start: start || null }),
  stopDownload: () => request<{ ok: boolean }>('data/stop', 'POST'),
  startUpdate: () => request<DataProgress>('data/update', 'POST'),
  /** 从已有的行情目录拷一份过来；文件多的时候要几分钟 */
  importData: (path: string) => request<Status & { copied: number }>('data/import', 'POST', { path }, 600_000),
  dataBoards: () => request<BoardsInfo>('data/boards'),
  updateDataBoards: () => request<BoardsInfo>('data/boards/update', 'POST'),
  dataRealtime: () => request<RealtimeSetting>('data/realtime'),
  setDataRealtime: (interval_seconds: number) => request<RealtimeSetting>('data/realtime', 'PUT', { interval_seconds }),
  dataSchedule: () => request<DataSchedule>('data/schedule'),
  setDataSchedule: (run_time: string) => request<DataSchedule>('data/schedule', 'PUT', { run_time }),
  setAutoSyncTsp: (auto_sync_tsp: boolean) => request<DataSchedule>('data/schedule', 'PUT', { auto_sync_tsp }),

  parse: (formula: string) => request<ParseResult>('parse', 'POST', { formula }),
  stats: (body: ConditionsBody) => request<StatsResult>('stats', 'POST', body),
  signals: (body: ConditionsBody, limit = 50) =>
    request<{ total: number; items: SignalItem[] }>('signals', 'POST', { ...body, limit }),
  picks: (body: ConditionsBody) => request<{ date: string; items: Pick[] }>('picks', 'POST', body),

  options: () => request<Options>('options'),
  strategies: () => request<StrategySummary[]>('strategies'),
  strategy: (id: string) => request<StrategyDetail>(`strategies/${seg(id)}`),
  createStrategy: (name: string, config: StrategyConfig) =>
    request<StrategyDetail>('strategies', 'POST', { name, config }),
  updateStrategy: (id: string, name: string, config: StrategyConfig) =>
    request<StrategyDetail>(`strategies/${seg(id)}`, 'PUT', { name, config }),
  deleteStrategy: (id: string) => request<{ ok: boolean }>(`strategies/${seg(id)}`, 'DELETE'),
  startStrategy: (id: string) => request<StrategyDetail>(`strategies/${seg(id)}/start`, 'POST'),
  stopStrategy: (id: string) => request<StrategyDetail>(`strategies/${seg(id)}/stop`, 'POST'),

  /** 组合回测要十几秒到一分钟，多等一会儿 */
  backtest: (config: StrategyConfig, strategy_id?: string) =>
    request<BacktestResult>('backtest', 'POST', { ...config, strategy_id: strategy_id ?? null }, 300_000),
  /** 这个策略上次保存的回测结果；还没有回测过返回 null */
  savedBacktest: async (id: string): Promise<BacktestResult | null> => {
    try {
      return await request<BacktestResult>(`strategies/${seg(id)}/backtest`)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  },

  today: () => request<Today>('today'),
  runDaily: () => request<DailyRunResult>('daily/run', 'POST', undefined, 300_000),

  setReal: (id: string, enabled: boolean, capital?: number) =>
    request<RealView>(`strategies/${seg(id)}/real`, 'PUT', { enabled, capital: capital ?? null }),
  skipAllTodos: (id: string) => request<RealView>(`strategies/${seg(id)}/real/skip-all`, 'POST'),
  addRealPosition: (id: string, body: TradeInput & { code: string }) =>
    request<RealView>(`strategies/${seg(id)}/real/positions`, 'POST', body),
  /** 答复一条提醒：照做了（done，带实际的价格、日期，买入还带股数），或者没做 */
  answerTodo: (todoId: number, done: boolean, trade?: { price: number; date: string; shares?: number }) =>
    request<RealView>(`real/todos/${todoId}`, 'POST', { done, ...trade }),
  editRealPosition: (positionId: number, body: TradeInput) =>
    request<RealView>(`real/positions/${positionId}`, 'PUT', body),
  sellRealPosition: (positionId: number, body: { price: number; date: string }) =>
    request<RealView>(`real/positions/${positionId}/sell`, 'POST', body),
  /** 删除一笔持仓或一笔已卖出的交易记录（两者是同一张表里的记录） */
  deleteRealPosition: (positionId: number) => request<RealView>(`real/positions/${positionId}`, 'DELETE'),

  settings: () => request<Settings>('settings'),
  updateSettings: (body: { costs?: Partial<Record<CostKey, number>>; initial_cash?: number }) =>
    request<Settings>('settings', 'PUT', body),
}

const ROOT = ['custom', 'tdx-formula'] as const

export const tdxKeys = {
  status: [...ROOT, 'status'] as const,
  options: [...ROOT, 'options'] as const,
  strategies: [...ROOT, 'strategies'] as const,
  strategy: (id: string) => [...ROOT, 'strategy', id] as const,
  backtest: (id: string) => [...ROOT, 'backtest', id] as const,
  today: [...ROOT, 'today'] as const,
  settings: [...ROOT, 'settings'] as const,
  dataSources: [...ROOT, 'data', 'sources'] as const,
  dataProgress: [...ROOT, 'data', 'progress'] as const,
  dataSchedule: [...ROOT, 'data', 'schedule'] as const,
  dataRealtime: [...ROOT, 'data', 'realtime'] as const,
  dataBoards: [...ROOT, 'data', 'boards'] as const,
}
