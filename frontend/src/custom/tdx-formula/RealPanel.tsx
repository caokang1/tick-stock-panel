import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { toast } from '@/components/Toast'
import { tdxApi, tdxKeys, type RealPosition, type RealTodo, type RealView, type TradeInput } from './client'
import { money, pct, pendingText, tone, type TradeMode } from './logic'
import { CapitalDialog, TradeDialog } from './TradeDialog'
import {
  BODY_ROW, BUTTON, HEAD_ROW, LINK, LINK_DANGER, LINK_MUTED, StockName, TH, Tag, WarnNote, errorText,
} from './ui'

type Dialog =
  | { kind: 'trade'; mode: TradeMode; todo?: RealTodo; position?: RealPosition }
  | { kind: 'capital' }

/** 一个策略的实际持仓：待确认的提醒、真实持仓、已卖出的交易。每次操作后重新取今日提醒。 */
export function RealPanel({ strategyId, real }: { strategyId: string; real: RealView }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [dialog, setDialog] = useState<Dialog | null>(null)

  /** 跑一个操作：成功后提示并重新取今日提醒，失败时把原因显示出来 */
  async function run(action: () => Promise<RealView>, done: string): Promise<boolean> {
    setBusy(true)
    try {
      await action()
      toast(done, 'success')
      void qc.invalidateQueries({ queryKey: tdxKeys.strategies })
      await qc.invalidateQueries({ queryKey: tdxKeys.today })
      return true
    } catch (e) {
      toast(errorText(e))
      return false
    } finally {
      setBusy(false)
    }
  }

  async function onTrade(input: TradeInput & { code: string }) {
    if (dialog?.kind !== 'trade') return
    const { mode, todo, position } = dialog
    const { price, date, shares } = input
    let ok = false
    if (mode === 'confirm-buy' && todo) {
      ok = await run(() => tdxApi.answerTodo(todo.id, true, { price, shares, date }), '已记为买入')
    } else if (mode === 'confirm-sell' && todo) {
      ok = await run(() => tdxApi.answerTodo(todo.id, true, { price, date }), '已记为卖出')
    } else if (mode === 'add') {
      ok = await run(() => tdxApi.addRealPosition(strategyId, input), '已添加')
    } else if (mode === 'edit' && position) {
      ok = await run(() => tdxApi.editRealPosition(position.id, { shares, price, date }), '已修改')
    } else if (mode === 'sell' && position) {
      ok = await run(() => tdxApi.sellRealPosition(position.id, { price, date }), '已记为卖出')
    }
    if (ok) setDialog(null)
  }

  const skip = (todo: RealTodo) =>
    void run(() => tdxApi.answerTodo(todo.id, false), todo.side === 'buy' ? '已记为没买' : '已记为没卖')
  const skipAll = () => {
    if (window.confirm(`把 ${real.todos.length} 条待确认的提醒都记为“没做”？`)) {
      void run(() => tdxApi.skipAllTodos(strategyId), '已全部记为没做')
    }
  }
  const remove = (id: number, what: string) => {
    if (window.confirm(`删除${what}？它对可用资金的影响也会一起撤回。只在记错的时候用。`)) {
      void run(() => tdxApi.deleteRealPosition(id), '已删除')
    }
  }
  const turnOff = () => {
    if (window.confirm('关闭实际持仓？关闭后，这个策略的提醒恢复按模拟账户生成。已经记下的持仓和交易都会留着，重新打开时接着用。')) {
      void run(() => tdxApi.setReal(strategyId, false), '已关闭实际持仓')
    }
  }

  return (
    <div className="space-y-3 rounded-card border border-accent/30 bg-accent/5 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-secondary">
          <b className="text-sm font-medium text-foreground">我的实际持仓</b>
          <span>累计 <b className={`font-medium tabular-nums ${tone(real.total_return)}`}>{pct(real.total_return)}</b></span>
          <span>总资产 <span className="tabular-nums text-foreground">{money(real.equity)}</span> 元</span>
          <span className={real.cash < 0 ? 'text-warning' : ''}>
            可用资金 <span className={`tabular-nums ${real.cash < 0 ? '' : 'text-foreground'}`}>{money(real.cash)}</span> 元
          </span>
          <span className="text-muted">投入 {money(real.capital)} 元</span>
          {real.date && <span className="text-muted">按 {real.date} 收盘计算</span>}
        </div>
        <div className="flex gap-3">
          <button className={LINK} disabled={busy} onClick={() => setDialog({ kind: 'capital' })}>修改投入资金</button>
          <button className={LINK_MUTED} disabled={busy} onClick={turnOff}>关闭实际持仓</button>
        </div>
      </div>
      {real.cash < 0 && (
        <WarnNote>可用资金是负数：记下的买入超过了你填的投入资金。请核对成交记录，或者修改投入资金。</WarnNote>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
        <h4 className="flex items-center gap-2 text-xs font-medium text-foreground">
          待确认的提醒
          <Tag tone={real.todos.length ? 'danger' : 'muted'}>{pendingText(real.todos, real.date)}</Tag>
        </h4>
        {real.todos.length > 1 && <button className={BUTTON} disabled={busy} onClick={skipAll}>全部没做</button>}
      </div>
      <p className="text-[11px] text-muted">
        这些提醒是按你的实际持仓和可用资金算的。照做了点“已买入 / 已卖出”并填上实际成交价；没做点“没买 / 没卖”。
        <b className="font-medium text-secondary">没有答复的一律当作没有成交。</b>
      </p>
      {real.timing.enabled && !real.timing.ok && <p className="text-[11px] text-warning">大盘择时建议空仓，今天没有新的买入提醒。</p>}
      {real.todos.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-right text-xs">
            <thead>
              <tr className={HEAD_ROW}>
                <th className={`${TH} text-left`}>提醒日期</th>
                <th className={`${TH} text-left`}>方向</th>
                <th className={`${TH} text-left`}>股票</th>
                <th className={TH}>建议股数</th>
                <th className={TH}>当天收盘价</th>
                <th className={`${TH} pl-4 text-left`}>原因</th>
                <th className={`${TH} pl-4 text-left`}>操作</th>
              </tr>
            </thead>
            <tbody>
              {real.todos.map(t => (
                <tr key={t.id} className={BODY_ROW}>
                  <td className="py-2 text-left text-secondary">{t.date}</td>
                  <td className="text-left"><Tag tone={t.side === 'buy' ? 'accent' : 'warning'}>{t.side === 'buy' ? '买入' : '卖出'}</Tag></td>
                  <td className="text-left"><StockName code={t.code} name={t.name} list={real.todos} /></td>
                  <td className="tabular-nums">
                    {t.shares > 0 ? t.shares.toLocaleString('en-US')
                      : <span className="text-muted" title="按每只的仓位比例和你的可用资金，不够买一手">资金不够一手</span>}
                  </td>
                  <td className="tabular-nums">{t.price.toFixed(2)}</td>
                  <td className="pl-4 text-left text-secondary">{t.reason}</td>
                  <td className="space-x-3 whitespace-nowrap pl-4 text-left">
                    <button
                      className={LINK}
                      disabled={busy}
                      onClick={() => setDialog({ kind: 'trade', mode: t.side === 'buy' ? 'confirm-buy' : 'confirm-sell', todo: t })}
                    >
                      {t.side === 'buy' ? '已买入' : '已卖出'}
                    </button>
                    <button className={LINK_MUTED} disabled={busy} onClick={() => skip(t)}>{t.side === 'buy' ? '没买' : '没卖'}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
        <h4 className="text-xs font-medium text-foreground">实际持仓</h4>
        <button className={BUTTON} disabled={busy} onClick={() => setDialog({ kind: 'trade', mode: 'add' })}>
          <Plus className="h-3.5 w-3.5" />手动添加持仓
        </button>
      </div>
      {real.positions.length === 0 ? <p className="text-[11px] text-muted">还没有记下任何持仓。</p> : (
        <div className="overflow-x-auto">
          <table className="w-full text-right text-xs">
            <thead>
              <tr className={HEAD_ROW}>
                <th className={`${TH} text-left`}>股票</th>
                <th className={`${TH} text-left`}>买入日</th>
                <th className={TH}>买入价</th>
                <th className={TH}>股数</th>
                <th className={TH}>市值（元）</th>
                <th className={TH}>盈亏</th>
                <th className={TH}>持有天数</th>
                <th className={`${TH} pl-4 text-left`}>操作</th>
              </tr>
            </thead>
            <tbody>
              {real.positions.map(p => (
                <tr key={p.id} className={BODY_ROW}>
                  <td className="py-2 text-left">
                    <StockName code={p.code} name={p.name} list={real.positions} />
                    {p.sell_reason && <span className="ml-2"><Tag tone="warning">该卖出：{p.sell_reason}</Tag></span>}
                  </td>
                  <td className="text-left text-secondary">{p.buy_date}</td>
                  <td className="tabular-nums">{p.buy_price.toFixed(2)}</td>
                  <td className="tabular-nums">{p.shares.toLocaleString('en-US')}</td>
                  <td className="tabular-nums">{money(p.value)}</td>
                  <td className={`tabular-nums ${tone(p.gain)}`}>{pct(p.gain)}</td>
                  <td>
                    {p.hold_days > 0 ? `第 ${p.hold_days} 天`
                      : <span className="text-muted" title="行情还没有更新到买入那天，下次更新后开始计算">等行情更新</span>}
                  </td>
                  <td className="space-x-3 whitespace-nowrap pl-4 text-left">
                    <button className={LINK} disabled={busy} onClick={() => setDialog({ kind: 'trade', mode: 'edit', position: p })}>修改</button>
                    <button className={LINK} disabled={busy} onClick={() => setDialog({ kind: 'trade', mode: 'sell', position: p })}>卖出</button>
                    <button className={LINK_DANGER} disabled={busy} onClick={() => remove(p.id, '这笔持仓')}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {real.trades.length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs text-secondary hover:text-foreground">已卖出的交易（最近 {real.trades.length} 笔）</summary>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-right text-xs">
              <thead>
                <tr className={HEAD_ROW}>
                  <th className={`${TH} text-left`}>股票</th>
                  <th className={`${TH} text-left`}>买入</th>
                  <th className={`${TH} text-left`}>卖出</th>
                  <th className={TH}>股数</th>
                  <th className={TH}>收益</th>
                  <th className={TH}>盈亏（元）</th>
                  <th className={`${TH} pl-4 text-left`}>卖出原因</th>
                  <th className={`${TH} pl-4 text-left`}>操作</th>
                </tr>
              </thead>
              <tbody>
                {real.trades.map(t => (
                  <tr key={t.id} className={BODY_ROW}>
                    <td className="py-2 text-left"><StockName code={t.code} name={t.name} list={real.trades} /></td>
                    <td className="text-left text-secondary">{t.buy_date} 买入价 {t.buy_price.toFixed(2)}</td>
                    <td className="text-left text-secondary">{t.sell_date} 卖出价 {t.sell_price.toFixed(2)}</td>
                    <td className="tabular-nums">{t.shares.toLocaleString('en-US')}</td>
                    <td className={`tabular-nums ${tone(t.return)}`}>{pct(t.return)}</td>
                    <td className={`tabular-nums ${tone(t.pnl)}`}>{money(t.pnl)}</td>
                    <td className="pl-4 text-left text-secondary">{t.reason}</td>
                    <td className="pl-4 text-left">
                      <button className={LINK_DANGER} disabled={busy} onClick={() => remove(t.id, '这笔交易记录')}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {dialog?.kind === 'trade' && (
        <TradeDialog
          mode={dialog.mode} todo={dialog.todo} position={dialog.position} saving={busy}
          onSubmit={input => void onTrade(input)} onClose={() => setDialog(null)}
        />
      )}
      {dialog?.kind === 'capital' && (
        <CapitalDialog
          title="修改实际投入的资金" initial={real.capital} saving={busy}
          onSubmit={async capital => { if (await run(() => tdxApi.setReal(strategyId, true, capital), '已保存')) setDialog(null) }}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  )
}
