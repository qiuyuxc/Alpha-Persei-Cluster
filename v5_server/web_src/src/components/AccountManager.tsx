import { useEffect, useRef, useState } from 'react'
import { apiRequest, resolveApiUrl } from '../lib/api'
import { useServer } from '../lib/serverContext'
import SubGmPolicy from './SubGmPolicy'

type Account = { uid: number; username: string | null; nick: string; level: number; disabled: number; is_admin: number; fresh_profile: number;
  online: boolean; ready: boolean; connection_count: number; connected_at: number; last_seen_at: number }

export default function AccountManager() {
  const { activeUid, setActiveUid } = useServer()
  const [accounts, setAccounts] = useState<Account[]>([])
  const [message, setMessage] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [search, setSearch] = useState('')
  const revision = useRef(0)
  const selected = accounts.find(a => a.uid === activeUid)
  const visible = accounts.filter(a => `${a.uid} ${a.nick} ${a.username ?? ''}`.includes(search))
    .sort((a, b) => Number(b.online) - Number(a.online) || a.uid - b.uid)
  const date = (ts: number) => ts ? new Date(ts * 1000).toLocaleString() : '—'
  async function reload(quiet = false) {
    const current = ++revision.current
    try {
      const result = await apiRequest<Account[]>('/api/gm/accounts')
      if (result.code !== 0) throw new Error(result.msg)
      if (current === revision.current) setAccounts(result.data)
    } catch (error) { if (!quiet && current === revision.current) setMessage((error as Error).message) }
  }
  useEffect(() => {
    void reload()
    const timer = setInterval(() => { if (!document.hidden) void reload(true) }, 5000)
    return () => { clearInterval(timer); revision.current++ }
  }, [])
  async function action(path: string, body: object) {
    setBusy(true); setMessage('')
    try {
      const result = await apiRequest<{ clients?: number }>('/api/gm/accounts/' + path, { method: 'POST', body: JSON.stringify({ uid: activeUid, ...body }) })
      if (result.code !== 0) throw new Error(result.msg)
      setMessage(path === 'kick' ? `已踢下线 ${result.data?.clients ?? 0} 个游戏连接，账号可重新登录。`
        : path === 'disabled' ? ((body as { disabled: boolean }).disabled ? '账号已封禁，游戏连接已关闭。' : '账号已解除封禁，可重新登录。')
        : '密码已更新，该账号的旧登录已失效。')
      setPassword(''); await reload(true)
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  return <section className="space-y-4 rounded-2xl border border-default bg-surface p-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">玩家管理 · {accounts.filter(a => a.online).length} 人在线</h2><a href={resolveApiUrl('/web/account.html')}>打开注册 / 账号中心</a></div>
    <div className="flex gap-2"><input className="min-w-0 flex-1 rounded-xl border border-default bg-transparent p-2" aria-label="搜索玩家" placeholder="昵称、账号或 UID" value={search} onChange={e => setSearch(e.target.value)} /><button disabled={busy} onClick={() => { void reload() }} className="rounded-xl border border-default px-3">刷新</button></div>
    <div className="max-h-72 overflow-auto rounded-xl border border-default">
      {visible.map(a => <button key={a.uid} className="flex w-full items-center justify-between gap-3 border-b border-default p-3 text-left last:border-0" aria-pressed={a.uid === activeUid} onClick={() => { setActiveUid(a.uid); setPassword(''); setMessage('') }}>
        <span><strong>{a.nick || a.username || '未命名'}</strong><small className="block text-muted">Lv.{a.level} · UID {a.uid}{a.is_admin ? ' · 管理员' : ''}</small></span>
        <span className={a.disabled ? 'text-danger' : a.online ? 'text-success' : 'text-muted'}>{a.disabled ? '已封禁' : a.online ? (a.ready ? '在线' : '登录中') : '离线'}</span>
      </button>)}
      {!visible.length && <p className="p-3 text-sm text-muted">没有匹配的玩家</p>}
    </div>
    <label className="block">当前管理的存档
      <select className="mt-2 w-full rounded-xl border border-default bg-surface p-3" value={activeUid} onChange={e => { setActiveUid(Number(e.target.value)); setMessage(''); setPassword('') }}>
        {accounts.map(a => <option key={a.uid} value={a.uid}>{a.username || '未绑定登录'} · {a.nick} · Lv.{a.level} · UID {a.uid}{a.disabled ? '（已停用）' : ''}{a.is_admin ? '（管理员）' : ''}</option>)}
      </select>
    </label>
    <p className="text-sm text-muted">切换后，角色、仓库、邮件和抽卡管理均针对该 UID。注册新号从 1 级开始，旧存档单独保留。</p>
    {selected && <div className="space-y-2 text-sm">
      <p>{selected.online ? `${selected.connection_count} 个游戏连接 · 连接时间 ${date(selected.connected_at)} · 最近活动 ${date(selected.last_seen_at)}` : '当前游戏离线'}</p>
      <button className="rounded-xl border border-default px-3 py-2" disabled={busy || !selected.online} onClick={() => { void action('kick', {}) }}>踢下线</button>
      <span className="ml-2 text-xs text-muted">在线状态每 5 秒更新。</span>
    </div>}
    {selected?.username && <div className="flex flex-wrap items-center gap-3">
      {!selected.is_admin && <button className="rounded-xl border border-default px-3 py-2" disabled={busy} onClick={() => action('disabled', { disabled: !selected.disabled })}>{selected.disabled ? '解除封禁' : '封禁账号'}</button>}
      <form className="flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); void action('password', { password }) }}>
        <input aria-label={`UID ${activeUid} 的新密码`} className="rounded-xl border border-default bg-transparent px-3 py-2" type="password" autoComplete="new-password" placeholder="新密码，8–64 位" minLength={8} maxLength={64} required value={password} onChange={e => setPassword(e.target.value)} />
        <button className="rounded-xl border border-default px-3 py-2" disabled={busy || !!selected.disabled}>重置该账号密码</button>
      </form>
    </div>}
    {message && <p role="status">{message}</p>}
    <SubGmPolicy />
  </section>
}
