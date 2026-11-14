import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { toast } from '@/components/Toast'
import { tdxApi, tdxKeys, type StrategyDetail, type StrategySummary } from './client'
import { formFromConfig, pct, statusLabel, tone, type StrategyForm } from './logic'
import { StrategyEditor } from './StrategyEditor'
import {
  BODY_ROW, BUTTON, CARD, ErrorNote, HEAD_ROW, Hint, LINK, LINK_DANGER, LINK_MUTED, PRIMARY, TH, Tag, errorText,
} from './ui'

interface Draft {
  /** 每载入一次加一，编辑区据此整个重来，不会混进上一个策略的内容 */
  key: number
  id: string | null
  name: string
  running: boolean
  form: StrategyForm
}

/** 「策略」页：上面是策略列表，下面是编辑区（公式、条件、股票范围、排名、买卖规则、择时、回测）。 */
export function StrategyTab({ onGoToday }: { onGoToday: () => void }) {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: tdxKeys.status, queryFn: tdxApi.status, staleTime: 30_000 })
  const options = useQuery({ queryKey: tdxKeys.options, queryFn: tdxApi.options, staleTime: Infinity })
  const list = useQuery({ queryKey: tdxKeys.strategies, queryFn: tdxApi.strategies, staleTime: 30_000 })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [justStarted, setJustStarted] = useState<string | null>(null)
  const dirty = useRef(false)
  const seq = useRef(0)

  const blank = (): Draft => ({ key: ++seq.current, id: null, name: '', running: false, form: formFromConfig({}, options.data) })
  // 选项到了之后才有编辑区；没有载入过策略时是一个空白的新策略
  const current = draft ?? (options.data ? { key: 0, id: null, name: '', running: false, form: formFromConfig({}, options.data) } : null)

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: tdxKeys.strategies })
    void qc.invalidateQueries({ queryKey: tdxKeys.today })
  }
  const leaveOk = () => !dirty.current || window.confirm('编辑区里的修改还没有保存，确定要放弃吗？')

  const load = useMutation({
    mutationFn: tdxApi.strategy,
    onSuccess: (detail: StrategyDetail) => {
      dirty.current = false
      setDraft({
        key: ++seq.current, id: detail.id, name: detail.name, running: detail.status === 'running',
        form: formFromConfig(detail.config, options.data),
      })
    },
    onError: e => toast(errorText(e)),
  })
  const start = useMutation({
    mutationFn: tdxApi.startStrategy,
    onSuccess: detail => {
      refresh()
      setJustStarted(detail.name)
      setDraft(d => (d && d.id === detail.id ? { ...d, running: true } : d))
      toast(`“${detail.name}”已设为运行中，第一份提醒已经生成`, 'success')
    },
    onError: e => toast(errorText(e)),
  })
  const stop = useMutation({
    mutationFn: tdxApi.stopStrategy,
    onSuccess: detail => {
      refresh()
      setDraft(d => (d && d.id === detail.id ? { ...d, running: false } : d))
      toast(`“${detail.name}”已停用，不再生成提醒`, 'success')
    },
    onError: e => toast(errorText(e)),
  })
  const remove = useMutation({
    mutationFn: tdxApi.deleteStrategy,
    onSuccess: (_ok, id) => {
      refresh()
      qc.removeQueries({ queryKey: tdxKeys.backtest(id) })
      if (draft?.id === id) { dirty.current = false; setDraft(blank()) }
      toast('已删除', 'success')
    },
    onError: e => toast(errorText(e)),
  })
  const busy = load.isPending || start.isPending || stop.isPending || remove.isPending

  const edit = (s: StrategySummary) => { if (leaveOk()) load.mutate(s.id) }
  const create = () => { if (leaveOk()) { dirty.current = false; setDraft(blank()) } }
  const del = (s: StrategySummary) => {
    if (window.confirm(`删除策略“${s.name}”？删除后不能恢复。`)) remove.mutate(s.id)
  }

  return (
    <div className="space-y-4">
      <section className={`${CARD} space-y-3`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-medium text-foreground">我的策略</h2>
          <button className={PRIMARY} disabled={busy || !options.data} onClick={create}>
            <Plus className="h-3.5 w-3.5" />新建策略
          </button>
        </div>
        {justStarted && (
          <div className="flex flex-wrap items-center gap-2 rounded-btn border border-accent/30 bg-accent/10 px-3 py-2 text-xs text-accent">
            “{justStarted}”已设为运行中，第一份提醒已经生成。以后每天收盘、行情更新后会自动生成新的提醒。
            <button className={PRIMARY} onClick={() => { setJustStarted(null); onGoToday() }}>去“今日提醒”查看</button>
          </div>
        )}
        {start.isPending && <p className="text-xs text-muted">正在设为运行中…第一次读取全市场数据要十几秒，请稍等。</p>}
        {list.isLoading ? <p className="text-xs text-muted">正在加载策略…</p>
          : list.error ? <ErrorNote>策略列表没有取到：{errorText(list.error)}</ErrorNote>
            : (list.data?.length ?? 0) === 0 ? (
              <Hint>还没有策略。在下面粘贴公式、设好买卖规则，起个名字点“保存”，就有了第一个策略。</Hint>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-right text-xs">
                  <thead>
                    <tr className={HEAD_ROW}>
                      <th className={`${TH} text-left`}>名称</th>
                      <th className={`${TH} text-left`}>状态</th>
                      <th className={TH}>条件</th>
                      <th className={TH}>回测年化收益</th>
                      <th className={TH}>回测最大回撤</th>
                      <th className={TH}>模拟账户收益</th>
                      <th className={TH}>实际持仓收益</th>
                      <th className={`${TH} pl-4 text-left`}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.data?.map(s => (
                      <tr key={s.id} className={BODY_ROW}>
                        <td className="py-2 text-left text-foreground">
                          {s.name}
                          {s.id === current?.id && <span className="ml-2 text-[10px] text-accent">正在编辑</span>}
                        </td>
                        <td className="text-left">
                          <Tag tone={s.status === 'running' ? 'accent' : 'muted'}>{statusLabel(s.status)}</Tag>
                        </td>
                        <td>{s.condition_count} 个</td>
                        <td className={tone(s.backtest?.annual_return)}>{s.backtest ? pct(s.backtest.annual_return) : '还没回测'}</td>
                        <td>{s.backtest ? pct(s.backtest.max_drawdown) : '—'}</td>
                        <td className={tone(s.live?.total_return)}>{s.live ? pct(s.live.total_return) : '—'}</td>
                        <td className={tone(s.real_return)}>{pct(s.real_return)}</td>
                        <td className="space-x-3 whitespace-nowrap pl-4 text-left">
                          <button className={LINK} disabled={busy || !options.data} onClick={() => edit(s)}>编辑</button>
                          {s.status === 'running' ? (
                            <button className={LINK_MUTED} disabled={busy} onClick={() => stop.mutate(s.id)}>停用</button>
                          ) : (
                            <button className={LINK} disabled={busy} onClick={() => start.mutate(s.id)}>
                              {start.isPending && start.variables === s.id ? '正在设置…' : '设为运行中'}
                            </button>
                          )}
                          <button className={LINK_DANGER} disabled={busy} onClick={() => del(s)}>删除</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        <p className="text-[11px] text-muted">
          “运行中”的策略每天收盘后按最新行情生成买卖提醒，并用一个模拟账户假设每条都照做，用来衡量策略本身。停用后不再生成提醒，记录都留着。
        </p>
      </section>

      {options.isLoading ? <p className="text-xs text-muted">正在加载编辑区…</p>
        : options.error || !options.data || !current ? (
          <div className="space-y-2">
            <ErrorNote>排名和择时的可选项没有取到：{errorText(options.error)}</ErrorNote>
            <button className={BUTTON} onClick={() => void options.refetch()}>再试一次</button>
          </div>
        ) : (
          <StrategyEditor
            key={current.key}
            initial={current}
            running={current.running}
            options={options.data}
            status={status.data}
            dirty={dirty}
            onSaved={detail => {
              refresh()
              setDraft(d => ({
                key: d?.key ?? current.key, id: detail.id, name: detail.name, running: detail.status === 'running',
                form: d?.form ?? current.form,
              }))
            }}
            onBacktested={() => void qc.invalidateQueries({ queryKey: tdxKeys.strategies })}
          />
        )}
    </div>
  )
}
