import { useId, useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { tdxApi, tdxKeys, type DataSourceInfo, type DataSources } from './client'
import {
  CAPABILITY_ORDER, LIMIT_FIELDS, capabilityLabel, dailyCapText, limitsBody, limitsForm, sourceState,
  type LimitKey, type LimitsForm,
} from './logic'
import { BODY_ROW, BUTTON, CARD, ErrorNote, HEAD_ROW, INPUT, LINK, PRIMARY, TH, Tag, WarnNote, errorText } from './ui'

const PANEL = 'max-h-[90vh] w-[94vw] max-w-xl overflow-y-auto rounded-card border border-border bg-surface p-4 shadow-xl'

/** 数据来源和限速：每个来源现在的状态、今天发了多少次请求，可以调整限速；下面是各类数据依次用哪些来源。 */
export function SourcesPanel({ data, loading, error, onRetry }: {
  data: DataSources | undefined
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const source = data?.sources.find(s => s.name === editing)
  const labelOf = (name: string) => data?.sources.find(s => s.name === name)?.label ?? name
  const priority = data?.priority ?? {}
  const capabilities = [
    ...CAPABILITY_ORDER.filter(key => key in priority),
    ...Object.keys(priority).filter(key => !CAPABILITY_ORDER.includes(key)),
  ]

  return (
    <section className={`${CARD} space-y-3`}>
      <h2 className="text-sm font-medium text-foreground">数据来源和限速</h2>
      <p className="text-[11px] leading-relaxed text-muted">
        这些来源都是免费公开的，用的时候要守它们的规矩：所有请求排队一个一个发、请求之间留间隔、每天有上限、被来源拒绝后自动暂停。
        下面的数值可以调，但调得太快可能被来源限制访问（BaoStock 会把地址列入黑名单）；数值不能超过来源官方公布的上限。不确定就不要改，默认值是偏保守的。
      </p>
      {warnings.length > 0 && (
        <WarnNote>已保存，但请留意：{warnings.join('；')}</WarnNote>
      )}
      {loading ? <p className="text-xs text-muted">正在加载数据来源…</p>
        : error || !data ? (
          <div className="space-y-2">
            <ErrorNote>数据来源没有取到：{error ?? '未知原因'}</ErrorNote>
            <button className={BUTTON} onClick={onRetry}>再试一次</button>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-xs text-secondary">
                <thead>
                  <tr className={`${HEAD_ROW} text-left`}>
                    <th className={TH}>来源</th>
                    <th className={TH}>状态</th>
                    <th className={`${TH} text-right`}>今天已请求</th>
                    <th className={`${TH} text-right`}>每日上限</th>
                    <th className={`${TH} text-right`}>最小间隔</th>
                    <th className={`${TH} text-right`}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {data.sources.map(s => {
                    const state = sourceState(s.state)
                    const official = s.policy.official_daily_limit
                    return (
                      <tr key={s.name} className={`${BODY_ROW} align-top`}>
                        <td className="py-2 pr-3 text-foreground">{s.label}</td>
                        <td className="max-w-[280px] py-2 pr-3">
                          <Tag tone={state.tone}>{state.label}</Tag>
                          {s.state_message && <div className="mt-1 text-[11px] text-muted">{s.state_message}</div>}
                          {s.cooldown_until && <div className="mt-1 text-[11px] text-muted">预计 {s.cooldown_until} 恢复</div>}
                        </td>
                        <td className="py-2 pr-3 text-right font-mono">{s.today_requests.toLocaleString('en-US')} 次</td>
                        <td className="py-2 pr-3 text-right">
                          <span className="font-mono">{dailyCapText(s.policy.daily_request_cap)}</span>
                          {official != null && (
                            <div className="mt-1 text-[11px] text-muted">官方上限 {official.toLocaleString('en-US')}</div>
                          )}
                        </td>
                        <td className="py-2 pr-3 text-right font-mono">{s.policy.min_interval_seconds} 秒</td>
                        <td className="py-2 text-right">
                          <button className={LINK} onClick={() => setEditing(s.name)}>调整限速</button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {capabilities.length > 0 && (
              <div className="space-y-1.5 border-t border-border pt-3">
                <h3 className="text-xs font-medium text-foreground">每一类数据依次用哪些来源</h3>
                <p className="text-[11px] text-muted">排在前面的先用；它失败、被限制或者正在暂停时，自动换下一个。这里只是说明，不能改。</p>
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                  {capabilities.map(key => (
                    <div key={key} className="contents">
                      <dt className="text-muted">{capabilityLabel(key)}</dt>
                      <dd className="text-secondary">{priority[key].map(labelOf).join(' → ') || '—'}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}
          </>
        )}
      {source && (
        <LimitsDialog
          key={source.name}
          source={source}
          onSaved={next => { setWarnings(next); setEditing(null) }}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  )
}

/** 调整一个来源的限速。每次打开都是新的一份，填的是这个来源现在生效的数值。 */
function LimitsDialog({ source, onSaved, onClose }: {
  source: DataSourceInfo
  onSaved: (warnings: string[]) => void
  onClose: () => void
}) {
  const qc = useQueryClient()
  const titleId = useId()
  const [form, setForm] = useState<LimitsForm>(() => limitsForm(source.policy))
  const [error, setError] = useState<string | null>(null)
  const official = source.policy.official_daily_limit
  const save = useMutation({
    mutationFn: (body: Parameters<typeof tdxApi.updateDataSource>[1]) => tdxApi.updateDataSource(source.name, body),
    onSuccess: ({ warnings, ...next }) => {
      qc.setQueryData(tdxKeys.dataSources, next)
      toast(`${source.label} 的限速已保存`, 'success')
      onSaved(warnings ?? [])
    },
    onError: e => setError(errorText(e)), // 超出允许范围时，后端会说明是哪一项、现在的值和允许的范围
  })

  const set = (key: LimitKey, value: string) => { setForm(f => ({ ...f, [key]: value })); setError(null) }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const parsed = limitsBody(form, source.policy)
    if (!parsed.ok) { setError(parsed.error); return }
    setError(null)
    save.mutate(parsed.value)
  }

  return (
    <Modal onClose={onClose} labelledBy={titleId} closeOnBackdrop={false} panelClassName={PANEL}>
      <form onSubmit={submit} className="space-y-3" noValidate>
        <h3 id={titleId} className="text-sm font-medium text-foreground">调整限速：{source.label}</h3>
        <label className="flex items-center gap-2 text-xs text-secondary">
          <input
            type="checkbox" checked={form.enabled}
            onChange={e => { setForm(f => ({ ...f, enabled: e.target.checked })); setError(null) }}
          />
          是否启用
          <span className="text-[11px] text-muted">不勾就不再用这个来源，默认{source.defaults.enabled ? '启用' : '停用'}</span>
        </label>
        <div className="space-y-2">
          {LIMIT_FIELDS.map(f => (
            <label key={f.key} className="grid grid-cols-[minmax(0,1fr)_96px] items-center gap-x-3 gap-y-0.5 text-xs text-secondary">
              {f.label}
              <input
                type="number" min={0} step={f.step} value={form[f.key]}
                onChange={e => set(f.key, e.target.value)}
                className={INPUT}
              />
              <span className="col-span-2 text-[11px] text-muted">
                {f.help}。默认 {f.key === 'daily_request_cap' ? dailyCapText(source.defaults[f.key]) : source.defaults[f.key]}
                {f.key === 'daily_request_cap' && official != null && `，官方公布的每日上限是 ${official.toLocaleString('en-US')}，不能超过`}
              </span>
            </label>
          ))}
        </div>
        <p className="text-[11px] text-muted">
          {official != null
            ? `这个来源官方公布了每日上限（${official.toLocaleString('en-US')} 次），超过或被限制后提前重试都可能被列入黑名单，所以上限和暂停时间有硬性的范围。`
            : '这个来源官方没有公布每日上限。'}
          调得越快，被限制访问的风险越大。
        </p>
        {error && <ErrorNote>{error}</ErrorNote>}
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <button
            type="button" className={BUTTON} disabled={save.isPending}
            onClick={() => { setForm(limitsForm(source.defaults)); setError(null) }}
            title="把各项填回默认值，还要点“保存”才生效"
          >
            <RotateCcw className="h-3.5 w-3.5" />恢复默认
          </button>
          <span className="flex gap-2">
            <button type="button" className={BUTTON} onClick={onClose}>取消</button>
            <button type="submit" className={PRIMARY} disabled={save.isPending}>{save.isPending ? '正在保存…' : '保存'}</button>
          </span>
        </div>
      </form>
    </Modal>
  )
}
