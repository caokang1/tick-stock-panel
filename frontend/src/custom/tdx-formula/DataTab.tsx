import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, FolderInput, RefreshCw, Save, Square } from 'lucide-react'
import { toast } from '@/components/Toast'
import { tdxApi, tdxKeys, type DataProgress, type Status } from './client'
import { boardsSummary, etaText, progressPercent, realtimeIntervalError, runTimeError, todayText } from './logic'
import { SourcesPanel } from './SourcesPanel'
import { BUTTON, CARD, ErrorNote, INPUT, PRIMARY, WarnNote, errorText } from './ui'

const DEFAULT_START = '2015-01-01'
const H2 = 'text-sm font-medium text-foreground'
const NOTE = 'text-[11px] leading-relaxed text-muted'

/** 下载、更新的进度；有任务在跑时每 2 秒问一次 */
export function useDataProgress() {
  return useQuery({
    queryKey: tdxKeys.dataProgress,
    queryFn: tdxApi.dataProgress,
    refetchInterval: query => (query.state.data?.running ? 2000 : false),
  })
}

/** 下载或更新一结束，就让行情状态、今日提醒、策略列表重新取一次。放在整页一直在的地方，切到别的页也照样生效。 */
export function useRefreshAfterDataTask(): DataProgress | undefined {
  const qc = useQueryClient()
  const progress = useDataProgress()
  const running = progress.data?.running
  const wasRunning = useRef(false)
  useEffect(() => {
    if (running === undefined) return
    if (wasRunning.current && !running) {
      void qc.invalidateQueries({ queryKey: tdxKeys.status })
      void qc.invalidateQueries({ queryKey: tdxKeys.today })
      void qc.invalidateQueries({ queryKey: tdxKeys.strategies })
      void qc.invalidateQueries({ queryKey: tdxKeys.dataSources })
    }
    wasRunning.current = running
  }, [running, qc])
  return progress.data
}

/** 行情管理（显示在 设置 → 数据源 → 免费公开来源 里）：行情下到哪了、下载和更新、每天几点自动更新、从别处导入、各个来源的限速。 */
export function DataTab() {
  const qc = useQueryClient()
  const progress = useDataProgress()
  const running = progress.data?.running ?? false
  const status = useQuery({
    queryKey: tdxKeys.status, queryFn: tdxApi.status, staleTime: 30_000,
    refetchInterval: running ? 30_000 : false, // 下载过程中，日期范围和股票数跟着长
  })
  const sources = useQuery({
    queryKey: tdxKeys.dataSources, queryFn: tdxApi.dataSources, staleTime: 30_000,
    refetchInterval: running ? 10_000 : false, // 顺便刷新各来源今天的请求次数
  })
  const [start, setStart] = useState('')
  const [taskError, setTaskError] = useState<string | null>(null)

  const onStarted = (next: DataProgress) => {
    setTaskError(null)
    qc.setQueryData(tdxKeys.dataProgress, next)
  }
  const onStartFailed = (e: unknown) => {
    setTaskError(errorText(e))
    void progress.refetch()
  }
  const download = useMutation({ mutationFn: tdxApi.startDownload, onSuccess: onStarted, onError: onStartFailed })
  const update = useMutation({ mutationFn: tdxApi.startUpdate, onSuccess: onStarted, onError: onStartFailed })
  const stop = useMutation({
    mutationFn: tdxApi.stopDownload,
    onSuccess: () => void progress.refetch(),
    onError: e => setTaskError(errorText(e)),
  })

  const data = status.data
  const libsMissing = sources.data?.libraries_ready === false
  const starting = download.isPending || update.isPending
  const blocked = running || starting || libsMissing
  const begun = Boolean(data?.has_data || data?.first_date)
  const stopAsked = running && (stop.isPending || stop.isSuccess)

  function startDownload() {
    stop.reset()
    download.mutate(begun ? undefined : start || undefined)
  }

  return (
    <div className="space-y-4">
      <section className={`${CARD} space-y-3`}>
        <h2 className={H2}>行情数据</h2>
        <p className={NOTE}>
          “通达信公式”页面用的是全市场 A 股的日线行情，来自免费的公开来源（新浪财经、腾讯财经、东方财富和 BaoStock），由这里下载、保存在这台电脑上，不需要账号，也不花钱。
        </p>
        <StatusBox status={data} loading={status.isLoading} error={status.error ? errorText(status.error) : null} />

        {libsMissing && (
          <ErrorNote>
            取数用的两个库（akshare、baostock）还没有安装，现在不能下载和更新。请到本面板左下角的“设置 → 数据源”，找到“免费公开来源”点“安装”；
            也可以自己手动安装这两个库，然后重启本面板。
          </ErrorNote>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <button className={PRIMARY} disabled={blocked} onClick={startDownload}>
            <Download className="h-3.5 w-3.5" />{begun ? '继续下载历史数据' : '开始下载历史数据'}
          </button>
          {progress.data?.kind === 'download' && running && (
            <button className={BUTTON} disabled={stopAsked} onClick={() => stop.mutate()}>
              <Square className="h-3.5 w-3.5" />停止
            </button>
          )}
          <button className={BUTTON} disabled={blocked || !data?.has_data} onClick={() => update.mutate()}>
            <RefreshCw className="h-3.5 w-3.5" />更新到最新
          </button>
          {!begun && !running && (
            <label className="flex items-center gap-1.5 text-xs text-secondary">
              从哪一天开始下
              <input
                type="date" value={start} max={todayText()} onChange={e => setStart(e.target.value)}
                className={`${INPUT} w-36`}
              />
              <span className="text-[11px] text-muted">不填就从 {DEFAULT_START} 开始</span>
            </label>
          )}
          {running && <span className="text-[11px] text-muted">已经有任务在运行，结束后才能再点</span>}
        </div>
        {taskError && <ErrorNote>{taskError}</ErrorNote>}
        {stopAsked && <p className="text-xs text-warning">正在停止：下完手上这一只股票就停，已经下好的都会留着。</p>}
        {progress.error && <ErrorNote>下载进度没有取到：{errorText(progress.error)}</ErrorNote>}
        {progress.data && <ProgressBox progress={progress.data} />}

        <p className={NOTE}>
          第一次下载全市场的历史行情大约要 3 个小时，期间电脑要开着、联着网，本面板不能关。下载是故意放慢、一个一个排队进行的，
          这是为了遵守数据来源的使用规则，避免被限制访问。可以随时点“停止”，下次点“继续下载历史数据”会从停下的地方接着下，不会重头再来。
          “更新到最新”只补最近几个交易日的行情，一般几分钟就好。
        </p>
      </section>

      <ScheduleSection />
      <RealtimeSection />
      <BoardsSection busy={running} />
      <ImportSection disabled={running} />

      <SourcesPanel
        data={sources.data}
        loading={sources.isLoading}
        error={sources.error ? errorText(sources.error) : null}
        onRetry={() => void sources.refetch()}
      />
    </div>
  )
}

function StatusBox({ status, loading, error }: { status: Status | undefined; loading: boolean; error: string | null }) {
  if (loading) return <p className="text-xs text-muted">正在检查行情数据…</p>
  if (error || !status) return <ErrorNote>行情数据的状态没有取到：{error ?? '未知原因'}</ErrorNote>
  if (!status.has_data) {
    return (
      <div className="space-y-2">
        <WarnNote>还没有行情数据。点下面的“开始下载历史数据”，或者在下方从已有的目录导入一份；有了行情才能回测和生成提醒。</WarnNote>
        <p className="break-all text-[11px] text-muted">下载的行情会保存在：{status.data_dir}</p>
      </div>
    )
  }
  return (
    <div className="space-y-2">
      <p className="text-xs text-secondary">
        行情：<b className="font-medium text-foreground">{status.first_date ?? '—'}</b> 到{' '}
        <b className="font-medium text-foreground">{status.latest_date ?? '—'}</b>，共{' '}
        <b className="font-medium text-foreground">{status.stock_count.toLocaleString('en-US')}</b> 只股票（含已退市的），后复权。
      </p>
      <p className="break-all text-[11px] text-muted">保存在：{status.data_dir}</p>
      {!status.download_finished && <WarnNote>历史行情还没有下完，点“继续下载历史数据”接着下。</WarnNote>}
      {!status.st_known && (
        <WarnNote>历史 ST 状态还没有补齐，“排除 ST 股”在缺数据的日子可能不准。点“继续下载历史数据”补齐。</WarnNote>
      )}
      {!status.float_shares_ready && (
        <WarnNote>历史流通股本还没有补齐，流通市值、换手率相关的条件在缺数据的日子不起作用。点“继续下载历史数据”补齐。</WarnNote>
      )}
    </div>
  )
}

function ProgressBox({ progress }: { progress: DataProgress }) {
  if (!progress.running) {
    if (!progress.message) return null
    return (
      <p className="rounded-btn border border-border bg-base px-3 py-2 text-xs text-secondary">
        上一次{progress.kind === 'update' ? '更新' : '下载'}的结果：{progress.message}
      </p>
    )
  }
  const updating = progress.kind === 'update'
  const known = progress.total > 0
  const percent = progressPercent(progress.done, progress.total)
  const eta = etaText(progress.eta_seconds)
  return (
    <div className="space-y-1.5 rounded-btn border border-border bg-base px-3 py-2" aria-live="polite">
      <p className="text-xs text-foreground">
        {updating ? '正在更新当天的行情' : '正在下载历史数据'}
        {!updating && progress.stage && `：${progress.stage}`}
      </p>
      <div
        role="progressbar" aria-label={updating ? '更新进度' : '下载进度'}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={known ? percent : undefined}
        className="h-2 overflow-hidden rounded-full bg-elevated"
      >
        <div
          className={`h-full rounded-full bg-accent transition-[width] ${known ? '' : 'animate-pulse'}`}
          style={{ width: known ? `${percent}%` : '100%' }}
        />
      </div>
      <p className="text-[11px] text-muted">
        {known
          ? `已完成 ${progress.done.toLocaleString('en-US')} / ${progress.total.toLocaleString('en-US')} 只股票（${percent}%）${eta ? `，预计还需${eta}` : ''}`
          : updating ? '一般几分钟就好，完成后这里会显示结果。' : '正在准备，马上开始…'}
      </p>
      {!updating && progress.message && <p className="text-[11px] text-muted">{progress.message}</p>}
    </div>
  )
}

function RealtimeSection() {
  const qc = useQueryClient()
  const setting = useQuery({ queryKey: tdxKeys.dataRealtime, queryFn: tdxApi.dataRealtime, staleTime: 30_000 })
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: tdxApi.setDataRealtime,
    onSuccess: next => {
      qc.setQueryData(tdxKeys.dataRealtime, next)
      setText(null)
      setError(null)
      toast(`已保存，实时行情每 ${next.interval_seconds} 秒取一轮`, 'success')
    },
    onError: e => setError(errorText(e)),
  })

  const data = setting.data
  const value = text ?? (data ? String(data.interval_seconds) : '')
  function submit() {
    if (!data) return
    const problem = realtimeIntervalError(value, data.interval_min, data.interval_max)
    setError(problem)
    if (!problem) save.mutate(Number(value))
  }

  return (
    <section className={`${CARD} space-y-2`}>
      <h2 className={H2}>实时行情和五档盘口</h2>
      {setting.isLoading ? <p className="text-xs text-muted">正在加载…</p>
        : setting.error || !data ? (
          <div className="space-y-2">
            <ErrorNote>实时行情的设置没有取到：{errorText(setting.error)}</ErrorNote>
            <button className={BUTTON} onClick={() => void setting.refetch()}>再试一次</button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs text-secondary">
              <label className="flex items-center gap-1.5">
                每隔
                <input
                  type="number" value={value} min={data.interval_min} max={data.interval_max} step={5}
                  onChange={e => { setText(e.target.value); setError(null) }}
                  className={`${INPUT} w-24`}
                />
                秒取一轮全市场快照
              </label>
              <button className={PRIMARY} disabled={save.isPending || Number(value) === data.interval_seconds} onClick={submit}>
                <Save className="h-3.5 w-3.5" />{save.isPending ? '保存中…' : '保存'}
              </button>
              <span className="text-[11px] text-muted">
                可以设在 {data.interval_min} 到 {data.interval_max} 秒之间，默认 {data.interval_default} 秒
              </span>
            </div>
            {error && <ErrorNote>{error}</ErrorNote>}
            <p className={NOTE}>
              把上面“能力路由”里的实时行情、五档盘口切到“免费公开来源”后生效，用的是腾讯的行情接口，只在交易时段取。
              一轮全市场约 7 次请求；TSP 自己的刷新间隔设得再短，这里设的秒数之内也只给上一轮的结果，不会多发请求。
              五档盘口直接用同一轮快照里带的数据。免费接口比付费行情慢几秒，覆盖沪深的股票、ETF 和指数，不含北交所。
            </p>
          </>
        )}
    </section>
  )
}

function BoardsSection({ busy }: { busy: boolean }) {
  const qc = useQueryClient()
  const boards = useQuery({
    queryKey: tdxKeys.dataBoards, queryFn: tdxApi.dataBoards, staleTime: 30_000,
    refetchInterval: query => (query.state.data?.running ? 2000 : false),
  })
  const [error, setError] = useState<string | null>(null)
  const update = useMutation({
    mutationFn: tdxApi.updateDataBoards,
    onSuccess: next => { qc.setQueryData(tdxKeys.dataBoards, next); setError(null) },
    onError: e => setError(errorText(e)),
  })
  const data = boards.data

  return (
    <section className={`${CARD} space-y-2`}>
      <h2 className={H2}>行业和概念</h2>
      {boards.isLoading ? <p className="text-xs text-muted">正在加载…</p>
        : boards.error || !data ? (
          <div className="space-y-2">
            <ErrorNote>行业和概念的情况没有取到：{errorText(boards.error)}</ErrorNote>
            <button className={BUTTON} onClick={() => void boards.refetch()}>再试一次</button>
          </div>
        ) : (
          <>
            <p className="text-xs text-secondary">{boardsSummary(data)}</p>
            <div className="flex flex-wrap items-center gap-2">
              <button className={PRIMARY} disabled={busy || data.running || update.isPending} onClick={() => update.mutate()}>
                <RefreshCw className="h-3.5 w-3.5" />{data.running ? '更新中…' : '更新行业和概念'}
              </button>
              {busy && <span className="text-[11px] text-muted">行情正在下载或更新，结束后才能更新这一项</span>}
            </div>
            {error && <ErrorNote>{error}</ErrorNote>}
            {!data.running && data.message && <p className="text-[11px] text-muted">{data.message}</p>}
            <p className={NOTE}>
              给 TSP 的“行业分析”“概念分析”用：从东方财富取每只股票所属的行业和概念，写进 TSP 的行业表和概念表，
              一次约 200 次请求、几分钟。之后每 {data.refresh_days} 天左右自动更新一次（放在周末）。
              行业用的是东方财富的行业分类（二级，如“白酒”），在 TSP 里显示为“所属行业”；
              概念只取有明确入选理由的题材概念。只有“现在属于哪些板块”，没有历史。
            </p>
          </>
        )}
    </section>
  )
}

function ScheduleSection() {
  const qc = useQueryClient()
  const schedule = useQuery({ queryKey: tdxKeys.dataSchedule, queryFn: tdxApi.dataSchedule, staleTime: 30_000 })
  const [time, setTime] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: tdxApi.setDataSchedule,
    onSuccess: next => {
      qc.setQueryData(tdxKeys.dataSchedule, next)
      setTime(null)
      setError(null)
      toast(`已保存，之后每个交易日 ${next.run_time} 自动更新`, 'success')
    },
    onError: e => setError(errorText(e)), // 后端不接受这个时间时，会说明允许的范围和原因
  })
  const autoSync = useMutation({
    mutationFn: tdxApi.setAutoSyncTsp,
    onSuccess: next => {
      qc.setQueryData(tdxKeys.dataSchedule, next)
      toast(next.auto_sync_tsp ? '已打开：行情更新完会自动让 TSP 同步' : '已关闭，TSP 要自己点同步', 'success')
    },
    onError: e => setError(errorText(e)),
  })

  const data = schedule.data
  const value = time ?? data?.run_time ?? ''
  function submit() {
    if (!data) return
    const problem = runTimeError(value, data.run_time_min, data.run_time_max)
    setError(problem)
    if (!problem) save.mutate(value)
  }

  return (
    <section className={`${CARD} space-y-2`}>
      <h2 className={H2}>每天自动更新的时间</h2>
      {schedule.isLoading ? <p className="text-xs text-muted">正在加载…</p>
        : schedule.error || !data ? (
          <div className="space-y-2">
            <ErrorNote>自动更新的时间没有取到：{errorText(schedule.error)}</ErrorNote>
            <button className={BUTTON} onClick={() => void schedule.refetch()}>再试一次</button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs text-secondary">
              <label className="flex items-center gap-1.5">
                每个交易日
                <input
                  type="time" value={value} min={data.run_time_min} max={data.run_time_max}
                  onChange={e => { setTime(e.target.value); setError(null) }}
                  className={`${INPUT} w-28`}
                />
              </label>
              <button className={PRIMARY} disabled={save.isPending || value === data.run_time} onClick={submit}>
                <Save className="h-3.5 w-3.5" />{save.isPending ? '保存中…' : '保存'}
              </button>
              <span className="text-[11px] text-muted">可以设在 {data.run_time_min} 到 {data.run_time_max} 之间</span>
            </div>
            {error && <ErrorNote>{error}</ErrorNote>}
            <p className={NOTE}>
              每个交易日到这个时间自动更新当天的行情，然后生成提醒；只在 TSP 运行时执行，更新失败会每半小时再试一次。
              股市 15:00 收盘，数据来源要到 {data.run_time_min} 之后才有当天完整的行情，所以不能设得更早。
            </p>
            <label className="flex items-center gap-2 text-xs text-secondary">
              <input
                type="checkbox" checked={data.auto_sync_tsp} disabled={autoSync.isPending}
                onChange={e => autoSync.mutate(e.target.checked)}
              />
              行情更新完以后，自动让 TSP 接着同步一次
            </label>
            <p className={NOTE}>
              TSP 自己的盘后同步默认在 15:35，那时这里还没有当天的行情。打开后，这里更新完、而 TSP 里的日 K 还停在前一天时，
              会替你点一次 TSP 的同步（只在日 K 用的是“免费公开来源”时）；不成功最多再试两次，每次隔半小时。
              {data.tsp_sync_message && <span className="block text-secondary">最近一次：{data.tsp_sync_message}</span>}
            </p>
          </>
        )}
    </section>
  )
}

function ImportSection({ disabled }: { disabled: boolean }) {
  const qc = useQueryClient()
  const [path, setPath] = useState('')
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null)
  const run = useMutation({
    mutationFn: tdxApi.importData,
    onSuccess: ({ copied, ...next }) => {
      qc.setQueryData(tdxKeys.status, next)
      void qc.invalidateQueries({ queryKey: tdxKeys.today })
      void qc.invalidateQueries({ queryKey: tdxKeys.strategies })
      setResult({ ok: true, text: `导入完成，一共拷贝了 ${copied.toLocaleString('en-US')} 个文件。` })
      toast('行情已导入', 'success')
    },
    onError: e => setResult({ ok: false, text: errorText(e) }),
  })

  return (
    <section className={`${CARD} space-y-2`}>
      <h2 className={H2}>不下载，从已有的目录导入</h2>
      <p className={NOTE}>已经用 quant-lab 下载过行情的，可以填它的 data 目录直接拷一份过来，省掉重新下载；原目录不会被改动。</p>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={path}
          onChange={e => { setPath(e.target.value); setResult(null) }}
          placeholder="例如 /home/me/quant-lab/data"
          aria-label="要导入的行情目录"
          maxLength={500}
          className={`${INPUT} min-w-[280px] flex-1`}
        />
        <button className={BUTTON} disabled={disabled || run.isPending || !path.trim()} onClick={() => run.mutate(path.trim())}>
          <FolderInput className="h-3.5 w-3.5" />{run.isPending ? '正在导入…' : '导入'}
        </button>
      </div>
      {run.isPending && <p className="text-[11px] text-muted">正在拷贝，文件多的时候要几分钟，请不要关掉页面。</p>}
      {disabled && <p className="text-[11px] text-muted">正在下载或更新行情，等它结束后才能导入。</p>}
      {result && (result.ok ? <p className="text-xs text-secondary">{result.text}</p> : <ErrorNote>{result.text}</ErrorNote>)}
    </section>
  )
}
