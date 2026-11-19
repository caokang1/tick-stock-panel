/** “每日汇总”用的端点和类型。 */
import { ApiError } from '@/lib/api'

export interface DigestChat { chat_id: string; name: string }

export interface DigestStatus {
  enabled: boolean
  /** 是否往飞书群发摘要卡片；关掉后每日汇总只发邮件 */
  feishu_card: boolean
  base_url: string
  /** 飞书自建应用的 ID；没填过是空的 */
  app_id: string
  /** 应用密钥存过没有（密钥本身不会传回页面） */
  secret_set: boolean
  chat_id: string
  chat_name: string
  /** “实时监控”里的邮箱配好没有；配好后网页会发到邮箱 */
  mail_ready: boolean
  state: { sent_for?: string; message?: string }
  /** 已经生成过网页的日期，新的在前 */
  pages: string[]
  /** 保存应用后才有：机器人所在的群 */
  chats?: DigestChat[]
}

export interface DigestRun {
  day: string
  link: string
  sent: boolean | null
  file_error: string | null
  /** 邮件发出去没有；null 是没配置邮箱 */
  mailed: boolean | null
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.detail
    throw new ApiError(typeof detail === 'string' ? detail : `请求没有成功（${response.status}）`, response.status)
  }
  return payload as T
}

const BASE = '/api/custom/daily-digest'
const json = (method: string, body: unknown): RequestInit => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})

export const digestKeys = { status: ['daily-digest', 'status'] as const }

export const digestApi = {
  status: () => call<DigestStatus>(`${BASE}/status`),
  saveApp: (body: { app_id: string; app_secret?: string; chat_id?: string }) => call<DigestStatus>(`${BASE}/app`, json('PUT', body)),
  removeApp: () => call<DigestStatus>(`${BASE}/app`, { method: 'DELETE' }),
  setFeishuCard: (on: boolean) => call<DigestStatus>(`${BASE}/options`, json('PUT', { feishu_card: on })),
  run: (send: boolean) => call<DigestRun>(`${BASE}/run?send=${send}`, { method: 'POST' }),
  pageUrl: (day: string) => `${BASE}/page/${day}`,
}

/** 试发之后给人看的一句话。 */
export function runMessage(result: DigestRun): { text: string; ok: boolean } {
  const problems: string[] = []
  if (result.sent === false) problems.push('摘要卡片没有发到飞书，请检查“实时监控”里的飞书机器人地址')
  if (result.mailed === false) problems.push('邮件没有发出去，请检查“实时监控”里的邮箱设置（163 邮箱要填授权码，不是登录密码）')
  if (result.file_error) problems.push(`网页文件没有发进飞书：${result.file_error}`)
  const done: string[] = []
  if (result.sent) done.push('飞书摘要卡片')
  if (result.mailed) done.push('邮件（正文和附件都是完整网页）')
  if (result.sent && !result.file_error && result.mailed === null) done.push('（还没有配置邮箱，所以没有发邮件）')
  if (!done.length && !problems.length) problems.push('飞书卡片已关闭，邮箱也还没有配置，所以什么都没有发出去')
  const head = done.length ? `已发出：${done.join('、')}。` : ''
  return { text: head + (problems.length ? `${problems.join('；')}。` : ''), ok: problems.length === 0 }
}
