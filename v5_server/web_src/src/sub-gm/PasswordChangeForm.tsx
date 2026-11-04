import { useState, type FormEvent } from 'react'
import { apiRequest } from '../lib/api'

export default function PasswordChangeForm({ required = false, changed }: {
  required?: boolean
  changed: () => void
}) {
  const [oldPassword, setOldPassword] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (password !== confirm) { setMessage('两次输入的新密码不一致。'); return }
    if (password === oldPassword) { setMessage('新密码须与原密码不同。'); return }
    setBusy(true); setMessage('')
    try {
      await apiRequest('/api/auth/password', {
        method: 'POST', body: JSON.stringify({ old_password: oldPassword, password }),
      })
      setOldPassword(''); setPassword(''); setConfirm('')
      changed()
    } catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }

  return <form onSubmit={submit}>
    {required && <p className="sub-intro">注册已完成。请把本地注册码作为原密码，设置新密码后登录游戏和子 GM。</p>}
    <label>{required ? '本地注册码（初始密码）' : '原密码'}<input type="password" autoComplete="current-password" minLength={8} maxLength={64} value={oldPassword} onChange={e => setOldPassword(e.target.value)} required /></label>
    <label>新密码<input type="password" autoComplete="new-password" minLength={8} maxLength={64} value={password} onChange={e => setPassword(e.target.value)} required /></label>
    <label>确认新密码<input type="password" autoComplete="new-password" minLength={8} maxLength={64} value={confirm} onChange={e => setConfirm(e.target.value)} required /></label>
    <small>新密码须为 8–64 个字符。修改后需在网页和游戏中重新登录。</small>
    {message && <p role="status" className="sub-message">{message}</p>}
    <button className="sub-primary" disabled={busy}>{busy ? '保存中…' : '保存新密码'}</button>
  </form>
}
