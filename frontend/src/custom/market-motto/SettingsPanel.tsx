/**
 * 设置 → 左上角的话：改语料库。一档一段，第一行是“名称（下限-上限）”，下面一行一句。
 * 保存后左上角马上按新的语料库挑。
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MessageSquareQuote, RotateCcw, Save } from 'lucide-react'
import { toast } from '@/components/Toast'
import { mottoApi, mottoKeys, type Corpus } from './client'

const BUTTON = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-50'
const PRIMARY = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-40'

export function MottoSettingsPanel() {
  const client = useQueryClient()
  const corpus = useQuery({ queryKey: mottoKeys.corpus, queryFn: mottoApi.corpus })
  const current = useQuery({ queryKey: mottoKeys.current, queryFn: mottoApi.current })
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const saved = corpus.data?.text
  const value = text ?? saved ?? ''

  // 别处（恢复自带的那份）换了内容后，编辑框跟着换
  useEffect(() => { setText(null) }, [saved])

  const done = (next: Corpus, message: string) => {
    client.setQueryData(mottoKeys.corpus, next)
    client.invalidateQueries({ queryKey: mottoKeys.current })
    setText(null)
    setError(null)
    toast(message, 'success')
  }
  const save = useMutation({
    mutationFn: () => mottoApi.save(value),
    onSuccess: next => done(next, '已保存，左上角按新的语料库显示'),
    onError: (e: Error) => setError(e.message),
  })
  const reset = useMutation({
    mutationFn: mottoApi.reset,
    onSuccess: next => done(next, '已恢复成自带的示例'),
    onError: (e: Error) => setError(e.message),
  })
  const onReset = () => {
    if (window.confirm('恢复成自带的示例会删掉你自己写的语料库，确定吗？')) reset.mutate()
  }

  const data = corpus.data
  const now = current.data
  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="mb-4 flex items-start gap-2.5">
        <MessageSquareQuote className="mt-0.5 h-4 w-4 text-secondary" />
        <div>
          <h2 className="text-sm font-medium text-foreground">左上角的话</h2>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            侧栏左上角那句话按市场情绪评分（0 到 100，和每日复盘里的是同一个数）从下面的语料库里挑。
            同一天固定一句，第二天换；盘中评分跨到另一档时跟着换。
          </p>
        </div>
      </div>

      {now && (
        <p className="mb-3 rounded-lg border border-border/60 bg-elevated/20 px-3 py-2 text-xs text-secondary">
          现在显示：<span className="font-medium text-foreground">{now.text}</span>
          {now.score == null
            ? '（还没有情绪评分，用的是默认的一句）'
            : `（情绪评分 ${Math.round(now.score)} · ${now.label}${now.band ? `，出自“${now.band}”这一档` : '，没有落在任何一档，用的是默认的一句'}）`}
        </p>
      )}

      {corpus.isLoading && <p className="py-6 text-center text-xs text-muted">正在读语料库…</p>}
      {corpus.error && <p className="py-6 text-center text-xs text-bear">语料库没有读到：{(corpus.error as Error).message}</p>}
      {data && (
        <>
          <textarea
            value={value} spellCheck={false} aria-label="语料库"
            onChange={e => { setText(e.target.value); setError(null) }}
            className="h-[420px] w-full resize-y rounded-btn border border-border bg-base px-3 py-2 font-mono text-xs leading-relaxed text-foreground outline-none focus:border-accent"
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button className={PRIMARY} disabled={save.isPending || value === saved} onClick={() => save.mutate()}>
              <Save className="h-3.5 w-3.5" />{save.isPending ? '保存中…' : '保存'}
            </button>
            <button className={BUTTON} disabled={reset.isPending || data.is_default} onClick={onReset}>
              <RotateCcw className="h-3.5 w-3.5" />恢复成自带的示例
            </button>
            <span className="text-[11px] text-muted">
              {data.is_default ? '现在用的是自带的示例，保存一次后就是你自己的了。' : `共 ${data.bands.length} 档、${data.bands.reduce((n, b) => n + b.count, 0)} 句。`}
            </span>
          </div>
          {error && <p className="mt-2 text-xs text-bear">没有保存：{error}</p>}
          {value === saved && data.notes.length > 0 && (
            <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[11px] text-muted">
              {data.notes.map(note => <li key={note}>{note}</li>)}
            </ul>
          )}
          <div className="mt-3 space-y-1 text-[11px] leading-relaxed text-muted">
            <p>写法：档位一行写成“名称（下限-上限）”，比如“冰点（0-30）”，下面一行一句。评分大于等于下限、小于上限时用这一档。井号开头的行是备注。</p>
            <p>档位的个数、名称和分数线都可以改，范围不能重叠。每句最好不超过 {data.max_chars} 个字，再长在侧栏里显示不全。</p>
            <p>语料库存在这台电脑上，也可以直接用记事本改这个文件：<span className="break-all font-mono text-secondary">{data.path}</span></p>
          </div>
        </>
      )}
    </section>
  )
}
