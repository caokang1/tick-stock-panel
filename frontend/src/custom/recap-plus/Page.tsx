/**
 * 每日复盘：首页看当天的关键指标和近 15 个交易日的走势，归档按日期翻看每天的复盘报告。
 *
 * 报告本身是面板“复盘”功能生成并存档的（手动或定时），这里只是换一种方式看：
 * 首页摘出最新一份报告的“市场全景”和“数据质量自查”两节，归档里是每天的全文。
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/PageHeader'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { recapApi, recapKeys, type Dashboard, type RecapReport } from './client'
import { latestPerDay, pct, section, shortDate, signedYi, yi } from './logic'
import { TrendChart } from './TrendChart'

const CARD = 'rounded-card border border-border bg-surface p-4'
const H2 = 'mb-2 border-l-4 border-accent pl-2 text-sm font-medium text-foreground'
const BULL = 'hsl(var(--bull))'
const BEAR = 'hsl(var(--bear))'
const ACCENT = 'hsl(var(--accent))'
const tone = (v: number | null | undefined) => (v == null || v === 0 ? 'text-foreground' : v > 0 ? 'text-bull' : 'text-bear')

function Kpi({ label, value, note, className = 'text-foreground' }: { label: string; value: React.ReactNode; note: string; className?: string }) {
  return (
    <div className="rounded-card border border-border bg-surface px-3 py-2.5">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`my-0.5 text-lg font-semibold leading-tight tabular-nums ${className}`}>{value}</div>
      <div className="text-[11px] text-muted">{note}</div>
    </div>
  )
}

function Home({ data, latest }: { data: Dashboard; latest: RecapReport | undefined }) {
  const days = data.days
  const today = days[days.length - 1]
  const margin = data.margin[data.margin.length - 1]
  const marginBefore = data.margin[data.margin.length - 2]
  const labels = days.map(d => d.date)
  const total = (today.up ?? 0) + (today.down ?? 0) + (today.flat ?? 0)
  const overview = latest && latest.as_of === data.as_of ? section(latest.content, '一') : ''
  const quality = latest && latest.as_of === data.as_of ? section(latest.content, '六') : ''

  return (
    <div className="space-y-5">
      <section>
        <h2 className={H2}>关键指标卡（{data.as_of} 收盘口径）</h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <Kpi
            label="上证指数" className={tone(today.sh_change)}
            value={today.sh_close?.toFixed(2) ?? '—'} note={pct(today.sh_change)}
          />
          <Kpi
            label="两市成交额" value={yi(today.amount)}
            note={today.amount_change == null ? '沪市 + 深市' : `较前日 ${signedYi(today.amount_change)}（${today.amount_change >= 0 ? '放量' : '缩量'}）`}
          />
          <Kpi
            label="涨停 / 跌停" note="含 ST · 沪深两市"
            value={<><span className="text-bull">{today.limit_up}</span> / <span className="text-bear">{today.limit_down}</span></>}
          />
          <Kpi
            label="涨跌家数（沪深两市）" note={total ? `上涨占比 ${(((today.up ?? 0) / total) * 100).toFixed(0)}%` : '—'}
            value={<><span className="text-bull">{today.up ?? '—'}</span> / <span className="text-bear">{today.down ?? '—'}</span></>}
          />
          <Kpi
            label="两融余额（次日披露）" value={yi(margin?.rzrqye)}
            note={margin ? `数据日期 ${margin.date.slice(5)}${marginBefore?.rzrqye && margin.rzrqye ? ` · 环比 ${signedYi(margin.rzrqye - marginBefore.rzrqye, 1)}` : ''}` : (data.margin_error ?? '没有取到')}
          />
          <Kpi
            label="融资净买入（次日披露）" className={tone(margin?.rzjme)} value={signedYi(margin?.rzjme, 1)}
            note={margin ? `融资买入额 ${yi(margin.rzmre)}` : '—'}
          />
        </div>
      </section>


      <section>
        <h2 className={H2}>今日主线结论</h2>
        <div className={CARD}>
          {overview
            ? <MarkdownRenderer content={overview} />
            : (
              <p className="text-xs text-muted">
                {data.as_of} 的复盘报告还没有生成。点右上角的 <Link to="/review" className="text-accent hover:underline">生成 / 重跑复盘</Link>，点“开始复盘”并保存，
                或者等每天的定时复盘跑完，这里就会显示当天的结论。
              </p>
            )}
        </div>
      </section>

      <section>
        <h2 className={H2}>趋势图（近 {days.length} 个交易日滚动窗口）</h2>
        <div className="grid gap-3 lg:grid-cols-2">
          <TrendChart
            title="上证指数收盘点位" caption="收盘口径 · 单位：点" labels={labels}
            series={[{ name: '上证', values: days.map(d => d.sh_close), color: ACCENT }]} format={v => v.toFixed(0)}
          />
          <TrendChart
            title="沪深两市成交额" caption="沪市 + 深市个股合计（不含北交所）· 单位：亿元" labels={labels} kind="bar"
            series={[{ name: '成交额', values: days.map(d => (d.amount == null ? null : d.amount / 1e8)), color: ACCENT }]}
            format={v => v.toFixed(0)}
          />
          <TrendChart
            title="涨停 / 跌停家数" caption="沪深两市 · 含 ST 口径 · 单位：家" labels={labels}
            series={[
              { name: '涨停', values: days.map(d => d.limit_up), color: BULL },
              { name: '跌停', values: days.map(d => d.limit_down), color: BEAR },
            ]}
            format={v => v.toFixed(0)}
          />
          <TrendChart
            title="全市场两融余额" caption={`次日披露，横轴是数据日期${margin ? `（最新到 ${margin.date.slice(5)}）` : ''}· 单位：亿元`}
            labels={data.margin.map(m => m.date)}
            series={[{ name: '两融余额', values: data.margin.map(m => (m.rzrqye == null ? null : m.rzrqye / 1e8)), color: ACCENT }]}
            format={v => v.toFixed(0)}
          />
        </div>
      </section>


      {quality && (
        <section>
          <h2 className={H2}>数据质量自查（{data.as_of}）</h2>
          <div className={CARD}><MarkdownRenderer content={quality} /></div>
        </section>
      )}
    </div>
  )
}

function Archive({ reports }: { reports: RecapReport[] }) {
  const [picked, setPicked] = useState<string | null>(null)
  if (!reports.length) {
    return (
      <p className={`${CARD} text-xs text-muted`}>
        还没有存档的复盘。点右上角的 <Link to="/review" className="text-accent hover:underline">生成 / 重跑复盘</Link> 生成并保存一份，或者等定时复盘跑完。
      </p>
    )
  }
  const current = reports.find(r => r.as_of === picked) ?? reports[0]
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2" role="tablist" aria-label="按日期切换">
        {reports.map(r => (
          <button
            key={r.as_of} role="tab" aria-selected={r.as_of === current.as_of} onClick={() => setPicked(r.as_of)}
            className={`h-8 cursor-pointer rounded-btn border px-3 text-xs transition-colors ${r.as_of === current.as_of
              ? 'border-accent/40 bg-accent/10 font-medium text-accent' : 'border-border text-secondary hover:bg-elevated'}`}
          >
            {shortDate(r.as_of)}
          </button>
        ))}
      </div>
      <article className={CARD}>
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2 border-b border-border/60 pb-2">
          <h2 className="text-sm font-medium text-foreground">{current.as_of} 收盘复盘</h2>
          <span className="text-[11px] text-muted">
            {current.summary}{current.created_at ? ` · 生成于 ${current.created_at.replace('T', ' ').slice(0, 16)}` : ''}
          </span>
        </div>
        <MarkdownRenderer content={current.content} />
      </article>
    </div>
  )
}

export function DailyReview() {
  const [tab, setTab] = useState<'home' | 'archive'>('home')
  const dashboard = useQuery({ queryKey: recapKeys.dashboard, queryFn: recapApi.dashboard, staleTime: 0, refetchOnMount: 'always' })
  const reports = useQuery({ queryKey: recapKeys.reports, queryFn: recapApi.reports, staleTime: 0, refetchOnMount: 'always' })
  const archive = latestPerDay(reports.data ?? [])
  const data = dashboard.data

  const tabClass = (active: boolean) => `-mb-px cursor-pointer border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${active
    ? 'border-accent text-accent' : 'border-transparent text-secondary hover:text-foreground'}`

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="每日复盘"
        subtitle={`数据日期：${data?.as_of ?? '—'} · 全部数据基于公开渠道整理，不构成投资建议`}
        right={
          <Link
            to="/review" title="手动生成或重跑当天的复盘，生成后会出现在归档里"
            className="shrink-0 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground"
          >
            生成 / 重跑复盘
          </Link>
        }
      />
      <nav className="flex gap-1 border-b border-border px-5" role="tablist">
        <button role="tab" aria-selected={tab === 'home'} className={tabClass(tab === 'home')} onClick={() => setTab('home')}>
          首页·市场全景
        </button>
        <button role="tab" aria-selected={tab === 'archive'} className={tabClass(tab === 'archive')} onClick={() => setTab('archive')}>
          每日复盘·归档
          <span className="ml-1.5 inline-block min-w-5 rounded-full bg-accent px-1.5 text-center text-[11px] font-semibold leading-[18px] text-white">
            {archive.length}
          </span>
        </button>
      </nav>
      <div className="flex-1 overflow-auto p-5">
        <div className="mx-auto max-w-[1220px]">
          {tab === 'home' && (
            dashboard.isLoading ? <p className="text-xs text-muted">正在读取……</p>
              : dashboard.isError ? <p role="alert" className="text-xs text-danger">{(dashboard.error as Error).message}</p>
                : data && data.days.length ? <Home data={data} latest={archive[0]} />
                  : <p className={`${CARD} text-xs text-muted`}>本机还没有日线数据，先到“数据”页同步。</p>
          )}
          {tab === 'archive' && (
            reports.isLoading ? <p className="text-xs text-muted">正在读取……</p>
              : reports.isError ? <p role="alert" className="text-xs text-danger">{(reports.error as Error).message}</p>
                : <Archive reports={archive} />
          )}
        </div>
      </div>
    </div>
  )
}
