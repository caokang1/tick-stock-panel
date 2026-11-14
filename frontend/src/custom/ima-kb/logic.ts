/** 不带界面的小逻辑，单独放着方便测试。 */
import type { KnowledgeBase, SearchItem } from './client'

/** 勾选或取消一个知识库；超过上限时不加，返回原来的 */
export function toggleBase(selected: KnowledgeBase[], base: KnowledgeBase, max: number): KnowledgeBase[] {
  if (selected.some(b => b.id === base.id)) return selected.filter(b => b.id !== base.id)
  return selected.length >= max ? selected : [...selected, base]
}

/** 两份选择是不是同一批知识库（不管顺序） */
export function sameSelection(a: KnowledgeBase[], b: KnowledgeBase[]): boolean {
  if (a.length !== b.length) return false
  const ids = new Set(a.map(x => x.id))
  return b.every(x => ids.has(x.id))
}

/** 搜索结果按知识库分组，保持知识库第一次出现的顺序 */
export function groupByBase(items: SearchItem[]): { base: string; items: SearchItem[] }[] {
  const groups = new Map<string, { base: string; items: SearchItem[] }>()
  for (const item of items) {
    const group = groups.get(item.base_id) ?? { base: item.base_name, items: [] }
    group.items.push(item)
    groups.set(item.base_id, group)
  }
  return [...groups.values()]
}

/** 搜索结果的一句话小结 */
export function resultSummary(query: string, count: number, bases: number): string {
  if (count === 0) return `在选中的 ${bases} 个知识库里没有搜到“${query}”。`
  return `“${query}”搜到 ${count} 条，来自 ${bases} 个知识库。`
}
