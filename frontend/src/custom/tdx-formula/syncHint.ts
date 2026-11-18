/** 数据页“立即同步”旁边的提示：本机行情还没到最新时，说明现在同步为什么不会有新数据。 */

/** 最近一个“当天数据已经可以下载”的工作日（不管节假日）。now 是北京时间的日期和 时:分。 */
export function expectedDay(today: string, clock: string, ready: string): string {
  const day = new Date(`${today}T00:00:00Z`)
  if (clock < ready) day.setUTCDate(day.getUTCDate() - 1)
  while (day.getUTCDay() === 0 || day.getUTCDay() === 6) day.setUTCDate(day.getUTCDate() - 1)
  return day.toISOString().slice(0, 10)
}

const isWeekday = (date: string) => {
  const d = new Date(`${date}T00:00:00Z`).getUTCDay()
  return d !== 0 && d !== 6
}

/**
 * 要显示的提示；不需要提示时是 null。
 * latest: 本机行情最新日期；ready: 当天数据最早几点能下载；runTime: 每天自动下载的时间。
 */
export function syncHint(latest: string | null, today: string, clock: string, ready: string, runTime: string): string | null {
  if (!latest) return null
  if (latest >= expectedDay(today, clock, ready)) {
    if (!(isWeekday(today) && clock < ready && latest < today)) return null
    return `本机行情到 ${latest}。今天的收盘数据 ${ready} 之后才能下载（${runTime} 自动下载并同步）；现在点“立即同步”只会整理本机已有的数据，不会有今天的。`
  }
  return `本机行情只到 ${latest}。“立即同步”不会联网下载：先到“通达信公式 → 数据”点“更新到最新”，下载完会自动同步到这里。`
}

/** 现在的北京时间：{ today: '2026-11-18', clock: '16:05' } */
export function beijingNow(now = new Date()): { today: string; clock: string } {
  const shifted = new Date(now.getTime() + 8 * 3600_000).toISOString()
  return { today: shifted.slice(0, 10), clock: shifted.slice(11, 16) }
}
