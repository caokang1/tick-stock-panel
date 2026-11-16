import type { SearchResult } from './client'
import { groupByBase, resultSummary } from './logic'

/** 搜索结果：按知识库分组，每条是标题加命中的片段 */
export function Results({ result, bases }: { result: SearchResult; bases: number }) {
  const groups = groupByBase(result.items)
  return (
    <div className="space-y-3">
      <p className="text-xs text-secondary">{resultSummary(result.query, result.items.length, bases)}</p>
      {result.errors.map(e => (
        <p key={e} role="alert" className="rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
          有一个知识库没有搜成功 —— {e}
        </p>
      ))}
      {groups.map(group => (
        <section key={group.base} className="rounded-card border border-border bg-surface p-4">
          <h3 className="mb-2 text-xs font-medium text-foreground">
            {group.base}<span className="ml-2 font-normal text-muted">{group.items.length} 条</span>
          </h3>
          <ul className="space-y-3">
            {group.items.map(item => (
              <li key={`${item.base_id}:${item.media_id}`} className="border-b border-border/50 pb-3 last:border-0 last:pb-0">
                <p className="text-sm text-foreground">
                  {item.title}
                  {item.matched && <span className="ml-2 text-[11px] text-muted">按“{item.matched}”搜到</span>}
                </p>
                {item.snippet
                  ? <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-secondary">{item.snippet}</p>
                  : null}
              </li>
            ))}
          </ul>
        </section>
      ))}
      {result.items.length > 0 && (
        <p className="text-[11px] text-muted">
          ima 的开放接口基本只按标题匹配，也不提供全文，所以这里多数只有标题。要看内容，请到 ima 里按标题打开。
        </p>
      )}
    </div>
  )
}
