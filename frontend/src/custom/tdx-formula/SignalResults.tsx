import { useState } from 'react'
import type { Pick, SignalItem, StatsResult, Status } from './client'
import { capText, frequencyText, pct, tone } from './logic'
import { Hint, StockName } from './ui'

const HOLDS = [1, 3, 5, 10]

/** ④ 历史信号统计的结果：统计表、最新一天选出的股票、历史信号明细 */
export function SignalResults({ stats, picks, signals, costs }: {
  stats: StatsResult
  picks: { date: string; items: Pick[] } | null
  signals: { total: number; items: SignalItem[] } | null
  costs: Status['costs']
}) {
  const [tab, setTab] = useState<'picks' | 'signals'>('picks')
  const rows = stats.stats.filter(r => r.entry === 'next_open')
  return (
    <>
      <p className="text-xs text-secondary">{frequencyText(stats.frequency)}</p>
      <p className="text-[11px] text-muted">
        统计范围 {stats.range.start} 到 {stats.range.end}。出信号后第二天开盘买入、持有若干个交易日后收盘卖出；开盘涨停买不进的不算。
        已扣成本：佣金 {pct(costs.commission, 3, false)}、印花税 {pct(costs.stamp_tax, 2, false)}、滑点 {pct(costs.slippage, 1, false)}。
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-right text-xs">
          <thead className="text-[11px] text-muted">
            <tr className="border-b border-border">
              <th className="py-1.5 text-left font-normal">持有</th>
              <th className="font-normal">次数</th>
              <th className="font-normal">胜率</th>
              <th className="font-normal">平均收益</th>
              <th className="font-normal">收益中位数</th>
              <th className="font-normal">期间平均最大回撤</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.hold} className="border-b border-border/50">
                <td className="py-1.5 text-left text-secondary">{r.hold} 天</td>
                <td>{r.count.toLocaleString('en-US')}</td>
                <td>{pct(r.win_rate, 1, false)}</td>
                <td className={tone(r.avg_return)}>{pct(r.avg_return)}</td>
                <td className={tone(r.median_return)}>{pct(r.median_return)}</td>
                <td>{pct(r.avg_drawdown)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!stats.st_known && <p className="text-[11px] text-warning">历史 ST 状态还没有补齐，ST 过滤暂时按现在的名称判断，结果可能略有偏差。</p>}
      <p className="text-[11px] text-muted">这是历史统计，不代表以后；不构成投资建议。</p>

      <div className="flex gap-1 border-b border-border pt-2">
        {(['picks', 'signals'] as const).map(key => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`-mb-px cursor-pointer border-b-2 px-3 py-1.5 text-xs ${tab === key ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-secondary'}`}
          >
            {key === 'picks' ? `最新一天选出的股票${picks ? `（${picks.items.length}）` : ''}` : `历史信号明细${signals ? `（最近 ${signals.items.length} 条）` : ''}`}
          </button>
        ))}
      </div>

      {tab === 'picks' && (
        !picks ? <Hint>正在计算…</Hint>
          : picks.items.length === 0 ? <Hint>{picks.date} 收盘后没有股票满足条件。</Hint>
            : (
              <div className="max-h-[420px] overflow-auto">
                <p className="pb-1 text-[11px] text-muted">按 {picks.date} 的收盘判断。点股票名称可以打开本面板的个股详情（需要本面板里有这只股票的数据）。</p>
                <table className="w-full text-right text-xs">
                  <thead className="sticky top-0 bg-surface text-[11px] text-muted">
                    <tr className="border-b border-border">
                      <th className="py-1.5 text-left font-normal">股票</th>
                      <th className="font-normal">收盘价</th>
                      <th className="font-normal">当天涨跌</th>
                      <th className="font-normal">流通市值</th>
                      <th className="text-left font-normal pl-3">行业</th>
                    </tr>
                  </thead>
                  <tbody>
                    {picks.items.map(p => (
                      <tr key={p.code} className="border-b border-border/50">
                        <td className="py-1.5 text-left">
                          <StockName code={p.code} name={p.name} list={picks.items} />
                        </td>
                        <td>{p.close.toFixed(2)}</td>
                        <td className={tone(p.change)}>{pct(p.change)}</td>
                        <td>{capText(p.float_cap)}</td>
                        <td className="pl-3 text-left text-secondary">{p.industry || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
      )}

      {tab === 'signals' && (
        !signals ? <Hint>正在计算…</Hint>
          : signals.items.length === 0 ? <Hint>历史上没有出过信号。</Hint>
            : (
              <div className="max-h-[420px] overflow-auto">
                <p className="pb-1 text-[11px] text-muted">一共 {signals.total.toLocaleString('en-US')} 条，这里是最近的 {signals.items.length} 条。后面几列是买入后持有若干天的收益；还没到天数的为空。</p>
                <table className="w-full text-right text-xs">
                  <thead className="sticky top-0 bg-surface text-[11px] text-muted">
                    <tr className="border-b border-border">
                      <th className="py-1.5 text-left font-normal">信号日</th>
                      <th className="text-left font-normal">股票</th>
                      {HOLDS.map(h => <th key={h} className="font-normal">{h} 天</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {signals.items.map(s => (
                      <tr key={`${s.date}-${s.code}`} className="border-b border-border/50">
                        <td className="py-1.5 text-left text-secondary">{s.date}</td>
                        <td className="text-left">
                          <StockName code={s.code} name={s.name} list={signals.items} />
                        </td>
                        {HOLDS.map(h => <td key={h} className={tone(s.returns[String(h)])}>{pct(s.returns[String(h)])}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
      )}
    </>
  )
}
