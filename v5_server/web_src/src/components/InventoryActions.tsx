import { useEffect, useRef, useState } from 'react'
import { Button, Card } from '@heroui/react'
import { addInventoryItem, addEquipSuit, previewInventoryCleanup, confirmInventoryCleanup, type CleanupPreview } from '../lib/api'

type Item = { id: number; name: string; type: number; rare: number; suit_id?: number }
const fieldClass = 'w-full rounded-lg border border-separator bg-surface px-3 py-2 text-sm text-foreground'

export function InventoryActions({ uid, enabled, selected, catalog, onChanged }: {
  uid: number; enabled: boolean; selected: Item | null; catalog: Item[]; onChanged: () => void
}) {
  const [itemId, setItemId] = useState('')
  const [quantity, setQuantity] = useState('1')
  const [asSuit, setAsSuit] = useState(false)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  const [category, setCategory] = useState<'equip' | 'servant'>('equip')
  const [maxStar, setMaxStar] = useState(3)
  const [preview, setPreview] = useState<CleanupPreview | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const requestId = useRef(0)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; requestId.current++ } }, [])
  useEffect(() => { if (selected) { setItemId(String(selected.id)); setAsSuit(false) } }, [selected])
  useEffect(() => { requestId.current++; setPreview(null); setConfirmation('') }, [category, maxStar, uid])
  const item = catalog.find(entry => entry.id === Number(itemId))
  const canSuit = item?.type === 7 && !!item.suit_id

  const grant = async () => {
    const id = Number(itemId), count = Number(quantity)
    if (!Number.isSafeInteger(id) || id <= 0 || !Number.isSafeInteger(count) || count <= 0) {
      setFailed(true); setMessage('请输入有效的物品 ID 和正整数数量'); return
    }
    setBusy('grant'); setMessage('')
    try {
      const res = asSuit && canSuit
        ? await addEquipSuit(item!.suit_id!, count, uid, Math.max(3, Math.min(5, item!.rare)))
        : await addInventoryItem(id, count, uid)
      if (res.code !== 0) throw new Error(res.msg || '发放失败')
      if (!mounted.current) return
      setFailed(false); setMessage(res.msg + (res.data?.balance !== undefined ? `；当前持有 ${res.data.balance}` : ''))
      onChanged()
    } catch (error) {
      if (mounted.current) { setFailed(true); setMessage(error instanceof Error ? error.message : '请求失败') }
    } finally { if (mounted.current) setBusy('') }
  }

  const loadPreview = async () => {
    const current = ++requestId.current
    setBusy('preview'); setMessage('')
    try {
      const res = await previewInventoryCleanup(uid, category, maxStar)
      if (res.code !== 0 || !res.data) throw new Error(res.msg || '预览失败')
      if (!mounted.current || current !== requestId.current) return
      setPreview(res.data); setConfirmation(''); setFailed(false)
    } catch (error) {
      if (mounted.current) { setFailed(true); setMessage(error instanceof Error ? error.message : '预览失败') }
    } finally { if (mounted.current) setBusy('') }
  }

  const clear = async () => {
    if (!preview || confirmation.trim() !== String(uid)) return
    setBusy('clear'); setMessage('')
    try {
      const res = await confirmInventoryCleanup(preview)
      if (res.code !== 0) throw new Error(res.msg || '清理失败')
      if (!mounted.current) return
      setFailed(false); setMessage(res.msg); setPreview(null); setConfirmation(''); onChanged()
    } catch (error) {
      if (mounted.current) { setFailed(true); setMessage(error instanceof Error ? error.message : '清理失败') }
    } finally { if (mounted.current) setBusy('') }
  }

  return <Card id="inventory-actions" className="p-5 space-y-4">
    <div><h2 className="font-semibold">物资管理 · UID {uid}</h2><p className="mt-1 text-xs text-muted">点击下方物品图标选择，也可直接输入 ID。执行后刷新真实库存。</p></div>
    <fieldset disabled={!enabled || !!busy} className="grid gap-3 sm:grid-cols-[1fr_140px_auto] disabled:opacity-60">
      <label className="space-y-1 text-xs">物品 ID<input className={fieldClass} value={itemId} onChange={event => { setItemId(event.target.value); setAsSuit(false) }} inputMode="numeric" placeholder="例如 38" /></label>
      <label className="space-y-1 text-xs">{asSuit ? '套数' : '数量'}<input className={fieldClass} type="number" min={1} step={1} value={quantity} onChange={event => setQuantity(event.target.value)} /></label>
      <Button className="self-end" onPress={grant} isDisabled={!enabled || !!busy || !itemId}>{busy === 'grant' ? '处理中…' : asSuit ? '发放六槽位套装' : '直接发放'}</Button>
    </fieldset>
    {item && <div className="text-sm">已选：{item.name} {canSuit && <label className="ml-3 text-xs"><input type="checkbox" checked={asSuit} disabled={!!busy} onChange={e => setAsSuit(e.target.checked)} /> 发放完整六件套</label>}</div>}
    {!enabled && <p className="text-xs text-warning">连接服务器并读取当前玩家库存后可操作。</p>}
    {message && <p role="status" className={`text-sm ${failed ? 'text-danger' : 'text-success'}`}>{message}</p>}
    <details className="border-t border-separator pt-3">
      <summary className="cursor-pointer text-sm font-medium">清理闲置刻印／钥从</summary>
      <p className="my-3 text-xs text-muted">直接删除，不返还材料。只处理未锁定、未装备、未培养的实例，每次最多 1000 件。预览后需要输入当前 UID 确认。</p>
      <fieldset disabled={!enabled || !!busy} className="flex flex-wrap items-end gap-3">
        <label className="text-xs">类别<select className={fieldClass} value={category} onChange={e => setCategory(e.target.value as 'equip' | 'servant')}><option value="equip">刻印</option><option value="servant">钥从</option></select></label>
        <label className="text-xs">最高星级<select className={fieldClass} value={maxStar} onChange={e => setMaxStar(Number(e.target.value))}>{[1, 2, 3, 4, 5].map(star => <option key={star} value={star}>{star} 星及以下</option>)}</select></label>
        <Button variant="secondary" onPress={loadPreview} isDisabled={!enabled || !!busy}>{busy === 'preview' ? '查询中…' : '预览清单'}</Button>
      </fieldset>
      {preview && <div className="mt-4 space-y-3 rounded-xl border border-warning/30 p-4">
        <p className="text-sm">UID {preview.uid}：本次可清理 {preview.count} 件{preview.category === 'equip' ? '刻印' : '钥从'}。</p>
        <p className="text-xs text-muted break-all">实例 ID：{preview.items.slice(0, 24).map(item => item.id).join('、') || '无'}{preview.count > 24 ? ' …' : ''}</p>
        {preview.count > 0 && <div className="flex gap-3"><input aria-label="输入当前 UID 确认清理" className={fieldClass} placeholder={`输入 ${uid} 确认`} value={confirmation} disabled={!!busy} onChange={e => setConfirmation(e.target.value)} /><Button variant="danger" onPress={clear} isDisabled={!!busy || confirmation.trim() !== String(uid)}>确认清理 {preview.count} 件</Button></div>}
      </div>}
    </details>
  </Card>
}
