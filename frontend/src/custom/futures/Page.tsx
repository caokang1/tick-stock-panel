/**
 * 期货：股指期货、贵金属、有色、能源黑色、国债和外盘主要品种的行情，点一行看日线。
 * 只展示，不参与选股、回测和提醒。数据来自新浪财经的公开接口，页面开着时每 30 秒取一次。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/PageHeader'
import { futuresApi, futuresKeys, type Bar, type FutureQuote } from './client'
import { candles, digitsOf, grouped, lots, pct, price, signed } from './logic'

const BULL = 'hsl(var(--bull))'
const BEAR = 'hsl(var(--bear))'
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull' : 'text-bear')
const TH = 'px-3 py-2 text-right font-normal'
const TD = 'px-3 py-2 text-right tabular-nums'

const W = 900
const H = 300
const PAD = { l: 8, r: 64, t: 12, b: 24 }

function KlineChart({ bars }: { bars: Bar[] }) {
  const [hover, setHover] = useState<number | null>(null)
  const { items, min, max, slot } = candles(bars, W, H, PAD)
  const shown = bars[hover ?? bars.length - 1]
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(t => ({ y: PAD.t + (H - PAD.t - PAD.b) * (1 - t), value: min + (max - min) * t }))
  const every = Math.max(1, Math.ceil(bars.length / 6))

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    if (!rect.width || !slot) return
    const x = ((e.clientX - rect.left) / rect.width) * W
    setHover(Math.max(0, Math.min(bars.length - 1, Math.floor((x - PAD.l) / slot))))
  }

  return (
    <div>
      <div className="mb-1 flex flex-wrap gap-x-4 text-[11px] text-muted tabular-nums">
        <span>{shown.date}</span>
        <span>开 {price(shown.open)}</span>
        <span>高 {price(shown.high)}</span>
        <span>低 {price(shown.low)}</span>
        <span className={shown.close >= shown.open ? 'text-bull' : 'text-bear'}>收 {price(shown.close)}</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="日线图"
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}
      >
        {ticks.map(t => (
          <g key={t.y}>
            <line x1={PAD.l} x2={W - PAD.r} y1={t.y} y2={t.y} stroke="hsl(var(--border))" strokeWidth={1} />
            <text x={W - PAD.r + 6} y={t.y + 4} fontSize={11} fill="hsl(var(--fg-muted))">{price(t.value)}</text>
          </g>
        ))}
        {items.map((c, i) => (
          <g key={bars[i].date} stroke={c.up ? BULL : BEAR} fill={c.up ? BULL : BEAR}>
            <line x1={c.x} x2={c.x} y1={c.yHigh} y2={c.yLow} strokeWidth={1} />
            <rect
              x={c.x - c.width / 2} width={c.width}
              y={Math.min(c.yOpen, c.yClose)} height={Math.max(1, Math.abs(c.yClose - c.yOpen))}
            />
          </g>
        ))}
        {hover != null && items[hover] && (
          <line x1={items[hover].x} x2={items[hover].x} y1={PAD.t} y2={H - PAD.b} stroke="hsl(var(--fg-muted))" strokeDasharray="3 3" />
        )}
        {bars.map((b, i) => i % every === 0 && (
          <text key={b.date} x={items[i].x} y={H - 6} fontSize={11} textAnchor="middle" fill="hsl(var(--fg-muted))">{b.date.slice(2)}</text>
        ))}
      </svg>
    </div>
  )
}

function Detail({ row }: { row: FutureQuote }) {
  const { data, isLoading, error } = useQuery({
    queryKey: futuresKeys.kline(row.code),
    queryFn: () => futuresApi.kline(row.code),
    staleTime: 10 * 60 * 1000,
  })
  return (
    <section className="rounded-card border border-border bg-surface p-4">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-sm font-medium text-foreground">{row.name} · 日线</h2>
        <span className={`text-lg font-semibold tabular-nums ${tone(row.change)}`}>{price(row.last)}</span>
        <span className={`text-xs tabular-nums ${tone(row.change)}`}>{signed(row.change, digitsOf(row.last))}（{pct(row.change_pct)}）</span>
        <span className="text-[11px] text-muted">最近 {data?.bars.length ?? 0} 个交易日</span>
      </div>
      {isLoading && <p className="py-10 text-center text-xs text-muted">正在取日线…</p>}
      {error && <p className="py-10 text-center text-xs text-bear">{(error as Error).message}</p>}
      {data && data.bars.length === 0 && (
        <p className="py-10 text-center text-xs text-muted">{data.error ?? '这个品种暂时没有日线数据。'}</p>
      )}
      {data && data.bars.length > 0 && <KlineChart bars={data.bars} />}
    </section>
  )
}

export function Futures() {
  const { data, isLoading, error } = useQuery({
    queryKey: futuresKeys.quotes,
    queryFn: futuresApi.quotes,
    refetchInterval: 30 * 1000,
    staleTime: 0,
  })
  const [picked, setPicked] = useState<string | null>(null)
  const items = data?.items ?? []
  const current = items.find(r => r.code === picked) ?? items[0]

  return (
    <div>
      <PageHeader title="期货" subtitle="股指期货、金银铜和外盘主要品种的行情，点一行看日线 · 只展示，不参与选股和回测" />
      <div className="space-y-4 p-4">
      <p className="text-[11px] text-muted">
        数据来自新浪财经公开行情，页面开着时每 30 秒更新一次。国内品种是主力连续合约，涨跌按昨结算价计算。
        {data?.fetched_at && ` 上次更新 ${data.fetched_at.slice(11)}。`}
      </p>
      {data?.error && (
        <p className="rounded-card border border-border bg-surface px-3 py-2 text-xs text-bear">
          这次没有取到新行情：{data.error}{items.length > 0 && ' 下面是上一次的数据。'}
        </p>
      )}
      {isLoading && <p className="py-10 text-center text-xs text-muted">正在取行情…</p>}
      {error && <p className="py-10 text-center text-xs text-bear">{(error as Error).message}</p>}
      {data && items.length === 0 && !data.error && (
        <p className="py-10 text-center text-xs text-muted">暂时没有行情。</p>
      )}

      {items.length > 0 && (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,4fr)]">
          <div className="min-w-0 space-y-4">
            {grouped(items).map(({ group, rows }) => {
              const index = rows.some(r => r.spot != null)
              const domestic = rows.some(r => r.volume != null)
              return (
                <section key={group} className="overflow-x-auto rounded-card border border-border bg-surface">
                  <h2 className="border-b border-border px-3 py-2 text-[13px] font-medium text-foreground">{group}</h2>
                  <table className="w-full min-w-[520px] text-xs">
                    <thead>
                      <tr className="text-muted">
                        <th className="px-3 py-2 text-left font-normal">品种</th>
                        <th className={TH}>最新</th>
                        <th className={TH}>涨跌</th>
                        <th className={TH}>涨跌幅</th>
                        <th className={TH}>最高</th>
                        <th className={TH}>最低</th>
                        {domestic && <th className={TH}>成交量</th>}
                        {domestic && <th className={TH}>持仓量</th>}
                        {index && <th className={TH} title="期货最新价 − 现货指数最新价；负数是贴水">升贴水</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map(r => (
                        <tr
                          key={r.code} onClick={() => setPicked(r.code)}
                          className={`cursor-pointer border-t border-border hover:bg-accent/5 ${current?.code === r.code ? 'bg-accent/10' : ''}`}
                        >
                          <td className="whitespace-nowrap px-3 py-2 text-left text-foreground">{r.name}</td>
                          <td className={`${TD} font-medium ${tone(r.change)}`}>{price(r.last)}</td>
                          <td className={`${TD} ${tone(r.change)}`}>{signed(r.change, digitsOf(r.last))}</td>
                          <td className={`${TD} ${tone(r.change)}`}>{pct(r.change_pct)}</td>
                          <td className={`${TD} text-foreground`}>{price(r.high)}</td>
                          <td className={`${TD} text-foreground`}>{price(r.low)}</td>
                          {domestic && <td className={`${TD} text-foreground`}>{lots(r.volume)}</td>}
                          {domestic && <td className={`${TD} text-foreground`}>{lots(r.open_interest)}</td>}
                          {index && (
                            <td className={`${TD} ${tone(r.basis)}`}>
                              {r.basis == null ? '—' : `${signed(r.basis, 1)}（${pct(r.basis_pct)}）`}
                            </td>
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </section>
              )
            })}
          </div>
          {current && <div className="min-w-0 xl:sticky xl:top-4 xl:self-start"><Detail row={current} /></div>}
        </div>
      )}
      </div>
    </div>
  )
}
