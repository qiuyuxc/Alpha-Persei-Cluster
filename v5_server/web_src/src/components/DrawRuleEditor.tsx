import { useEffect, useRef, useState } from 'react'
import { Button, Card } from '@heroui/react'
import { fetchDrawRules, saveDrawRules, resetDrawRules, type DrawRules } from '../lib/api'

const fields: Array<{ key: keyof DrawRules; label: string; percent?: boolean; max: number; min: number; step: string }> = [
  { key: 'ssr_rate', label: '基础出金率（%）', percent: true, min: 0, max: 100, step: '0.01' },
  { key: 'soft_pity_start', label: '软保底从第几抽开始', min: 1, max: 1000, step: '1' },
  { key: 'hard_pity', label: '硬保底抽数', min: 1, max: 1000, step: '1' },
  { key: 'sr_rate', label: '未出金时 A／四星概率（%）', percent: true, min: 0, max: 100, step: '0.01' },
  { key: 'sr_pity', label: 'A／四星保底抽数', min: 1, max: 1000, step: '1' },
  { key: 'up_rate', label: '出金命中 UP（%）', percent: true, min: 0, max: 100, step: '0.1' },
  { key: 'cost_multiplier', label: '服务端扣券倍率', min: 0, max: 100, step: '0.1' },
]

export function DrawRuleEditor({ group, enabled, onSaved }: { group: string; enabled: boolean; onSaved: () => void }) {
  const [values, setValues] = useState<Record<string, string> | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const fill = (rules: DrawRules) => setValues(Object.fromEntries(fields.map(f => [f.key, String(Number((rules[f.key] * (f.percent ? 100 : 1)).toFixed(6)))])))
  useEffect(() => {
    let current = true
    setValues(null)
    if (!enabled) return
    fetchDrawRules().then(res => {
      if (!current) return
      if (res.code !== 0 || !res.data?.rules[group]) throw new Error(res.msg || '规则读取失败')
      fill(res.data.rules[group]); setMessage(''); setFailed(false)
    }).catch(error => { if (current) { setFailed(true); setMessage(error.message) } })
    return () => { current = false }
  }, [group, enabled])
  const apply = async (reset: boolean) => {
    if (!values) return
    setBusy(true); setMessage('')
    try {
      const rules = reset ? null : Object.fromEntries(fields.map(f => {
        if (!values[f.key].trim()) throw new Error('请填写完整的规则')
        const value = Number(values[f.key]); if (!Number.isFinite(value)) throw new Error('请输入有效数值')
        return [f.key, value / (f.percent ? 100 : 1)]
      })) as unknown as DrawRules
      const res = reset ? await resetDrawRules(group) : await saveDrawRules(group, rules!)
      if (res.code !== 0 || !res.data) throw new Error(res.msg || '保存失败')
      if (!mounted.current) return
      fill(res.data.rules); setFailed(false); setMessage(res.msg); onSaved()
    } catch (error) {
      if (mounted.current) { setFailed(true); setMessage(error instanceof Error ? error.message : '请求失败') }
    } finally { if (mounted.current) setBusy(false) }
  }
  return <Card className="p-5 space-y-4">
    <div><h2 className="font-semibold">当前系列 · 自定义抽卡规则</h2><p className="mt-1 text-xs text-muted">同一系列对所有玩家生效，保存或恢复默认均保留已有保底进度。</p></div>
    <fieldset disabled={!enabled || !values || busy} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 disabled:opacity-60">
      {fields.map(field => <label key={field.key} className="space-y-1 text-xs">{field.label}<input type="number" min={field.min} max={field.max} step={field.step} value={values?.[field.key] ?? ''}
        disabled={field.key === 'up_rate' && group !== 'hero_precision_70'}
        onChange={e => setValues(old => old ? { ...old, [field.key]: e.target.value } : old)}
        className="w-full rounded-lg border border-separator bg-surface px-3 py-2 text-sm text-foreground disabled:opacity-50" /></label>)}
    </fieldset>
    <p className="text-xs text-muted">修改的是实际抽取和扣除规则；游戏内固定概率说明、原价提示及发起抽卡的余额判断可能仍按原版显示。软保底起点等于硬保底时，不提前提升概率。</p>
    <div className="flex flex-wrap gap-2"><Button isDisabled={!enabled || !values || busy} onPress={() => apply(false)}>{busy ? '处理中…' : '保存抽卡规则'}</Button><Button variant="secondary" isDisabled={!enabled || !values || busy} onPress={() => apply(true)}>恢复本系列默认规则</Button></div>
    {message && <p role="status" className={`text-sm ${failed ? 'text-danger' : 'text-success'}`}>{message}</p>}
  </Card>
}
