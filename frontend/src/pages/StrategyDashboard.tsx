import { useState, useEffect } from 'react'
import { apiFetch } from '../api'

interface Account {
  id: number
  name: string
  proxy_wallet: string
}

interface SweepConfig {
  id: number
  account_id: number
  account_name?: string
  proxy_wallet?: string
  name: string
  enabled: number
  fixed_entry_shares: number
  entry_wait_ms: number
  sweep_outcome_filter: string
  stop_loss_ratio: number
  exit_wait_ms: number
  tick_verify_retries: number
  tick_verify_backoff_ms: number
  params_version: number
  created_at: string
  updated_at: string
}

interface Props {
  darkMode: boolean
}

const DEFAULT_FORM = {
  account_id: 0,
  name: '',
  fixed_entry_shares: 100,
  entry_wait_ms: 30000,
  sweep_outcome_filter: 'no',
  stop_loss_ratio: 0.6,
  exit_wait_ms: 5000,
  tick_verify_retries: 3,
  tick_verify_backoff_ms: 1000,
}

export default function StrategyDashboard({ darkMode }: Props) {
  const [configs, setConfigs] = useState<SweepConfig[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState(DEFAULT_FORM)
  const [saving, setSaving] = useState(false)
  const [togglingId, setTogglingId] = useState<number | null>(null)

  const fetchConfigs = async () => {
    try {
      const res = await apiFetch('/api/strategy/configs')
      if (res.ok) {
        const data = await res.json()
        setConfigs(data.configs || [])
      }
    } catch (e) {
      console.error('Failed to fetch configs', e)
    } finally {
      setLoading(false)
    }
  }

  const fetchAccounts = async () => {
    try {
      const res = await apiFetch('/api/accounts')
      if (res.ok) {
        const data = await res.json()
        setAccounts(data.accounts || [])
      }
    } catch (e) {
      console.error('Failed to fetch accounts', e)
    }
  }

  useEffect(() => {
    fetchConfigs()
    fetchAccounts()
  }, [])

  const handleToggle = async (cfg: SweepConfig) => {
    setTogglingId(cfg.id)
    try {
      await apiFetch(`/api/strategy/configs/${cfg.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !cfg.enabled }),
      })
      await fetchConfigs()
    } catch (e) {
      console.error('Toggle failed', e)
    } finally {
      setTogglingId(null)
    }
  }

  const handleSave = async () => {
    if (!form.name.trim() || !form.account_id) return
    setSaving(true)
    try {
      if (editingId) {
        await apiFetch(`/api/strategy/configs/${editingId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(form),
        })
      } else {
        await apiFetch('/api/strategy/configs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(form),
        })
      }
      setShowForm(false)
      setEditingId(null)
      setForm(DEFAULT_FORM)
      await fetchConfigs()
    } catch (e) {
      console.error('Save failed', e)
    } finally {
      setSaving(false)
    }
  }

  const handleEdit = (cfg: SweepConfig) => {
    setEditingId(cfg.id)
    setForm({
      account_id: cfg.account_id,
      name: cfg.name,
      fixed_entry_shares: cfg.fixed_entry_shares,
      entry_wait_ms: cfg.entry_wait_ms,
      sweep_outcome_filter: cfg.sweep_outcome_filter,
      stop_loss_ratio: cfg.stop_loss_ratio,
      exit_wait_ms: cfg.exit_wait_ms,
      tick_verify_retries: cfg.tick_verify_retries,
      tick_verify_backoff_ms: cfg.tick_verify_backoff_ms,
    })
    setShowForm(true)
  }

  const handleDelete = async (id: number) => {
    if (!confirm('确定删除此配置?')) return
    await apiFetch(`/api/strategy/configs/${id}`, { method: 'DELETE' })
    await fetchConfigs()
  }

  const bg = darkMode ? '#0f172a' : '#f8fafc'
  const cardBg = darkMode ? '#1e293b' : '#ffffff'
  const border = darkMode ? '#334155' : '#e2e8f0'
  const textPrimary = darkMode ? '#f8fafc' : '#0f172a'
  const textSecondary = darkMode ? '#94a3b8' : '#64748b'

  const inputStyle: React.CSSProperties = {
    width: '100%',
    padding: '8px 12px',
    borderRadius: '6px',
    border: `1px solid ${border}`,
    background: darkMode ? '#334155' : '#f1f5f9',
    color: textPrimary,
    fontSize: '14px',
    boxSizing: 'border-box',
  }

  return (
    <div style={{ flex: 1, overflow: 'auto', padding: '24px', background: bg }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
        <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: textPrimary }}>
          Weather Sweep 策略配置
        </h2>
        <button
          onClick={() => { setShowForm(true); setEditingId(null); setForm(DEFAULT_FORM) }}
          style={{
            padding: '8px 20px',
            borderRadius: '8px',
            border: 'none',
            background: '#2563eb',
            color: '#fff',
            fontSize: '14px',
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          + 新增配置
        </button>
      </div>

      {/* Config List */}
      {loading ? (
        <div style={{ color: textSecondary }}>加载中...</div>
      ) : configs.length === 0 ? (
        <div style={{ color: textSecondary, padding: '40px', textAlign: 'center' }}>
          暂无配置，点击右上角"新增配置"开始使用策略
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {configs.map(cfg => (
            <div key={cfg.id} style={{
              background: cardBg,
              borderRadius: '12px',
              padding: '20px',
              border: `1px solid ${border}`,
              display: 'flex',
              alignItems: 'center',
              gap: '16px',
            }}>
              {/* Enable toggle */}
              <button
                onClick={() => handleToggle(cfg)}
                disabled={togglingId === cfg.id}
                style={{
                  width: '48px',
                  height: '26px',
                  borderRadius: '13px',
                  border: 'none',
                  background: cfg.enabled ? '#22c55e' : (darkMode ? '#475569' : '#cbd5e1'),
                  cursor: 'pointer',
                  position: 'relative',
                  flexShrink: 0,
                  transition: 'background 0.2s',
                }}
              >
                <div style={{
                  width: '20px',
                  height: '20px',
                  borderRadius: '50%',
                  background: '#fff',
                  position: 'absolute',
                  top: '3px',
                  left: cfg.enabled ? '25px' : '3px',
                  transition: 'left 0.2s',
                }} />
              </button>

              {/* Info */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
                  <span style={{ fontSize: '15px', fontWeight: 600, color: textPrimary }}>{cfg.name}</span>
                  <span style={{
                    fontSize: '11px',
                    padding: '2px 8px',
                    borderRadius: '4px',
                    background: cfg.enabled ? '#dcfce7' : (darkMode ? '#1e293b' : '#f1f5f9'),
                    color: cfg.enabled ? '#16a34a' : textSecondary,
                  }}>
                    {cfg.enabled ? '运行中' : '已停止'}
                  </span>
                </div>
                <div style={{ fontSize: '13px', color: textSecondary, display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
                  <span>账户: {cfg.account_name || `#${cfg.account_id}`}</span>
                  <span>份额: {cfg.fixed_entry_shares}</span>
                  <span>止损: {(cfg.stop_loss_ratio * 100).toFixed(0)}%</span>
                  <span>过滤: {cfg.sweep_outcome_filter}</span>
                  <span>v{cfg.params_version}</span>
                </div>
              </div>

              {/* Actions */}
              <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                <button
                  onClick={() => handleEdit(cfg)}
                  style={{
                    padding: '6px 14px',
                    borderRadius: '6px',
                    border: `1px solid ${border}`,
                    background: 'transparent',
                    color: textPrimary,
                    fontSize: '13px',
                    cursor: 'pointer',
                  }}
                >
                  编辑
                </button>
                <button
                  onClick={() => handleDelete(cfg.id)}
                  style={{
                    padding: '6px 14px',
                    borderRadius: '6px',
                    border: '1px solid #fecaca',
                    background: 'transparent',
                    color: '#ef4444',
                    fontSize: '13px',
                    cursor: 'pointer',
                  }}
                >
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create/Edit Modal */}
      {showForm && (
        <div style={{
          position: 'fixed',
          inset: 0,
          background: 'rgba(0,0,0,0.5)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000,
        }}>
          <div style={{
            background: cardBg,
            borderRadius: '16px',
            padding: '32px',
            width: '480px',
            maxHeight: '80vh',
            overflow: 'auto',
            border: `1px solid ${border}`,
          }}>
            <h3 style={{ margin: '0 0 24px', fontSize: '18px', fontWeight: 600, color: textPrimary }}>
              {editingId ? '编辑配置' : '新增配置'}
            </h3>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              {/* Name */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>配置名称</label>
                <input
                  value={form.name}
                  onChange={e => setForm({ ...form, name: e.target.value })}
                  placeholder="例: 主账户-保守"
                  style={inputStyle}
                />
              </div>

              {/* Account */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>账户</label>
                <select
                  value={form.account_id}
                  onChange={e => setForm({ ...form, account_id: Number(e.target.value) })}
                  style={inputStyle}
                  disabled={!!editingId}
                >
                  <option value={0}>-- 选择账户 --</option>
                  {accounts.map(a => (
                    <option key={a.id} value={a.id}>{a.name} ({a.proxy_wallet.slice(0, 8)}...)</option>
                  ))}
                </select>
              </div>

              {/* Fixed Entry Shares */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  买入份额 (shares)
                </label>
                <input
                  type="number"
                  value={form.fixed_entry_shares}
                  onChange={e => setForm({ ...form, fixed_entry_shares: Number(e.target.value) })}
                  style={inputStyle}
                />
              </div>

              {/* Stop Loss Ratio */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  止损比率 (0-1)
                </label>
                <input
                  type="number"
                  step="0.01"
                  value={form.stop_loss_ratio}
                  onChange={e => setForm({ ...form, stop_loss_ratio: Number(e.target.value) })}
                  style={inputStyle}
                />
              </div>

              {/* Sweep Outcome Filter */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  信号方向过滤
                </label>
                <select
                  value={form.sweep_outcome_filter}
                  onChange={e => setForm({ ...form, sweep_outcome_filter: e.target.value })}
                  style={inputStyle}
                >
                  <option value="no">仅 No (推荐)</option>
                  <option value="yes">仅 Yes</option>
                  <option value="all">全部</option>
                </select>
              </div>

              {/* Entry Wait */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  等待成交超时 (ms)
                </label>
                <input
                  type="number"
                  value={form.entry_wait_ms}
                  onChange={e => setForm({ ...form, entry_wait_ms: Number(e.target.value) })}
                  style={inputStyle}
                />
              </div>

              {/* Exit Wait */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  退出等待 (ms)
                </label>
                <input
                  type="number"
                  value={form.exit_wait_ms}
                  onChange={e => setForm({ ...form, exit_wait_ms: Number(e.target.value) })}
                  style={inputStyle}
                />
              </div>

              {/* Tick Verify Retries */}
              <div style={{ display: 'flex', gap: '12px' }}>
                <div style={{ flex: 1 }}>
                  <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                    Tick 验证重试
                  </label>
                  <input
                    type="number"
                    value={form.tick_verify_retries}
                    onChange={e => setForm({ ...form, tick_verify_retries: Number(e.target.value) })}
                    style={inputStyle}
                  />
                </div>
                <div style={{ flex: 1 }}>
                  <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                    重试间隔 (ms)
                  </label>
                  <input
                    type="number"
                    value={form.tick_verify_backoff_ms}
                    onChange={e => setForm({ ...form, tick_verify_backoff_ms: Number(e.target.value) })}
                    style={inputStyle}
                  />
                </div>
              </div>
            </div>

            {/* Buttons */}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px', marginTop: '24px' }}>
              <button
                onClick={() => { setShowForm(false); setEditingId(null) }}
                style={{
                  padding: '8px 20px',
                  borderRadius: '8px',
                  border: `1px solid ${border}`,
                  background: 'transparent',
                  color: textPrimary,
                  fontSize: '14px',
                  cursor: 'pointer',
                }}
              >
                取消
              </button>
              <button
                onClick={handleSave}
                disabled={saving || !form.name.trim() || !form.account_id}
                style={{
                  padding: '8px 20px',
                  borderRadius: '8px',
                  border: 'none',
                  background: '#2563eb',
                  color: '#fff',
                  fontSize: '14px',
                  fontWeight: 500,
                  cursor: saving ? 'not-allowed' : 'pointer',
                  opacity: saving || !form.name.trim() || !form.account_id ? 0.6 : 1,
                }}
              >
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
