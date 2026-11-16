/**
 * 首页的走势小图：一条或两条折线，或者一组柱子。用内联 SVG 画，颜色跟着面板的主题走。
 * 鼠标移到图上时，右上角显示离得最近那一天的数值。
 */
import { useState } from 'react'
import { axisDate, scale } from './logic'

export interface Series {
  name: string
  values: (number | null)[]
  /** CSS 颜色，例如 hsl(var(--bull)) */
  color: string
}

const W = 520
const H = 220
const LINE_PAD = { l: 52, r: 14, t: 14, b: 30 }
const BAR_PAD = { l: 66, r: 26, t: 14, b: 30 }  // 柱子有宽度，两头多留一点，免得压到纵轴的数字

export function TrendChart({ title, caption, labels, series, kind = 'line', format }: {
  title: string
  caption: string
  labels: string[]
  series: Series[]
  kind?: 'line' | 'bar'
  format: (v: number) => string
}) {
  const [hover, setHover] = useState<number | null>(null)
  const PAD = kind === 'bar' ? BAR_PAD : LINE_PAD
  const all = series.flatMap(s => s.values)
  const range = scale(all, W, H, PAD, kind === 'bar')
  // 几条线共用同一把纵轴尺子
  const scaled = series.map(s => {
    const own = scale([...s.values, range.min, range.max], W, H, PAD, kind === 'bar')
    const step = s.values.length > 1 ? (W - PAD.l - PAD.r) / (s.values.length - 1) : 0
    return own.points.slice(0, s.values.length).map((p, i) =>
      p && { ...p, x: PAD.l + (s.values.length > 1 ? i * step : (W - PAD.l - PAD.r) / 2) })
  })
  const hasData = all.some(v => v != null)
  const count = labels.length
  const slot = count > 1 ? (W - PAD.l - PAD.r) / (count - 1) : W - PAD.l - PAD.r
  const barWidth = Math.max(4, Math.min(22, slot * 0.6))
  const ticks = [0, 0.5, 1].map(t => ({ y: PAD.t + (H - PAD.t - PAD.b) * (1 - t), value: range.min + (range.max - range.min) * t }))
  const labelEvery = Math.max(1, Math.ceil(count / 8))
  const shown = hover ?? (count ? count - 1 : null)

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    if (!rect.width || count === 0) return
    const x = ((e.clientX - rect.left) / rect.width) * W
    const index = count > 1 ? Math.round((x - PAD.l) / slot) : 0
    setHover(Math.max(0, Math.min(count - 1, index)))
  }

  return (
    <section className="rounded-card border border-border bg-surface p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-[13px] font-medium text-foreground">{title}</h3>
          <p className="text-[11px] text-muted">{caption}</p>
        </div>
        {hasData && shown != null && (
          <div className="shrink-0 text-right text-[11px] tabular-nums">
            <div className="text-muted">{labels[shown]}</div>
            {series.map(s => (
              <div key={s.name} style={{ color: s.color }}>
                {series.length > 1 && <span className="mr-1">{s.name}</span>}
                {s.values[shown] == null ? '—' : format(s.values[shown] as number)}
              </div>
            ))}
          </div>
        )}
      </div>
      {hasData ? (
        <svg
          viewBox={`0 0 ${W} ${H}`} className="mt-1 block h-auto w-full" role="img" aria-label={title}
          onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        >
          {ticks.map(t => (
            <g key={t.y}>
              <line x1={PAD.l} x2={W - PAD.r} y1={t.y} y2={t.y} stroke="hsl(var(--border))" strokeWidth="1" strokeDasharray="3 3" />
              <text x={PAD.l - 6} y={t.y + 3.5} textAnchor="end" fontSize="10" fill="hsl(var(--fg-muted))">{format(t.value)}</text>
            </g>
          ))}
          {labels.map((label, i) => (i % labelEvery === 0 || i === count - 1) && (
            <text key={label} x={PAD.l + (count > 1 ? i * slot : slot / 2)} y={H - 10} textAnchor="middle" fontSize="10" fill="hsl(var(--fg-muted))">
              {axisDate(label)}
            </text>
          ))}
          {kind === 'bar'
            ? scaled[0].map((p, i) => p && (
              <rect
                key={labels[i]} x={p.x - barWidth / 2} y={p.y} width={barWidth} height={Math.max(0, H - PAD.b - p.y)}
                fill={series[0].color} opacity={shown === i ? 1 : 0.6} rx="2"
              />
            ))
            : scaled.map((points, k) => {
              const path = points.filter(Boolean).map((p, i) => `${i === 0 ? 'M' : 'L'}${p!.x.toFixed(1)},${p!.y.toFixed(1)}`).join(' ')
              return (
                <g key={series[k].name}>
                  <path d={path} fill="none" stroke={series[k].color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
                  {points.map((p, i) => p && (
                    <circle key={labels[i]} cx={p.x} cy={p.y} r={shown === i ? 3.5 : 2} fill={series[k].color} />
                  ))}
                </g>
              )
            })}
          {hover != null && (
            <line
              x1={PAD.l + (count > 1 ? hover * slot : slot / 2)} x2={PAD.l + (count > 1 ? hover * slot : slot / 2)}
              y1={PAD.t} y2={H - PAD.b} stroke="hsl(var(--fg-muted))" strokeWidth="1" opacity="0.5"
            />
          )}
        </svg>
      ) : (
        <div className="flex h-40 items-center justify-center text-xs text-muted">没有数据</div>
      )}
    </section>
  )
}
