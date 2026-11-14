import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { FolderCog } from 'lucide-react'
import { toast } from '@/components/Toast'
import { tdxApi, tdxKeys, type Status } from './client'
import { BUTTON, ErrorNote, INPUT, PRIMARY, errorText } from './ui'

/** 行情数据从哪里来；没找到时让用户填 quant-lab 的 data 目录 */
export function DataSource({ status, loading, error }: { status: Status | undefined; loading: boolean; error: string | null }) {
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
    return <ErrorNote>行情数据的状态没有取到：{error ?? '未知原因'}</ErrorNote>
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
