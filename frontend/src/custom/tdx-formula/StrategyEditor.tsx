import { useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LineChart, Play, Save } from 'lucide-react'
import { toast } from '@/components/Toast'
import { BacktestView } from './BacktestView'
import {
  tdxApi, tdxKeys,
  type BacktestResult, type Condition, type Options, type Pick, type SignalItem, type StatsResult, type Status,
  type StrategyDetail, type Universe,
} from './client'
import {
  conditionFromParsed, conditionName, configFromForm, withParam,
  type RankFactorForm, type StrategyForm, type TimingForm, type TradeForm,
} from './logic'
import { RankEditor, TimingEditor, TradeEditor } from './RulesEditor'
import { SignalResults } from './SignalResults'
import { BUTTON, CARD, ErrorNote, Hint, INPUT, PRIMARY, WarnNote, errorText } from './ui'

const EXAMPLE = `ZT:=C/REF(C,1)>=1.098 AND C=H;
XG:EXIST(ZT,12) AND C>MA(C,20) AND V>MA(V,5)*1.5;`

const FLAGS: { key: 'exclude_st' | 'exclude_star' | 'exclude_chinext' | 'exclude_bse' | 'exclude_suspended'; label: string }[] = [
  { key: 'exclude_st', label: '排除 ST' },
  { key: 'exclude_star', label: '排除科创板' },
  { key: 'exclude_chinext', label: '排除创业板' },
  { key: 'exclude_bse', label: '排除北交所' },
  { key: 'exclude_suspended', label: '排除当天停牌' },
]

const H2 = 'text-sm font-medium text-foreground'

/** 一个策略的编辑区。换一个策略时由外面换 key 整个重来。 */
export function StrategyEditor({ initial, running, options, status, dirty, onSaved, onBacktested }: {
  initial: { id: string | null; name: string; form: StrategyForm }
  running: boolean
  options: Options
  status: Status | undefined
  /** 有没有还没保存的修改，给外面在换策略前提醒用 */
  dirty: MutableRefObject<boolean>
  onSaved: (detail: StrategyDetail) => void
  onBacktested: () => void
}) {
  const qc = useQueryClient()
  const [id, setId] = useState(initial.id)
  const [name, setName] = useState(initial.name)
  const [formula, setFormula] = useState(initial.form.formula)
  const [conditions, setConditions] = useState<Condition[]>(initial.form.conditions)
  const [universe, setUniverse] = useState<Universe>(initial.form.universe)
  const [rank, setRank] = useState<RankFactorForm[]>(initial.form.rank)
  const [trade, setTrade] = useState<TradeForm>(initial.form.trade)
  const [timing, setTiming] = useState<TimingForm>(initial.form.timing)
  const [warnings, setWarnings] = useState<string[]>([])
  const [parseError, setParseError] = useState<string | null>(null)

  const form: StrategyForm = useMemo(
    () => ({ formula, conditions, universe, rank, trade, timing }),
    [formula, conditions, universe, rank, trade, timing],
  )
  // 和上次保存（或刚载入）时的内容比，判断有没有没保存的修改
  const snapshot = JSON.stringify({ name, form })
  const [savedSnapshot, setSavedSnapshot] = useState(snapshot)
  const isDirty = snapshot !== savedSnapshot
  useEffect(() => {
    dirty.current = isDirty
  }, [dirty, isDirty])

  const parse = useMutation({
    mutationFn: tdxApi.parse,
    onSuccess: result => {
      setConditions(result.conditions.map(conditionFromParsed))
      // 公式里带的股票池过滤只管几个开关；流通市值和行业的限制保持页面上现有的
      setUniverse(u => ({ ...u, ...Object.fromEntries(FLAGS.map(f => [f.key, result.universe[f.key]])) }))
      setWarnings(result.warnings)
      setParseError(null)
    },
    onError: e => setParseError(errorText(e)),
  })

  const body = useMemo(() => ({ conditions, universe }), [conditions, universe])
  const enabledCount = conditions.filter(c => c.enabled).length
  const ready = Boolean(status?.has_data) && enabledCount > 0
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

  // ---------- 保存 ----------
  const checked = configFromForm(form, options.rank_blocks)
  const save = useMutation({
    mutationFn: async () => {
      if (!checked.ok) throw new Error(checked.error)
      return id ? tdxApi.updateStrategy(id, name.trim(), checked.value) : tdxApi.createStrategy(name.trim(), checked.value)
    },
    onMutate: () => snapshot,
    onSuccess: (detail, _vars, saved) => {
      setId(detail.id)
      setSavedSnapshot(saved)
      onSaved(detail)
      toast(`已保存“${detail.name}”`, 'success')
    },
    onError: e => toast(errorText(e)),
  })

  // ---------- 组合回测：载入策略时显示上次保存的结果，点按钮后显示新的 ----------
  const savedBacktest = useQuery({
    queryKey: tdxKeys.backtest(id ?? ''),
    queryFn: () => tdxApi.savedBacktest(id ?? ''),
    enabled: Boolean(id),
    staleTime: Infinity,
  })
  const [fresh, setFresh] = useState<BacktestResult | null>(null)
  const [backtestError, setBacktestError] = useState<string | null>(null)
  const backtest = useMutation({
    mutationFn: async () => {
      if (!checked.ok) throw new Error(checked.error)
      return tdxApi.backtest(checked.value, id ?? undefined)
    },
    onMutate: () => ({ id }),
    onSuccess: (result, _vars, ctx) => {
      setFresh(result)
      setBacktestError(null)
      if (ctx?.id) {
        qc.setQueryData(tdxKeys.backtest(ctx.id), result)
        onBacktested()
      }
    },
    onError: e => { setBacktestError(errorText(e)); toast(errorText(e)) },
  })
  const shown = fresh ?? savedBacktest.data ?? null

  const setCondition = (index: number, next: Condition) =>
    setConditions(list => list.map((c, i) => (i === index ? next : c)))
  const setCap = (key: 'min_float_cap' | 'max_float_cap', raw: string) => {
    const value = Number(raw)
    setUniverse(u => ({ ...u, [key]: raw.trim() !== '' && Number.isFinite(value) && value >= 0 ? value : null }))
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className={H2}>{id ? `编辑策略：${initial.name || name}` : '新建策略'}</h2>
        {isDirty && <span className="text-[11px] text-warning">有修改还没有保存</span>}
        {running && <span className="text-[11px] text-muted">这个策略正在运行：保存后，从下一次生成提醒开始按新的内容来。</span>}
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {/* 左：公式、条件、股票范围、排名、买卖规则、择时 */}
        <section className={`${CARD} space-y-3`}>
          <div className="flex items-center justify-between gap-2">
            <h3 className={H2}>① 粘贴公式</h3>
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
          {parseError && <ErrorNote>{parseError}</ErrorNote>}
          {warnings.map((w, i) => <WarnNote key={i}>{w}</WarnNote>)}

          <h3 className={`${H2} pt-2`}>② 条件和参数</h3>
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

          <h3 className={`${H2} pt-2`}>③ 股票范围</h3>
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
          {(universe.min_float_cap != null || universe.max_float_cap != null) && status?.has_data
            && !status.float_shares_ready && (
            <p className="text-xs text-warning">
              quant-lab 的历史流通股本还没有补齐。没有这项数据的股票不会被选中，统计结果会偏少。
            </p>
          )}

          <h3 className={`${H2} pt-2`}>⑤ 排名<span className="ml-2 text-[11px] font-normal text-muted">同一天选出的股票太多时先买哪些</span></h3>
          <RankEditor blocks={options.rank_blocks} factors={rank} onChange={setRank} />

          <h3 className={`${H2} pt-2`}>⑥ 买卖规则</h3>
          <TradeEditor form={trade} onChange={setTrade} />

          <h3 className={`${H2} pt-2`}>⑦ 大盘择时</h3>
          <TimingEditor options={options} form={timing} onChange={setTiming} />

          <div className="space-y-2 border-t border-border pt-3">
            <div className="flex flex-wrap items-center gap-2">
              <input
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="给策略起个名字"
                aria-label="策略名称"
                maxLength={60}
                className={`${INPUT} w-52`}
              />
              <button
                className={PRIMARY}
                disabled={!checked.ok || !name.trim() || save.isPending || backtest.isPending}
                onClick={() => save.mutate()}
              >
                <Save className="h-3.5 w-3.5" />{save.isPending ? '保存中…' : id ? '保存修改' : '保存'}
              </button>
              <button
                className={BUTTON}
                disabled={!checked.ok || !status?.has_data || backtest.isPending || save.isPending}
                onClick={() => backtest.mutate()}
              >
                <LineChart className="h-3.5 w-3.5" />{backtest.isPending ? '回测中…' : '组合回测'}
              </button>
            </div>
            {!checked.ok && conditions.length > 0 && <p className="text-xs text-danger">{checked.error}</p>}
            <p className="text-[11px] text-muted">
              保存在本机，不会上传。组合回测按页面上现在的内容（包括还没保存的修改），在全部历史上模拟一个账户按这些规则买卖，通常要十几秒到一分钟。
            </p>
          </div>
        </section>

        {/* 右：统计结果 */}
        <section className={`${CARD} space-y-3`}>
          <div className="flex items-center justify-between gap-2">
            <h3 className={H2}>④ 历史信号统计</h3>
            {computing && <span className="text-[11px] text-muted">计算中…全市场第一次读取要十几秒</span>}
          </div>
          {!status?.has_data ? (
            <Hint>还没有行情数据，先到“设置”页设置 quant-lab 的行情目录。</Hint>
          ) : enabledCount === 0 ? (
            <Hint>解析公式并至少勾选一个条件，这里会自动算出历史上的信号统计。</Hint>
          ) : statsError ? (
            <ErrorNote>{statsError}</ErrorNote>
          ) : !stats ? (
            <Hint>正在计算…</Hint>
          ) : (
            <SignalResults stats={stats} picks={picks} signals={signals} costs={status.costs} />
          )}
        </section>
      </div>

      <section className={`${CARD} space-y-3`}>
        <h3 className={H2}>⑧ 组合回测结果</h3>
        {backtest.isPending ? (
          <Hint>正在回测…要在全部历史上逐日模拟买卖，通常十几秒到一分钟，请不要关闭页面。</Hint>
        ) : (
          <>
            {backtestError && <ErrorNote>这次回测没有成功：{backtestError}</ErrorNote>}
            {savedBacktest.error && !shown && <ErrorNote>上次的回测结果没有取到：{errorText(savedBacktest.error)}</ErrorNote>}
            {shown ? <BacktestView result={shown} fromSaved={!fresh} />
              : !backtestError && !savedBacktest.error && (
                <Hint>{savedBacktest.isLoading ? '正在读取上次的回测结果…' : '还没有回测过。设好上面的内容后点“组合回测”。'}</Hint>
              )}
          </>
        )}
      </section>
    </div>
  )
}
