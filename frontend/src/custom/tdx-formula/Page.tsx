import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderCog, Play, Save, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { openStockPreview } from '@/extensions/stockPreview'
import {
  tdxApi, tdxKeys,
  type Condition, type Pick, type Saved, type SignalItem, type StatsResult, type Status, type Universe,
} from './client'
import {
  capText, conditionFromParsed, conditionName, defaultUniverse, frequencyText, pct, tone, toPanelSymbol, withParam,
} from './logic'

const EXAMPLE = `ZT:=C/REF(C,1)>=1.098 AND C=H;
XG:EXIST(ZT,12) AND C>MA(C,20) AND V>MA(V,5)*1.5;`

const FLAGS: { key: 'exclude_st' | 'exclude_star' | 'exclude_chinext' | 'exclude_bse' | 'exclude_suspended'; label: string }[] = [
  { key: 'exclude_st', label: '排除 ST' },
  { key: 'exclude_star', label: '排除科创板' },
  { key: 'exclude_chinext', label: '排除创业板' },
  { key: 'exclude_bse', label: '排除北交所' },
  { key: 'exclude_suspended', label: '排除当天停牌' },
]

const HOLDS = [1, 3, 5, 10]
const INPUT = 'h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground'
const BUTTON = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-50'
const PRIMARY = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-50'

const errorText = (e: unknown): string => (e instanceof Error && e.message ? e.message : '出错了，请再试一次')

/** 通达信公式选股：粘贴公式 → 拆成可以调参数的条件 → 在全市场历史上统计信号。数据和计算内核来自 quant-lab。 */
export function TdxFormula() {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: tdxKeys.status, queryFn: tdxApi.status, staleTime: 30_000 })
  const saved = useQuery({ queryKey: tdxKeys.saved, queryFn: tdxApi.saved, staleTime: 60_000 })

  const [formula, setFormula] = useState('')
  const [conditions, setConditions] = useState<Condition[]>([])
  const [universe, setUniverse] = useState<Universe>(defaultUniverse)
  const [warnings, setWarnings] = useState<string[]>([])
  const [parseError, setParseError] = useState<string | null>(null)
  const [savedId, setSavedId] = useState<string | undefined>()
  const [saveName, setSaveName] = useState('')

  const parse = useMutation({
    mutationFn: tdxApi.parse,
    onSuccess: result => {
      setConditions(result.conditions.map(conditionFromParsed))
      // 公式里带的股票池过滤只管几个开关；流通市值和行业的限制保持页面上现有的
      setUniverse(u => ({ ...u, ...Object.fromEntries(FLAGS.map(f => [f.key, result.universe[f.key]])) }))
      setWarnings(result.warnings)
      setParseError(null)
      setSavedId(undefined)
    },
    onError: e => setParseError(errorText(e)),
  })

  const body = useMemo(() => ({ conditions, universe }), [conditions, universe])
  const enabledCount = conditions.filter(c => c.enabled).length
  const ready = Boolean(status.data?.has_data) && enabledCount > 0
  const capInvalid = universe.min_float_cap != null && universe.max_float_cap != null
    && universe.min_float_cap > universe.max_float_cap

  // ---------- 信号统计：条件、参数、股票范围一变，停手半秒后自动重算；只认最后一次的结果 ----------
  const [stats, setStats] = useState<StatsResult | null>(null)
  const [statsError, setStatsError] = useState<string | null>(null)
  const [computing, setComputing] = useState(false)
  const [picks, setPicks] = useState<{ date: string; items: Pick[] } | null>(null)
  const [signals, setSignals] = useState<{ total: number; items: SignalItem[] } | null>(null)
  const seq = useRef(0)
  const bodyKey = JSON.stringify(body)

  useEffect(() => {
    const mine = ++seq.current
    if (!ready || capInvalid) {
      setStats(null); setPicks(null); setSignals(null); setStatsError(null); setComputing(false)
      return
    }
    setComputing(true)
    const timer = window.setTimeout(async () => {
      try {
        const request = JSON.parse(bodyKey) as typeof body
        const [s, p, list] = await Promise.all([tdxApi.stats(request), tdxApi.picks(request), tdxApi.signals(request)])
        if (mine !== seq.current) return
        setStats(s); setPicks(p); setSignals(list); setStatsError(null)
      } catch (e) {
        if (mine !== seq.current) return
        setStatsError(errorText(e))
      } finally {
        if (mine === seq.current) setComputing(false)
      }
    }, 500)
    return () => window.clearTimeout(timer)
  }, [bodyKey, ready, capInvalid])

  // ---------- 保存、载入、删除 ----------
  const save = useMutation({
    mutationFn: () => tdxApi.save({ id: savedId, name: saveName, formula, conditions, universe }),
    onSuccess: item => {
      setSavedId(item.id)
      void qc.invalidateQueries({ queryKey: tdxKeys.saved })
      toast(`已保存“${item.name}”`, 'success')
    },
    onError: e => toast(errorText(e)),
  })
  const remove = useMutation({
    mutationFn: tdxApi.remove,
    onSuccess: (_ok, id) => {
      if (id === savedId) setSavedId(undefined)
      void qc.invalidateQueries({ queryKey: tdxKeys.saved })
      toast('已删除', 'success')
    },
    onError: e => toast(errorText(e)),
  })

  function load(item: Saved) {
    setFormula(item.formula)
    setConditions(item.conditions)
    setUniverse({ ...defaultUniverse(), ...item.universe })
    setWarnings([])
    setParseError(null)
    setSavedId(item.id)
    setSaveName(item.name)
  }

  const setCondition = (index: number, next: Condition) =>
    setConditions(list => list.map((c, i) => (i === index ? next : c)))
  const setCap = (key: 'min_float_cap' | 'max_float_cap', raw: string) => {
    const value = Number(raw)
    setUniverse(u => ({ ...u, [key]: raw.trim() !== '' && Number.isFinite(value) && value >= 0 ? value : null }))
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="通达信公式"
        subtitle="粘贴通达信公式, 拆成可以调参数的条件, 在全市场历史上统计信号 · 行情数据和计算内核来自 quant-lab"
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-7xl space-y-4">
          <DataSource status={status.data} loading={status.isLoading} error={status.error ? errorText(status.error) : null} />

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {/* 左：公式和条件 */}
            <section className="space-y-3 rounded-card border border-border bg-surface p-4">
              <div className="flex items-center justify-between gap-2">
                <h2 className="text-sm font-medium text-foreground">① 粘贴公式</h2>
                <button className={BUTTON} onClick={() => setFormula(EXAMPLE)}>填入示例</button>
              </div>
              <textarea
                value={formula}
                onChange={e => setFormula(e.target.value)}
                rows={7}
                spellCheck={false}
                aria-label="通达信公式"
                placeholder="把通达信的选股公式粘贴到这里，例如：XG:C>MA(C,20) AND V>MA(V,5)*1.5;"
                className="w-full rounded-input border border-border bg-base p-2 font-mono text-xs leading-5 text-foreground"
              />
              <div className="flex flex-wrap items-center gap-2">
                <button className={PRIMARY} disabled={!formula.trim() || parse.isPending} onClick={() => parse.mutate(formula)}>
                  <Play className="h-3.5 w-3.5" />{parse.isPending ? '解析中…' : '解析公式'}
                </button>
                <span className="text-[11px] text-muted">含未来函数的公式会被拒绝并说明原因；画图语句会被忽略。</span>
              </div>
              {parseError && (
                <p role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{parseError}</p>
              )}
              {warnings.map((w, i) => (
                <p key={i} className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{w}</p>
              ))}

              <h2 className="pt-2 text-sm font-medium text-foreground">② 条件和参数</h2>
              {conditions.length === 0 ? (
                <p className="rounded-card border border-dashed border-border px-4 py-6 text-center text-xs text-muted">
                  还没有条件。粘贴公式后点“解析公式”，公式里用 AND 连起来的每一部分会变成这里的一个条件。
                </p>
              ) : (
                <ul className="space-y-2">
                  {conditions.map((c, index) => (
                    <li key={index} className="rounded-card border border-border bg-base p-3">
                      <label className="flex cursor-pointer items-start gap-2">
                        <input
                          type="checkbox"
                          checked={c.enabled}
                          onChange={e => setCondition(index, { ...c, enabled: e.target.checked })}
                          className="mt-0.5"
                        />
                        <span className="min-w-0">
                          <span className="block text-xs font-medium text-foreground">{conditionName(c)}</span>
                          {c.pasted?.description && (
                            <span className="block break-all text-[11px] text-muted">{c.pasted.description}</span>
                          )}
                        </span>
                      </label>
                      {c.pasted && c.pasted.params_spec.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-2 pl-5">
                          {c.pasted.params_spec.map(p => (
                            <label key={p.key} className="flex items-center gap-1.5 text-[11px] text-secondary">
                              {p.label}
                              <input
                                type="number"
                                value={c.params[p.key] ?? p.default}
                                min={p.max > p.min ? p.min : undefined}
                                max={p.max > p.min ? p.max : undefined}
                                step={p.step || 'any'}
                                disabled={!c.enabled}
                                onChange={e => setCondition(index, withParam(c, p.key, e.target.value))}
                                className={`${INPUT} w-24 disabled:opacity-50`}
                              />
                              {p.unit}
                            </label>
                          ))}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}

              <h2 className="pt-2 text-sm font-medium text-foreground">③ 股票范围</h2>
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                {FLAGS.map(f => (
                  <label key={f.key} className="flex cursor-pointer items-center gap-1.5 text-xs text-secondary">
                    <input
                      type="checkbox"
                      checked={universe[f.key]}
                      onChange={e => setUniverse(u => ({ ...u, [f.key]: e.target.checked }))}
                    />
                    {f.label}
                  </label>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-secondary">
                流通市值
                <input type="number" min={0} placeholder="不限" aria-label="流通市值下限"
                  value={universe.min_float_cap ?? ''} onChange={e => setCap('min_float_cap', e.target.value)}
                  className={`${INPUT} w-24`} />
                到
                <input type="number" min={0} placeholder="不限" aria-label="流通市值上限"
                  value={universe.max_float_cap ?? ''} onChange={e => setCap('max_float_cap', e.target.value)}
                  className={`${INPUT} w-24`} />
                亿元
                <span className="text-[11px] text-muted">留空表示不限</span>
              </div>
              {capInvalid && <p className="text-xs text-danger">流通市值的下限不能比上限大。</p>}
              {(universe.min_float_cap != null || universe.max_float_cap != null) && status.data?.has_data
                && !status.data.float_shares_ready && (
                <p className="text-xs text-warning">
                  quant-lab 的历史流通股本还没有补齐。没有这项数据的股票不会被选中，统计结果会偏少。
                </p>
              )}

              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
                <input
                  value={saveName}
                  onChange={e => setSaveName(e.target.value)}
                  placeholder="起个名字"
                  aria-label="保存的名字"
                  maxLength={60}
                  className={`${INPUT} w-44`}
                />
                <button
                  className={BUTTON}
                  disabled={conditions.length === 0 || !saveName.trim() || save.isPending}
                  onClick={() => save.mutate()}
                >
                  <Save className="h-3.5 w-3.5" />{savedId ? '保存修改' : '保存'}
                </button>
                <span className="text-[11px] text-muted">保存在本机，不会上传。</span>
              </div>
              {(saved.data?.length ?? 0) > 0 && (
                <div>
                  <h3 className="mb-1 text-xs text-muted">保存过的</h3>
                  <ul className="space-y-1">
                    {saved.data?.map(item => (
                      <li key={item.id} className="flex items-center justify-between gap-2 rounded-btn px-2 py-1 hover:bg-elevated">
                        <button className="min-w-0 flex-1 cursor-pointer truncate text-left text-xs text-foreground" onClick={() => load(item)}>
                          {item.name}
                          {item.id === savedId && <span className="ml-2 text-[10px] text-accent">当前</span>}
                          <span className="ml-2 text-[10px] text-muted">{item.conditions.length} 个条件 · {item.updated_at.slice(0, 16)}</span>
                        </button>
                        <button
                          title="删除"
                          aria-label={`删除 ${item.name}`}
                          disabled={remove.isPending}
                          onClick={() => { if (window.confirm(`删除“${item.name}”？`)) remove.mutate(item.id) }}
                          className="cursor-pointer rounded p-1 text-muted hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>

            {/* 右：统计结果 */}
            <section className="space-y-3 rounded-card border border-border bg-surface p-4">
              <div className="flex items-center justify-between gap-2">
                <h2 className="text-sm font-medium text-foreground">④ 历史信号统计</h2>
                {computing && <span className="text-[11px] text-muted">计算中…全市场第一次读取要十几秒</span>}
              </div>
              {!status.data?.has_data ? (
                <Hint>还没有行情数据，先在上面设置 quant-lab 的行情目录。</Hint>
              ) : enabledCount === 0 ? (
                <Hint>解析公式并至少勾选一个条件，这里会自动算出历史上的信号统计。</Hint>
              ) : statsError ? (
                <p role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{statsError}</p>
              ) : !stats ? (
                <Hint>正在计算…</Hint>
              ) : (
                <Results stats={stats} picks={picks} signals={signals} costs={status.data.costs} />
              )}
            </section>
          </div>
        </div>
      </div>
    </div>
  )
}

function Hint({ children }: { children: React.ReactNode }) {
  return <p className="rounded-card border border-dashed border-border px-4 py-8 text-center text-xs text-muted">{children}</p>
}

/** 行情数据从哪里来；没找到时让用户填 quant-lab 的 data 目录 */
function DataSource({ status, loading, error }: { status: Status | undefined; loading: boolean; error: string | null }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [dir, setDir] = useState('')
  const setDataDir = useMutation({
    mutationFn: tdxApi.setDataDir,
    onSuccess: next => {
      qc.setQueryData(tdxKeys.status, next)
      setEditing(false)
      toast(next.has_data ? '行情目录已设置' : '已清除页面上设置的目录', 'success')
    },
    onError: e => toast(errorText(e)),
  })

  if (loading) return <p className="text-xs text-muted">正在检查行情数据…</p>
  if (error || !status) {
    return <p role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">行情数据的状态没有取到：{error ?? '未知原因'}</p>
  }
  const showForm = editing || !status.has_data
  return (
    <section className="rounded-card border border-border bg-surface px-4 py-3 text-xs text-secondary">
      {status.has_data ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>
            行情：<b className="text-foreground">{status.first_date}</b> 到 <b className="text-foreground">{status.latest_date}</b>，
            共 {status.stock_count.toLocaleString('en-US')} 只股票（含已退市的），后复权。
            <span className="ml-2 text-muted">来自 {status.data_dir}（{status.found_by}）</span>
          </span>
          <button className={BUTTON} onClick={() => { setDir(status.saved_data_dir); setEditing(v => !v) }}>
            <FolderCog className="h-3.5 w-3.5" />换目录
          </button>
        </div>
      ) : (
        <p className="text-warning">
          还没有找到 quant-lab 的行情数据。这一页用的是 quant-lab 下载到本机的全市场日线（免费来源、带复权和历史 ST），
          请先安装 quant-lab 并下载历史行情，再把它的 data 目录填在下面。
        </p>
      )}
      {showForm && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            value={dir}
            onChange={e => setDir(e.target.value)}
            placeholder="quant-lab 的 data 目录，例如 /home/me/quant-lab/data"
            aria-label="quant-lab 的 data 目录"
            className={`${INPUT} min-w-[280px] flex-1`}
          />
          <button className={PRIMARY} disabled={setDataDir.isPending} onClick={() => setDataDir.mutate(dir)}>保存</button>
          <span className="text-[11px] text-muted">留空保存表示不指定，改用环境变量或与本项目并排的 quant-lab。只读，不会改动那里的文件。</span>
        </div>
      )}
    </section>
  )
}

function Results({ stats, picks, signals, costs }: {
  stats: StatsResult
  picks: { date: string; items: Pick[] } | null
  signals: { total: number; items: SignalItem[] } | null
  costs: Status['costs']
}) {
  const [tab, setTab] = useState<'picks' | 'signals'>('picks')
  const rows = stats.stats.filter(r => r.entry === 'next_open')
  const preview = (code: string, name: string, list: { code: string; name: string }[]) =>
    openStockPreview({
      symbol: toPanelSymbol(code), name,
      navList: list.map(x => ({ symbol: toPanelSymbol(x.code), name: x.name })),
    })

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
                          <button className="cursor-pointer text-foreground hover:text-accent" onClick={() => preview(p.code, p.name, picks.items)}>
                            {p.name || p.code}
                          </button>
                          <span className="ml-2 font-mono text-[10px] text-muted">{toPanelSymbol(p.code)}</span>
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
                          <button className="cursor-pointer text-foreground hover:text-accent" onClick={() => preview(s.code, s.name, signals.items)}>
                            {s.name || s.code}
                          </button>
                          <span className="ml-2 font-mono text-[10px] text-muted">{toPanelSymbol(s.code)}</span>
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
