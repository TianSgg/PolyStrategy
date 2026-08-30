import { useState, useEffect } from 'react'
import { apiFetch } from '../api'
import { useBalance } from '../contexts/BalanceContext'
import SweepTrades from './SweepTrades'

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
  signal_source_filter: string
  signal_threshold_filter: string
  direction_filter: string
  stop_loss_ratio: number
  exit_wait_ms: number
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
  entry_wait_ms: 1200000,
  sweep_outcome_filter: 'no',
  signal_source_filter: 'all',
  signal_threshold_filter: 'all',
  direction_filter: 'all',
  stop_loss_ratio: 0.6,
  exit_wait_ms: 5000,
}

const CLOSE_REASON_LABELS: Record<string, string> = {
  normal_exit: '正常退出 normal_exit',
  tick_exit: '旧版Tick退出 tick_exit',
  stop_loss: '止损 stop_loss',
  force_exit: '强制退出 force_exit',
  buy_failed: '买入失败 buy_failed',
  timeout_no_fill: '入场超时 timeout_no_fill',
  sell_failed: '卖出失败 sell_failed',
}

export default function StrategyDashboard({ darkMode }: Props) {
  const { accountBalances } = useBalance()
  const [tab, setTab] = useState<'configs' | 'trades'>('configs')
  const [configs, setConfigs] = useState<SweepConfig[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState(DEFAULT_FORM)
  const [saving, setSaving] = useState(false)
  const [togglingId, setTogglingId] = useState<number | null>(null)
  const [expandedConfigId, setExpandedConfigId] = useState<number | null>(null)
  const [configTrades, setConfigTrades] = useState<any[]>([])
  const [tradesLoading, setTradesLoading] = useState(false)
  const [expandedTradeId, setExpandedTradeId] = useState<string | null>(null)
  const [tradeSteps, setTradeSteps] = useState<any[]>([])
  const [stepsLoading, setStepsLoading] = useState(false)

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
      const res = await apiFetch('/api/account/list')
      if (res.ok) {
        const data = await res.json()
        setAccounts(Array.isArray(data) ? data : data.accounts || [])
      }
    } catch (e) {
      console.error('Failed to fetch accounts', e)
    }
  }

  useEffect(() => {
    fetchConfigs()
    fetchAccounts()
  }, [])

  const handleExpandTrades = async (cfg: SweepConfig) => {
    if (expandedConfigId === cfg.id) {
      setExpandedConfigId(null)
      setConfigTrades([])
      setExpandedTradeId(null)
      setTradeSteps([])
      return
    }
    setExpandedConfigId(cfg.id)
    setExpandedTradeId(null)
    setTradeSteps([])
    setTradesLoading(true)
    try {
      const params = new URLSearchParams()
      if (cfg.proxy_wallet) params.set('proxy_wallet', cfg.proxy_wallet)
      const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString()
      params.set('since', since)
      params.set('limit', '50')
      const res = await apiFetch(`/api/strategy/trades?${params}`)
      if (res.ok) {
        const data = await res.json()
        setConfigTrades(data.trades || [])
      }
    } catch (e) {
      console.error('Failed to fetch trades', e)
    } finally {
      setTradesLoading(false)
    }
  }

  const handleExpandTrade = async (eventId: string) => {
    if (expandedTradeId === eventId) {
      setExpandedTradeId(null)
      setTradeSteps([])
      return
    }
    setExpandedTradeId(eventId)
    setStepsLoading(true)
    try {
      const res = await apiFetch(`/api/strategy/events/${eventId}`)
      if (res.ok) {
        const data = await res.json()
        setTradeSteps(data.steps || [])
      }
    } catch (e) {
      console.error('Failed to fetch steps', e)
    } finally {
      setStepsLoading(false)
    }
  }

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
      signal_source_filter: cfg.signal_source_filter || 'all',
      signal_threshold_filter: cfg.signal_threshold_filter || 'all',
      direction_filter: cfg.direction_filter || 'all',
      stop_loss_ratio: cfg.stop_loss_ratio,
      exit_wait_ms: cfg.exit_wait_ms,
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
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div style={{ display: 'flex', borderRadius: '8px', border: `1px solid ${border}`, overflow: 'hidden' }}>
            <button
              onClick={() => setTab('configs')}
              style={{
                padding: '6px 16px', border: 'none', fontSize: '13px', cursor: 'pointer',
                background: tab === 'configs' ? '#2563eb' : 'transparent',
                color: tab === 'configs' ? '#fff' : textSecondary,
                fontWeight: tab === 'configs' ? 600 : 400,
              }}
            >
              策略配置
            </button>
            <button
              onClick={() => setTab('trades')}
              style={{
                padding: '6px 16px', border: 'none', fontSize: '13px', cursor: 'pointer',
                background: tab === 'trades' ? '#2563eb' : 'transparent',
                color: tab === 'trades' ? '#fff' : textSecondary,
                fontWeight: tab === 'trades' ? 600 : 400,
              }}
            >
              交易记录
            </button>
          </div>
          {tab === 'configs' && (
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
          )}
        </div>
      </div>

      {/* Trades Tab */}
      {tab === 'trades' && (
        <SweepTrades darkMode={darkMode} />
      )}

      {/* Config List */}
      {tab === 'configs' && (loading ? (
        <div style={{ color: textSecondary }}>加载中...</div>
      ) : configs.length === 0 ? (
        <div style={{ color: textSecondary, padding: '40px', textAlign: 'center' }}>
          暂无配置，点击右上角"新增配置"开始使用策略
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {configs.map(cfg => (
            <div key={cfg.id}>
            <div
              onClick={() => handleExpandTrades(cfg)}
              style={{
              background: cardBg,
              borderRadius: expandedConfigId === cfg.id ? '12px 12px 0 0' : '12px',
              padding: '20px',
              border: `1px solid ${expandedConfigId === cfg.id ? '#3b82f6' : border}`,
              borderBottom: expandedConfigId === cfg.id ? 'none' : undefined,
              display: 'flex',
              alignItems: 'center',
              gap: '16px',
              cursor: 'pointer',
            }}>
              {/* Enable toggle */}
              <button
                onClick={e => { e.stopPropagation(); handleToggle(cfg) }}
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
                <div style={{ fontSize: '13px', color: textSecondary, display: 'flex', gap: '14px', flexWrap: 'wrap', alignItems: 'center' }}>
                  <a
                    href={cfg.proxy_wallet ? `https://polymarket.com/profile/${cfg.proxy_wallet}` : '#'}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={e => e.stopPropagation()}
                    style={{ color: '#3b82f6', fontWeight: 500, textDecoration: 'none' }}
                  >
                    {cfg.account_name || `#${cfg.account_id}`}
                    {cfg.proxy_wallet && accountBalances[cfg.proxy_wallet.toLowerCase()]
                      ? ` ($${accountBalances[cfg.proxy_wallet.toLowerCase()].total_value?.toFixed(2)})`
                      : ''}
                  </a>
                  <span>份额: {cfg.fixed_entry_shares}</span>
                  <span>止损: {(cfg.stop_loss_ratio * 100).toFixed(0)}%</span>
                  <span>买入超时: {cfg.entry_wait_ms >= 60000 ? `${(cfg.entry_wait_ms / 60000).toFixed(0)}分钟` : `${cfg.entry_wait_ms / 1000}秒`}</span>
                  <span>方向: {cfg.direction_filter === 'all' ? '全部' : cfg.direction_filter === 'highest' ? '最高温' : '最低温'}</span>
                  {cfg.sweep_outcome_filter !== 'no' && <span>outcome: {cfg.sweep_outcome_filter}</span>}
                </div>
              </div>

              {/* Actions */}
              <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                <button
                  onClick={e => { e.stopPropagation(); handleExpandTrades(cfg) }}
                  style={{
                    padding: '6px 14px',
                    borderRadius: '6px',
                    border: `1px solid ${expandedConfigId === cfg.id ? '#3b82f6' : border}`,
                    background: expandedConfigId === cfg.id ? '#2563eb' : 'transparent',
                    color: expandedConfigId === cfg.id ? '#fff' : textPrimary,
                    fontSize: '13px',
                    cursor: 'pointer',
                  }}
                >
                  24h 交易
                </button>
                <button
                  onClick={e => { e.stopPropagation(); handleEdit(cfg) }}
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
                  onClick={e => { e.stopPropagation(); handleDelete(cfg.id) }}
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

            {/* Expanded trades panel */}
            {expandedConfigId === cfg.id && (
              <div style={{
                background: darkMode ? '#0f172a' : '#f8fafc',
                border: `1px solid #3b82f6`,
                borderTop: 'none',
                borderRadius: '0 0 12px 12px',
                padding: '16px',
                maxHeight: '500px',
                overflow: 'auto',
              }}>
                {tradesLoading ? (
                  <div style={{ color: textSecondary, textAlign: 'center', padding: '12px' }}>加载中...</div>
                ) : configTrades.length === 0 ? (
                  <div style={{ color: textSecondary, textAlign: 'center', padding: '12px' }}>暂无交易记录</div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {configTrades.map((t: any) => (
                      <div key={t.event_id}>
                        <div
                          onClick={() => handleExpandTrade(t.event_id)}
                          style={{
                            padding: '10px 14px',
                            borderRadius: expandedTradeId === t.event_id ? '8px 8px 0 0' : '8px',
                            background: expandedTradeId === t.event_id ? (darkMode ? '#334155' : '#eff6ff') : cardBg,
                            border: `1px solid ${expandedTradeId === t.event_id ? '#3b82f6' : border}`,
                            cursor: 'pointer',
                            transition: 'all 0.15s',
                          }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '13px' }}>
                            <span style={{
                              fontSize: '11px', padding: '2px 6px', borderRadius: '4px', fontWeight: 500,
                              background: t.status === 'exit_failed' ? '#ef444420' : t.status === 'closed' ? '#64748b20' : t.status === 'exit_working' ? '#f59e0b20' : '#3b82f620',
                              color: t.status === 'exit_failed' ? '#ef4444' : t.status === 'closed' ? '#64748b' : t.status === 'exit_working' ? '#f59e0b' : '#3b82f6',
                            }}>
                              {t.status === 'entry_working' ? '入场中' : t.status === 'exit_working' ? '出场中' : t.status === 'exit_failed' ? '退出失败' : '已平仓'}
                            </span>
                            {t.close_reason && <span style={{ fontSize: '11px', color: textSecondary }}>({CLOSE_REASON_LABELS[t.close_reason] || t.close_reason})</span>}
                            <span style={{ fontWeight: 500, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {t.event_slug ? (
                                <a
                                  href={`https://polymarket.com/event/${encodeURIComponent(t.event_slug)}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  onClick={e => e.stopPropagation()}
                                  style={{ color: '#3b82f6', textDecoration: 'none' }}
                                  onMouseEnter={e => (e.currentTarget.style.textDecoration = 'underline')}
                                  onMouseLeave={e => (e.currentTarget.style.textDecoration = 'none')}
                                >{t.event_slug}</a>
                              ) : (
                                <span style={{ color: textPrimary }}>{t.market_slug || t.event_id.slice(0, 8)}</span>
                              )}
                            </span>
                            {t.outcome && (
                              <span style={{ fontSize: '11px', padding: '1px 5px', borderRadius: '3px', fontWeight: 500,
                                background: t.outcome === 'yes' ? '#22c55e20' : '#ef444420',
                                color: t.outcome === 'yes' ? '#22c55e' : '#ef4444',
                              }}>{t.outcome.toUpperCase()}</span>
                            )}
                            {t.is_from_main !== null && t.is_from_main !== undefined && (
                              <span style={{ fontSize: '10px', padding: '1px 4px', borderRadius: '3px',
                                background: t.is_from_main ? '#3b82f620' : '#8b5cf620',
                                color: t.is_from_main ? '#3b82f6' : '#8b5cf6',
                              }}>{t.is_from_main ? 'Main' : 'Next'}</span>
                            )}
                            {t.temperature_label && <span style={{ fontSize: '11px', color: textSecondary }}>{t.temperature_label}</span>}
                            {t.city && <span style={{ color: textSecondary }}>{t.city} {t.direction === 'highest' ? '↑' : t.direction === 'lowest' ? '↓' : ''}</span>}
                            {(t.entry_order_size || t.entry_shares) && (
                              <span style={{ fontSize: '11px', color: textSecondary }}>
                                买{t.entry_order_size ? (
                                  <span style={{ color: t.entry_shares && parseFloat(t.entry_shares) >= parseFloat(t.entry_order_size) ? '#22c55e' : '#f59e0b', fontWeight: 500 }}>
                                    {' '}{t.entry_shares || '0'}/{t.entry_order_size}
                                  </span>
                                ) : ` ${t.entry_shares}`}
                                {(t.exit_order_size || t.exit_shares) && (
                                  <>
                                    {' '}卖{t.exit_order_size ? (
                                      <span style={{ color: t.exit_shares && parseFloat(t.exit_shares) >= parseFloat(t.exit_order_size) ? '#22c55e' : '#f59e0b', fontWeight: 500 }}>
                                        {' '}{t.exit_shares || '0'}/{t.exit_order_size}
                                      </span>
                                    ) : ` ${t.exit_shares}`}
                                  </>
                                )}
                              </span>
                            )}
                            {t.pnl != null && (
                              <span style={{ fontWeight: 600, color: parseFloat(t.pnl) > 0 ? '#22c55e' : parseFloat(t.pnl) < 0 ? '#ef4444' : textSecondary }}>
                                {parseFloat(t.pnl) > 0 ? '+' : ''}{parseFloat(t.pnl).toFixed(4)}
                              </span>
                            )}
                            <span style={{ color: textSecondary, fontSize: '11px' }}>
                              {t.started_at ? new Date(t.started_at.endsWith('Z') ? t.started_at : t.started_at + 'Z').toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                            </span>
                          </div>
                        </div>

                        {/* Trade steps expand */}
                        {expandedTradeId === t.event_id && (
                          <div style={{
                            background: darkMode ? '#1e293b' : '#ffffff',
                            border: `1px solid #3b82f6`,
                            borderTop: 'none',
                            borderRadius: '0 0 8px 8px',
                            padding: '12px 16px',
                          }}>
                            {stepsLoading ? (
                              <div style={{ color: textSecondary, textAlign: 'center', padding: '8px' }}>加载详情...</div>
                            ) : tradeSteps.length === 0 ? (
                              <div style={{ color: textSecondary, textAlign: 'center', padding: '8px' }}>无步骤记录</div>
                            ) : (
                              <div style={{ position: 'relative', paddingLeft: '20px' }}>
                                <div style={{ position: 'absolute', left: '6px', top: '4px', bottom: '4px', width: '2px', background: darkMode ? '#475569' : '#cbd5e1' }} />
                                {tradeSteps.map((step: any, idx: number) => {
                                  const phaseColor = ({ entry: '#3b82f6', monitor: '#8b5cf6', exit: '#22c55e', exit_risk: '#ef4444', exit_force: '#f59e0b' } as any)[step.phase] || '#64748b'
                                  return (
                                    <div key={step.id || idx} style={{ position: 'relative', marginBottom: '10px' }}>
                                      <div style={{ position: 'absolute', left: '-18px', top: '5px', width: '8px', height: '8px', borderRadius: '50%', background: phaseColor, border: `2px solid ${darkMode ? '#1e293b' : '#ffffff'}` }} />
                                      <div style={{ display: 'flex', alignItems: 'baseline', gap: '6px', marginBottom: '2px' }}>
                                        <span style={{ fontSize: '11px', padding: '1px 5px', borderRadius: '3px', background: `${phaseColor}20`, color: phaseColor, fontWeight: 500 }}>
                                          {step.phase}
                                        </span>
                                        <span style={{ fontSize: '12px', fontWeight: 600, color: textPrimary }}>{step.step}</span>
                                        <span style={{ fontSize: '10px', color: textSecondary, marginLeft: 'auto' }}>
                                          {step.occurred_at ? new Date(step.occurred_at.endsWith('Z') ? step.occurred_at : step.occurred_at + 'Z').toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''}
                                        </span>
                                      </div>
                                      {step.detail && Object.keys(step.detail).length > 0 && (
                                        <div style={{ fontSize: '11px', color: textSecondary, background: darkMode ? '#0f172a' : '#f1f5f9', padding: '4px 8px', borderRadius: '4px', fontFamily: 'monospace', lineHeight: '1.4' }}>
                                          {Object.entries(step.detail).map(([k, v]) => (
                                            <div key={k}><span style={{ color: darkMode ? '#93c5fd' : '#2563eb' }}>{k}</span>: {typeof v === 'object' ? JSON.stringify(v) : String(v)}</div>
                                          ))}
                                        </div>
                                      )}
                                    </div>
                                  )
                                })}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            </div>
          ))}
        </div>
      ))}

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
                  Token 方向
                </label>
                <select
                  value={form.sweep_outcome_filter}
                  onChange={e => setForm({ ...form, sweep_outcome_filter: e.target.value })}
                  style={inputStyle}
                >
                  <option value="no">仅 No</option>
                  <option value="yes">仅 Yes</option>
                  <option value="all">全部</option>
                </select>
              </div>

              {/* Signal Source Filter */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  信号来源
                </label>
                <select
                  value={form.signal_source_filter}
                  onChange={e => setForm({ ...form, signal_source_filter: e.target.value })}
                  style={inputStyle}
                >
                  <option value="all">全部</option>
                  <option value="main">仅主监控器</option>
                  <option value="next">仅 Next 候选</option>
                </select>
              </div>

              {/* Signal Threshold Filter */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  触发阈值
                </label>
                <select
                  value={form.signal_threshold_filter}
                  onChange={e => setForm({ ...form, signal_threshold_filter: e.target.value })}
                  style={inputStyle}
                >
                  <option value="all">全部 (0.99 + 0.98)</option>
                  <option value="0.99">仅 0.99</option>
                  <option value="0.98">仅 0.98</option>
                </select>
              </div>

              {/* Direction Filter */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  温度方向
                </label>
                <select
                  value={form.direction_filter}
                  onChange={e => setForm({ ...form, direction_filter: e.target.value })}
                  style={inputStyle}
                >
                  <option value="all">全部 (最高温+最低温)</option>
                  <option value="highest">仅最高温</option>
                  <option value="lowest">仅最低温</option>
                </select>
              </div>

              {/* Entry Wait */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  买入超时 (ms)
                </label>
                <input
                  type="number"
                  value={form.entry_wait_ms}
                  onChange={e => setForm({ ...form, entry_wait_ms: Number(e.target.value) })}
                  style={inputStyle}
                />
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
