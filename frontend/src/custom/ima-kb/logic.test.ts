import { describe, expect, it } from 'vitest'
import { baseNote, groupByBase, resultSummary, sameSelection, toggleBase } from './logic'

const a = { id: 'a', name: '研报' }
const b = { id: 'b', name: '笔记' }

describe('ima 知识库', () => {
  it('勾选、取消知识库，超过上限不再加', () => {
    expect(toggleBase([], a, 2)).toEqual([a])
    expect(toggleBase([a], a, 2)).toEqual([])
    expect(toggleBase([a, b], { id: 'c', name: '新的' }, 2)).toEqual([a, b])
  })

  it('判断两份选择是不是同一批', () => {
    expect(sameSelection([a, b], [b, a])).toBe(true)
    expect(sameSelection([a], [a, b])).toBe(false)
    expect(sameSelection([a], [b])).toBe(false)
  })

  it('搜索结果按知识库分组', () => {
    const item = (base_id: string, base_name: string, title: string) => ({ base_id, base_name, media_id: title, title, snippet: '' })
    const groups = groupByBase([item('a', '研报', '一'), item('b', '笔记', '二'), item('a', '研报', '三')])
    expect(groups.map(g => [g.base, g.items.map(i => i.title)])).toEqual([['研报', ['一', '三']], ['笔记', ['二']]])
  })

  it('知识库的类型和篇数', () => {
    expect(baseNote({ id: 'x', name: 'x', kind: '共享知识库', count: 1839 })).toBe('共享知识库 · 1,839 篇')
    expect(baseNote({ id: 'x', name: 'x', kind: '个人知识库', count: null })).toBe('个人知识库')
    expect(baseNote(a)).toBe('')
  })

  it('搜索结果的小结', () => {
    expect(resultSummary('茅台', 0, 2)).toBe('在选中的 2 个知识库里没有搜到“茅台”。')
    expect(resultSummary('茅台', 5, 2)).toBe('“茅台”搜到 5 条，来自 2 个知识库。')
  })
})
