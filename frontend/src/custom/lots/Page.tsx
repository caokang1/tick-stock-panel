import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowUpRight, CalendarClock, Pencil, Plus, Search, Trash2 } from 'lucide-react'
import { api, type Lot } from '@/lib/api'
import { fetchDailyBatchChunked } from '@/lib/klineBatchChunked'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { DatePicker } from '@/components/DatePicker'
import { DateShortcuts } from '@/components/DateShortcuts'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toNavItems } from '@/lib/listNav'
import { boardTag } from '@/components/stock-table/primitives'
import { fetchHkQuotes, fxOf, hkKeys, hkSymbolFrom, isHk } from './hk'

const emptyDraft = (): Lot => ({
  id: '',
  symbol: '',
  qty: 0,
  cost_price: 0,
  buy_date: '',
  target_pct: 0,
  stop_pct: 0,
  remind_date: '',
  lead_days: 1,
})

/** 剩余天数单元格: 到期日 − 今天, 可为负 = 已超期; 无到期日 → — */
function RemainingDays({ remind }: { remind?: string | null }) {
  if (!remind) return <span className="text-muted/60">—</span>
  const remindMs = new Date(`${remind}T00:00:00`).getTime()
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const n = Math.floor((remindMs - today.getTime()) / 86400000)
  if (n < 0) return <span className="font-mono text-warning">已超期{-n}天</span>
  return <span className="font-mono text-secondary">{n}天</span>
}

/** 成本 vs 现价的盈亏% (纯价格比例, 无数量参与) */
/** 市值 = 数量 × 现价; 没填数量或没有现价时算不出来 */
/** fx: 折成人民币乘的数 (A 股 1, 港股是汇率; null = 没有汇率, 算不出) */
function marketValue(qty: number, close?: number, fx: number | null = 1): number | null {
  return qty > 0 && close != null && Number.isFinite(close) && fx != null ? qty * close * fx : null
}

function fmtMarketValue(v: number | null): string {
  return v == null ? '—' : v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function CostPnL({ close, cost, qty, fx = 1 }: { close?: number; cost: number; qty: number; fx?: number | null }) {
  if (close == null || !cost) return <span className="text-muted/60">—</span>
  // 成本为负 (落袋的盈利已超过本金): 比例没有意义, 改成显示盈亏金额
  if (cost < 0) {
    if (!(qty > 0) || fx == null) return <span className="text-muted/60">—</span>
    const amount = qty * (close - cost) * fx
    return (
      <span className={cn('font-mono', priceColorClass(amount))} title="成本为负, 无法算比例, 这里是盈亏金额">
        {amount >= 0 ? '+' : ''}{fmtMarketValue(amount)}
      </span>
    )
  }
  const pnl = (close - cost) / cost
  return <span className={cn('font-mono', priceColorClass(pnl))}>{fmtPct(pnl)}</span>
}

export function Lots() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editing, setEditing] = useState<Lot | null>(null) // null=关闭
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const lotsQuery = useQuery({ queryKey: QK.lots, queryFn: api.lotsList })
  const lots = lotsQuery.data?.lots ?? []

  // 港股不在面板的 A 股数据里: 名称和现价单独从港股报价取, 不参与下面的 A 股查询
  const allSymbols = useMemo(() => Array.from(new Set(lots.map(l => l.symbol).filter(s => !isHk(s)))), [lots])
  const hkSymbols = useMemo(() => Array.from(new Set(lots.map(l => l.symbol).filter(isHk))).sort(), [lots])
  const hkQuery = useQuery({
    queryKey: hkKeys.quotes(hkSymbols),
    queryFn: () => fetchHkQuotes(hkSymbols),
    enabled: hkSymbols.length > 0,
    // 跟着实时行情开关: 开着按同一个间隔刷新, 关着不刷新
    refetchInterval: q => (q.state.data?.live ? Math.max(10, q.state.data.interval) * 1000 : false),
  })
  const hk = hkQuery.data
  const namesQuery = useQuery({
    queryKey: QK.instrumentNames(allSymbols),
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300000,
  })
  const symbolNames = useMemo(() => {
    const names: Record<string, string> = { ...(namesQuery.data?.names ?? {}) }
    for (const q of Object.values(hk?.items ?? {})) if (q.name) names[q.symbol] = q.name
    return names
  }, [namesQuery.data, hk])

  // 9999 哨兵: 未记买入日期的排最后
  const sortedLots = useMemo(() => {
    return [...lots].sort((a, b) => (a.buy_date ?? '9999-12-31').localeCompare(b.buy_date ?? '9999-12-31'))
  }, [lots])
  const lotsNavItems = useMemo(
    () => toNavItems(sortedLots.filter(l => !isHk(l.symbol)).map(l => ({ symbol: l.symbol, name: symbolNames[l.symbol] }))),
    [sortedLots, symbolNames],
  )

  const dailyQuery = useQuery({
    queryKey: QK.lotsKline(allSymbols.join(',')),
    // 后端单次 symbols 上限 400, 分块拉取合并 (响应形状不变)
    queryFn: () => fetchDailyBatchChunked(allSymbols, 5),
    enabled: allSymbols.length > 0,
    staleTime: 60000,
  })
  const lastPrices = useMemo(() => {
    const m: Record<string, number> = {}
    for (const [sym, rows] of Object.entries(dailyQuery.data?.data ?? {})) {
      const last = rows[rows.length - 1]
      if (last?.close != null) m[sym] = Number(last.close)
    }
    for (const q of Object.values(hk?.items ?? {})) m[q.symbol] = q.last // 港元
    return m
  }, [dailyQuery.data, hk])

  const del = useMutation({
    mutationFn: api.lotDelete,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      setConfirmId(null)
    },
  })

  // 删除: 第一次进确认态, 第二次真删, 3 秒后自动复位 (与监控中心一致)
  const handleClickDelete = (id: string) => {
    if (confirmId === id) {
      if (resetTimer.current) clearTimeout(resetTimer.current)
      setConfirmId(null)
      del.mutate(id)
    } else {
      setConfirmId(id)
      if (resetTimer.current) clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setConfirmId(null), 3000)
    }
  }

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="持仓提醒" subtitle="记录个股 / ETF 买入批次, 自动生成止盈止损 / 到期监控规则" />
      <div className="flex-1 min-h-0 px-5 py-4">
        <div className="mx-auto max-w-5xl space-y-4">
          <div className="flex items-center justify-between">
            <div className="text-xs text-secondary">{lots.length} 个批次</div>
            <button
              onClick={() => setEditing(emptyDraft())}
              className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/15 cursor-pointer"
            >
              <Plus className="h-3.5 w-3.5" />新增批次
            </button>
          </div>

          {lotsQuery.isLoading ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center text-xs text-muted">加载中…</div>
          ) : lotsQuery.isError ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-xs text-danger">批次加载失败</div>
              <button
                onClick={() => lotsQuery.refetch()}
                className="mt-2 rounded-btn border border-border px-3 py-1 text-[11px] text-secondary hover:bg-elevated cursor-pointer"
              >
                重试
              </button>
            </div>
          ) : lots.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-sm text-muted">还没有批次</div>
              <div className="mt-1 text-[11px] text-muted/70">记录一笔买入后, 系统会按成本价 ± 止盈/止损% 生成价格监控; 填了到期日则自动生成到期提醒。这里只用于生成提醒, 不是持仓记账。</div>
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border bg-surface/40 shadow-sm">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-border/60 bg-surface/60 text-[10px] uppercase tracking-wide text-muted">
                      <th className="px-4 py-2 font-medium">标的</th>
                      <th className="px-2 py-2 font-medium text-right">数量(参考)</th>
                      <th className="px-2 py-2 font-medium text-right">成本价</th>
                      <th className="px-2 py-2 font-medium text-right">现价</th>
                      <th className="px-2 py-2 font-medium text-right">市值</th>
                      <th className="px-2 py-2 font-medium text-right">盈亏%</th>
                      <th className="px-2 py-2 font-medium text-right">止盈%</th>
                      <th className="px-2 py-2 font-medium text-right">止损%</th>
                      <th className="px-2 py-2 font-medium">买入日期</th>
                      <th className="px-2 py-2 font-medium text-right">剩余天数</th>
                      <th className="px-2 py-2 font-medium">到期提醒</th>
                      <th className="px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {sortedLots.map(lot => (
                      <tr key={lot.id} className="border-b border-border/40 last:border-0 hover:bg-elevated/40">
                        <td className="px-4 py-2.5">
                          <button
                            onClick={() => { if (!isHk(lot.symbol)) setPreviewSymbol(lot.symbol) }}
                            title={isHk(lot.symbol) ? '港股: 只有报价, 没有 K 线; 成本和现价是港元, 市值折成人民币' : `查看 ${lot.symbol} 日K`}
                            className="inline-flex items-center gap-1.5 min-w-0 hover:bg-elevated/50 rounded px-0.5 py-0.5 transition-colors cursor-pointer"
                          >
                            <span className="font-mono font-medium text-foreground">{lot.symbol}</span>
                            {isHk(lot.symbol) && <span className="inline-flex items-center justify-center rounded px-1 text-[9px] font-bold leading-tight border text-sky-400 bg-sky-400/12 border-sky-400/25">港</span>}
                            {(() => { const b = boardTag(lot.symbol); return b && <span className={`inline-flex items-center justify-center rounded px-1 text-[9px] font-bold leading-tight border ${b.color}`}>{b.label}</span> })()}
                            {symbolNames[lot.symbol] && <span className="text-secondary truncate max-w-28">{symbolNames[lot.symbol]}</span>}
                          </button>
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">{lot.qty}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-foreground">{lot.cost_price ? `${isHk(lot.symbol) ? 'HK$' : ''}${lot.cost_price}` : '—'}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">{lastPrices[lot.symbol] != null ? `${isHk(lot.symbol) ? 'HK$' : ''}${fmtPrice(lastPrices[lot.symbol], isHk(lot.symbol) ? 3 : 2)}` : '—'}</td>
                        <td className="px-2 py-2.5 text-right font-mono tabular-nums text-foreground">{fmtMarketValue(marketValue(lot.qty, lastPrices[lot.symbol], fxOf(lot.symbol, hk?.rate)))}</td>
                        <td className="px-2 py-2.5 text-right"><CostPnL close={lastPrices[lot.symbol]} cost={lot.cost_price} qty={lot.qty} fx={fxOf(lot.symbol, hk?.rate)} /></td>
                        <td className="px-2 py-2.5 text-right font-mono text-bull">{lot.target_pct > 0 ? `${lot.target_pct}%` : '—'}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-bear">{lot.stop_pct > 0 ? `${lot.stop_pct}%` : '—'}</td>
                        <td className="px-2 py-2.5 text-muted">{lot.buy_date || '—'}</td>
                        <td className="px-2 py-2.5 text-right"><RemainingDays remind={lot.remind_date} /></td>
                        <td className="px-2 py-2.5">
                          {lot.remind_date ? (
                            <span className="inline-flex items-center gap-1 text-rose-400">
                              <CalendarClock className="h-3 w-3" />
                              {lot.remind_date}
                              {lot.lead_days > 0 && <span className="text-muted">· 提前{lot.lead_days}天</span>}
                            </span>
                          ) : <span className="text-muted/60">—</span>}
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex items-center justify-end gap-0.5">
                            <button
                              onClick={() => setEditing(lot)}
                              title="编辑"
                              className="p-1.5 rounded-md text-secondary transition-all hover:bg-accent/10 hover:text-accent cursor-pointer"
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </button>
                            {confirmId === lot.id ? (
                              <button
                                onClick={() => handleClickDelete(lot.id)}
                                title="再次点击确认删除"
                                className="inline-flex items-center gap-1 rounded-md bg-danger/15 px-1.5 py-0.5 text-[9px] font-medium text-danger border border-danger/30 animate-pulse cursor-pointer"
                              >
                                <Trash2 className="h-2.5 w-2.5" />确认
                              </button>
                            ) : (
                              <button
                                onClick={() => handleClickDelete(lot.id)}
                                title="删除 (同步删除生成的监控规则)"
                                className="p-1.5 rounded-md text-secondary transition-all hover:bg-danger/10 hover:text-danger cursor-pointer"
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="border-t border-border/60 bg-surface/60 text-[11px]">
                      <td className="px-4 py-2 text-muted" colSpan={4}>市值合计</td>
                      <td className="px-2 py-2 text-right font-mono font-semibold tabular-nums text-foreground">
                        {fmtMarketValue(sortedLots.reduce<number | null>((sum, lot) => {
                          const v = marketValue(lot.qty, lastPrices[lot.symbol], fxOf(lot.symbol, hk?.rate))
                          return v == null ? sum : (sum ?? 0) + v
                        }, null))}
                      </td>
                      <td className="px-2 py-2 text-muted" colSpan={7}>
                        {hkSymbols.length > 0 && (
                          hk?.rate
                            ? `港股按 1 港元 = ${hk.rate.toFixed(4)} 人民币折算 · 港股报价 ${hk.fetched_at ?? '—'}${hk.live ? '' : ' (实时行情关着, 收盘后更新一次)'}${hk.error ? ` · 这次没取到: ${hk.error}` : ''}`
                            : hkQuery.isLoading ? '港股报价加载中…' : `港股报价还没取到${hk?.error ? `: ${hk.error}` : hkQuery.isError ? ', 请稍后刷新' : ''}`
                        )}
                      </td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </div>
          )}

          <div className="flex items-center justify-center gap-1 text-[11px] text-muted">
            生成的止盈止损 / 到期提醒规则已同步至监控中心
            <button onClick={() => navigate('/monitor')} className="inline-flex items-center gap-0.5 text-accent hover:text-accent/80 cursor-pointer">
              去查看 <ArrowUpRight className="h-3 w-3" />
            </button>
          </div>
        </div>
      </div>

      {editing && <LotDialog lot={editing} onClose={() => setEditing(null)} />}

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol ? symbolNames[previewSymbol] : undefined}
        navList={lotsNavItems}
        onNavigate={(sym) => setPreviewSymbol(sym)}
        onClose={() => setPreviewSymbol(null)}
      />
    </div>
  )
}

function LotDialog({ lot, onClose }: { lot: Lot; onClose: () => void }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Lot>(() => ({ ...lot }))
  const [symbolQuery, setSymbolQuery] = useState('')
  const [error, setError] = useState('')

  // 数字字段用本地字符串承载 (可先清空再输入), 提交时才解析成数值;
  // 否则受控 number + parseFloat 会在清空瞬间把值塞回 0, 导致「0 去不掉」。
  const [nums, setNums] = useState<Record<string, string>>(() => {
    const f = (v: number | undefined | null) => (v == null || v === 0 ? '' : String(v))
    return {
      qty: f(lot.qty), cost_price: f(lot.cost_price),
      target_pct: f(lot.target_pct), stop_pct: f(lot.stop_pct),
      lead_days: f(lot.lead_days),
    }
  })
  const numField = (key: string) => ({
    value: nums[key] ?? '',
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setNums(s => ({ ...s, [key]: e.target.value })),
  })

  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbolQuery, 'stock,etf'),
    queryFn: () => api.instrumentSearch(symbolQuery, 20, 'stock,etf'),
    enabled: symbolQuery.trim().length > 0,
  })

  const hkCandidate = hkSymbolFrom(symbolQuery)

  const save = useMutation({
    mutationFn: (vals: Partial<Lot>) => api.lotSave({ ...draft, ...vals, symbol: draft.symbol.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      onClose()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  const parseNum = (s: string | undefined) => {
    const n = parseFloat(s ?? '')
    return Number.isFinite(n) ? n : 0
  }

  const submit = () => {
    setError('')
    if (!draft.symbol.trim()) return setError('请选择标的')
    const vals: Partial<Lot> = {
      qty: parseNum(nums.qty),
      cost_price: parseNum(nums.cost_price),
      target_pct: parseNum(nums.target_pct),
      stop_pct: parseNum(nums.stop_pct),
      lead_days: Math.floor(parseNum(nums.lead_days)),
    }
    if ((vals.qty ?? 0) < 0 || (vals.target_pct ?? 0) < 0 || (vals.stop_pct ?? 0) < 0 || (vals.lead_days ?? 0) < 0) {
      return setError('数量 / 百分比 / 提前天数不能为负数')
    }
    // 成本价、止盈、止损、到期日都可以先不填 (只登记持仓, 不产生提醒); 但设了止盈/止损就得有成本价
    if (isHk(draft.symbol) && ((vals.target_pct ?? 0) > 0 || (vals.stop_pct ?? 0) > 0)) {
      return setError('港股只有报价, 面板监控不到, 不能设止盈止损 (到期提醒可以设)')
    }
    if (((vals.target_pct ?? 0) > 0 || (vals.stop_pct ?? 0) > 0) && !((vals.cost_price ?? 0) > 0)) {
      return setError('设了止盈% 或止损% 时必须填大于 0 的成本价')
    }
    save.mutate(vals)
  }

  return (
    <Modal onClose={onClose} ariaLabel={lot.id ? '编辑批次' : '新增批次'} panelClassName="w-[92vw] max-w-md bg-surface border border-border rounded-card shadow-xl">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-3">
        <span className="text-sm font-medium text-foreground">{lot.id ? '编辑批次' : '新增批次'}</span>
        <span className="text-[10px] text-muted">保存后自动同步监控规则</span>
      </div>
      <div className="space-y-3 px-4 py-4">
        {/* 标的 */}
        <div className="space-y-1.5">
          <span className="text-[11px] text-muted">标的</span>
          {draft.symbol ? (
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1 rounded bg-elevated px-2 py-1 font-mono text-[11px] text-secondary">
                {draft.symbol}
                <button onClick={() => setDraft(d => ({ ...d, symbol: '' }))} className="text-muted hover:text-danger cursor-pointer"><span className="text-[10px]">✕</span></button>
              </span>
              <span className="text-[10px] text-muted">点 ✕ 可重选</span>
            </div>
          ) : (
            <div className="relative">
              <input
                value={symbolQuery}
                onChange={e => setSymbolQuery(e.target.value)}
                placeholder="搜索代码或名称; 港股输 5 位代码, 如 00981"
                autoFocus
                className="h-9 w-full rounded-btn border border-border bg-base pl-8 pr-3 text-xs text-foreground focus:outline-none focus:border-accent/50"
              />
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted" />
              {(hkCandidate || (symbolSearch.data && symbolSearch.data.results.length > 0)) && (
                <div className="absolute z-10 mt-1 max-h-48 w-full overflow-auto rounded border border-border bg-surface shadow-lg">
                  {hkCandidate && (
                    <button
                      onClick={() => { setDraft(d => ({ ...d, symbol: hkCandidate })); setSymbolQuery('') }}
                      className="block w-full px-2.5 py-1.5 text-left text-[11px] hover:bg-elevated cursor-pointer"
                    >
                      <span className="font-mono text-foreground/80">{hkCandidate}</span>
                      <span className="ml-1.5 text-muted">按港股登记 (成本填港元)</span>
                    </button>
                  )}
                  {(symbolSearch.data?.results ?? []).map(r => (
                    <button
                      key={r.symbol}
                      onClick={() => { setDraft(d => ({ ...d, symbol: r.symbol })); setSymbolQuery('') }}
                      className="block w-full px-2.5 py-1.5 text-left text-[11px] hover:bg-elevated cursor-pointer"
                    >
                      <span className="font-mono text-foreground/80">{r.symbol}</span>
                      <span className="ml-1.5 text-muted">{r.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">数量 (参考)</span>
            <input type="number" min={0} placeholder="0" {...numField('qty')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">成本价 (可先不填, 可为负)</span>
            <input type="number" step="any" placeholder="0" {...numField('cost_price')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止盈%</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('target_pct')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止损%</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('stop_pct')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">买入日期 (可选)</span>
            <DateShortcuts value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} options={[{ label: '今天', days: 0 }]} />
            <DatePicker value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} placeholder="不记录" />
          </div>
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">到期日 (可选)</span>
            <DateShortcuts value={draft.remind_date ?? ''} onChange={v => setDraft(d => ({ ...d, remind_date: v || null }))} options={[{ label: '5天', days: 5 }, { label: '10天', days: 10 }, { label: '15天', days: 15 }]} base={draft.buy_date || undefined} />
            <DatePicker value={draft.remind_date ?? ''} onChange={v => setDraft(d => ({ ...d, remind_date: v || null }))} placeholder="不提醒" />
          </div>
        </div>

        {draft.remind_date && (
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">提前提醒天数</span>
            <input type="number" min={0} placeholder="1" {...numField('lead_days')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
            <span className="block text-[10px] text-muted">提醒仅在交易时段评估; 到期日若逢周末或长假, 请把提前天数调大些 (建议 ≥ 2, 长假更大)</span>
          </label>
        )}

        {error && <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] text-danger">{error}</div>}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-border/60 px-4 py-3">
        <button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated cursor-pointer">取消</button>
        <button
          onClick={submit}
          disabled={save.isPending}
          className={cn('h-9 rounded-btn px-4 text-xs font-medium bg-accent/90 text-white hover:bg-accent cursor-pointer disabled:opacity-50')}
        >
          {save.isPending ? '保存中...' : '保存'}
        </button>
      </div>
    </Modal>
  )
}
