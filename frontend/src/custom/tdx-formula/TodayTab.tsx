import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ClipboardList, RefreshCw } from 'lucide-react'
import { toast } from '@/components/Toast'
import { tdxApi, tdxKeys, type PushChannels, type Reminder, type TodayStrategy } from './client'
import { money, pct, tone } from './logic'
import { RealPanel } from './RealPanel'
import { CapitalDialog } from './TradeDialog'
import { BODY_ROW, BUTTON, CARD, ErrorNote, HEAD_ROW, Hint, PRIMARY, StockName, TH, Tag, WarnNote, errorText } from './ui'

const pushText = (push: PushChannels): string =>
  `飞书${push.feishu ? '已配置' : '未配置'}，企业微信${push.wecom ? '已配置' : '未配置'}`

/** 「今日提醒」页：每个运行中的策略一张卡片；记了实际持仓的先看实际持仓。 */
export function TodayTab({ onGoStrategy }: { onGoStrategy: () => void }) {
  const qc = useQueryClient()
  const today = useQuery({ queryKey: tdxKeys.today, queryFn: tdxApi.today, staleTime: 30_000 })
  const [runMessage, setRunMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const runDaily = useMutation({
    mutationFn: tdxApi.runDaily,
    onSuccess: result => {
      setRunMessage({ ok: result.ok, text: result.message })
      toast(result.ok ? '提醒已生成' : '提醒没有全部生成，原因见页面上方', result.ok ? 'success' : 'error')
      void qc.invalidateQueries({ queryKey: tdxKeys.today })
      void qc.invalidateQueries({ queryKey: tdxKeys.strategies })
    },
    onError: e => { setRunMessage({ ok: false, text: errorText(e) }); toast(errorText(e)) },
  })

  if (today.isLoading) return <p className="text-xs text-muted">正在加载今日提醒…</p>
  if (today.error || !today.data) {
    return (
      <div className="space-y-2">
        <ErrorNote>今日提醒没有取到：{errorText(today.error)}</ErrorNote>
        <button className={BUTTON} onClick={() => void today.refetch()}>再试一次</button>
      </div>
    )
  }
  const { latest_date, strategies, last_run, push } = today.data
  const pushReady = push.feishu || push.wecom

  return (
    <div className="space-y-4">
      <section className={`${CARD} space-y-2`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-secondary">
            <span>
              行情更新到 <b className="font-medium text-foreground">{latest_date ?? '还没有行情数据'}</b>
            </span>
            <span className={pushReady ? '' : 'text-warning'}>推送：{pushText(push)}</span>
            {today.isFetching && <span className="text-muted">正在刷新…</span>}
          </div>
          <button className={PRIMARY} disabled={runDaily.isPending || strategies.length === 0} onClick={() => runDaily.mutate()}>
            <RefreshCw className={`h-3.5 w-3.5 ${runDaily.isPending ? 'animate-spin' : ''}`} />
            {runDaily.isPending ? '正在生成…' : '现在生成提醒'}
          </button>
        </div>
        {runDaily.isPending && <p className="text-[11px] text-muted">正在按最新行情生成提醒，第一次读取全市场数据要十几秒。</p>}
        {runMessage ? (
          runMessage.ok ? <p className="text-xs text-secondary">{runMessage.text}</p> : <ErrorNote>{runMessage.text}</ErrorNote>
        ) : last_run?.message ? (
          <p className={`text-xs ${last_run.ok === false ? 'text-warning' : 'text-secondary'}`}>
            上一次生成提醒（{(last_run.finished_at ?? last_run.started_at).slice(0, 16).replace('T', ' ')}）：{last_run.message}
          </p>
        ) : null}
        <p className="text-[11px] text-muted">
          行情来自免费的公开来源，每个交易日到「数据」页里设定的时间自动更新；这里发现行情到了新的一天就会自动生成提醒并推送，一般不用手动点。
          {!pushReady && '要收到推送，请到本面板左下角的“设置”里配置飞书或企业微信的机器人地址。'}
        </p>
      </section>

      {strategies.length === 0 ? (
        <div className="space-y-3">
          <Hint>还没有运行中的策略。到“策略”页建一个策略，回测满意后点“设为运行中”，这里每天就会有买卖提醒。</Hint>
          <div className="text-center"><button className={PRIMARY} onClick={onGoStrategy}>去“策略”页</button></div>
        </div>
      ) : strategies.map(s => <StrategyCard key={s.strategy_id} item={s} />)}
    </div>
  )
}

function StrategyCard({ item }: { item: TodayStrategy }) {
  const qc = useQueryClient()
  const [asking, setAsking] = useState(false)
  const { reminder, real } = item
  const timing = reminder?.timing
  const enable = useMutation({
    mutationFn: (capital: number) => tdxApi.setReal(item.strategy_id, true, capital),
    onSuccess: async () => {
      setAsking(false)
      toast('已打开实际持仓。之后的提醒按你记下的持仓和资金来算', 'success')
      void qc.invalidateQueries({ queryKey: tdxKeys.strategies })
      await qc.invalidateQueries({ queryKey: tdxKeys.today })
    },
    onError: e => toast(errorText(e)),
  })

  return (
    <section className={`${CARD} space-y-3`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-medium text-foreground">{item.name}</h2>
          <Tag tone="accent">运行中</Tag>
          {timing?.enabled && (
            <>
              <span className="text-[11px] text-muted">大盘择时</span>
              <Tag tone={timing.ok ? 'accent' : 'warning'}>{timing.ok ? '可以出手' : '建议空仓'}</Tag>
            </>
          )}
        </div>
        {!real && (
          <button className={BUTTON} disabled={enable.isPending} onClick={() => setAsking(true)}>
            <ClipboardList className="h-3.5 w-3.5" />记录实际持仓
          </button>
        )}
      </div>
      {item.behind && (
        <WarnNote>
          这份提醒是按 {reminder?.date} 的收盘生成的，比最新的行情旧，请不要照着操作。点上面的“现在生成提醒”更新。
        </WarnNote>
      )}

      {real && <RealPanel strategyId={item.strategy_id} real={real} />}

      {!reminder ? (
        <p className="text-xs text-muted">还没有生成过提醒。点上面的“现在生成提醒”。</p>
      ) : real ? (
        <details>
          <summary className="cursor-pointer text-xs text-secondary hover:text-foreground">
            模拟账户的提醒（假设每条都照做，用来衡量策略本身）
          </summary>
          <div className="mt-3"><PaperReminder reminder={reminder} /></div>
        </details>
      ) : (
        <PaperReminder reminder={reminder} />
      )}

      {asking && (
        <CapitalDialog
          title={`记录“${item.name}”的实际持仓`} saving={enable.isPending}
          onSubmit={capital => enable.mutate(capital)} onClose={() => setAsking(false)}
        />
      )}
    </section>
  )
}

const SUB = 'mb-1 text-xs font-medium text-foreground'

/** 模拟账户的一份提醒：明天买什么、卖什么、现在持有什么、账户怎么样 */
function PaperReminder({ reminder }: { reminder: Reminder }) {
  const { buys, sells, notes, positions, account } = reminder
  const referenceOnly = buys.some(b => b.reference_only)

  return (
    <div className="space-y-3">
      <p className="text-[11px] text-muted">根据 {reminder.date} 收盘生成，下一个交易日开盘执行。</p>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <h3 className={SUB}>买入 <span className="font-normal text-muted">{buys.length} 只</span></h3>
          {buys.length === 0 ? <p className="text-xs text-muted">没有要买入的股票。</p> : (
            <table className="w-full text-right text-xs">
              <thead>
                <tr className={HEAD_ROW}>
                  <th className={`${TH} text-left`}>股票</th>
                  <th className={TH}>排名</th>
                  <th className={TH}>排名得分</th>
                  <th className={`${TH} pl-4 text-left`}>入选原因</th>
                </tr>
              </thead>
              <tbody>
                {buys.map(b => (
                  <tr key={b.code} className={BODY_ROW}>
                    <td className="py-1.5 text-left">
                      <StockName code={b.code} name={b.name} list={buys} />
                      {b.reference_only && <span className="ml-2"><Tag tone="warning">仅供参考，不会买入</Tag></span>}
                    </td>
                    <td className="tabular-nums">{b.rank > 0 ? `第 ${b.rank}` : '—'}</td>
                    <td className="tabular-nums">{b.score}</td>
                    <td className="pl-4 text-left text-secondary">{b.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {referenceOnly && <p className="mt-1 text-[11px] text-warning">大盘择时建议空仓：上面的股票只是满足了选股条件，模拟账户不会买。</p>}
        </div>
        <div>
          <h3 className={SUB}>卖出 <span className="font-normal text-muted">{sells.length} 只</span></h3>
          {sells.length === 0 ? <p className="text-xs text-muted">没有要卖出的股票。</p> : (
            <table className="w-full text-right text-xs">
              <thead>
                <tr className={HEAD_ROW}>
                  <th className={`${TH} text-left`}>股票</th>
                  <th className={TH}>到现在的盈亏</th>
                  <th className={`${TH} pl-4 text-left`}>卖出原因</th>
                </tr>
              </thead>
              <tbody>
                {sells.map(s => (
                  <tr key={s.code} className={BODY_ROW}>
                    <td className="py-1.5 text-left"><StockName code={s.code} name={s.name} list={sells} /></td>
                    <td className={`tabular-nums ${tone(s.gain)}`}>{pct(s.gain)}</td>
                    <td className="pl-4 text-left text-secondary">{s.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {notes.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-warning">
          {notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}

      <div>
        <h3 className={SUB}>模拟账户现在的持仓 <span className="font-normal text-muted">{positions.length} 只</span></h3>
        {positions.length === 0 ? <p className="text-xs text-muted">现在是空仓。</p> : (
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
                </tr>
              </thead>
              <tbody>
                {positions.map(p => (
                  <tr key={p.code} className={BODY_ROW}>
                    <td className="py-1.5 text-left"><StockName code={p.code} name={p.name} list={positions} /></td>
                    <td className="text-left text-secondary">{p.buy_date}</td>
                    <td className="tabular-nums">{p.buy_price.toFixed(2)}</td>
                    <td className="tabular-nums">{p.shares.toLocaleString('en-US')}</td>
                    <td className="tabular-nums">{money(p.value)}</td>
                    <td className={`tabular-nums ${tone(p.gain)}`}>{pct(p.gain)}</td>
                    <td>第 {p.hold_days} 天</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="text-xs text-secondary">
        模拟账户：{account.started_on} 开始，起始 {money(account.initial_cash, 0)} 元，现在总资产{' '}
        <span className="tabular-nums text-foreground">{money(account.equity)}</span> 元（其中现金 {money(account.cash)} 元），累计{' '}
        <b className={`font-medium tabular-nums ${tone(account.total_return)}`}>{pct(account.total_return)}</b>
      </p>
    </div>
  )
}
