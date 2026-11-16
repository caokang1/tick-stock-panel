/**
 * 看板标题旁情绪标签 (如「偏冷 · 35」) 的悬停明细。
 *
 * 鼠标停在标签上 (或键盘聚焦) 时, 弹出总分是怎么算出来的: 6 个维度的得分,
 * 以及每个指标的实际值、0..100 的换算分、权重和区间。
 * 数据来自 /api/overview/market 的 emotion.dimensions (口径在后端 emotion_score.py);
 * 旧快照没有这部分时只显示标签, 不弹明细。
 *
 * 弹层用 portal 挂到 body 并按标签位置定位 —— 标题栏是 overflow-hidden 的, 直接放里面会被裁掉。
 */
import { useCallback, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { EmotionDimensionDetail, EmotionTermDetail, OverviewMarket } from '@/lib/api'
import { scoreColor } from '@/components/dashboard/shared'

/** 这几个指标后端给的是小数 (0.01 = 1%), 展示时换成百分比 */
const FRACTION_KEYS = new Set(['avg_pct', 'median_pct', 'mainline_avg'])
const WIDTH = 420

function trim(n: number, digits = 2): string {
  return String(Number(n.toFixed(digits)))
}

export function formatTermNumber(term: Pick<EmotionTermDetail, 'key' | 'unit'>, n: number | null): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (FRACTION_KEYS.has(term.key)) return `${trim(n * 100)}%`
  return `${trim(n)}${term.unit}`
}

/** 分档线 → 「≥70 强势 · ≥55 偏暖 · …」 */
export function thresholdText(th: Record<string, number> | undefined): string {
  if (!th) return ''
  const names: [string, string][] = [['strong', '强势'], ['warm', '偏暖'], ['neutral', '震荡'], ['cold', '偏冷']]
  const parts = names.filter(([k]) => th[k] != null).map(([k, name]) => `≥${trim(th[k])} ${name}`)
  return parts.length ? `${parts.join(' · ')} · 其余 冰点` : ''
}

function DimensionBlock({ dim }: { dim: EmotionDimensionDetail }) {
  return (
    <div className="border-t border-border/60 pt-1.5 first:border-0 first:pt-0">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-medium text-foreground">
          {dim.label}
          {dim.invert && <span className="ml-1 font-normal text-muted">（反向：跌得越多分越低）</span>}
        </span>
        <span className="font-mono text-[12px] font-semibold tabular-nums" style={{ color: scoreColor(dim.value) }}>
          {dim.value}
        </span>
      </div>
      <table className="mt-0.5 w-full text-[10px]">
        <tbody>
          {(dim.terms ?? []).map(t => (
            <tr key={t.key} className="text-secondary">
              <td className="py-px pr-2">{t.label}</td>
              <td className="py-px pr-2 text-right font-mono tabular-nums text-foreground">{formatTermNumber(t, t.value)}</td>
              <td className="py-px pr-2 text-right font-mono tabular-nums text-muted">
                {formatTermNumber(t, t.low)} → {formatTermNumber(t, t.high)}
              </td>
              <td className="py-px pr-2 text-right font-mono tabular-nums">{t.score == null ? '缺数' : `${t.score} 分`}</td>
              <td className="py-px text-right font-mono tabular-nums text-muted">×{trim(t.weight * 100, 0)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function EmotionBadge({ emotion, score }: { emotion: OverviewMarket['emotion']; score: number }) {
  const anchor = useRef<HTMLSpanElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const dims = emotion.dimensions ?? []
  const hasDetail = dims.length > 0

  const show = useCallback(() => {
    const rect = anchor.current?.getBoundingClientRect()
    if (!rect || !hasDetail) return
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - WIDTH - 8))
    setPos({ left, top: rect.bottom + 6 })
  }, [hasDetail])
  const hide = useCallback(() => setPos(null), [])

  const color = scoreColor(score)
  const equal = dims.every(d => d.weight === dims[0]?.weight)
  return (
    <>
      <span
        ref={anchor}
        tabIndex={hasDetail ? 0 : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        aria-describedby={pos ? 'emotion-detail' : undefined}
        className={`rounded-full border px-2 py-0.5 text-[10px] font-medium outline-none ${hasDetail ? 'cursor-help focus-visible:ring-1 focus-visible:ring-accent' : ''}`}
        style={{ color, borderColor: `${color}40`, background: `${color}14` }}
      >
        {emotion.label} · {score}
      </span>
      {pos && createPortal(
        <div
          id="emotion-detail"
          role="tooltip"
          className="pointer-events-none fixed z-[1000] max-h-[80vh] overflow-hidden rounded-card border border-border bg-surface p-3 shadow-xl"
          style={{ left: pos.left, top: pos.top, width: WIDTH, maxWidth: 'calc(100vw - 16px)' }}
        >
          <div className="mb-1.5 flex items-baseline justify-between">
            <span className="text-[12px] font-semibold text-foreground">情绪评分明细</span>
            <span className="font-mono text-[12px] font-semibold" style={{ color }}>{emotion.label} · {score}</span>
          </div>
          <div className="mb-1.5 grid grid-cols-[1fr_auto_auto_auto_auto] gap-x-2 text-[9px] text-muted">
            <span>指标 / 今天的值 / 0 分 → 100 分的区间 / 换算分 / 维度内权重</span>
          </div>
          <div className="space-y-1.5">
            {dims.map(d => <DimensionBlock key={d.key} dim={d} />)}
          </div>
          <p className="mt-2 border-t border-border/60 pt-1.5 text-[10px] leading-relaxed text-muted">
            总分 = {dims.length} 个维度{equal ? '等权平均' : '按权重平均'}
            {equal ? `：(${dims.map(d => d.value).join(' + ')}) ÷ ${dims.length} ≈ ${score}` : ` ≈ ${score}`}。
            {thresholdText(emotion.thresholds) && <><br />分档：{thresholdText(emotion.thresholds)}</>}
            <br />权重和区间可在「情绪雷达」卡片上调整。
          </p>
        </div>,
        document.body,
      )}
    </>
  )
}
