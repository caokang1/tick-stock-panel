/** 这个扩展几个页面共用的小部件和样式。 */
import type { ReactNode } from 'react'
import { openStockPreview } from '@/extensions/stockPreview'
import { toPanelSymbol } from './logic'

export const INPUT = 'h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground'
export const BUTTON = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-50'
export const PRIMARY = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-50'
/** 表格里一行内的文字按钮 */
export const LINK = 'cursor-pointer text-xs text-accent hover:underline disabled:cursor-not-allowed disabled:opacity-50 disabled:no-underline'
export const LINK_MUTED = 'cursor-pointer text-xs text-secondary hover:underline disabled:cursor-not-allowed disabled:opacity-50 disabled:no-underline'
export const LINK_DANGER = 'cursor-pointer text-xs text-danger hover:underline disabled:cursor-not-allowed disabled:opacity-50 disabled:no-underline'
export const CARD = 'rounded-card border border-border bg-surface p-4'
export const TH = 'py-1.5 font-normal'
export const HEAD_ROW = 'border-b border-border text-[11px] text-muted'
export const BODY_ROW = 'border-b border-border/50'

export const errorText = (e: unknown): string => (e instanceof Error && e.message ? e.message : '出错了，请再试一次')

export function Hint({ children }: { children: ReactNode }) {
  return <p className="rounded-card border border-dashed border-border px-4 py-8 text-center text-xs text-muted">{children}</p>
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return <p role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{children}</p>
}

export function WarnNote({ children }: { children: ReactNode }) {
  return <p className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">{children}</p>
}

const TAG_TONE = {
  muted: 'border-border text-muted',
  accent: 'border-accent/30 bg-accent/10 text-accent',
  warning: 'border-warning/30 bg-warning/10 text-warning',
  danger: 'border-danger/30 bg-danger/10 text-danger',
}

export function Tag({ tone = 'muted', children }: { tone?: keyof typeof TAG_TONE; children: ReactNode }) {
  return <span className={`inline-block whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px] leading-none ${TAG_TONE[tone]}`}>{children}</span>
}

interface StockRef {
  code: string
  name: string
}

/** 股票名称 + 代码；点名称打开本面板的个股详情，list 里的其他股票可以前后切换 */
export function StockName({ code, name, list }: StockRef & { list?: readonly StockRef[] }) {
  const open = () => openStockPreview({
    symbol: toPanelSymbol(code), name,
    navList: list?.map(x => ({ symbol: toPanelSymbol(x.code), name: x.name })),
  })
  return (
    <>
      <button type="button" className="cursor-pointer text-foreground hover:text-accent" onClick={open}>{name || code}</button>
      <span className="ml-2 font-mono text-[10px] text-muted">{toPanelSymbol(code)}</span>
    </>
  )
}
