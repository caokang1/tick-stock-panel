/**
 * 设置 → 每日汇总：看今天发过没有、打开已生成的网页、试发一次，
 * 以及两种“在哪里都能打开网页”的办法：发到邮箱（推荐），或者用飞书自建应用把文件发进群。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ExternalLink, Newspaper, Save, Send, Trash2 } from 'lucide-react'
import { toast } from '@/components/Toast'
import { digestApi, digestKeys, runMessage, type DigestChat, type DigestStatus } from './client'

const BUTTON = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-50'
const PRIMARY = 'inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-40'
const INPUT = 'h-8 w-full rounded-btn border border-border bg-base px-2.5 text-xs text-foreground outline-none focus:border-accent'

export function DigestSettingsPanel() {
  const client = useQueryClient()
  const status = useQuery({ queryKey: digestKeys.status, queryFn: digestApi.status })
  const [appId, setAppId] = useState<string | null>(null)
  const [secret, setSecret] = useState('')
  const [chats, setChats] = useState<DigestChat[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ text: string; ok: boolean } | null>(null)
  const data = status.data
  const idValue = appId ?? data?.app_id ?? ''
  const appReady = !!(data?.app_id && data.secret_set)

  const applied = (next: DigestStatus, message: string) => {
    client.setQueryData(digestKeys.status, next)
    setChats(next.chats ?? null)
    setAppId(null)
    setSecret('')
    setError(null)
    toast(message, 'success')
  }
  const save = useMutation({
    mutationFn: (chatId?: string) => digestApi.saveApp({ app_id: idValue.trim(), app_secret: secret.trim() || undefined, chat_id: chatId ?? data?.chat_id }),
    onSuccess: next => applied(next, next.chat_id ? `已保存，网页文件会发到“${next.chat_name}”` : '应用填对了，还差选一个群'),
    onError: (e: Error) => setError(e.message),
  })
  const remove = useMutation({
    mutationFn: digestApi.removeApp,
    onSuccess: next => applied(next, '已删除，以后只发摘要卡片'),
    onError: (e: Error) => setError(e.message),
  })
  const run = useMutation({
    mutationFn: () => digestApi.run(true),
    onSuccess: r => { setResult(runMessage(r)); client.invalidateQueries({ queryKey: digestKeys.status }) },
    onError: (e: Error) => setResult({ text: `没有发出去：${e.message}`, ok: false }),
  })
  const card = useMutation({
    mutationFn: (on: boolean) => digestApi.setFeishuCard(on),
    onSuccess: next => { client.setQueryData(digestKeys.status, next); toast(next.feishu_card ? '以后会往飞书群发摘要卡片' : '以后不再往飞书群发摘要卡片', 'success') },
    onError: (e: Error) => setError(e.message),
  })
  const onRemove = () => {
    if (window.confirm('删除后网页不再作为文件发进飞书，只发摘要卡片。确定吗？')) remove.mutate()
  }

  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="mb-4 flex items-start gap-2.5">
        <Newspaper className="mt-0.5 h-4 w-4 text-secondary" />
        <div>
          <h2 className="text-sm font-medium text-foreground">每日汇总</h2>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            每个交易日的定时任务都跑完后（定时复盘生成之后），给看板、连板梯队、概念板块、行业板块、市场环境、每日复盘各截一张长图，做成一个网页发给你。一天只发一次，休市日不发。
          </p>
        </div>
      </div>

      {status.isLoading && <p className="py-6 text-center text-xs text-muted">正在读取…</p>}
      {status.error && <p className="py-6 text-center text-xs text-bear">没有读到：{(status.error as Error).message}</p>}
      {data && (
        <>
          <div className="rounded-lg border border-border/60 bg-elevated/20 px-3 py-2 text-xs text-secondary">
            <p>最近一次自动推送：{data.state.message ? `${data.state.sent_for ?? ''} ${data.state.message}` : '还没有自动推送过'}</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button className={PRIMARY} disabled={run.isPending} onClick={() => { setResult(null); run.mutate() }}>
                <Send className="h-3.5 w-3.5" />{run.isPending ? '发送中…' : '现在试发一次'}
              </button>
              {data.pages.length > 0 && (
                <a className={BUTTON} href={digestApi.pageUrl(data.pages[0])} target="_blank" rel="noreferrer">
                  <ExternalLink className="h-3.5 w-3.5" />打开最近一份网页（{data.pages[0]}）
                </a>
              )}
            </div>
            {result && <p className={`mt-2 ${result.ok ? 'text-secondary' : 'text-bear'}`}>{result.text}</p>}
            <p className="mt-2 text-[11px] text-muted">试发用的是面板里最新一天的行情，不影响当天的自动推送。</p>
          </div>

          <label className="mt-4 flex cursor-pointer items-center gap-2 text-xs text-secondary">
            <input type="checkbox" checked={data.feishu_card} disabled={card.isPending} onChange={e => card.mutate(e.target.checked)} />
            同时往飞书群发一张摘要卡片
            <span className="text-[11px] text-muted">（只管每日汇总；监控信号的飞书推送不受影响）</span>
          </label>

          <h3 className="mb-1 mt-5 text-xs font-medium text-foreground">把网页发到邮箱（手机、电脑都能打开）</h3>
          <p className="text-[11px] leading-relaxed text-muted">
            {data.mail_ready
              ? '已启用。每天发一封邮件：正文是几行摘要，完整网页在附件里，用浏览器打开。'
              : '还没有启用。'}
            邮箱在 <Link className="text-accent hover:underline" to="/settings?tab=monitoring">设置 → 实时监控</Link> 的“邮件”里填，这里直接沿用。
          </p>
          {!data.mail_ready && (
            <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-[11px] leading-relaxed text-muted">
              <li>登录 163 邮箱网页版，“设置 → POP3/SMTP/IMAP”，开启 SMTP 服务，拿到一个授权码。</li>
              <li>在“实时监控”的邮件设置里填：服务器 smtp.163.com、端口 465、加密方式 SSL，用户名和发件人都填你的 163 邮箱，密码填授权码（不是登录密码），收件人填你想收信的邮箱。</li>
              <li>保存后回到这里点“现在试发一次”。</li>
            </ol>
          )}

          <h3 className="mb-1 mt-5 text-xs font-medium text-foreground">把网页作为文件发进飞书（可选）</h3>
          <p className="text-[11px] leading-relaxed text-bear/80">注意：个人版飞书建的应用会被飞书拒绝登录（错误码 21001），要在企业团队下建的应用才行。用邮箱就不需要这一步。</p>
          <p className="text-[11px] leading-relaxed text-muted">
            {appReady
              ? (data.chat_id ? `已启用，网页文件发到“${data.chat_name}”。` : '应用已填好，还需要在下面选一个群。')
              : '还没有启用。现在只发摘要卡片，卡片里的网页链接只能在这台电脑上打开。'}
          </p>
          <div className="mt-2 grid max-w-xl gap-2">
            <label className="text-[11px] text-muted">应用 ID（App ID，以 cli_ 开头）
              <input className={`${INPUT} mt-1`} value={idValue} spellCheck={false} placeholder="cli_xxxxxxxxxxxxxxxx"
                onChange={e => { setAppId(e.target.value); setError(null) }} />
            </label>
            <label className="text-[11px] text-muted">应用密钥（App Secret）
              <input className={`${INPUT} mt-1`} type="password" value={secret} autoComplete="off"
                placeholder={data.secret_set ? '已保存，不改就留空' : '粘贴应用密钥'}
                onChange={e => { setSecret(e.target.value); setError(null) }} />
            </label>
            {chats && chats.length > 1 && (
              <label className="text-[11px] text-muted">机器人在多个群里，选一个接收的群
                <select className={`${INPUT} mt-1`} value={data.chat_id} onChange={e => save.mutate(e.target.value)}>
                  <option value="">请选择</option>
                  {chats.map(c => <option key={c.chat_id} value={c.chat_id}>{c.name || c.chat_id}</option>)}
                </select>
              </label>
            )}
            {chats && chats.length === 0 && (
              <p className="text-[11px] text-bear">应用填对了，但机器人还没有加入任何群。把它加进群以后，再点一次“保存并检查”。</p>
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button className={PRIMARY} disabled={save.isPending || !idValue.trim() || (!secret.trim() && !data.secret_set)} onClick={() => save.mutate(undefined)}>
              <Save className="h-3.5 w-3.5" />{save.isPending ? '检查中…' : '保存并检查'}
            </button>
            {(data.app_id || data.secret_set) && (
              <button className={BUTTON} disabled={remove.isPending} onClick={onRemove}>
                <Trash2 className="h-3.5 w-3.5" />删除这个应用的设置
              </button>
            )}
          </div>
          {error && <p className="mt-2 text-xs text-bear">没有保存：{error}</p>}

          <div className="mt-4 space-y-1 text-[11px] leading-relaxed text-muted">
            <p className="text-secondary">怎么拿到应用 ID 和密钥（只做一次）：</p>
            <ol className="list-decimal space-y-0.5 pl-4">
              <li>用浏览器打开飞书开放平台 open.feishu.cn，登录后进“开发者后台”，点“创建企业自建应用”，名字随意（比如“行情面板”）。</li>
              <li>在应用里点“添加应用能力”，添加“机器人”。</li>
              <li>在“权限管理”里开通两项：“以应用的身份发消息”（im:message:send_as_bot）、“获取与上传图片或文件资源”（im:resource）；再开通“获取群组信息”（im:chat:readonly），用来找到要发的群。</li>
              <li>在“版本管理与发布”里创建一个版本并发布（需要团队管理员审核通过）。</li>
              <li>在飞书里打开要接收的群，“设置 → 群机器人 → 添加机器人”，选刚才这个应用。</li>
              <li>回到应用的“凭证与基础信息”，把 App ID 和 App Secret 填到上面，点“保存并检查”。</li>
            </ol>
            <p>密钥只保存在这台电脑上，页面上不会再显示出来。发送时只联系飞书开放平台。</p>
          </div>
        </>
      )}
    </section>
  )
}
