import { useEffect, useState, type FormEvent } from 'react'
import { apiRequest } from '../lib/api'

type Policy = { enabled: boolean; currency_level: number; material_level: number; hero_level: number; equipment_level: number }
const fields = [{ key: 'currency_level', label: '货币奖励' }, { key: 'material_level', label: '养成材料' },
  { key: 'hero_level', label: '角色解锁与培养' }, { key: 'equipment_level', label: '刻印与钥从' }] as const

export default function SubGmPolicy() {
  const [policy, setPolicy] = useState<Policy | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    apiRequest<Policy>('/api/gm/sub-gm/policy').then(result => {
      if (result.code !== 0) throw new Error(result.msg)
      if (active) setPolicy(result.data)
    }).catch(error => { if (active) setMessage(error.message) })
    return () => { active = false }
  }, [])
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage('')
    try {
      const result = await apiRequest<Policy>('/api/gm/sub-gm/policy', { method: 'POST', body: JSON.stringify(policy) })
      if (result.code !== 0) throw new Error(result.msg)
      setPolicy(result.data); setMessage('权限等级已保存，按玩家当前游戏等级生效。')
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  return <div className="space-y-3 border-t border-default pt-4">
    <h3 className="font-semibold">子 GM 权限等级</h3>
    <p className="text-sm text-muted">普通账号按游戏等级使用个人奖励与培养功能；管理员保留完整管理权限。</p>
    {policy && <form onSubmit={save} className="space-y-3">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={policy.enabled} onChange={e => setPolicy({ ...policy, enabled: e.target.checked })} />开放个人子 GM</label>
      <fieldset disabled={busy} className="grid gap-3 sm:grid-cols-2">{fields.map(field => <label key={field.key} className="text-sm">{field.label}开放等级<input required type="number" min={1} max={200} className="mt-1 w-full rounded-lg border border-default bg-transparent p-2" value={policy[field.key]} onChange={e => setPolicy({ ...policy, [field.key]: Number(e.target.value) })} /></label>)}</fieldset>
      <button disabled={busy} className="rounded-xl border border-default px-3 py-2">{busy ? '保存中…' : '保存权限等级'}</button>
    </form>}
    {message && <p role="status" className="text-sm">{message}</p>}
  </div>
}
