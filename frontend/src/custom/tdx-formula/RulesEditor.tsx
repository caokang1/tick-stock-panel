import { Plus, X } from 'lucide-react'
import type { Block, Direction, OnFail, Options, ParamSpec } from './client'
import {
  blockDefaults, newRankFactor, paramValue, tradeFromForm,
  type RankFactorForm, type TimingForm, type TradeForm,
} from './logic'
import { BUTTON, INPUT } from './ui'

const SELECT = `${INPUT} cursor-pointer pr-1`

/** 一个积木的参数：每个参数一个数字输入框 */
function ParamInputs({ specs, values, onChange, disabled }: {
  specs: ParamSpec[]
  values: Record<string, number>
  onChange: (next: Record<string, number>) => void
  disabled?: boolean
}) {
  return (
    <>
      {specs.map(p => (
        <label key={p.key} className="flex items-center gap-1.5 text-[11px] text-secondary">
          {p.label}
          <input
            type="number"
            value={values[p.key] ?? p.default}
            min={p.max > p.min ? p.min : undefined}
            max={p.max > p.min ? p.max : undefined}
            step={p.step || 'any'}
            disabled={disabled}
            onChange={e => onChange({ ...values, [p.key]: paramValue(p, e.target.value) })}
            className={`${INPUT} w-20 disabled:opacity-50`}
          />
          {p.unit}
        </label>
      ))}
    </>
  )
}

/** ⑤ 排名：同一天选出的股票太多时先买哪些 */
export function RankEditor({ blocks, factors, onChange }: {
  blocks: Block[]
  factors: RankFactorForm[]
  onChange: (next: RankFactorForm[]) => void
}) {
  const setAt = (index: number, next: RankFactorForm) => onChange(factors.map((f, i) => (i === index ? next : f)))
  const add = () => {
    const unused = blocks.find(b => !factors.some(f => f.block_id === b.id)) ?? blocks[0]
    if (unused) onChange([...factors, newRankFactor(unused)])
  }

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted">
        同一天选出的股票比空位多时，按这里的排名从前往后买。
        {factors.length === 0 && '现在没有设置：按近 5 天的平均成交额从大到小取。'}
        {factors.length > 1 && '有多项时按权重合成一个总分，权重大的那一项更说了算。'}
      </p>
      {factors.map((f, index) => {
        const block = blocks.find(b => b.id === f.block_id)
        return (
          <div key={index} className="rounded-card border border-border bg-base p-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <select
                aria-label={`第 ${index + 1} 项排名按什么排`}
                value={f.block_id}
                onChange={e => {
                  const next = blocks.find(b => b.id === e.target.value)
                  setAt(index, { ...f, block_id: e.target.value, params: blockDefaults(next) })
                }}
                className={SELECT}
              >
                {!block && <option value={f.block_id}>{f.block_id}（已不可用）</option>}
                {blocks.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
              </select>
              <ParamInputs specs={block?.params ?? []} values={f.params} onChange={params => setAt(index, { ...f, params })} />
              <select
                aria-label={`第 ${index + 1} 项排名的方向`}
                value={f.direction}
                onChange={e => setAt(index, { ...f, direction: e.target.value as Direction })}
                className={SELECT}
              >
                <option value="desc">从大到小</option>
                <option value="asc">从小到大</option>
              </select>
              <label className="flex items-center gap-1.5 text-[11px] text-secondary">
                权重
                <input
                  type="number" min={0} step="any" value={f.weight}
                  onChange={e => setAt(index, { ...f, weight: e.target.value })}
                  className={`${INPUT} w-16`}
                />
              </label>
              <button
                type="button"
                title="去掉这一项"
                aria-label={`去掉第 ${index + 1} 项排名`}
                onClick={() => onChange(factors.filter((_, i) => i !== index))}
                className="ml-auto cursor-pointer rounded p-1 text-muted hover:bg-danger/10 hover:text-danger"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            {block?.description && <p className="mt-1.5 text-[11px] text-muted">{block.description}</p>}
          </div>
        )
      })}
      <button type="button" className={BUTTON} disabled={blocks.length === 0} onClick={add}>
        <Plus className="h-3.5 w-3.5" />添加一项排名
      </button>
    </div>
  )
}

const TRADE_ROW = 'flex flex-wrap items-center gap-1.5 text-xs text-secondary'

/** ⑥ 买卖规则 */
export function TradeEditor({ form, onChange }: { form: TradeForm; onChange: (next: TradeForm) => void }) {
  const set = (key: keyof TradeForm) => (e: React.ChangeEvent<HTMLInputElement>) => onChange({ ...form, [key]: e.target.value })
  const checked = tradeFromForm(form)
  const maxPositions = Number(form.max_positions)
  const even = Number.isInteger(maxPositions) && maxPositions > 0 ? `${Number((100 / maxPositions).toFixed(1))}%` : ''

  return (
    <div className="space-y-2">
      <label className={TRADE_ROW}>
        最多同时持有
        <input type="number" min={1} max={100} step={1} value={form.max_positions} onChange={set('max_positions')} className={`${INPUT} w-20`} />
        只股票
      </label>
      <label className={TRADE_ROW}>
        每只股票用总资金的
        <input type="number" min={0} max={100} step="any" placeholder="平均分" value={form.position_pct} onChange={set('position_pct')} className={`${INPUT} w-20`} />
        %
        <span className="text-[11px] text-muted">留空表示平均分配{even && `（现在是每只 ${even}）`}</span>
      </label>
      <label className={TRADE_ROW}>
        买入后最多持有
        <input type="number" min={1} max={500} step={1} placeholder="不限" value={form.max_hold_days} onChange={set('max_hold_days')} className={`${INPUT} w-20`} />
        个交易日，到期卖出
        <span className="text-[11px] text-muted">留空表示不按天数卖</span>
      </label>
      <label className={TRADE_ROW}>
        止盈：比买入价涨了
        <input type="number" min={0} step="any" placeholder="不用" value={form.take_profit} onChange={set('take_profit')} className={`${INPUT} w-20`} />
        % 就卖出
        <span className="text-[11px] text-muted">留空表示不止盈</span>
      </label>
      <label className={TRADE_ROW}>
        止损：比买入价跌了
        <input type="number" min={0} step="any" placeholder="不用" value={form.stop_loss} onChange={set('stop_loss')} className={`${INPUT} w-20`} />
        % 就卖出
        <span className="text-[11px] text-muted">留空表示不止损</span>
      </label>
      {!checked.ok && <p role="alert" className="text-xs text-danger">{checked.error}</p>}
      <p className="text-[11px] text-muted">收盘后判断，第二个交易日开盘买入或卖出。</p>
    </div>
  )
}

/** ⑦ 大盘择时：大盘不好的时候少出手 */
export function TimingEditor({ options, form, onChange }: {
  options: Pick<Options, 'timing_blocks' | 'indexes'>
  form: TimingForm
  onChange: (next: TimingForm) => void
}) {
  const block = options.timing_blocks.find(b => b.id === form.block_id)
  const off = !form.enabled

  return (
    <div className="space-y-2">
      <label className="flex cursor-pointer items-center gap-1.5 text-xs text-secondary">
        <input
          type="checkbox"
          checked={form.enabled}
          disabled={options.timing_blocks.length === 0}
          onChange={e => onChange({ ...form, enabled: e.target.checked })}
        />
        只在大盘满足条件时才买入
        <span className="text-[11px] text-muted">不勾选表示任何时候都可以买</span>
      </label>
      {form.enabled && (
        <div className="space-y-2 rounded-card border border-border bg-base p-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-secondary">
            <label className="flex items-center gap-1.5">
              看哪个指数
              <select value={form.index_code} disabled={off} onChange={e => onChange({ ...form, index_code: e.target.value })} className={SELECT}>
                {options.indexes.map(i => <option key={i.code} value={i.code}>{i.name}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-1.5">
              条件
              <select
                value={form.block_id}
                disabled={off}
                onChange={e => {
                  const next = options.timing_blocks.find(b => b.id === e.target.value)
                  onChange({ ...form, block_id: e.target.value, params: blockDefaults(next) })
                }}
                className={SELECT}
              >
                {!block && form.block_id && <option value={form.block_id}>{form.block_id}（已不可用）</option>}
                {options.timing_blocks.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
              </select>
            </label>
            <ParamInputs specs={block?.params ?? []} values={form.params} disabled={off} onChange={params => onChange({ ...form, params })} />
          </div>
          {block?.description && <p className="text-[11px] text-muted">{block.description}</p>}
          <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-secondary">
            <legend className="float-left mr-3">条件不满足时</legend>
            {([['stop_buy', '停止买入（已有的持仓照常按规则卖）'], ['clear', '清仓（全部卖出）']] as [OnFail, string][]).map(([value, label]) => (
              <label key={value} className="flex cursor-pointer items-center gap-1.5">
                <input type="radio" name="tdx-timing-on-fail" checked={form.on_fail === value} onChange={() => onChange({ ...form, on_fail: value })} />
                {label}
              </label>
            ))}
          </fieldset>
        </div>
      )}
    </div>
  )
}
