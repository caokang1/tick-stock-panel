import { useMemo, useState } from 'react'
import type { BacktestResult } from './client'
import { curveRange, linePath, money, pct, tone } from './logic'
import { BODY_ROW, HEAD_ROW, StockName, TH } from './ui'

const TRADES_SHOWN = 100
const W = 1000
const H = 260

function Tile({ label, value, className = '' }: { label: string; value: string; className?: string }) {
  return (
    <div className="rounded-card border border-border bg-base px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`mt-0.5 text-sm font-medium tabular-nums ${className || 'text-foreground'}`}>{value}</div>
    </div>
  )
}

/** 净值 1.2345 → "+23.45%"（相对起点） */
const growth = (v: number | null | undefined): string => (v == null ? '—' : pct(v - 1))

/** 策略和基准的净值曲线。鼠标移上去显示那一天的数。 */
function EquityChart({ curve, benchmarkName }: { curve: BacktestResult['curve']; benchmarkName: string }) {
  const [hover, setHover] = useState<number | null>(null)
  const n = curve.dates.length
  const { lo, hi, equityPath, benchPath } = useMemo(() => {
    const [min, max] = curveRange([curve.equity, curve.benchmark])
    return {
      lo: min, hi: max,
      equityPath: linePath(curve.equity, min, max, W, H),
      benchPath: linePath(curve.benchmark, min, max, W, H),
    }
  }, [curve])
  if (n < 2) return <p className="text-xs text-muted">数据太少，画不出曲线。</p>

  const at = hover ?? n - 1
  const yOf = (v: number) => H - ((v - lo) / (hi - lo || 1)) * H
  const ticks = [hi, (hi + lo) / 2, lo]
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const ratio = box.width > 0 ? (e.clientX - box.left) / box.width : 0
    setHover(Math.min(n - 1, Math.max(0, Math.round(ratio * (n - 1)))))
  }

  return (
    <figure className="space-y-1">
      <figcaption className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-secondary">
        <span className="text-muted">{curve.dates[at]}{hover == null && '（最后一天）'}</span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-4 bg-accent" />策略
          <b className={`font-medium tabular-nums ${tone((curve.equity[at] ?? 1) - 1)}`}>{growth(curve.equity[at])}</b>
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-4 bg-muted" />{benchmarkName}
          <b className={`font-medium tabular-nums ${tone((curve.benchmark[at] ?? 1) - 1)}`}>{growth(curve.benchmark[at])}</b>
        </span>
        <span className="text-muted">起点都算作 0%</span>
      </figcaption>
      <div className="flex gap-2">
        <div className="flex w-12 shrink-0 flex-col justify-between text-right text-[10px] tabular-nums text-muted" aria-hidden="true">
          {ticks.map((t, i) => <span key={i}>{pct(t - 1, 0)}</span>)}
        </div>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`策略和${benchmarkName}的累计收益曲线，${curve.dates[0]} 到 ${curve.dates[n - 1]}`}
          className="h-64 w-full min-w-0 rounded-btn border border-border bg-base"
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        >
          {lo < 1 && hi > 1 && (
            <line x1={0} x2={W} y1={yOf(1)} y2={yOf(1)} className="stroke-border" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
          )}
          <path d={benchPath} fill="none" className="stroke-muted" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
          <path d={equityPath} fill="none" className="stroke-accent" strokeWidth={1.6} vectorEffect="non-scaling-stroke" />
          {hover != null && (
            <line x1={(hover / (n - 1)) * W} x2={(hover / (n - 1)) * W} y1={0} y2={H} className="stroke-secondary" strokeWidth={1} vectorEffect="non-scaling-stroke" />
          )}
        </svg>
      </div>
      <div className="flex justify-between pl-14 text-[10px] tabular-nums text-muted" aria-hidden="true">
        <span>{curve.dates[0]}</span>
        <span>{curve.dates[Math.floor((n - 1) / 2)]}</span>
        <span>{curve.dates[n - 1]}</span>
      </div>
    </figure>
  )
}

/** 一次组合回测的结果：几个总数、净值曲线、每年收益、口径说明、交易明细 */
export function BacktestView({ result, fromSaved }: { result: BacktestResult; fromSaved: boolean }) {
  const s = result.summary
  const bench = result.benchmark.name
  const trades = result.trades.slice(0, TRADES_SHOWN)

  return (
    <div className="space-y-4">
      <p className="text-[11px] text-muted">
        回测范围 {result.range.start} 到 {result.range.end}
        {s.initial_cash != null && `，起始资金 ${money(s.initial_cash, 0)} 元`}
        {s.final_equity != null && `，最后是 ${money(s.final_equity, 0)} 元`}。
        {fromSaved ? `这是 ${result.created_at} 那次回测的结果，之后改过内容的话请重新回测。` : `回测于 ${result.created_at}。`}
      </p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <Tile label="总收益" value={pct(s.total_return)} className={tone(s.total_return)} />
        <Tile label="年化收益" value={pct(s.annual_return)} className={tone(s.annual_return)} />
        <Tile label="最大回撤（中途最多亏掉多少）" value={pct(s.max_drawdown)} />
        {s.sharpe != null && <Tile label="夏普比率（越高越稳）" value={s.sharpe.toFixed(2)} />}
        {s.win_rate != null && <Tile label="胜率（赚钱的交易占比）" value={pct(s.win_rate, 1, false)} />}
        <Tile label="交易次数（已卖出的）" value={`${s.trade_count.toLocaleString('en-US')} 笔`} />
        {s.avg_hold_days != null && <Tile label="平均持有" value={`${s.avg_hold_days.toFixed(1)} 个交易日`} />}
        <Tile label={`同期${bench}年化`} value={pct(s.benchmark_annual_return)} className={tone(s.benchmark_annual_return)} />
        {s.ok_day_ratio != null && <Tile label="大盘择时允许买入的日子" value={pct(s.ok_day_ratio, 1, false)} />}
      </div>

      <EquityChart curve={result.curve} benchmarkName={bench} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <h4 className="mb-1 text-xs font-medium text-foreground">每年的收益</h4>
          {result.yearly.length === 0 ? <p className="text-xs text-muted">没有数据。</p> : (
            <table className="w-full text-right text-xs">
              <thead>
                <tr className={HEAD_ROW}>
                  <th className={`${TH} text-left`}>年份</th>
                  <th className={TH}>策略</th>
                  <th className={TH}>{bench}</th>
                  <th className={TH}>比{bench}多</th>
                </tr>
              </thead>
              <tbody>
                {result.yearly.map(y => {
                  const extra = y.strategy != null && y.benchmark != null ? y.strategy - y.benchmark : null
                  return (
                    <tr key={y.year} className={BODY_ROW}>
                      <td className="py-1.5 text-left text-secondary">{y.year}</td>
                      <td className={`tabular-nums ${tone(y.strategy)}`}>{pct(y.strategy)}</td>
                      <td className={`tabular-nums ${tone(y.benchmark)}`}>{pct(y.benchmark)}</td>
                      <td className={`tabular-nums ${tone(extra)}`}>{pct(extra)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
        <div>
          <h4 className="mb-1 text-xs font-medium text-foreground">这次回测是怎么算的</h4>
          <ul className="list-disc space-y-1 pl-4 text-[11px] text-secondary">
            {result.assumptions.map((a, i) => <li key={i}>{a}</li>)}
            <li>这是历史回测，不代表以后；不构成投资建议。</li>
          </ul>
        </div>
      </div>

      <div>
        <h4 className="mb-1 text-xs font-medium text-foreground">
          交易明细
          <span className="ml-2 font-normal text-muted">
            一共 {result.trade_count.toLocaleString('en-US')} 笔{result.trade_count > trades.length && `，这里是最近的 ${trades.length} 笔`}
          </span>
        </h4>
        {trades.length === 0 ? <p className="text-xs text-muted">回测期间一笔交易都没有。条件可能太严，或者买卖规则让它买不进。</p> : (
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full text-right text-xs">
              <thead className="sticky top-0 bg-surface">
                <tr className={HEAD_ROW}>
                  <th className={`${TH} text-left`}>股票</th>
                  <th className={`${TH} text-left`}>买入日</th>
                  <th className={TH}>买入价</th>
                  <th className={`${TH} pl-4 text-left`}>卖出日</th>
                  <th className={TH}>卖出价</th>
                  <th className={TH}>股数</th>
                  <th className={TH}>持有</th>
                  <th className={TH}>收益</th>
                  <th className={TH}>盈亏（元）</th>
                  <th className={`${TH} pl-4 text-left`}>卖出原因</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={`${t.code}-${t.buy_date}-${i}`} className={BODY_ROW}>
                    <td className="py-1.5 text-left"><StockName code={t.code} name={t.name} list={trades} /></td>
                    <td className="text-left text-secondary">{t.buy_date}</td>
                    <td className="tabular-nums">{t.buy_price?.toFixed(2) ?? '—'}</td>
                    <td className="pl-4 text-left text-secondary">{t.sell_date ?? '还持有'}</td>
                    <td className="tabular-nums">{t.sell_price?.toFixed(2) ?? '—'}</td>
                    <td className="tabular-nums">{t.shares.toLocaleString('en-US')}</td>
                    <td className="tabular-nums">{t.hold_days} 天</td>
                    <td className={`tabular-nums ${tone(t.return)}`}>{pct(t.return)}</td>
                    <td className={`tabular-nums ${tone(t.pnl)}`}>{money(t.pnl)}</td>
                    <td className="pl-4 text-left text-secondary">{t.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
