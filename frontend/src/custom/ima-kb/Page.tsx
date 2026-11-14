import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, Search, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { imaApi, imaKeys, type ImaConfig, type KnowledgeBase, type SearchResult } from './client'
import { sameSelection, toggleBase } from './logic'
import { Results } from './Results'

const INPUT = 'h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground'
const BUTTON = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-50'
const PRIMARY = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-50'
const CARD = 'rounded-card border border-border bg-surface p-4'
const H2 = 'text-sm font-medium text-foreground'
const NOTE = 'text-[11px] leading-relaxed text-muted'
const errorText = (e: unknown): string => (e instanceof Error && e.message ? e.message : '出错了，请再试一次')

function ErrorNote({ children }: { children: React.ReactNode }) {
  return <p role="alert" className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{children}</p>
}

/** 知识库：在选中的腾讯 ima 知识库里搜索；下面是凭证、选哪些知识库、AI 要不要参考的设置。 */
export function ImaKnowledge() {
  const config = useQuery({ queryKey: imaKeys.config, queryFn: imaApi.config, staleTime: 30_000 })
  const data = config.data
  const ready = Boolean(data?.configured && data.selected.length > 0)

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="知识库" subtitle="在自己的腾讯 ima 知识库里搜索 · AI 分析个股时可以参考搜到的内容" />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-4xl space-y-4">
          {config.isLoading ? <p className="text-xs text-muted">正在加载…</p>
            : config.error || !data ? (
              <div className="space-y-2">
                <ErrorNote>设置没有取到：{errorText(config.error)}</ErrorNote>
                <button className={BUTTON} onClick={() => void config.refetch()}>再试一次</button>
              </div>
            ) : (
              <>
                {ready ? <SearchSection bases={data.selected.length} />
                  : <p className={`${CARD} text-xs text-secondary`}>{data.configured ? '凭证已经填好，还要在下面勾选要用的知识库。' : '先在下面填 ima 的凭证，再勾选要用的知识库，就可以搜索了。'}</p>}
                <CredentialsSection config={data} />
                {data.configured && <BasesSection config={data} />}
                {data.configured && <AiSection config={data} />}
              </>
            )}
        </div>
      </div>
    </div>
  )
}

function SearchSection({ bases }: { bases: number }) {
  const [text, setText] = useState('')
  const [result, setResult] = useState<SearchResult | null>(null)
  const search = useMutation({ mutationFn: imaApi.search, onSuccess: setResult })
  function submit(e: FormEvent) {
    e.preventDefault()
    if (text.trim()) search.mutate(text.trim())
  }
  return (
    <section className="space-y-3">
      <form onSubmit={submit} className={`${CARD} flex flex-wrap items-center gap-2`}>
        <input
          value={text} onChange={e => setText(e.target.value)} maxLength={200}
          placeholder="搜股票名称、行业、关键词…" aria-label="搜索内容" className={`${INPUT} min-w-0 flex-1`}
        />
        <button className={PRIMARY} disabled={search.isPending || !text.trim()}>
          <Search className="h-3.5 w-3.5" />{search.isPending ? '搜索中…' : '搜索'}
        </button>
        <span className={`${NOTE} w-full`}>在选中的 {bases} 个知识库里各搜一次。</span>
      </form>
      {search.error && <ErrorNote>{errorText(search.error)}</ErrorNote>}
      {result && !search.isPending && <Results result={result} bases={bases} />}
    </section>
  )
}

function CredentialsSection({ config }: { config: ImaConfig }) {
  const qc = useQueryClient()
  const [clientId, setClientId] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const done = (next: ImaConfig, message: string) => {
    qc.setQueryData(imaKeys.config, next)
    setClientId(''); setApiKey(''); setError(null)
    toast(message, 'success')
  }
  const save = useMutation({
    mutationFn: () => imaApi.setCredentials(clientId, apiKey),
    onSuccess: next => done(next, '凭证可以用，已保存'),
    onError: e => setError(errorText(e)),
  })
  const clear = useMutation({
    mutationFn: imaApi.clearCredentials,
    onSuccess: next => done(next, '凭证已清除'),
    onError: e => setError(errorText(e)),
  })
  return (
    <section className={`${CARD} space-y-2`}>
      <h2 className={H2}>ima 凭证</h2>
      <p className="text-xs text-secondary">
        {config.configured ? `已填好（Client ID：${config.client_id_masked}）。要换一组的话在下面重新填。` : '还没有填。'}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <input value={clientId} onChange={e => { setClientId(e.target.value); setError(null) }} placeholder="Client ID"
          aria-label="Client ID" autoComplete="off" className={`${INPUT} w-64`} />
        <input value={apiKey} onChange={e => { setApiKey(e.target.value); setError(null) }} placeholder="API Key"
          aria-label="API Key" type="password" autoComplete="off" className={`${INPUT} w-64`} />
        <button className={PRIMARY} disabled={save.isPending || !clientId.trim() || !apiKey.trim()} onClick={() => save.mutate()}>
          <Save className="h-3.5 w-3.5" />{save.isPending ? '正在验证…' : '验证并保存'}
        </button>
        {config.configured && (
          <button className={BUTTON} disabled={clear.isPending} onClick={() => { if (window.confirm('清除已保存的 ima 凭证？')) clear.mutate() }}>
            <Trash2 className="h-3.5 w-3.5" />清除
          </button>
        )}
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
      <p className={NOTE}>
        凭证在 ima.qq.com/agent-interface 申请（API Key 只显示一次）。保存前会先用它调一次 ima 的接口，能用才保存。
        凭证只存在这台电脑上，页面上不会再显示完整内容。这里只搜索、不往知识库里写东西；
        但这组凭证本身的权限等同于你的 ima 账号，请不要告诉别人。
      </p>
    </section>
  )
}

function BasesSection({ config }: { config: ImaConfig }) {
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<KnowledgeBase[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const bases = useQuery({ queryKey: imaKeys.bases(query), queryFn: () => imaApi.knowledgeBases(query), staleTime: 60_000, retry: false })
  const chosen = picked ?? config.selected
  const save = useMutation({
    mutationFn: () => imaApi.setSelected(chosen),
    onSuccess: next => { qc.setQueryData(imaKeys.config, next); setPicked(null); setError(null); toast('已保存要用的知识库', 'success') },
    onError: e => setError(errorText(e)),
  })
  // 列表里没有、但已经选中的也要显示出来，不然没法取消
  const listed = bases.data?.items ?? []
  const rows = [...listed, ...chosen.filter(c => !listed.some(l => l.id === c.id))]
  const full = chosen.length >= config.max_selected

  return (
    <section className={`${CARD} space-y-2`}>
      <h2 className={H2}>用哪些知识库</h2>
      <form className="flex flex-wrap items-center gap-2" onSubmit={e => { e.preventDefault(); setQuery(text.trim()) }}>
        <input value={text} onChange={e => setText(e.target.value)} placeholder="按名称找知识库（留空列出自己的）"
          aria-label="知识库名称" maxLength={100} className={`${INPUT} w-72`} />
        <button className={BUTTON}><Search className="h-3.5 w-3.5" />查找</button>
      </form>
      {bases.isLoading ? <p className="text-xs text-muted">正在读取知识库列表…</p>
        : bases.error ? <ErrorNote>知识库列表没有取到：{errorText(bases.error)}</ErrorNote>
          : rows.length === 0 ? <p className="text-xs text-muted">没有找到知识库。</p> : (
            <ul className="grid gap-1 sm:grid-cols-2">
              {rows.map(base => {
                const checked = chosen.some(c => c.id === base.id)
                return (
                  <li key={base.id}>
                    <label className="flex items-center gap-2 text-xs text-secondary">
                      <input type="checkbox" checked={checked} disabled={!checked && full}
                        onChange={() => setPicked(toggleBase(chosen, base, config.max_selected))} />
                      {base.name}
                    </label>
                  </li>
                )
              })}
            </ul>
          )}
      <div className="flex flex-wrap items-center gap-2">
        <button className={PRIMARY} disabled={save.isPending || sameSelection(chosen, config.selected)} onClick={() => save.mutate()}>
          <Save className="h-3.5 w-3.5" />{save.isPending ? '保存中…' : '保存选择'}
        </button>
        <span className={NOTE}>已选 {chosen.length} 个，最多 {config.max_selected} 个</span>
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
      <p className={NOTE}>
        留空查找列出的是你自己的知识库；订阅的、别人共享给你的要按名称查找。每搜索一次，选中的每个知识库各发一次请求，所以不要选太多。
      </p>
    </section>
  )
}

function AiSection({ config }: { config: ImaConfig }) {
  const qc = useQueryClient()
  const toggle = useMutation({
    mutationFn: imaApi.setUseInAi,
    onSuccess: next => {
      qc.setQueryData(imaKeys.config, next)
      toast(next.use_in_ai ? '已打开：AI 分析个股时会参考知识库' : '已关闭', 'success')
    },
  })
  return (
    <section className={`${CARD} space-y-2`}>
      <h2 className={H2}>AI 分析时参考知识库</h2>
      <label className="flex items-center gap-2 text-xs text-secondary">
        <input type="checkbox" checked={config.use_in_ai} disabled={toggle.isPending} onChange={e => toggle.mutate(e.target.checked)} />
        AI 分析某一只股票时，先到知识库里搜这只股票，把搜到的片段一起交给 AI
      </label>
      {toggle.error && <ErrorNote>{errorText(toggle.error)}</ErrorNote>}
      <p className={NOTE}>
        只在分析单只股票时生效（比如个股分析）；复盘这类一次涉及很多股票的不会去搜。最多给 AI 8 条片段。
        目前只有 AI 走“本机 claude 命令行”这个转接口时才生效；直接填 DeepSeek 等接口时，面板不经过这里，知识库不会被带上。
        打开后，知识库里搜到的片段会随分析材料一起发给你配置的 AI。
      </p>
    </section>
  )
}
