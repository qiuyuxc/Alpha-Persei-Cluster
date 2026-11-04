import { useEffect, useState, type FormEvent } from 'react'
import { Moon, RefreshCw, Sun } from 'lucide-react'
import { apiRequest } from '../lib/api'
import { useTheme } from '../lib/theme'
import type { LoginAccount } from '../components/AuthGate'
import PasswordChangeForm from './PasswordChangeForm'

type Capability = 'currency' | 'materials' | 'heroes' | 'equipment'
type Profile = {
  game: { uid: number; nick: string; level: number }
  online: boolean
  enabled: boolean
  permissions: Record<Capability, { label: string; level: number; allowed: boolean }>
  limits: Record<Capability, number>
  heroes: Array<{ id: number; name: string; owned: boolean; level: number }>
  history: Array<{ action: string; item_id: number; amount: number; created_at: number }>
}
const categories: Capability[] = ['currency', 'materials', 'heroes', 'equipment']

export default function SelfWorkspace({ account, logout, passwordChanged }: {
  account: LoginAccount; logout: () => void; passwordChanged: () => void
}) {
  const { resolved, setMode } = useTheme()
  const [profile, setProfile] = useState<Profile | null>(null)
  const [category, setCategory] = useState<Capability>('currency')
  const [items, setItems] = useState<Array<{ id: number; name: string }>>([])
  const [search, setSearch] = useState('')
  const [itemId, setItemId] = useState('')
  const [count, setCount] = useState('100')
  const [heroId, setHeroId] = useState(0)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const permitted = !!profile?.permissions[category].allowed
  const hero = profile?.heroes.find(value => value.id === heroId)

  async function reload() {
    try {
      const result = await apiRequest<Profile>('/api/sub-gm/me')
      if (result.code !== 0) throw new Error(result.msg)
      setProfile(result.data)
      setHeroId(old => old || result.data.heroes.find(h => h.owned)?.id || result.data.heroes[0]?.id || 0)
    } catch (error) { setMessage((error as Error).message) }
  }
  useEffect(() => {
    void reload()
    const timer = setInterval(() => { if (!document.hidden) void reload() }, 10000)
    return () => clearInterval(timer)
  }, [])
  useEffect(() => {
    let active = true
    setItems([]); setItemId('')
    if (!permitted) return
    const timer = setTimeout(() => {
      apiRequest<{ items: Array<{ id: number; name: string }> }>(`/api/sub-gm/items?category=${category}&search=${encodeURIComponent(search)}`)
        .then(result => {
          if (!active) return
          if (result.code !== 0) throw new Error(result.msg)
          setItems(result.data.items)
          setItemId(String(result.data.items[0]?.id ?? ''))
        }).catch(error => { if (active) setMessage(error.message) })
    }, 250)
    return () => { active = false; clearTimeout(timer) }
  }, [category, search, permitted])

  async function run(path: string, body: object, success: string) {
    setBusy(true); setMessage('')
    try {
      const result = await apiRequest<{ sync?: { clients: number; requires_relogin?: boolean } }>(`/api/sub-gm/${path}`, { method: 'POST', body: JSON.stringify(body) })
      if (result.code !== 0) throw new Error(result.msg)
      setMessage(success + (result.data.sync?.requires_relogin || !result.data.sync?.clients ? '，重进游戏后可查看。' : '，已同步到游戏。'))
      await reload()
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }
  function grant(event: FormEvent) {
    event.preventDefault()
    void run('grant', { item_id: Number(itemId), count: Number(count) }, `已发放 ${count} 个 ${items.find(i => i.id === Number(itemId))?.name ?? itemId}`)
  }

  return <div className="sub-workspace">
    <header className="sub-header">
      <span className="sub-mark">GM</span>
      <div className="sub-heading"><h1>子 GM</h1><span>{profile?.game.nick || account.username} · Lv.{profile?.game.level ?? '—'} · {profile?.online ? '游戏在线' : '游戏离线'}</span></div>
      <button className="sub-icon" aria-label="切换明暗主题" onClick={() => setMode(resolved === 'dark' ? 'light' : 'dark')}>{resolved === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button>
      <button className="sub-icon" aria-label="刷新账号等级与权限" disabled={busy} onClick={() => { void reload() }}><RefreshCw size={18} /></button>
      <button className="sub-text-button" disabled={busy} onClick={logout}>退出</button>
    </header>
    <main className="sub-content">
      <p className="sub-intro">当前游戏账号 UID {account.uid}，奖励与培养操作发给此账号。</p>
      <details className="self-card"><summary>修改账号密码</summary>
        <PasswordChangeForm changed={passwordChanged} />
      </details>
      {message && <p className="sub-message" role="status">{message}</p>}
      {!profile ? <p role="status">正在读取游戏等级…</p> : <>
        {!profile.enabled && <p className="sub-message">子 GM 奖励功能当前已关闭。</p>}
        <div className="self-permissions">{categories.map(key => <div key={key} data-allowed={profile.permissions[key].allowed}>
          <strong>{profile.permissions[key].label}</strong>
          <span>{profile.permissions[key].allowed ? '已开放' : `Lv.${profile.permissions[key].level} 开放`}</span>
        </div>)}</div>
        <section className="self-card">
          <h2>发放奖励与养成物品</h2>
          <form onSubmit={grant}>
            <label>奖励分类<select value={category} onChange={e => { const value = e.target.value as Capability; setCategory(value); setCount(value === 'heroes' || value === 'equipment' ? '1' : '100') }}>
              {categories.map(key => <option key={key} value={key}>{profile.permissions[key].label}{!profile.permissions[key].allowed ? ` · Lv.${profile.permissions[key].level}` : ''}</option>)}
            </select></label>
            <fieldset disabled={busy || !permitted}>
              <label>搜索物品<input placeholder="物品名称或编号" value={search} onChange={e => setSearch(e.target.value)} maxLength={100} /></label>
              <label>选择物品<select required value={itemId} onChange={e => setItemId(e.target.value)}>
                {!items.length && <option value="">没有匹配的可发放物品</option>}
                {items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
              </select></label>
              <label>数量<input required type="number" min={1} max={profile.limits[category]} value={count} onChange={e => setCount(e.target.value)} /></label>
              <button className="sub-primary" disabled={!itemId}>{busy ? '处理中…' : '发给当前账号'}</button>
            </fieldset>
            {!permitted && <p className="sub-intro">此分类尚未开放，达到对应游戏等级后自动解锁。</p>}
          </form>
        </section>
        {profile.permissions.heroes.allowed && <section className="self-card">
          <h2>角色培养</h2>
          <label>选择角色<select value={heroId} onChange={e => setHeroId(Number(e.target.value))}>
            {profile.heroes.map(value => <option key={value.id} value={value.id}>{value.name} · {value.id} · {value.owned ? `Lv.${value.level}` : '未解锁'}</option>)}
          </select></label>
          <div className="self-actions">
            <button className="sub-primary" disabled={busy || !heroId || !!hero?.owned} onClick={() => { void run('hero/unlock', { hero_id: heroId }, '角色已解锁') }}>解锁角色</button>
            <button className="sub-primary" disabled={busy || !hero?.owned} onClick={() => { void run('hero/graduate', { hero_id: heroId }, '角色培养已完成') }}>角色满级培养</button>
          </div>
        </section>}
        {!!profile.history.length && <details className="self-card"><summary>近期操作</summary>
          {profile.history.map((entry, index) => <p className="sub-intro" key={index}>{new Date(entry.created_at * 1000).toLocaleString()} · {entry.action === 'grant' ? '发放' : entry.action === 'unlock' ? '解锁' : '培养'} {entry.item_id} × {entry.amount}</p>)}
        </details>}
      </>}
    </main>
  </div>
}
