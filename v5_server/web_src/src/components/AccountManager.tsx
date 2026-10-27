import { useEffect, useState } from 'react'
import { apiRequest, resolveApiUrl } from '../lib/api'
import { useServer } from '../lib/serverContext'

type Account = { uid: number; username: string | null; nick: string; level: number; disabled: number; is_admin: number; fresh_profile: number }

export default function AccountManager() {
  const { activeUid, setActiveUid } = useServer()
  const [accounts, setAccounts] = useState<Account[]>([])
  const [message, setMessage] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const selected = accounts.find(a => a.uid === activeUid)
  async function reload() {
    try { setAccounts((await apiRequest<Account[]>('/api/gm/accounts')).data) }
    catch (error) { setMessage((error as Error).message) }
  }
  useEffect(() => { void reload() }, [])
  async function action(path: string, body: object) {
    setBusy(true); setMessage('')
    try {
      await apiRequest('/api/gm/accounts/' + path, { method: 'POST', body: JSON.stringify({ uid: activeUid, ...body }) })
      setMessage('已保存，该账号的旧登录已失效。'); setPassword(''); await reload()
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  return <section className="space-y-4 rounded-2xl border border-default bg-surface p-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">多用户管理</h2><a href={resolveApiUrl('/web/account.html')}>打开注册 / 账号中心</a></div>
    <label className="block">当前管理的存档
      <select className="mt-2 w-full rounded-xl border border-default bg-surface p-3" value={activeUid} onChange={e => { setActiveUid(Number(e.target.value)); setMessage(''); setPassword('') }}>
        {accounts.map(a => <option key={a.uid} value={a.uid}>{a.username || '未绑定登录'} · {a.nick} · Lv.{a.level} · UID {a.uid}{a.disabled ? '（已停用）' : ''}{a.is_admin ? '（管理员）' : ''}</option>)}
      </select>
    </label>
    <p className="text-sm text-muted">切换后，角色、仓库、邮件和抽卡管理均针对该 UID。注册新号从 1 级开始，旧存档单独保留。</p>
    {selected?.username && <div className="flex flex-wrap items-center gap-3">
      {!selected.is_admin && <button className="rounded-xl border border-default px-3 py-2" disabled={busy} onClick={() => action('disabled', { disabled: !selected.disabled })}>{selected.disabled ? '启用账号' : '停用账号'}</button>}
      <form className="flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); void action('password', { password }) }}>
        <input aria-label={`UID ${activeUid} 的新密码`} className="rounded-xl border border-default bg-transparent px-3 py-2" type="password" autoComplete="new-password" placeholder="新密码，8–64 位" minLength={8} maxLength={64} required value={password} onChange={e => setPassword(e.target.value)} />
        <button className="rounded-xl border border-default px-3 py-2" disabled={busy || !!selected.disabled}>重置该账号密码</button>
      </form>
    </div>}
    {message && <p role="status">{message}</p>}
  </section>
}
