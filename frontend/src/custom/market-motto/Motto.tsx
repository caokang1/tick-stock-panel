/**
 * 侧栏左上角的那句话：按当天的市场情绪评分从语料库里挑。
 * 鼠标移上去看评分，点一下去每日复盘。评分取不到时显示默认的一句。
 */
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { FALLBACK, hint, mottoApi, mottoKeys } from './client'

export function Motto({ color }: { color: string }) {
  const navigate = useNavigate()
  const { data } = useQuery({
    queryKey: mottoKeys.current,
    queryFn: mottoApi.current,
    refetchInterval: 60 * 1000,
    staleTime: 30 * 1000,
    retry: false,
  })
  return (
    <button
      type="button" title={hint(data)} onClick={() => navigate('/daily-review')}
      className="min-w-0 cursor-pointer truncate text-left text-[13px] font-semibold tracking-[0.08em] drop-shadow-[0_0_8px_rgba(139,92,246,0.4)]"
      style={{ color }}
    >
      {data?.text ?? FALLBACK}
    </button>
  )
}
