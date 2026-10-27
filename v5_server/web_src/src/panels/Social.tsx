import { useEffect, useState, type FormEvent } from 'react'
import { Button, Card } from '@heroui/react'
import { RefreshCw } from 'lucide-react'
import { apiRequest } from '../lib/api'
import { PageHeader } from '../components/kit'

type Settings = {
  enabled: boolean; basic_enabled: boolean; reply_enabled: boolean; ai_enabled: boolean
  guild_progress_enabled: boolean; chatter_minutes: number; reply_minutes: number
  api_daily_limit: number; reply_tokens: number; boss_hours: number
  daily_vitality: number; daily_donation: number
}
type Status = {
  settings: Settings
  bots: { uid: number; name: string; icon: number; level: number }[]
  guilds: { id: number; name: string; level: number; members: number; vitality: number; donation: number
    signed_in: number; boss_damage: number; boss_hp: number }[]
  api_usage: { reserved_requests: number; completed: number; failed: number }
  next_chatter_at: number; next_reply_at: number
}
const fields: { key: keyof Pick<Settings, 'chatter_minutes' | 'reply_minutes' | 'api_daily_limit' | 'reply_tokens' | 'boss_hours' | 'daily_vitality' | 'daily_donation'>; title: string; min: number; max: number }[] = [
  { key: 'chatter_minutes', title: '基础发言间隔（分钟）', min: 5, max: 180 },
  { key: 'reply_minutes', title: '自动回复全服冷却（分钟）', min: 5, max: 180 },
  { key: 'api_daily_limit', title: '每日 API 请求上限', min: 0, max: 100 },
  { key: 'reply_tokens', title: '每次回复 Token 上限', min: 32, max: 256 },
  { key: 'boss_hours', title: '人机推进一轮 Boss 的时长（小时）', min: 6, max: 168 },
  { key: 'daily_vitality', title: '每公会每日人机活跃目标', min: 0, max: 5000 },
  { key: 'daily_donation', title: '每公会每日人机捐献目标', min: 0, max: 1000 },
]
const toggles: { key: keyof Pick<Settings, 'enabled' | 'basic_enabled' | 'reply_enabled' | 'ai_enabled' | 'guild_progress_enabled'>; title: string; description: string }[] = [
  { key: 'enabled', title: '启用人机', description: '关闭后暂停发言、回复和定时贡献，已有进度保留。' },
  { key: 'basic_enabled', title: '基础活跃', description: '有玩家在线时低频发送预设内容，不调用 API。' },
  { key: 'reply_enabled', title: '自动回复', description: '回应玩家的文字消息，人机之间不会互相触发。' },
  { key: 'ai_enabled', title: '使用智能回复', description: '沿用 AI 修正者的模型配置，未配置或额度用完后改用预设回复。' },
  { key: 'guild_progress_enabled', title: '公会定时支援', description: '按时间推进签到、活跃、捐献和 Boss 进度，再随机分配给人机。' },
]

export default function SocialPanel() {
  const [data, setData] = useState<Status | null>(null)
  const [form, setForm] = useState<Settings | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  async function reload() {
    setBusy(true)
    try {
      const response = await apiRequest<Status>('/api/gm/social/status')
      setData(response.data); setForm(response.data.settings)
    } catch (error) { setFailed(true); setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  useEffect(() => { void reload() }, [])
  async function save(event: FormEvent) {
    event.preventDefault()
    if (!form) return
    setBusy(true); setMessage('')
    try {
      await apiRequest('/api/gm/social/settings', { method: 'POST', body: JSON.stringify(form) })
      setFailed(false); setMessage('已保存，全服人机将使用这些设置。')
      await reload()
    } catch (error) { setFailed(true); setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  return <div className="space-y-5">
    <PageHeader title="人机与公会" description="全服低频聊天、API 额度和公会定时支援。"
      actions={<Button variant="secondary" isDisabled={busy} onPress={reload}><RefreshCw size={16} />刷新状态</Button>} />
    {message && <p role="status" className={'rounded-2xl p-4 ' + (failed ? 'bg-danger/10 text-danger' : 'bg-success/10 text-success')}>{message}</p>}
    {data && <Card className="p-5 space-y-2">
      <h2 className="font-semibold">今日智能回复额度</h2>
      <p className="text-2xl font-semibold">{data.api_usage.reserved_requests} <span className="text-sm font-normal text-muted">/ {data.settings.api_daily_limit} 次请求额度</span></p>
      <p className="text-xs text-muted leading-5">成功回复 {data.api_usage.completed} 次，失败 {data.api_usage.failed} 次。兼容接口按最多两次尝试预留，失败也计入。每天按服务器 05:00 重置额度，重启和开关不会清零。</p>
    </Card>}
    <form onSubmit={save} className="space-y-5">
      <Card className="p-5 space-y-5">
        {toggles.map(({ key, title, description }) => <label key={key} className="flex items-start gap-3">
          <input className="mt-1 size-5 shrink-0" type="checkbox" checked={form?.[key] ?? false} disabled={!form || busy}
            onChange={e => setForm(old => old && ({ ...old, [key]: e.target.checked }))} />
          <span><strong className="text-sm font-medium">{title}</strong><span className="mt-1 block text-xs text-muted leading-5">{description}</span></span>
        </label>)}
      </Card>
      <Card className="p-5 space-y-4">
        <h2 className="font-semibold">频率与推进速度</h2>
        <fieldset disabled={!form || busy} className="grid gap-4 sm:grid-cols-2 disabled:opacity-60">
          {fields.map(({ key, title, min, max }) => <label key={key} className="space-y-2 text-sm">{title}
            <input required type="number" min={min} max={max} step={1} value={form?.[key] ?? min}
              onChange={e => setForm(old => old && ({ ...old, [key]: Number(e.target.value) }))}
              className="block w-full rounded-2xl border border-separator bg-surface-secondary px-4 py-2" />
          </label>)}
        </fieldset>
        <p className="text-xs text-muted leading-5">基础发言会带少量随机间隔。Boss 进度、贡献总和与人机排名一致；不替真人增加个人伤害，不运行真实战斗。上调时长或降低每日目标不会扣回已有贡献。</p>
        <Button type="submit" isDisabled={!form || busy}>{busy ? '处理中…' : '保存人机设置'}</Button>
      </Card>
    </form>
    {data && <Card className="p-5 space-y-3">
      <h2 className="font-semibold">支援角色 · {data.bots.length} 名</h2>
      <div className="flex flex-wrap gap-2">{data.bots.map(bot => <span key={bot.uid} className="rounded-xl bg-surface-secondary px-3 py-2 text-sm">{bot.name}</span>)}</div>
    </Card>}
    {data?.guilds.map(guild => <Card key={guild.id} className="p-5 space-y-3">
      <div><h2 className="font-semibold">{guild.name}</h2><p className="mt-1 text-xs text-muted">ID {guild.id} · Lv.{guild.level} · {guild.members} 名成员</p></div>
      <div className="grid grid-cols-3 gap-2 text-sm">
        <div>今日签到<p className="mt-1 font-semibold">{guild.signed_in}</p></div>
        <div>今日活跃<p className="mt-1 font-semibold">{guild.vitality}</p></div>
        <div>今日捐献<p className="mt-1 font-semibold">{guild.donation}</p></div>
      </div>
      <label className="block text-sm">本轮 Boss 支援进度
        <progress className="mt-2 block h-2 w-full overflow-hidden rounded-full accent-accent" max={guild.boss_hp} value={Math.min(guild.boss_damage, guild.boss_hp)} />
      </label>
      <p className="text-xs text-muted">{guild.boss_damage.toLocaleString()} / {guild.boss_hp.toLocaleString()}</p>
    </Card>)}
  </div>
}
