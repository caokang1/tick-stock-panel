import { useId, useState, type FormEvent } from 'react'
import { Modal } from '@/components/Modal'
import type { RealPosition, RealTodo, TradeInput } from './client'
import {
  TRADE_TITLE, capitalError, initialTradeForm, isSellMode, money, needsCode, needsShares, todayText, toPanelSymbol, tradeFormError,
  tradeInput, type TradeDialogForm, type TradeMode,
} from './logic'
import { BUTTON, ErrorNote, INPUT, PRIMARY } from './ui'

const ROW = 'grid grid-cols-[84px_1fr] items-center gap-2 text-xs text-secondary'
const PANEL = 'w-[92vw] max-w-md rounded-card border border-border bg-surface p-4 shadow-xl'

/**
 * 记一笔实际成交：确认提醒、手动添加、修改、卖出都用这一个对话框，只是要填的项不同。
 * 每次打开都是新的一份（由外面按需渲染），不会带着上一次填的内容。
 */
export function TradeDialog({ mode, todo, position, saving, onSubmit, onClose }: {
  mode: TradeMode
  todo?: RealTodo
  position?: RealPosition
  saving: boolean
  onSubmit: (input: TradeInput & { code: string }) => void
  onClose: () => void
}) {
  const titleId = useId()
  const today = todayText()
  const [form, setForm] = useState<TradeDialogForm>(() => initialTradeForm(mode, today, todo, position))
  const [error, setError] = useState<string | null>(null)
  const set = (key: keyof TradeDialogForm) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setForm(f => ({ ...f, [key]: e.target.value }))
    setError(null)
  }

  const sell = isSellMode(mode)
  const stock = todo ?? position
  const fixedShares = todo?.shares ?? position?.shares
  const shares = needsShares(mode) ? Number(form.shares) : fixedShares
  const amount = shares && Number(form.price) > 0 ? shares * Number(form.price) : null

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const problem = tradeFormError(mode, form, today)
    setError(problem)
    if (!problem) onSubmit(tradeInput(form))
  }

  return (
    <Modal onClose={onClose} labelledBy={titleId} closeOnBackdrop={false} panelClassName={PANEL}>
      <form onSubmit={submit} className="space-y-3" noValidate>
        <h3 id={titleId} className="text-sm font-medium text-foreground">{TRADE_TITLE[mode]}</h3>
        {needsCode(mode) ? (
          <label className={ROW}>
            股票代码
            <input value={form.code} onChange={set('code')} placeholder="6 位代码，例如 600000" inputMode="numeric" maxLength={6} className={INPUT} />
          </label>
        ) : (
          <div className={ROW}>
            股票
            <span className="text-foreground">{stock ? `${stock.name || stock.code}（${toPanelSymbol(stock.code)}）` : ''}</span>
          </div>
        )}
        {needsShares(mode) ? (
          <label className={ROW}>
            股数
            <span className="flex items-center gap-1.5">
              <input type="number" min={1} step={100} value={form.shares} onChange={set('shares')} className={`${INPUT} w-36`} />股
            </span>
          </label>
        ) : (
          <div className={ROW}>
            股数
            <span className="text-foreground">{fixedShares?.toLocaleString('en-US')} 股（全部卖出）</span>
          </div>
        )}
        <label className={ROW}>
          {sell ? '卖出价' : '买入价'}
          <span className="flex items-center gap-1.5">
            <input type="number" min={0.01} step={0.01} value={form.price} onChange={set('price')} className={`${INPUT} w-36`} />元
          </span>
        </label>
        <label className={ROW}>
          {sell ? '卖出日期' : '买入日期'}
          <input type="date" max={today} value={form.date} onChange={set('date')} className={`${INPUT} w-36`} />
        </label>
        {amount != null && (
          <p className="text-[11px] text-muted">成交金额约 {money(amount)} 元，佣金和印花税按“设置”里的交易成本自动计算。</p>
        )}
        {todo && (
          <p className="text-[11px] text-muted">预填的价格是 {todo.date} 的收盘价，只作参考，请改成你实际成交的价格。</p>
        )}
        {error && <ErrorNote>{error}</ErrorNote>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className={BUTTON} onClick={onClose}>取消</button>
          <button type="submit" className={PRIMARY} disabled={saving}>{saving ? '正在保存…' : '确定'}</button>
        </div>
      </form>
    </Modal>
  )
}

/** 填实际投入了多少钱：第一次打开实际持仓、以后修改投入资金都用它 */
export function CapitalDialog({ title, initial, saving, onSubmit, onClose }: {
  title: string
  initial?: number
  saving: boolean
  onSubmit: (capital: number) => void
  onClose: () => void
}) {
  const titleId = useId()
  const [text, setText] = useState(initial == null ? '' : String(initial))
  const [error, setError] = useState<string | null>(null)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const problem = capitalError(text)
    setError(problem)
    if (!problem) onSubmit(Number(text))
  }

  return (
    <Modal onClose={onClose} labelledBy={titleId} closeOnBackdrop={false} panelClassName={PANEL}>
      <form onSubmit={submit} className="space-y-3" noValidate>
        <h3 id={titleId} className="text-sm font-medium text-foreground">{title}</h3>
        <label className="block space-y-1.5 text-xs text-secondary">
          <span className="block">这个策略实际投入了多少钱（元）？追加或取出资金后可以再改。</span>
          <span className="flex items-center gap-1.5">
            <input
              type="number" min={1000} step={1000} value={text}
              onChange={e => { setText(e.target.value); setError(null) }}
              placeholder="例如 100000"
              className={`${INPUT} w-44`}
            />元
          </span>
        </label>
        <p className="text-[11px] text-muted">
          之后的买入提醒会按这笔钱和你记下的持仓来算该买多少股。模拟账户不受影响，继续按“每条提醒都照做”来衡量策略本身。
        </p>
        {error && <ErrorNote>{error}</ErrorNote>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className={BUTTON} onClick={onClose}>取消</button>
          <button type="submit" className={PRIMARY} disabled={saving}>{saving ? '正在保存…' : '保存'}</button>
        </div>
      </form>
    </Modal>
  )
}
