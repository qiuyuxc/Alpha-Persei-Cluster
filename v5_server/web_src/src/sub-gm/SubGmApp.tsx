import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowLeft, Boxes, ChevronRight, Clock3, LayoutDashboard, Mail, Moon, RefreshCw, Settings, ShoppingBag, Sliders, Sun, Swords, Users, Bot } from 'lucide-react'
import { apiRequest, getAuthToken, setAuthToken } from '../lib/api'
import { ServerProvider, useServer } from '../lib/serverContext'
import { ThemeProvider, useTheme } from '../lib/theme'
import { ErrorBoundary } from '../components/ErrorBoundary'
import type { LoginAccount } from '../components/AuthGate'
import AccountsPanel from '../panels/Accounts'
import HeroesPanel from '../panels/Heroes'
import InventoryPanel from '../panels/Inventory'
import MailPanel from '../panels/Mail'
import GachaPanel from '../panels/Gacha'
import AutomationPanel from '../panels/Automation'
import ShopPanel from '../panels/Shop'
import OverviewPanel from '../panels/Overview'
import SettingsPanel from '../panels/Settings'
import AIChatPanel from '../panels/AIChat'
import SocialPanel from '../panels/Social'

const PANELS = [
  { id: 'accounts', title: '账号数据', description: '档案、头像与账号管理', icon: Users, Panel: AccountsPanel },
  { id: 'heroes', title: '角色状态', description: '解锁、培养与角色配置', icon: Swords, Panel: HeroesPanel },
  { id: 'inventory', title: '资源背包', description: '货币、材料与刻印', icon: Boxes, Panel: InventoryPanel },
  { id: 'mail', title: '邮件系统', description: '附件、有效期与投递记录', icon: Mail, Panel: MailPanel },
  { id: 'gacha', title: '卡池管理', description: '上架、抽卡规则与保底', icon: Sliders, Panel: GachaPanel },
  { id: 'automation', title: '周期与自动化', description: '多维变量、轮换与等级礼包', icon: Clock3, Panel: AutomationPanel },
  { id: 'social', title: '人机与公会', description: '低频互动、额度与公会支援', icon: Users, Panel: SocialPanel },
  { id: 'shop', title: '商店系统', description: '商品、限购与刷新', icon: ShoppingBag, Panel: ShopPanel },
  { id: 'overview', title: '状态总览', description: '服务状态与运行日志', icon: LayoutDashboard, Panel: OverviewPanel },
  { id: 'aichat', title: 'AI 修正者聊天', description: '对话、人设与模型配置', icon: Bot, Panel: AIChatPanel },
  { id: 'settings', title: '其他设置', description: '战令、充值与服务配置', icon: Settings, Panel: SettingsPanel },
]

function selectedPanel() {
  const id = location.hash.slice(1)
  return PANELS.some(p => p.id === id) ? id : ''
}

type ManagedAccount = { uid: number; username: string | null; nick: string; level: number }

function Workspace({ account, logout }: { account: LoginAccount; logout: () => void }) {
  const { activeUid, setActiveUid, isOnline, refreshStatus } = useServer()
  const { resolved, setMode } = useTheme()
  const [panelId, setPanelId] = useState(selectedPanel)
  const [accounts, setAccounts] = useState<ManagedAccount[]>([])
  const [message, setMessage] = useState('')
  const [refresh, setRefresh] = useState(0)
  const scroll = useRef<HTMLElement>(null)
  const panel = PANELS.find(p => p.id === panelId)

  useEffect(() => {
    let active = true
    apiRequest<ManagedAccount[]>('/api/gm/accounts').then(r => { if (active) setAccounts(r.data) })
      .catch(e => { if (active) setMessage(e.message) })
    return () => { active = false }
  }, [refresh])
  useEffect(() => {
    const changed = () => setPanelId(selectedPanel())
    const navigate = (event: Event) => {
      const id = (event as CustomEvent<string>).detail
      if (PANELS.some(p => p.id === id)) location.hash = id
    }
    addEventListener('hashchange', changed)
    addEventListener('nav-to-tab', navigate)
    return () => { removeEventListener('hashchange', changed); removeEventListener('nav-to-tab', navigate) }
  }, [])
  useEffect(() => { scroll.current?.scrollTo(0, 0) }, [panelId, activeUid])

  return <div className="sub-workspace">
    <header className="sub-header">
      {panel ? <button className="sub-icon" aria-label="返回功能列表" onClick={() => { location.hash = '' }}><ArrowLeft size={20} /></button> : <span className="sub-mark">GM</span>}
      <div className="sub-heading"><h1>{panel?.title || '子 GM'}</h1><span data-online={isOnline}>{isOnline ? '已连接' : '连接中'} · 管理员 {account.username}</span></div>
      <button className="sub-icon" aria-label="切换明暗主题" onClick={() => setMode(resolved === 'dark' ? 'light' : 'dark')}>{resolved === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button>
      <button className="sub-icon" aria-label="刷新当前面板" onClick={() => { setMessage(''); setRefresh(n => n + 1); void refreshStatus() }}><RefreshCw size={18} /></button>
      <button className="sub-text-button" onClick={logout}>退出</button>
    </header>
    <div className="sub-target">
      <label htmlFor="sub-target-uid">操作存档</label>
      <select id="sub-target-uid" value={activeUid} onChange={e => setActiveUid(Number(e.target.value))}>
        {!accounts.some(a => a.uid === activeUid) && <option value={activeUid}>UID {activeUid}</option>}
        {accounts.map(a => <option key={a.uid} value={a.uid}>{a.nick || a.username || '未命名'} · Lv.{a.level} · UID {a.uid}</option>)}
      </select>
    </div>
    <main ref={scroll} className="sub-content" id="sub-content">
      {message && <p role="status" className="sub-message">{message}</p>}
      {panel ? <div className="sub-panel" data-panel={panelId} key={`${panelId}:${activeUid}:${refresh}`}>
        <ErrorBoundary fallbackTitle={`${panel.title}加载异常`}><panel.Panel /></ErrorBoundary>
      </div> : <>
        <p className="sub-intro">玩家数据操作使用上方选中的存档；卡池规则、周期和服务配置等全服设置会影响所有玩家。</p>
        <nav aria-label="子 GM 功能" className="sub-menu">
          {[{ title: '存档管理', items: PANELS.slice(0, 4) }, { title: '服务与配置', items: PANELS.slice(4) }].map(group => <section className="sub-menu-group" key={group.title}>
            <h2>{group.title}</h2>
            <div className="sub-menu-list">{group.items.map(({ id, title, description, icon: Icon }) => <button key={id} className="sub-menu-item" onClick={() => { location.hash = id }}>
              <span className={`sub-menu-symbol sub-symbol-${id}`}><Icon size={21} strokeWidth={1.8} /></span>
              <span className="sub-menu-copy"><strong>{title}</strong><small>{description}</small></span>
              <ChevronRight className="sub-chevron" size={17} strokeWidth={1.8} />
            </button>)}</div>
          </section>)}
        </nav>
      </>}
    </main>
  </div>
}

function SubSession() {
  const [account, setAccount] = useState<LoginAccount | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  useEffect(() => {
    let active = true
    const expired = () => { setAccount(null); setMessage('登录已失效，请重新登录。') }
    addEventListener('gm-auth-expired', expired)
    if (getAuthToken()) apiRequest<LoginAccount>('/api/auth/me')
      .then(r => {
        if (!active) return
        if (!r.data.is_admin) { setAuthToken(''); setMessage('此页面仅供管理员使用。'); return }
        setAccount(r.data)
      }).catch(e => { if (active) setMessage(e.message) })
      .finally(() => { if (active) setLoading(false) })
    else setLoading(false)
    return () => { active = false; removeEventListener('gm-auth-expired', expired) }
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage('')
    try {
      const r = await apiRequest<LoginAccount & { token: string }>('/api/auth/login', {
        method: 'POST', body: JSON.stringify({ username, password }),
      })
      setPassword(''); setAuthToken(r.data.token)
      if (!r.data.is_admin) {
        try { await apiRequest('/api/auth/logout', { method: 'POST' }) } finally { setAuthToken('') }
        throw new Error('此账号没有管理员权限，请使用管理员账号登录。')
      }
      setAccount(r.data)
    } catch (e) { setMessage((e as Error).message) }
    finally { setBusy(false) }
  }

  async function logout() {
    setBusy(true)
    try { await apiRequest('/api/auth/logout', { method: 'POST' }) }
    catch { /* 本地退出始终生效；服务器会话仍受有效期限制。 */ }
    finally { setAuthToken(''); setAccount(null); setPassword(''); setMessage('已退出登录。'); setBusy(false) }
  }

  if (loading) return <p role="status" className="sub-message">正在验证登录…</p>
  if (account) return <ServerProvider><Workspace account={account} logout={() => { if (!busy) void logout() }} /></ServerProvider>
  return <main className="sub-login">
    <section>
      <span className="sub-mark">GM</span><h1>子 GM</h1>
      <p>登录管理员账号，在游戏内管理存档。</p>
      <form onSubmit={submit}>
        <label>管理员账号<input autoComplete="username" inputMode="numeric" pattern="[0-9]{6,16}" minLength={6} maxLength={16} value={username} onChange={e => setUsername(e.target.value)} required /></label>
        <label>密码<input type="password" autoComplete="current-password" minLength={8} maxLength={64} value={password} onChange={e => setPassword(e.target.value)} required /></label>
        <button className="sub-primary" disabled={busy}>{busy ? '登录中…' : '登录'}</button>
      </form>
      {message && <p role="status" className="sub-message">{message}</p>}
      <small>此设备会记住登录状态，使用后可在顶部退出。</small>
    </section>
  </main>
}

export default function SubGmApp() {
  return <ThemeProvider><ErrorBoundary fallbackTitle="子 GM 加载异常"><SubSession /></ErrorBoundary></ThemeProvider>
}
