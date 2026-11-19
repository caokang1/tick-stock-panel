import { describe, expect, it } from 'vitest'
import { runMessage } from './client'

const run = { day: '2026-11-19', link: 'x', sent: true, file_error: null, mailed: true }

describe('每日汇总：试发之后的提示', () => {
  it('都发出去了', () => {
    const r = runMessage(run)
    expect(r.ok).toBe(true)
    expect(r.text).toContain('飞书摘要卡片')
    expect(r.text).toContain('邮件')
  })
  it('卡片没发出去时提示检查飞书机器人', () => {
    expect(runMessage({ ...run, sent: false })).toMatchObject({ ok: false })
  })
  it('邮件没发出去时提示授权码', () => {
    const r = runMessage({ ...run, mailed: false })
    expect(r.ok).toBe(false)
    expect(r.text).toContain('授权码')
  })
  it('没配邮箱时说明没有发邮件', () => {
    const r = runMessage({ ...run, mailed: null })
    expect(r.ok).toBe(true)
    expect(r.text).toContain('还没有配置邮箱')
  })
  it('关掉飞书卡片后只说邮件', () => {
    const r = runMessage({ ...run, sent: null })
    expect(r.ok).toBe(true)
    expect(r.text).not.toContain('飞书摘要卡片')
    expect(runMessage({ ...run, sent: null, mailed: null }).ok).toBe(false)
  })
  it('飞书文件没发出去时说出原因', () => {
    expect(runMessage({ ...run, file_error: '机器人还没有加入任何群' }).text).toContain('机器人还没有加入任何群')
  })
})
