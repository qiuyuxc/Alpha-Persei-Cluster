import { useEffect, useRef, useState } from 'react'
import { Button, Card } from '@heroui/react'
import { unlockHero, unlockAllHeroes, graduateHero } from '../lib/api'

export function HeroActions({ uid, heroId, name, enabled, onChanged }: {
  uid: number; heroId: number; name: string; enabled: boolean; onChanged: () => void
}) {
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const apply = async (action: 'unlock' | 'all' | 'graduate') => {
    setBusy(action); setMessage('')
    try {
      const res = action === 'all' ? await unlockAllHeroes(uid) : action === 'unlock' ? await unlockHero(heroId, uid) : await graduateHero(heroId, uid)
      if (res.code !== 0) throw new Error(res.msg || '操作失败')
      if (!mounted.current) return
      setFailed(false); setMessage(res.msg); onChanged()
    } catch (error) {
      if (mounted.current) { setFailed(true); setMessage(error instanceof Error ? error.message : '请求失败') }
    } finally { if (mounted.current) setBusy('') }
  }
  return <Card className="p-4 space-y-3">
    <div className="text-sm font-medium">角色管理 · UID {uid} · {name}（{heroId}）</div>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="secondary" isDisabled={!enabled || !!busy} onPress={() => apply('unlock')}>解锁当前角色</Button>
      <Button size="sm" variant="secondary" isDisabled={!enabled || !!busy} onPress={() => apply('all')}>解锁可用全图鉴</Button>
      <Button size="sm" isDisabled={!enabled || !!busy} onPress={() => apply('graduate')}>{busy === 'graduate' ? '处理中…' : '当前角色基础养成满级'}</Button>
    </div>
    <p className="text-xs text-muted">基础满级：等级 80、Ω、技能 35、权钥 60、支持的武器模组满级。刻印和钥从需另行配置。</p>
    {message && <p role="status" className={`text-sm ${failed ? 'text-danger' : 'text-success'}`}>{message}</p>}
  </Card>
}
