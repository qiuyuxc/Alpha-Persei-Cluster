import { useEffect, useState, type FormEvent } from 'react'
import { apiRequest } from '../lib/api'

type Status = { settings: { enabled: boolean; interval_days: number; time: string; shop_ids: number[]; start_ts: number }; next_restock_ts: number; last_restock_ts: number; timezone: string; utc_offset_seconds: number }

export default function ShopRestockSchedule({ refreshed }: { refreshed: () => void }) {
  const [status, setStatus] = useState<Status | null>(null)
  const [enabled, setEnabled] = useState(true)
  const [days, setDays] = useState(1)
  const [clock, setClock] = useState('05:00')
  const [shops, setShops] = useState('')
  const [firstDate, setFirstDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const date = (ts: number) => ts ? new Date((ts + (status?.utc_offset_seconds ?? 28800)) * 1000).toISOString().slice(0,16).replace('T', ' ') : '—'
  useEffect(() => {
    let active = true
    apiRequest<Status>('/api/gm/shop/restock/status').then(result => {
      if (result.code !== 0) throw new Error(result.msg)
      if (!active) return
      setStatus(result.data); setEnabled(result.data.settings.enabled); setDays(result.data.settings.interval_days)
      setClock(result.data.settings.time); setShops(result.data.settings.shop_ids.join(','))
    }).catch(error => { if (active) setMessage(error.message) })
    return () => { active = false }
  }, [])
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage('')
    try {
      const ids = shops.trim() ? shops.trim().split(/[,，\s]+/).map(Number) : []
      const start = firstDate ? Math.floor(Date.parse(`${firstDate}T${clock}:00Z`) / 1000) - (status?.utc_offset_seconds ?? 28800) : undefined
      if (firstDate && !Number.isFinite(start)) throw new Error('首次补货日期无效')
      const result = await apiRequest<Status>('/api/gm/shop/restock/settings', { method: 'POST', body: JSON.stringify({ enabled, interval_days: days, time: clock, shop_ids: ids, ...(start === undefined ? {} : { start_ts: start }) }) })
      if (result.code !== 0) throw new Error(result.msg)
      setStatus(result.data); setFirstDate(''); setMessage(enabled ? '补货计划已保存。' : '补货计划已停用，使用游戏原有周期。')
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  async function run() {
    setBusy(true); setMessage('')
    try {
      const result = await apiRequest<Status>('/api/gm/shop/restock/run', { method: 'POST', body: '{}' })
      if (result.code !== 0) throw new Error(result.msg)
      setStatus(result.data); setMessage('周期商品已补货。'); refreshed()
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  return <section className="space-y-3 rounded-2xl border border-default bg-surface p-5">
    <h2 className="font-semibold">定时补货</h2>
    <p className="text-sm text-muted">每日或按自定义间隔补货，刷新周期限购与每日采购货架。永久限购保持原有领取状态。</p>
    {status && <p className="text-sm">下次补货：{date(status.next_restock_ts)} · 服务器时区 {status.timezone}</p>}
    <form onSubmit={save} className="space-y-3">
      <fieldset disabled={busy || !status} className="space-y-3">
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用自动补货</label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm">间隔天数<input required type="number" min={1} max={365} className="mt-1 w-full rounded-lg border border-default bg-transparent p-2" value={days} onChange={e => setDays(Number(e.target.value))} /></label>
          <label className="text-sm">补货时刻（服务器时间）<input required type="time" className="mt-1 w-full rounded-lg border border-default bg-transparent p-2" value={clock} onChange={e => setClock(e.target.value)} /></label>
          <label className="text-sm">首次补货日期（可选）<input type="date" className="mt-1 w-full rounded-lg border border-default bg-transparent p-2" value={firstDate} onChange={e => setFirstDate(e.target.value)} /></label>
          <label className="text-sm">商店编号（可选）<input className="mt-1 w-full rounded-lg border border-default bg-transparent p-2" placeholder="留空为全部周期商店；例：2,4" value={shops} onChange={e => setShops(e.target.value)} /></label>
        </div>
        <div className="flex flex-wrap gap-2"><button className="rounded-xl border border-default px-3 py-2">{busy ? '处理中…' : '保存补货计划'}</button><button type="button" className="rounded-xl border border-default px-3 py-2" onClick={() => { void run() }}>现在补货</button></div>
      </fieldset>
    </form>
    {message && <p role="status" className="text-sm">{message}</p>}
  </section>
}
