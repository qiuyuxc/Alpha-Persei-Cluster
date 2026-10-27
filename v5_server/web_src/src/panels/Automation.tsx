import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Button, Card } from '@heroui/react'
import { Clock3, RefreshCw } from 'lucide-react'
import { PageHeader, Section } from '../components/kit'
import { useServer } from '../lib/serverContext'
import {
  fetchAutomationStatus, fetchGachaPools, isDisplayOnlyItem, saveLevelMailRule, saveRuntimeSchedule,
  type AutomationStatus, type GachaPoolEntry, type LevelMailRuleDraft, type RotationSettings,
} from '../lib/api'
import catalog from '../data/items_catalog.json'

const control = 'w-full rounded-lg border border-separator bg-surface px-3 py-2 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-accent'

function serverDate(ts: number, offset: number, input = false) {
  if (!ts) return input ? '' : '—'
  const value = new Date((ts + offset) * 1000).toISOString().slice(0, 16)
  return input ? value : value.replace('T', ' ')
}

function newRule(): LevelMailRuleDraft {
  return { name: '成长礼包', enabled: true, level_min: 11, level_max: 100, title: '成长礼包',
    sender: '隐科组总务部', content: '管理员，成长物资已送达，请在有效期内查收。',
    valid_days: 30, include_existing: true,
    attachments: [{ id: 38, name: '精确探测凭证', number: 20 }, { id: 2, name: '艾因索菲币', number: 200000 },
      { id: 40103, name: '高级作战记录', number: 30 }, { id: 40301, name: '神力因子', number: 50 },
      { id: 41301, name: '次级权能因子', number: 30 }] }
}

function draftOf(rule: LevelMailRuleDraft): LevelMailRuleDraft {
  return { id: rule.id, name: rule.name, enabled: rule.enabled, level_min: rule.level_min, level_max: rule.level_max,
    title: rule.title, sender: rule.sender, content: rule.content, valid_days: rule.valid_days,
    attachments: rule.attachments.map(item => ({ ...item })) }
}

function PoolPicker({ pools, selected, onChange }: { pools: GachaPoolEntry[]; selected: number[]; onChange: (ids: number[]) => void }) {
  const [search, setSearch] = useState('')
  const filtered = pools.filter(p => `${p.name} ${p.pool_id} ${p.type_label}`.toLowerCase().includes(search.toLowerCase()))
  return <div className="space-y-2 pt-3">
    <input className={control} aria-label="搜索卡池" placeholder="搜索卡池名称或编号" value={search} onChange={e => setSearch(e.target.value)} />
    <div className="grid max-h-52 gap-2 overflow-y-auto sm:grid-cols-2">
      {filtered.map(pool => <label key={pool.pool_id} className="flex items-start gap-2 rounded-lg bg-surface-secondary px-3 py-2 text-xs">
        <input type="checkbox" checked={selected.includes(pool.pool_id)} onChange={e => onChange(e.target.checked ? [...selected, pool.pool_id] : selected.filter(id => id !== pool.pool_id))} />
        <span>{pool.name} <span className="text-muted">({pool.pool_id})</span></span>
      </label>)}
      {!filtered.length && <p className="text-xs text-muted">没有匹配的可用卡池</p>}
    </div>
  </div>
}

export default function AutomationPanel() {
  const { isOnline } = useServer()
  const [data, setData] = useState<AutomationStatus | null>(null)
  const [pools, setPools] = useState<GachaPoolEntry[]>([])
  const [bpDays, setBpDays] = useState('42')
  const [polyDays, setPolyDays] = useState('7')
  const [rotation, setRotation] = useState<RotationSettings | null>(null)
  const [rotationStart, setRotationStart] = useState('')
  const [rule, setRule] = useState<LevelMailRuleDraft>(newRule)
  const [search, setSearch] = useState('')
  const [itemId, setItemId] = useState('')
  const [itemCount, setItemCount] = useState('10')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  const offset = data?.utc_offset_seconds ?? 28800
  const date = (ts: number) => serverDate(ts, offset)
  const items = useMemo(() => catalog.filter(item => !isDisplayOnlyItem(item.id, item.type, item.name)), [])
  const matches = useMemo(() => items.filter(item => `${item.name} ${item.id}`.toLowerCase().includes(search.toLowerCase())).slice(0, 80), [items, search])

  async function reload(reset = false) {
    setLoading(true)
    try {
      const [status, poolData] = await Promise.all([fetchAutomationStatus(), fetchGachaPools()])
      if (status.code !== 0) throw new Error(status.msg)
      if (poolData.code !== 0) throw new Error(poolData.msg)
      setData(status.data)
      setPools([...new Map(poolData.data.pools.filter(p => !p.is_version_locked).map(p => [p.pool_id, p])).values()])
      if (reset || !data || !rotation) {
        const cfg = status.data.settings
        setBpDays(String(cfg.battlepass.next_duration_days ?? cfg.battlepass.duration_days))
        setPolyDays(String(cfg.polyhedron?.next_duration_days ?? cfg.polyhedron?.duration_days ?? 7))
        setRotation(structuredClone(cfg.rotation))
        setRotationStart(serverDate(cfg.rotation.start_ts, status.data.utc_offset_seconds, true))
        if (status.data.mail_rules[0]) setRule(draftOf(status.data.mail_rules[0]))
      }
    } catch (error) {
      setFailed(true)
      setMessage(error instanceof Error ? error.message : '读取自动化配置失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (isOnline) void reload(true) }, [isOnline])

  async function savePeriod(event: FormEvent) {
    event.preventDefault()
    setBusy('period'); setMessage('')
    try {
      const result = await saveRuntimeSchedule({ battlepass: { duration_days: Number(bpDays) } })
      if (result.code !== 0) throw new Error(result.msg)
      setData(old => old ? { ...old, ...result.data } : old)
      setFailed(false)
      setMessage('对策协议周期已保存。新天数从下一轮生效，本轮进度保留。')
    } catch (error) {
      setFailed(true); setMessage(error instanceof Error ? error.message : '保存失败')
    } finally { setBusy('') }
  }

  async function savePolyhedron(event: FormEvent) {
    event.preventDefault()
    if (!data?.polyhedron || !data.settings.polyhedron) return
    setBusy('polyhedron'); setMessage('')
    try {
      const result = await saveRuntimeSchedule({ polyhedron: { duration_days: Number(polyDays) } })
      if (result.code !== 0) throw new Error(result.msg)
      setData(old => old ? { ...old, ...result.data } : old)
      setFailed(false)
      setMessage('多维变量周期已保存。新天数从下一轮生效，本轮窗口不变。')
    } catch (error) {
      setFailed(true); setMessage(error instanceof Error ? error.message : '保存失败')
    } finally { setBusy('') }
  }

  async function saveRotation(event: FormEvent) {
    event.preventDefault()
    if (!rotation) return
    setBusy('rotation'); setMessage('')
    try {
      const start = Math.floor(Date.parse(rotationStart + ':00Z') / 1000) - offset
      if (!Number.isFinite(start)) throw new Error('请填写有效的轮换起始时间')
      const result = await saveRuntimeSchedule({ rotation: { enabled: rotation.enabled, duration_days: rotation.duration_days,
        start_ts: start, fixed_pool_ids: rotation.fixed_pool_ids, steps: rotation.steps } })
      if (result.code !== 0) throw new Error(result.msg)
      setData(old => old ? { ...old, ...result.data } : old)
      setRotation(structuredClone(result.data.settings.rotation))
      setFailed(false); setMessage(rotation.enabled ? '自动轮换已保存，将按起始时间和顺序循环上架。' : '自动轮换已停用。')
    } catch (error) {
      setFailed(true); setMessage(error instanceof Error ? error.message : '保存失败')
    } finally { setBusy('') }
  }

  async function saveMailRule(event: FormEvent) {
    event.preventDefault()
    setBusy('rule'); setMessage('')
    try {
      const result = await saveLevelMailRule(rule)
      if (result.code !== 0) throw new Error(result.msg)
      setRule(draftOf(result.data))
      setFailed(false); setMessage(result.msg)
      await reload()
    } catch (error) {
      setFailed(true); setMessage(error instanceof Error ? error.message : '保存失败')
    } finally { setBusy('') }
  }

  const disabled = !isOnline || !data || !!busy
  const selected = data?.mail_rules.find(item => item.id === rule.id)

  return <div className="space-y-6">
    <PageHeader title="周期与自动化" description="全服周期、卡池轮换和等级成长礼包。保存后自动执行。"
      actions={<Button variant="secondary" onPress={() => reload()} isDisabled={loading || !!busy}><RefreshCw className="size-4" />{loading ? '读取中…' : '刷新状态'}</Button>} />
    {message && <p role="status" className={`rounded-xl border p-3 text-sm ${failed ? 'border-danger/30 text-danger' : 'border-success/30 text-success'}`}>{message}</p>}
    {!isOnline && <p className="text-sm text-muted">连接服务器后可查看和配置自动化。</p>}
    {data && <Card className="flex flex-wrap items-center justify-between gap-3 p-4 text-sm">
      <span className="flex items-center gap-2"><Clock3 className="size-4" />后台检查：{data.scheduler.running ? '运行中' : '未启动'}，每 {data.scheduler.interval_seconds} 秒一次</span>
      <span className="text-xs text-muted">最近检查 {date(data.scheduler.last_tick_ts)} · 服务器时间 {data.timezone}</span>
      {data.scheduler.last_error && <p className="w-full text-danger">最近检查失败：{data.scheduler.last_error}</p>}
    </Card>}

    <Section title="对策协议与任务周期" description="每日任务在服务器当地 05:00 刷新，周常任务在每周一 05:00 刷新。">
      <Card className="space-y-4 p-5">
        <div className="grid gap-3 text-sm sm:grid-cols-3">
          <p>本轮对策协议<br /><span className="text-xs text-muted">{date(data?.battlepass.start_ts ?? 0)} 至 {date(data?.battlepass.end_ts ?? 0)}</span></p>
          <p>下次日常刷新<br /><span className="text-xs text-muted">{date(data?.tasks.next_daily_ts ?? 0)}</span></p>
          <p>下次周常刷新<br /><span className="text-xs text-muted">{date(data?.tasks.next_weekly_ts ?? 0)}</span></p>
        </div>
        <form onSubmit={savePeriod} className="space-y-3">
          <fieldset disabled={disabled} className="flex flex-wrap items-end gap-3 disabled:opacity-60">
            <label className="space-y-1 text-sm">下一轮对策协议天数<input required className={control} type="number" min={1} max={365} value={bpDays} onChange={e => setBpDays(e.target.value)} /></label>
            <Button type="submit" isDisabled={disabled}>保存对策协议周期</Button>
          </fieldset>
          <p className="text-xs text-muted">本轮使用 {data?.settings.battlepass.duration_days ?? 42} 天。到期后重置对策协议等级、领取记录和付费档位，其他养成进度保留。修改天数从下一轮生效。</p>
        </form>
      </Card>
    </Section>

    <Section title="多维变量周期" description="多维变量（矩阵肉鸽）按本窗口轮换，客户端活动倒计时取同一个起止时间。">
      <Card className="space-y-4 p-5">
        {data && (!data.polyhedron || !data.settings.polyhedron) ? <p className="text-sm text-muted">当前服务端暂不支持多维变量周期配置，更新后可用。其他周期设置可正常使用。</p> : <>
        <p className="text-sm">本轮多维变量<br /><span className="text-xs text-muted">{date(data?.polyhedron?.start_ts ?? 0)} 至 {date(data?.polyhedron?.end_ts ?? 0)}</span></p>
        <form onSubmit={savePolyhedron} className="space-y-3">
          <fieldset disabled={disabled} className="flex flex-wrap items-end gap-3 disabled:opacity-60">
            <label className="space-y-1 text-sm">下一轮多维变量天数<input required className={control} type="number" min={1} max={365} value={polyDays} onChange={e => setPolyDays(e.target.value)} /></label>
            <Button type="submit" isDisabled={disabled}>保存多维变量周期</Button>
          </fieldset>
          <p className="text-xs text-muted">本轮使用 {data?.settings.polyhedron?.duration_days ?? 7} 天。修改天数从下一轮生效，本轮窗口与已有探索记录不受影响。</p>
        </form>
        </>}
      </Card>
    </Section>

    <Section title="卡池自动轮换" description="常驻池一直开放，轮换组按顺序循环。已有保底和自选目标继续保留。">
      <Card className="space-y-4 p-5">
        {data && <p className="text-sm">{data.rotation.enabled ? (data.rotation.waiting ? '等待起始时间' : `当前：${data.rotation.step_name}`) : '自动轮换已停用'}
          {data.rotation.enabled && <span className="ml-2 text-xs text-muted">{data.rotation.waiting ? '首次开始' : '下次轮换'} {date(data.rotation.waiting ? data.settings.rotation.start_ts : data.rotation.next_rotation_ts)}</span>}</p>}
        <form onSubmit={saveRotation} className="space-y-4">
          <fieldset disabled={disabled || !rotation} className="space-y-4 disabled:opacity-60">
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={rotation?.enabled ?? false} onChange={e => setRotation(old => old ? { ...old, enabled: e.target.checked } : old)} />启用自动轮换</label>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-1 text-sm">每组持续天数<input required className={control} type="number" min={1} max={365} value={rotation?.duration_days ?? 7} onChange={e => setRotation(old => old ? { ...old, duration_days: Number(e.target.value) } : old)} /></label>
              <label className="space-y-1 text-sm">第一组开始时间（服务器时间）<input required className={control} type="datetime-local" value={rotationStart} onChange={e => setRotationStart(e.target.value)} /></label>
            </div>
            <details className="rounded-xl border border-separator p-3"><summary className="cursor-pointer text-sm">常驻卡池 · 已选 {rotation?.fixed_pool_ids.length ?? 0} 个</summary>
              <PoolPicker pools={pools} selected={rotation?.fixed_pool_ids ?? []} onChange={ids => setRotation(old => old ? { ...old, fixed_pool_ids: ids,
                steps: old.steps.map(step => ({ ...step, pool_ids: step.pool_ids.filter(id => !ids.includes(id)) })) } : old)} />
            </details>
            {rotation?.steps.map((step, index) => <div key={index} className="space-y-3 rounded-xl border border-separator p-3">
              <div className="flex flex-wrap items-center gap-2">
                <label className="min-w-36 flex-1 text-xs">第 {index + 1} 组名称<input required className={control} maxLength={80} value={step.name} onChange={e => setRotation(old => old ? { ...old, steps: old.steps.map((s, i) => i === index ? { ...s, name: e.target.value } : s) } : old)} /></label>
                <Button type="button" variant="secondary" isDisabled={index === 0} onPress={() => setRotation(old => {
                  if (!old) return old
                  const steps = [...old.steps]; [steps[index - 1], steps[index]] = [steps[index], steps[index - 1]]
                  return { ...old, steps }
                })}>上移</Button>
                <Button type="button" variant="secondary" isDisabled={index === rotation.steps.length - 1} onPress={() => setRotation(old => {
                  if (!old) return old
                  const steps = [...old.steps]; [steps[index + 1], steps[index]] = [steps[index], steps[index + 1]]
                  return { ...old, steps }
                })}>下移</Button>
                <Button type="button" variant="secondary" isDisabled={rotation.steps.length <= 1} onPress={() => setRotation(old => old ? { ...old, steps: old.steps.filter((_, i) => i !== index) } : old)}>移除</Button>
              </div>
              <details><summary className="cursor-pointer text-xs">轮换卡池 · 已选 {step.pool_ids.length} 个：{step.pool_ids.map(id => pools.find(p => p.pool_id === id)?.name ?? id).join('、') || '仅开放常驻池'}</summary>
                <PoolPicker pools={pools.filter(p => !rotation.fixed_pool_ids.includes(p.pool_id))} selected={step.pool_ids} onChange={ids => setRotation(old => old ? { ...old, steps: old.steps.map((s, i) => i === index ? { ...s, pool_ids: ids } : s) } : old)} />
              </details>
            </div>)}
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="secondary" isDisabled={!rotation || rotation.steps.length >= 30} onPress={() => setRotation(old => old ? { ...old, steps: [...old.steps, { name: `第 ${old.steps.length + 1} 组`, pool_ids: [] }] } : old)}>添加轮换组</Button>
              <Button type="submit" isDisabled={disabled}>保存轮换设置</Button>
            </div>
          </fieldset>
          <p className="text-xs text-muted">轮换启用后，每次检查按已保存的轮换组上架卡池。调整起始时间或顺序会重新计算当前组。</p>
        </form>
      </Card>
    </Section>

    <Section title="等级自动邮件礼包" description="进入等级范围后自动投递。每个账号每条规则只发一次，重复登录、重新升级或重启不会重复发送。">
      <Card className="space-y-4 p-5">
        <div className="flex flex-wrap gap-2">{data?.mail_rules.map(item => <Button key={item.id} variant={rule.id === item.id ? 'primary' : 'secondary'} isDisabled={!!busy} onPress={() => setRule(draftOf(item))}>{item.name} · {item.enabled ? '启用' : '停用'}</Button>)}
          <Button variant="secondary" isDisabled={!!busy} onPress={() => setRule(newRule())}>新建规则</Button>
        </div>
        {selected && <p className="text-xs text-muted">已发 {selected.sent_count} 个账号 · 待发 {selected.pending_count} 个 · 创建时跳过 {selected.skipped_count} 个。修改规则保留已发记录。</p>}
        {selected?.failures.map(item => <p key={item.uid} className="text-xs text-danger">UID {item.uid} 投递失败，将自动重试：{item.last_error}</p>)}
        <form onSubmit={saveMailRule} className="space-y-4">
          <fieldset disabled={disabled} className="space-y-4 disabled:opacity-60">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-1 text-sm">规则名称<input required maxLength={80} className={control} value={rule.name} onChange={e => setRule({ ...rule, name: e.target.value })} /></label>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={rule.enabled} onChange={e => setRule({ ...rule, enabled: e.target.checked })} />启用这条规则</label>
              <label className="space-y-1 text-sm">等级下限<input required type="number" min={1} max={100} className={control} value={rule.level_min} onChange={e => setRule({ ...rule, level_min: Number(e.target.value) })} /></label>
              <label className="space-y-1 text-sm">等级上限<input required type="number" min={rule.level_min} max={100} className={control} value={rule.level_max} onChange={e => setRule({ ...rule, level_max: Number(e.target.value) })} /></label>
              <label className="space-y-1 text-sm">邮件标题<input required maxLength={120} className={control} value={rule.title} onChange={e => setRule({ ...rule, title: e.target.value })} /></label>
              <label className="space-y-1 text-sm">发件人<input required maxLength={80} className={control} value={rule.sender} onChange={e => setRule({ ...rule, sender: e.target.value })} /></label>
              <label className="space-y-1 text-sm">邮件有效天数<input required type="number" min={1} max={3650} className={control} value={rule.valid_days} onChange={e => setRule({ ...rule, valid_days: Number(e.target.value) })} /></label>
            </div>
            <label className="block space-y-1 text-sm">邮件正文<textarea required rows={3} maxLength={4000} className={control} value={rule.content} onChange={e => setRule({ ...rule, content: e.target.value })} /></label>
            {rule.id === undefined && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={rule.include_existing ?? true} onChange={e => setRule({ ...rule, include_existing: e.target.checked })} />对现有已在等级范围的账号补发一次；取消后只覆盖后续达标账号</label>}
            <div className="space-y-2">
              <p className="text-sm font-medium">礼包附件</p>
              {rule.attachments.map((item, index) => <div key={index} className="flex items-center gap-3 rounded-lg bg-surface-secondary p-2">
                <span className="min-w-0 flex-1 text-sm">{item.name || items.find(entry => entry.id === item.id)?.name || item.id}</span>
                <input required aria-label={`${item.name || item.id}数量`} type="number" min={1} max={999999999} className={`${control} max-w-32`} value={item.number} onChange={e => setRule({ ...rule, attachments: rule.attachments.map((a, i) => i === index ? { ...a, number: Number(e.target.value) } : a) })} />
                <Button type="button" variant="secondary" onPress={() => setRule({ ...rule, attachments: rule.attachments.filter((_, i) => i !== index) })}>移除</Button>
              </div>)}
              <div className="grid gap-2 sm:grid-cols-4">
                <input className={control} aria-label="搜索奖励物品" placeholder="搜索奖励物品" value={search} onChange={e => { setSearch(e.target.value); setItemId('') }} />
                <select className={control} aria-label="选择奖励物品" value={itemId} onChange={e => setItemId(e.target.value)}><option value="">选择物品</option>{matches.map(item => <option key={item.id} value={item.id}>{item.name} ({item.id})</option>)}</select>
                <input className={control} aria-label="新附件数量" type="number" min={1} max={999999999} value={itemCount} onChange={e => setItemCount(e.target.value)} />
                <Button type="button" variant="secondary" isDisabled={!itemId || !Number.isInteger(Number(itemCount)) || Number(itemCount) < 1 || rule.attachments.length >= 100} onPress={() => {
                  const item = items.find(entry => entry.id === Number(itemId))
                  if (item) setRule({ ...rule, attachments: [...rule.attachments, { id: item.id, name: item.name, number: Number(itemCount) }] })
                }}>添加附件</Button>
              </div>
            </div>
            <Button type="submit" isDisabled={disabled || !rule.attachments.length}>保存等级礼包规则</Button>
          </fieldset>
          <p className="text-xs text-muted">等级上限也参与匹配。达到 11 级后均可领取的礼包，可设为 11 至 100 级。到期未领取的附件不再发放。</p>
        </form>
      </Card>
    </Section>
  </div>
}
