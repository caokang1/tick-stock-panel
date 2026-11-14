/** 页面用到的纯函数：解析结果转成条件、数字的显示。没有界面依赖，方便单独测试。 */
import type { Condition, ParsedCondition, Universe } from './client'

export function defaultUniverse(): Universe {
  return {
    exclude_st: true,
    exclude_star: true,
    exclude_chinext: true,
    exclude_bse: true,
    exclude_suspended: true,
    min_float_cap: null,
    max_float_cap: null,
    exclude_industries: [],
  }
}

/** 公式拆出来的一个条件 → 页面上可以勾选、调参数的条件，参数先取公式里原来的数 */
export function conditionFromParsed(parsed: ParsedCondition): Condition {
  const params: Record<string, number> = {}
  for (const p of parsed.params) params[p.key] = p.default
  return {
    pasted: { name: parsed.name, description: parsed.description, formula: parsed.formula, params_spec: parsed.params },
    enabled: true,
    params,
  }
}

export function conditionName(c: Condition): string {
  return c.pasted?.name ?? c.block_id ?? '条件'
}

/** 改一个参数：输入框清空或填的不是数时，回到公式里原来的数 */
export function withParam(c: Condition, key: string, raw: string): Condition {
  const spec = c.pasted?.params_spec.find(p => p.key === key)
  const value = Number(raw)
  const next = raw.trim() !== '' && Number.isFinite(value) ? value : (spec?.default ?? 0)
  return { ...c, params: { ...c.params, [key]: next } }
}

/** 0.0123 → "+1.23%"；空值显示成横线 */
export function pct(value: number | null | undefined, digits = 2, signed = true): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const text = (value * 100).toFixed(digits)
  return `${signed && value > 0 ? '+' : ''}${text}%`
}

/** 涨用红、跌用绿（A 股习惯）；0 和空值不上色 */
export function tone(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value === 0) return ''
  return value > 0 ? 'text-bull' : 'text-bear'
}

/** 金额（元）显示成"多少亿" */
export function capText(yuan: number | null | undefined): string {
  if (yuan == null || !Number.isFinite(yuan)) return '—'
  const yi = yuan / 1e8
  return `${yi < 1 ? yi.toFixed(2) : yi < 1000 ? yi.toFixed(1) : Math.round(yi).toLocaleString('en-US')} 亿`
}

/** quant-lab 的代码写法 sh.600000 → 本面板的写法 600000.SH；认不出来的原样返回 */
export function toPanelSymbol(code: string): string {
  const m = /^(sh|sz|bj)\.(\d{6})$/i.exec(code)
  return m ? `${m[2]}.${m[1].toUpperCase()}` : code
}

/** 每天平均出多少个信号的大白话 */
export function frequencyText(f: { avg_per_day: number; per_year: number; total: number }): string {
  if (f.total === 0) return '历史上从来没有出过信号。条件可能太严，放宽参数试试。'
  const perDay = f.avg_per_day >= 1 ? `平均每天 ${f.avg_per_day.toFixed(1)} 个` : `平均每年 ${f.per_year.toFixed(0)} 个`
  const hint = f.avg_per_day > 100 ? '。信号太多，条件可能太松' : f.per_year < 20 ? '。信号很少，统计结果不太可靠' : ''
  return `历史上一共 ${f.total.toLocaleString('en-US')} 个信号，${perDay}${hint}`
}
