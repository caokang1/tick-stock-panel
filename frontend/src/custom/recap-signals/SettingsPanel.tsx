/**
 * 设置 → 复盘信号：看这一批信号出自哪天的复盘、有哪些、哪些价位没通过核对，
 * 可以按最新的复盘重新生成一遍，或者整个关掉。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { BellRing, RefreshCw } from 'lucide-react'
import { toast } from '@/components/Toast'
import { recapSignalApi, recapSignalKeys, signalText, type RecapSignalStatus } from './client'

const PRIMARY = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-40'

export function RecapSignalSettingsPanel() {
  const client = useQueryClient()
  const status = useQuery({ queryKey: recapSignalKeys.status, queryFn: recapSignalApi.status })
  const [error, setError] = useState<string | null>(null)
  const data = status.data
  const applied = (next: RecapSignalStatus, message: string) => {
    client.setQueryData(recapSignalKeys.status, next)
    setError(null)
    toast(message, 'success')
  }
  const run = useMutation({
    mutationFn: recapSignalApi.run,
    onSuccess: next => applied(next, next.signals.length ? `已生成 ${next.signals.length} 条信号` : '这份复盘没有可用的价位'),
    onError: (e: Error) => setError(e.message),
  })
  const toggle = useMutation({
    mutationFn: (on: boolean) => recapSignalApi.setEnabled(on),
    onSuccess: next => applied(next, next.enabled ? '已打开，下一份复盘出来后自动生成' : '已关闭，信号已清除'),
    onError: (e: Error) => setError(e.message),
  })

  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="mb-4 flex items-start gap-2.5">
        <BellRing className="mt-0.5 h-4 w-4 text-secondary" />
        <div>
          <h2 className="text-sm font-medium text-foreground">复盘信号</h2>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            每天收盘后的复盘生成好以后，把“持仓复盘”里写明的防守位、突破位变成监控提醒，只管下一个交易日。
            那个交易日走完就自动删掉，换成新一份复盘的信号。提醒和其他监控一样，要打开实时行情才会触发，触发后推到飞书。
          </p>
        </div>
      </div>

      {status.isLoading && <p className="py-6 text-center text-xs text-muted">正在读取…</p>}
      {status.error && <p className="py-6 text-center text-xs text-bear">没有读到：{(status.error as Error).message}</p>}
      {data && (
        <>
          <div className="rounded-lg border border-border/60 bg-elevated/20 px-3 py-2 text-xs text-secondary">
            <p>{data.message ?? '还没有生成过信号'}</p>
            <div className="mt-2 flex flex-wrap items-center gap-3">
              <button className={PRIMARY} disabled={run.isPending || !data.enabled} onClick={() => { setError(null); run.mutate() }}>
                <RefreshCw className="h-3.5 w-3.5" />{run.isPending ? '生成中（约一分钟）…' : '按最新复盘重新生成'}
              </button>
              <label className="inline-flex cursor-pointer items-center gap-1.5">
                <input type="checkbox" checked={data.enabled} disabled={toggle.isPending} onChange={e => toggle.mutate(e.target.checked)} />
                复盘后自动生成
              </label>
              <Link to="/monitor" className="text-accent hover:underline">去监控中心查看</Link>
            </div>
            {error && <p className="mt-2 text-bear">{error}</p>}
          </div>

          {data.signals.length > 0 && (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-left text-muted">
                    <th className="py-1.5 pr-3 font-normal">股票</th>
                    <th className="py-1.5 pr-3 font-normal">触发条件</th>
                    <th className="py-1.5 font-normal">复盘里的做法</th>
                  </tr>
                </thead>
                <tbody>
                  {data.signals.map(s => (
                    <tr key={s.rule_id} className="border-b border-border/40">
                      <td className="py-1.5 pr-3 whitespace-nowrap text-foreground">{s.name}</td>
                      <td className={`py-1.5 pr-3 whitespace-nowrap tabular-nums ${s.direction === 'below' ? 'text-bear' : 'text-bull'}`}>{signalText(s)}</td>
                      <td className="py-1.5 text-secondary">{s.action}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {(data.dropped.length > 0 || data.skipped_hk.length > 0) && (
            <div className="mt-3 text-[11px] leading-relaxed text-muted">
              {data.dropped.length > 0 && <p>没有采用的价位：{data.dropped.join('；')}。</p>}
              {data.skipped_hk.length > 0 && <p>港股持仓（{data.skipped_hk.join('、')}）不在监控范围内，没有生成信号。</p>}
            </div>
          )}
        </>
      )}
    </section>
  )
}
