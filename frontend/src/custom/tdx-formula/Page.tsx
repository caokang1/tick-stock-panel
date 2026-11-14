import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/PageHeader'
import { tdxApi, tdxKeys } from './client'
import { defaultTab } from './logic'
import { SettingsTab } from './SettingsTab'
import { StrategyTab } from './StrategyTab'
import { TodayTab } from './TodayTab'

type TabKey = 'today' | 'strategy' | 'settings'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'today', label: '今日提醒' },
  { key: 'strategy', label: '策略' },
  { key: 'settings', label: '设置' },
]

/** 通达信公式：把公式做成策略 → 组合回测 → 设为运行中后每天生成买卖提醒。数据和计算内核来自 quant-lab。 */
export function TdxFormula() {
  const today = useQuery({ queryKey: tdxKeys.today, queryFn: tdxApi.today, staleTime: 30_000 })
  const [picked, setPicked] = useState<TabKey | null>(null)
  // 没有手动选过时：有运行中的策略先看今日提醒，否则（包括提醒没取到）先去建策略
  const settled = today.isSuccess || today.isError
  const tab: TabKey | null = picked ?? (settled ? defaultTab(today.data?.strategies.length ?? 0) : null)

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="通达信公式"
        subtitle="粘贴通达信公式做成策略, 回测后设为运行中, 每天收盘后生成买卖提醒 · 行情数据和计算内核来自 quant-lab"
      />
      <div role="tablist" aria-label="通达信公式的页面" className="flex gap-1 border-b border-border px-5">
        {TABS.map(t => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setPicked(t.key)}
            className={`-mb-px cursor-pointer border-b-2 px-4 py-2 text-sm ${tab === t.key ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-secondary'}`}
          >
            {t.label}
            {t.key === 'today' && (today.data?.strategies.length ?? 0) > 0 && (
              <span className="ml-1.5 text-[11px] text-muted">{today.data?.strategies.length}</span>
            )}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-7xl">
          {tab === null && <p className="text-xs text-muted">正在加载…</p>}
          {tab === 'today' && <TodayTab onGoStrategy={() => setPicked('strategy')} />}
          {/* 策略页一直留着（只是藏起来），切到别的页再回来时，正在编辑的内容不会丢 */}
          <div className={tab === 'strategy' ? '' : 'hidden'}>
            <StrategyTab onGoToday={() => setPicked('today')} />
          </div>
          {tab === 'settings' && <SettingsTab />}
        </div>
      </div>
    </div>
  )
}
