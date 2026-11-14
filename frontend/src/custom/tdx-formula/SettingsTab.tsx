import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RotateCcw, Save } from 'lucide-react'
import { Link } from 'react-router-dom'
import { toast } from '@/components/Toast'
import { tdxApi, tdxKeys, type CostKey, type Settings } from './client'
import { COST_UNITS, DATA_SETTINGS_PATH, costFromInput, costInput, costText, initialCashError, money } from './logic'
import { BUTTON, CARD, ErrorNote, INPUT, LINK, PRIMARY, Tag, errorText } from './ui'

const COST_KEYS: CostKey[] = ['commission', 'min_commission', 'stamp_tax', 'slippage']
const COST_HELP: Record<CostKey, string> = {
  commission: '买和卖都收，按成交金额的比例',
  min_commission: '一笔交易的佣金不到这个数时，按这个数收',
  stamp_tax: '只在卖出时收，按成交金额的比例',
  slippage: '实际成交价比预想的差多少：买的时候贵一点，卖的时候便宜一点',
}

type CostForm = Record<CostKey, string>

const costForm = (costs: Record<CostKey, number>): CostForm =>
  Object.fromEntries(COST_KEYS.map(k => [k, costInput(k, costs[k])])) as CostForm

/** 「设置」页：交易成本、模拟账户初始资金、推送用哪里的机器人；行情数据的下载和更新在「设置 → 数据源 → 免费公开来源」。 */
export function SettingsTab() {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: tdxKeys.status, queryFn: tdxApi.status, staleTime: 30_000 })
  const settings = useQuery({ queryKey: tdxKeys.settings, queryFn: tdxApi.settings, staleTime: 30_000 })

  const [costs, setCosts] = useState<CostForm | null>(null)
  const [cash, setCash] = useState('')
  const [error, setError] = useState<string | null>(null)
  const fill = (s: Settings) => { setCosts(costForm(s.costs)); setCash(String(s.initial_cash)); setError(null) }
  // 第一次取到设置时填进输入框；之后以输入框里的为准，保存成功后再用返回的内容填一次
  const data = settings.data
  const filled = costs !== null
  useEffect(() => {
    if (data && !filled) fill(data)
  }, [data, filled])

  const save = useMutation({
    mutationFn: tdxApi.updateSettings,
    onSuccess: next => {
      qc.setQueryData(tdxKeys.settings, next)
      fill(next)
      toast('设置已保存', 'success')
    },
    onError: e => { setError(errorText(e)); toast(errorText(e)) },
  })

  function submit() {
    if (!data || !costs) return
    const values: Partial<Record<CostKey, number>> = {}
    for (const key of COST_KEYS) {
      const parsed = costFromInput(key, costs[key], data.cost_limits[key])
      if (!parsed.ok) { setError(parsed.error); return }
      values[key] = parsed.value
    }
    const cashProblem = initialCashError(cash)
    if (cashProblem) { setError(cashProblem); return }
    setError(null)
    save.mutate({ costs: values, initial_cash: Number(cash) })
  }

  return (
    <div className="space-y-4">
      <section className="flex flex-wrap items-center justify-between gap-2 rounded-card border border-border bg-surface px-4 py-3 text-xs text-secondary">
        <span>
          {status.isLoading ? '正在检查行情数据…'
            : status.error || !status.data ? `行情数据的状态没有取到：${errorText(status.error)}`
            : status.data.has_data
              ? `行情：${status.data.first_date ?? '—'} 到 ${status.data.latest_date ?? '—'}，共 ${status.data.stock_count.toLocaleString('en-US')} 只股票（含已退市的），后复权。`
              : '还没有行情数据。'}
        </span>
        <Link className={LINK} to={DATA_SETTINGS_PATH}>下载、更新和限速在「设置 → 数据源 → 免费公开来源」</Link>
      </section>

      <section className={`${CARD} space-y-3`}>
        <h2 className="text-sm font-medium text-foreground">交易成本和模拟账户</h2>
        {settings.isLoading ? <p className="text-xs text-muted">正在加载设置…</p>
          : settings.error || !data || !costs ? (
            <div className="space-y-2">
              <ErrorNote>设置没有取到：{errorText(settings.error)}</ErrorNote>
              <button className={BUTTON} onClick={() => void settings.refetch()}>再试一次</button>
            </div>
          ) : (
            <>
              <div className="space-y-2">
                {COST_KEYS.map(key => {
                  const limit = data.cost_limits[key]
                  const { unit } = COST_UNITS[key]
                  return (
                    <label key={key} className="grid grid-cols-[110px_auto_1fr] items-center gap-2 text-xs text-secondary">
                      {limit.label}
                      <span className="flex items-center gap-1.5">
                        {unit !== '元' && unit}
                        <input
                          type="number" min={0} step="any" value={costs[key]}
                          onChange={e => { setCosts({ ...costs, [key]: e.target.value }); setError(null) }}
                          className={`${INPUT} w-24`}
                        />
                        {unit === '元' && unit}
                      </span>
                      <span className="text-[11px] text-muted">
                        {COST_HELP[key]}。可以填 {costText(key, limit.min)} 到 {costText(key, limit.max)}，默认 {costText(key, data.cost_defaults[key])}
                      </span>
                    </label>
                  )
                })}
              </div>
              <p className="text-[11px] text-muted">
                交易成本用在组合回测、模拟账户和实际持仓的记账上；改了之后，已经做过的回测要重新回测才会按新的成本算。“历史信号统计”那一栏用的是固定的默认成本，不受这里影响。
              </p>
              <label className="grid grid-cols-[110px_auto_1fr] items-center gap-2 border-t border-border pt-3 text-xs text-secondary">
                模拟账户初始资金
                <span className="flex items-center gap-1.5">
                  <input
                    type="number" min={10000} step={10000} value={cash}
                    onChange={e => { setCash(e.target.value); setError(null) }}
                    className={`${INPUT} w-32`}
                  />元
                </span>
                <span className="text-[11px] text-muted">
                  {Number(cash) > 0 && `即 ${money(Number(cash), 0)} 元。`}只影响以后新开的模拟账户（策略第一次设为运行中时开），已经在运行的不变。组合回测也用这个数作起始资金。
                </span>
              </label>
              {error && <ErrorNote>{error}</ErrorNote>}
              <div className="flex flex-wrap items-center gap-2">
                <button className={PRIMARY} disabled={save.isPending} onClick={submit}>
                  <Save className="h-3.5 w-3.5" />{save.isPending ? '保存中…' : '保存设置'}
                </button>
                <button
                  className={BUTTON}
                  disabled={save.isPending}
                  onClick={() => { setCosts(costForm(data.cost_defaults)); setError(null) }}
                  title="把四项成本填回默认值，还要点“保存设置”才生效"
                >
                  <RotateCcw className="h-3.5 w-3.5" />成本填回默认值
                </button>
              </div>
            </>
          )}
      </section>

      <section className={`${CARD} space-y-2`}>
        <h2 className="text-sm font-medium text-foreground">推送</h2>
        {data && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-secondary">
            <span className="flex items-center gap-1.5">飞书 <Tag tone={data.push.feishu ? 'accent' : 'muted'}>{data.push.feishu ? '已配置' : '未配置'}</Tag></span>
            <span className="flex items-center gap-1.5">企业微信 <Tag tone={data.push.wecom ? 'accent' : 'muted'}>{data.push.wecom ? '已配置' : '未配置'}</Tag></span>
          </div>
        )}
        <p className="text-[11px] text-muted">
          每天的买卖提醒用本面板自己的机器人推送：到左下角的“设置”里填飞书或企业微信的机器人地址，这里不用另外配置。两个都没配置时，提醒只在“今日提醒”页里看。
        </p>
      </section>
    </div>
  )
}
