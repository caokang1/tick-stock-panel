import { useQuery } from '@tanstack/react-query'
import { Info } from 'lucide-react'
import { tdxApi, tdxKeys } from './client'
import { beijingNow, syncHint } from './syncHint'

/** 数据页“立即同步”按钮旁的提示。只在日线来自免费公开来源、而且本机行情还没到最新时出现。 */
export function SyncHint({ provider }: { provider: string }) {
  const on = provider === 'freecn'
  const status = useQuery({ queryKey: tdxKeys.status, queryFn: tdxApi.status, enabled: on, refetchInterval: 60_000 })
  const schedule = useQuery({ queryKey: tdxKeys.dataSchedule, queryFn: tdxApi.dataSchedule, enabled: on, staleTime: 60_000 })
  if (!on || !status.data || !schedule.data) return null
  const { today, clock } = beijingNow()
  const text = syncHint(status.data.latest_date, today, clock, schedule.data.run_time_min, schedule.data.run_time)
  if (!text) return null
  return (
    <div className="flex max-w-md items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/10 px-2.5 py-1.5 text-[11px] leading-snug text-warning">
      <Info className="mt-0.5 h-3 w-3 shrink-0" />
      <span>{text}</span>
    </div>
  )
}
