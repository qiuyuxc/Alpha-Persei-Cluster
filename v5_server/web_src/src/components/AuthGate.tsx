import { useEffect, useState, type ReactNode, type FormEvent } from 'react'
import { apiRequest, getAuthToken, setAuthToken, resolveApiUrl, getStoredApiBase, setStoredApiBase } from '../lib/api'

export interface LoginAccount {
  uid: number; username: string; is_admin: number; fresh_profile: number
}

const field = 'w-full rounded-xl border border-default bg-transparent px-3 py-2'
const button = 'rounded-xl bg-accent px-4 py-2 text-accent-foreground disabled:opacity-50'

export default function AuthGate({ children, portal = false }: { children?: ReactNode; portal?: boolean }) {
  const [account, setAccount] = useState<LoginAccount | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [setupToken] = useState(() => new URLSearchParams(location.hash.slice(1)).get('setup') || '')
  const [mode, setMode] = useState<'login' | 'register' | 'setup'>(setupToken ? 'setup' : portal ? 'register' : 'login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [oldPassword, setOldPassword] = useState('')
  const [server, setServer] = useState(getStoredApiBase())

  useEffect(() => {
    if (setupToken) history.replaceState(null, '', location.pathname + location.search)
    let active = true
    const expired = () => { setAccount(null); setMessage('登录已失效，请重新登录。') }
    window.addEventListener('gm-auth-expired', expired)
    if (getAuthToken()) apiRequest<LoginAccount>('/api/auth/me')
      .then(r => { if (active) setAccount(r.data) })
      .catch(() => { setAuthToken('') })
      .finally(() => { if (active) setLoading(false) })
    else setLoading(false)
    return () => { active = false; window.removeEventListener('gm-auth-expired', expired) }
  }, [setupToken])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (mode !== 'login' && password !== confirm) { setMessage('两次输入的密码不一致。'); return }
    setBusy(true); setMessage('')
    try {
      if (mode !== 'login') {
        await apiRequest(`/api/auth/${mode}`, { method: 'POST', body: JSON.stringify({ username, password, setup_token: setupToken }) })
      }
      const result = await apiRequest<LoginAccount & { token: string }>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) })
      setAuthToken(result.data.token); setAccount(result.data); setPassword(''); setConfirm('')
      setMessage(mode === 'register' ? '注册成功。回到游戏，选择账号密码登录即可。' : mode === 'setup' ? '管理员设置完成，原存档已保留。' : '')
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }

  async function logout() {
    try { await apiRequest('/api/auth/logout', { method: 'POST' }) }
    finally { setAuthToken(''); setAccount(null); setMode('login') }
  }

  async function changePassword(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage('')
    try {
      await apiRequest('/api/auth/password', { method: 'POST', body: JSON.stringify({ old_password: oldPassword, password }) })
      setAuthToken(''); setAccount(null); setMode('login'); setPassword(''); setOldPassword('')
      setMessage('密码已修改，请在网页和游戏中重新登录。')
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }

  if (loading) return <p className="p-8" role="status">正在验证登录…</p>
  if (account?.is_admin && !portal) return <>
    <div className="flex items-center justify-end gap-4 px-4 py-2 text-sm">
      <span>管理员 {account.username}</span>
      <a href={resolveApiUrl('/web/account.html')}>账号中心 / 注册新号</a>
      <button onClick={logout}>退出登录</button>
    </div>
    {children}
  </>

  return <main className="min-h-screen bg-background p-6 text-foreground">
    <section className="mx-auto mt-10 max-w-lg space-y-5 rounded-2xl border border-default bg-surface p-6 shadow-sm">
      <h1 className="text-2xl font-semibold">{account ? '账号中心' : mode === 'setup' ? '设置管理员' : mode === 'register' ? '注册新账号' : '账号登录'}</h1>
      {account ? <>
        <p>账号 {account.username} · UID {account.uid}</p>
        <p>{account.fresh_profile ? '这是独立的新存档，从 1 级、初始角色和未通关状态开始。' : '这个账号绑定原有存档，等级、角色和物品均已保留。'}</p>
        <p>游戏内选择「账号密码登录」，输入同一账号和密码。切换账号时先退出游戏中的旧登录。</p>
        <div className="flex gap-4">
          {account.is_admin ? <a className={button} href={resolveApiUrl('/web/gm_console.html')}>进入 GM 后台</a> : <span>普通玩家账号</span>}
          <button onClick={logout}>退出 / 注册其他账号</button>
        </div>
        <form className="space-y-3 border-t border-default pt-4" onSubmit={changePassword}>
          <h2 className="font-semibold">修改密码</h2>
          <label className="block">原密码<input className={field} type="password" autoComplete="current-password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} required /></label>
          <label className="block">新密码<input className={field} type="password" autoComplete="new-password" minLength={8} maxLength={64} value={password} onChange={e => setPassword(e.target.value)} required /></label>
          <button className={button} disabled={busy}>保存密码</button>
        </form>
      </> : <>
        <p>{mode === 'setup' ? '设置账号密码后，将绑定已有的高等级存档。该账号同时用于 GM 后台管理。' : '新账号拥有独立存档，从 1 级逐步解锁。原有存档不会被覆盖。'}</p>
        {location.protocol === 'file:' && <label className="block">服务器地址<input className={field} value={server} onChange={e => setServer(e.target.value)} onBlur={() => setStoredApiBase(server)} placeholder="https://服务器地址" /></label>}
        <form className="space-y-4" onSubmit={submit}>
          <label className="block">数字账号<input className={field} autoComplete="username" inputMode="numeric" pattern="[0-9]{6,16}" minLength={6} maxLength={16} value={username} onChange={e => setUsername(e.target.value)} required /></label>
          <p className="text-sm text-muted">6–16 位数字，无需真实手机号。游戏登录页使用数字键盘。</p>
          <label className="block">密码<input className={field} type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} minLength={8} maxLength={64} value={password} onChange={e => setPassword(e.target.value)} required /></label>
          {mode !== 'login' && <label className="block">确认密码<input className={field} type="password" autoComplete="new-password" value={confirm} onChange={e => setConfirm(e.target.value)} required /></label>}
          <button className={button} disabled={busy}>{busy ? '处理中…' : mode === 'setup' ? '保存并绑定原存档' : mode === 'register' ? '注册并登录' : '登录'}</button>
        </form>
        {mode !== 'setup' && <button onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setMessage('') }}>{mode === 'login' ? '没有账号？注册新号' : '已有账号？登录'}</button>}
        <p className="text-sm text-muted">注册后回到游戏使用账号密码登录，短信验证码入口暂未启用。</p>
      </>}
      {message && <p role="status" className="rounded-xl bg-default p-3">{message}</p>}
    </section>
  </main>
}
