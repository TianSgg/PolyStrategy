import { useState, useEffect } from 'react'
import { apiFetch } from '../api'
import { useBalance } from '../contexts/BalanceContext'
import FollowSweeperTrades from './FollowSweeperTrades'
import { badgeStyle, exitBadge, iconStyle, lifecycleBadge } from '../tradePresentation'
import { formatEventDetailValue, formatUtcTime } from '../utils/time'
import { polymarketEventSlug } from '../utils/polymarket'

interface Account {
  id: number
  name: string
  proxy_wallet: string
}

interface ConfigParams {
  fixed_entry_shares: number
  entry_wait_ms: number
  stop_loss_ratio: number
  exit_wait_ms: number
  leader_wallet: string
  outcome_filter: string
  slug_script: string | null
}

interface FollowConfig {
  id: number
  account_id: number
  account_name?: string
  proxy_wallet?: string
  name: string
  enabled: number
  params: ConfigParams
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
  entry_wait_min: 20,
  stop_loss_ratio: 0.6,
  exit_wait_ms: 5000,
  leader_wallet: '',
  outcome_filter: 'no',
  slug_script: '',
}

export default function FollowSweeperDashboard({ darkMode }: Props) {
  const { accountBalances, refreshAccountBalance } = useBalance()
  const [tab, setTab] = useState<'configs' | 'trades'>('configs')
  const [configs, setConfigs] = useState<FollowConfig[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState(DEFAULT_FORM)
  const [saving, setSaving] = useState(false)
  const [togglingId, setTogglingId] = useState<number | null>(null)
  const [slugValidation, setSlugValidation] = useState<{ valid: boolean; message?: string } | null>(null)
  const [slugValidating, setSlugValidating] = useState(false)
  const [testSlug, setTestSlug] = useState('')
  const [testResult, setTestResult] = useState<{ result?: boolean; error?: boolean; message?: string } | null>(null)
  const [testRunning, setTestRunning] = useState(false)
  const [expandedConfigId, setExpandedConfigId] = useState<number | null>(null)
  const [configTrades, setConfigTrades] = useState<any[]>([])
  const [tradesLoading, setTradesLoading] = useState(false)
  const [expandedTradeId, setExpandedTradeId] = useState<string | null>(null)
  const [tradeSteps, setTradeSteps] = useState<any[]>([])
  const [stepsLoading, setStepsLoading] = useState(false)
  const [leaderNames, setLeaderNames] = useState<Record<string, string>>({})
  const [leaderValues, setLeaderValues] = useState<Record<string, { position: number; total: number }>>({})

  const fetchConfigs = async () => {
    try {
      const res = await apiFetch('/api/follow-weather/configs')
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

  useEffect(() => {
    const leaderWallets = configs
      .map(c => c.params?.leader_wallet)
      .filter((w): w is string => !!w)
    const uniqueLeaders = [...new Set(leaderWallets.map(w => w.toLowerCase()))]
    uniqueLeaders.forEach(async addr => {
      if (!leaderNames[addr]) {
        try {
          const res = await apiFetch(`/api/follow-weather/profile?address=${encodeURIComponent(addr)}`)
          if (res.ok) {
            const data = await res.json()
            if (data.name) setLeaderNames(prev => ({ ...prev, [addr]: data.name }))
          }
        } catch { /* ignore */ }
      }
      try {
        const res = await apiFetch(`/api/follow-weather/wallet-value?address=${encodeURIComponent(addr)}`)
        if (res.ok) {
          const data = await res.json()
          setLeaderValues(prev => ({ ...prev, [addr]: { position: data.position_value ?? 0, total: data.total_value ?? 0 } }))
        }
      } catch { /* ignore */ }
    })
    const followerWallets = configs
      .map(c => c.proxy_wallet)
      .filter((w): w is string => !!w)
    const uniqueFollowers = [...new Set(followerWallets.map(w => w.toLowerCase()))]
    uniqueFollowers.forEach(w => refreshAccountBalance(w))
  }, [configs])

  const handleExpandTrades = async (cfg: FollowConfig) => {
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
      // A follower can run multiple leader configurations; config_id keeps this panel isolated.
      params.set('config_id', String(cfg.id))
      const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString()
      params.set('since', since)
      params.set('limit', '50')
      const res = await apiFetch(`/api/follow-weather/trades?${params}`)
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
      const res = await apiFetch(`/api/follow-weather/events/${eventId}`)
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

  const handleToggle = async (cfg: FollowConfig) => {
    setTogglingId(cfg.id)
    try {
      await apiFetch(`/api/follow-weather/configs/${cfg.id}`, {
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
      if (form.slug_script.trim()) {
        const vRes = await apiFetch('/api/follow-weather/validate-slug-script', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug_script: form.slug_script }),
        })
        const vData = await vRes.json()
        if (!vData.valid) {
          setSlugValidation(vData)
          setSaving(false)
          return
        }
      }

      const params = {
        fixed_entry_shares: form.fixed_entry_shares,
        entry_wait_ms: Math.round(form.entry_wait_min * 60000),
        stop_loss_ratio: form.stop_loss_ratio,
        exit_wait_ms: form.exit_wait_ms,
        leader_wallet: form.leader_wallet.trim().toLowerCase(),
        outcome_filter: form.outcome_filter,
        slug_script: form.slug_script || null,
      }

      if (editingId) {
        await apiFetch(`/api/follow-weather/configs/${editingId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: form.name, params }),
        })
      } else {
        await apiFetch('/api/follow-weather/configs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ account_id: form.account_id, name: form.name, params }),
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

  const handleEdit = (cfg: FollowConfig) => {
    setEditingId(cfg.id)
    const p = cfg.params || {} as ConfigParams
    setForm({
      account_id: cfg.account_id,
      name: cfg.name,
      fixed_entry_shares: p.fixed_entry_shares ?? 100,
      entry_wait_min: (p.entry_wait_ms ?? 1200000) / 60000,
      stop_loss_ratio: p.stop_loss_ratio ?? 0.6,
      exit_wait_ms: p.exit_wait_ms ?? 5000,
      leader_wallet: p.leader_wallet || '',
      outcome_filter: p.outcome_filter || 'no',
      slug_script: p.slug_script || '',
    })
    setShowForm(true)
  }

  const handleDelete = async (id: number) => {
    if (!confirm('确定删除此配置?')) return
    await apiFetch(`/api/follow-weather/configs/${id}`, { method: 'DELETE' })
    await fetchConfigs()
  }

  const SLUG_SCRIPT_EXAMPLE = 'def should_include(slug):\n    keywords = ["weather", "temperature", "rain"]\n    for kw in keywords:\n        if kw in slug:\n            return True\n    return False'

  const handleValidateSlugScript = async () => {
    if (!form.slug_script.trim()) {
      setSlugValidation({ valid: true, message: '脚本为空，不进行过滤' })
      return
    }
    setSlugValidating(true)
    setSlugValidation(null)
    try {
      const res = await apiFetch('/api/follow-weather/validate-slug-script', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug_script: form.slug_script }),
      })
      const data = await res.json()
      setSlugValidation(data)
    } catch {
      setSlugValidation({ valid: false, message: '校验请求失败' })
    } finally {
      setSlugValidating(false)
    }
  }

  const handleTestSlugScript = async () => {
    if (!testSlug.trim()) return
    setTestRunning(true)
    setTestResult(null)
    try {
      const res = await apiFetch('/api/follow-weather/test-slug-script', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug_script: form.slug_script, test_slug: testSlug }),
      })
      setTestResult(await res.json())
    } catch {
      setTestResult({ error: true, message: '测试请求失败' })
    } finally {
      setTestRunning(false)
    }
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
          Follow Sweeper 策略配置
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
        <FollowSweeperTrades darkMode={darkMode} />
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
              {(() => {
                const lw = cfg.params?.leader_wallet?.toLowerCase() || ''
                const leaderName = leaderNames[lw] || (lw ? lw.slice(0, 8) + '...' : '未设置')
                const leaderVal = leaderValues[lw]
                const fw = cfg.proxy_wallet?.toLowerCase() || ''
                const followerBal = fw ? accountBalances[fw] : undefined
                const followerName = cfg.account_name || `#${cfg.account_id}`
                return (
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
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
                  {cfg.params?.slug_script && <span style={{ fontSize: '11px', padding: '2px 8px', borderRadius: '4px', background: darkMode ? '#292524' : '#fef3c7', color: '#f59e0b' }}>有市场过滤</span>}
                </div>
                <div style={{ fontSize: '13px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ color: textSecondary, width: '65px', flexShrink: 0 }}>Leader:</span>
                    <a
                      href={lw ? `https://polymarket.com/profile/${lw}` : '#'}
                      target="_blank" rel="noopener noreferrer"
                      onClick={e => e.stopPropagation()}
                      style={{ fontWeight: 600, color: textPrimary, textDecoration: 'none' }}
                    >{leaderName}</a>
                    {leaderVal && (
                      <span style={{ color: '#22c55e' }}>
                        持仓 ${leaderVal.position.toFixed(2)} / 总 ${leaderVal.total.toFixed(2)}
                      </span>
                    )}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ color: textSecondary, width: '65px', flexShrink: 0 }}>Follower:</span>
                    <a
                      href={fw ? `https://polymarket.com/profile/${fw}` : '#'}
                      target="_blank" rel="noopener noreferrer"
                      onClick={e => e.stopPropagation()}
                      style={{ fontWeight: 600, color: textPrimary, textDecoration: 'none' }}
                    >{followerName}</a>
                    {followerBal && (
                      <span style={{ color: '#22c55e' }}>
                        持仓 ${followerBal.total_position_value?.toFixed(2)} / 总 ${followerBal.total_value?.toFixed(2)}
                      </span>
                    )}
                  </div>
                  <div style={{ display: 'flex', gap: '14px', color: textSecondary, marginTop: '2px' }}>
                    <span>ID: <b>#{cfg.id}</b></span>
                    <span>买入超时: <b>{cfg.params?.entry_wait_ms != null ? (cfg.params.entry_wait_ms >= 60000 ? `${(cfg.params.entry_wait_ms / 60000).toFixed(0)}分钟` : `${cfg.params.entry_wait_ms / 1000}秒`) : '-'}</b></span>
                    <span>份额: <b>{cfg.params?.fixed_entry_shares ?? '-'}</b></span>
                    <span>止损: <b>{cfg.params?.stop_loss_ratio != null ? (cfg.params.stop_loss_ratio * 100).toFixed(0) + '%' : '-'}</b></span>
                    <span>Token: <b>{{ no: '仅No', yes: '仅Yes', all: '全部' }[cfg.params?.outcome_filter || 'no'] || cfg.params?.outcome_filter}</b></span>
                  </div>
                </div>
              </div>
                )
              })()}

              {/* Actions */}
              <div style={{ display: 'flex', gap: '8px', flexShrink: 0, alignItems: 'center' }}>
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
                    {configTrades.map((t: any) => {
                      const lifecycle = lifecycleBadge(t)
                      const exit = exitBadge(t)
                      const eventSlug = polymarketEventSlug(t.event_slug)
                      const formatTradePrice = (price: string | null | undefined) => (
                        price == null || price === '' ? '--' : Number(price).toFixed(4)
                      )
                      return (
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
                            <span style={badgeStyle(lifecycle.tone, darkMode)}>
                              <span style={iconStyle(lifecycle.tone, darkMode)}>{lifecycle.icon}</span>
                              {lifecycle.label}
                            </span>
                            {exit && (
                              <span style={badgeStyle(exit.tone, darkMode)} title={t.close_reason || undefined}>
                                <span style={iconStyle(exit.tone, darkMode)}>{exit.icon}</span>
                                {exit.label}
                                {exit.detail && <span style={{ opacity: 0.75 }}>{exit.detail}</span>}
                              </span>
                            )}
                            <span style={{ fontWeight: 500, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {eventSlug ? (
                                <a
                                  href={`https://polymarket.com/event/${encodeURIComponent(eventSlug)}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  onClick={e => e.stopPropagation()}
                                  style={{ color: '#3b82f6', textDecoration: 'none' }}
                                  onMouseEnter={e => (e.currentTarget.style.textDecoration = 'underline')}
                                  onMouseLeave={e => (e.currentTarget.style.textDecoration = 'none')}
                                >{eventSlug || t.market_slug}</a>
                              ) : (
                                <span style={{ color: textPrimary }}>{t.market_slug || t.event_id.slice(0, 8)}</span>
                              )}
                            </span>
                            {(t.entry_order_size || t.entry_shares) && (
                              <span style={{ fontSize: '11px', color: textSecondary }}>
                                买{t.entry_order_size ? (
                                  <span style={{ color: t.entry_shares && parseFloat(t.entry_shares) > 0 ? '#22c55e' : '#f59e0b', fontWeight: 500 }}>
                                    {' '}{t.entry_shares || '0'}/{t.entry_order_size} @ {formatTradePrice(t.entry_price)}
                                  </span>
                                ) : t.entry_shares ? ` ${t.entry_shares} @ ${formatTradePrice(t.entry_price)}` : ''}
                                {(t.exit_order_size || t.exit_shares) && (
                                  <>
                                    {' '}卖{t.exit_order_size ? (
                                      <span style={{ color: t.exit_shares && parseFloat(t.exit_shares) > 0 ? '#22c55e' : '#f59e0b', fontWeight: 500 }}>
                                        {' '}{t.exit_shares || '0'}/{t.exit_order_size} @ {formatTradePrice(t.exit_price)}
                                      </span>
                                    ) : t.exit_shares ? ` ${t.exit_shares} @ ${formatTradePrice(t.exit_price)}` : ''}
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
                              {formatUtcTime(t.started_at)}
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
                                  const phaseColor = ({ entry: '#3b82f6', exit: '#22c55e' } as any)[step.phase] || '#64748b'
                                  return (
                                    <div key={step.id || idx} style={{ position: 'relative', marginBottom: '10px' }}>
                                      <div style={{ position: 'absolute', left: '-18px', top: '5px', width: '8px', height: '8px', borderRadius: '50%', background: phaseColor, border: `2px solid ${darkMode ? '#1e293b' : '#ffffff'}` }} />
                                      <div style={{ display: 'flex', alignItems: 'baseline', gap: '6px', marginBottom: '2px' }}>
                                        <span style={{ fontSize: '11px', padding: '1px 5px', borderRadius: '3px', background: `${phaseColor}20`, color: phaseColor, fontWeight: 500 }}>
                                          {step.phase}
                                        </span>
                                        <span style={{ fontSize: '12px', fontWeight: 600, color: textPrimary }}>{step.step}</span>
                                        <span style={{ fontSize: '10px', color: textSecondary, marginLeft: 'auto' }}>
                                          {formatUtcTime(step.occurred_at)}
                                        </span>
                                      </div>
                                      {step.detail && Object.keys(step.detail).length > 0 && (
                                        <div style={{ fontSize: '11px', color: textSecondary, background: darkMode ? '#0f172a' : '#f1f5f9', padding: '4px 8px', borderRadius: '4px', fontFamily: 'monospace', lineHeight: '1.4' }}>
                                          {(() => {
                                            const detailEntries = Object.entries(step.detail || {}).filter(([key]) => (
                                              key !== 'event_id'
                                              && !(step.step === 'buy_order_placed' && key === 'bbo_observed_at')
                                            ))
                                            if (step.step === 'buy_order_placed') {
                                              const preferredOrder = ['order', 'clob_status', 'order_response_at', 'pre_bbo', 'aft_bbo', 'bbo_observation_pending']
                                              const detailMap = new Map(detailEntries)
                                              const preferredKeys = new Set(preferredOrder)
                                              const orderedEntries = preferredOrder
                                                .filter(key => detailMap.has(key))
                                                .map(key => [key, detailMap.get(key)] as [string, any])
                                              detailEntries.splice(0, detailEntries.length, ...orderedEntries, ...detailEntries.filter(([key]) => !preferredKeys.has(key)))
                                            }
                                            return ([
                                              ['event_id', step.event_id],
                                              ...detailEntries,
                                            ] as [string, any][]).map(([k, v]) => (
                                            <div key={k}><span style={{ color: darkMode ? '#93c5fd' : '#2563eb' }}>{k}</span>: {formatEventDetailValue(k, v, step.step)}</div>
                                            ))
                                          })()}
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
                    )})}
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
                  placeholder="例: 主账户-跟随"
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

              {/* Leader Wallet */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  Leader 钱包地址
                </label>
                <input
                  value={form.leader_wallet}
                  onChange={e => setForm({ ...form, leader_wallet: e.target.value })}
                  placeholder="0x1234..."
                  style={{ ...inputStyle, fontFamily: 'monospace', fontSize: '12px' }}
                />
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

              {/* Entry Wait */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  买入超时 (分钟)
                </label>
                <input
                  type="number"
                  step="1"
                  value={form.entry_wait_min}
                  onChange={e => setForm({ ...form, entry_wait_min: Number(e.target.value) })}
                  style={inputStyle}
                />
              </div>

              {/* Outcome Filter */}
              <div>
                <label style={{ fontSize: '13px', color: textSecondary, marginBottom: '4px', display: 'block' }}>
                  Token 方向
                </label>
                <select
                  value={form.outcome_filter}
                  onChange={e => setForm({ ...form, outcome_filter: e.target.value })}
                  style={inputStyle}
                >
                  <option value="no">仅 No</option>
                  <option value="yes">仅 Yes</option>
                  <option value="all">全部</option>
                </select>
              </div>

              {/* Slug Script */}
              <div>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
                  <label style={{ fontSize: '13px', color: textSecondary }}>
                    市场过滤脚本 (留空表示不过滤)
                  </label>
                  {!form.slug_script.trim() && (
                    <button
                      type="button"
                      onClick={() => { setForm({ ...form, slug_script: SLUG_SCRIPT_EXAMPLE }); setSlugValidation(null) }}
                      style={{
                        padding: '2px 8px', borderRadius: '4px', border: `1px solid ${border}`,
                        background: 'transparent', color: '#3b82f6', fontSize: '11px', cursor: 'pointer',
                      }}
                    >
                      填入示例
                    </button>
                  )}
                </div>
                <textarea
                  value={form.slug_script}
                  onChange={e => { setForm({ ...form, slug_script: e.target.value }); setSlugValidation(null) }}
                  onKeyDown={e => {
                    if (e.key === 'Tab') {
                      e.preventDefault()
                      const ta = e.currentTarget
                      const start = ta.selectionStart
                      const end = ta.selectionEnd
                      const val = form.slug_script
                      const updated = val.substring(0, start) + '    ' + val.substring(end)
                      setForm({ ...form, slug_script: updated })
                      requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = start + 4 })
                    }
                  }}
                  placeholder="支持 Python 子集: 变量、if/for/while、字符串方法、in 等。点击右上角「填入示例」快速开始"
                  rows={6}
                  style={{
                    ...inputStyle,
                    resize: 'vertical',
                    fontFamily: 'monospace',
                    fontSize: '12px',
                    lineHeight: '1.5',
                    tabSize: 4,
                  }}
                />
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: '4px' }}>
                  <div style={{ fontSize: '11px', color: textSecondary }}>
                    支持: contains / starts_with / ends_with / contains_any / matches
                  </div>
                  <button
                    type="button"
                    onClick={handleValidateSlugScript}
                    disabled={slugValidating}
                    style={{
                      padding: '3px 10px', borderRadius: '4px', border: `1px solid ${border}`,
                      background: 'transparent', color: textPrimary, fontSize: '12px', cursor: 'pointer',
                    }}
                  >
                    {slugValidating ? '校验中...' : '校验语法'}
                  </button>
                </div>
                {slugValidation && (
                  <div style={{
                    marginTop: '6px', padding: '6px 10px', borderRadius: '6px', fontSize: '12px',
                    background: slugValidation.valid ? (darkMode ? '#052e16' : '#dcfce7') : (darkMode ? '#450a0a' : '#fee2e2'),
                    color: slugValidation.valid ? '#16a34a' : '#ef4444',
                  }}>
                    {slugValidation.valid ? '语法正确' : `语法错误: ${slugValidation.message}`}
                  </div>
                )}
                {form.slug_script.trim() && (
                  <div style={{ marginTop: '8px', padding: '10px', borderRadius: '6px', background: darkMode ? '#1a2332' : '#f1f5f9' }}>
                    <div style={{ fontSize: '12px', color: textSecondary, marginBottom: '6px' }}>测试: 输入 market slug 查看过滤结果</div>
                    <div style={{ display: 'flex', gap: '6px' }}>
                      <input
                        value={testSlug}
                        onChange={e => { setTestSlug(e.target.value); setTestResult(null) }}
                        onKeyDown={e => { if (e.key === 'Enter') handleTestSlugScript() }}
                        placeholder="输入 market slug 测试..."
                        style={{ ...inputStyle, flex: 1, fontFamily: 'monospace', fontSize: '12px' }}
                      />
                      <button
                        type="button"
                        onClick={handleTestSlugScript}
                        disabled={testRunning || !testSlug.trim()}
                        style={{
                          padding: '6px 14px', borderRadius: '6px', border: `1px solid ${border}`,
                          background: 'transparent', color: textPrimary, fontSize: '12px',
                          cursor: testRunning || !testSlug.trim() ? 'not-allowed' : 'pointer',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {testRunning ? '...' : '测试'}
                      </button>
                    </div>
                    {testResult && (
                      <div style={{
                        marginTop: '6px', fontSize: '12px', fontWeight: 500,
                        color: testResult.error ? '#ef4444' : testResult.result ? '#16a34a' : '#f59e0b',
                      }}>
                        {testResult.error
                          ? testResult.message
                          : testResult.result
                            ? `通过 — "${testSlug}" 会被接受`
                            : `拒绝 — "${testSlug}" 会被过滤`}
                      </div>
                    )}
                  </div>
                )}
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
