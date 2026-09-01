import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../api'
import { badgeStyle, exitBadge, iconStyle, lifecycleBadge } from '../tradePresentation'

interface Trade {
  id: number
  event_id: string
  config_id: number
  owner_user_id: number
  proxy_wallet: string
  signal_id: string | null
  token_id: string | null
  market_slug: string | null
  event_slug: string | null
  city: string | null
  direction: string | null
  status: 'entry_working' | 'exit_working' | 'closed' | 'exit_failed'
  lifecycle_status?: 'entry_working' | 'exit_working' | 'closed' | null
  trade_outcome?: 'completed' | 'failed' | null
  close_reason: string | null
  failure_reason?: string | null
  needs_attention?: boolean | number | null
  entry_price: string | null
  entry_shares: string | null
  entry_cost: string | null
  entry_order_size: string | null
  exit_price: string | null
  exit_shares: string | null
  exit_revenue: string | null
  exit_order_size: string | null
  pnl: string | null
  pnl_pct: string | null
  duration_ms: number | null
  started_at: string
  closed_at: string | null
}

interface EventStep {
  id: number
  event_id: string
  phase: string
  step: string
  sequence_no: number
  detail: Record<string, any>
  occurred_at: string
}

interface Props {
  darkMode: boolean
  proxyWallet?: string
  onBack?: () => void
}

const PHASE_COLORS: Record<string, string> = {
  entry: '#3b82f6',
  monitor: '#8b5cf6',
  exit: '#22c55e',
  exit_risk: '#ef4444',
  exit_force: '#f59e0b',
  strategy: '#f97316',
}

const PHASE_LABELS: Record<string, string> = {
  entry: '入场',
  monitor: '监控',
  exit: '退出',
  exit_risk: '风控退出',
  exit_force: '强制退出',
  strategy: '策略',
}

const PAGE_SIZE = 30

export default function SweepTrades({ darkMode, proxyWallet, onBack }: Props) {
  const [trades, setTrades] = useState<Trade[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [walletFilter, setWalletFilter] = useState<string>('')
  const [directionFilter, setDirectionFilter] = useState<string>('')
  const [timeRange, setTimeRange] = useState<string>('')
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [page, setPage] = useState(0)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [steps, setSteps] = useState<EventStep[]>([])
  const [stepsLoading, setStepsLoading] = useState(false)
  const [accounts, setAccounts] = useState<{id: number; name: string; proxy_wallet: string}[]>([])

  useEffect(() => {
    if (!proxyWallet) {
      apiFetch('/api/account/list').then(async res => {
        if (res.ok) {
          const data = await res.json()
          setAccounts(Array.isArray(data) ? data : [])
        }
      })
    }
  }, [proxyWallet])

  const fetchTrades = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (proxyWallet) params.set('proxy_wallet', proxyWallet)
      else if (walletFilter) params.set('proxy_wallet', walletFilter)
      if (statusFilter === 'entry_working' || statusFilter === 'exit_working' || statusFilter === 'closed') {
        params.set('lifecycle_status', statusFilter)
      } else if (statusFilter === 'failed' || statusFilter === 'completed') {
        params.set('trade_outcome', statusFilter)
      } else if (statusFilter === 'needs_attention') {
        params.set('needs_attention', 'true')
      } else if (statusFilter) {
        params.set('status', statusFilter)
      }
      if (directionFilter) params.set('direction', directionFilter)
      if (search) params.set('search', search)
      if (timeRange) {
        const hours = { '24h': 24, '7d': 168, '30d': 720 }[timeRange]
        if (hours) {
          params.set('since', new Date(Date.now() - hours * 3600000).toISOString())
        }
      }
      params.set('limit', String(PAGE_SIZE))
      params.set('offset', String(page * PAGE_SIZE))
      const res = await apiFetch(`/api/strategy/trades?${params}`)
      if (res.ok) {
        const data = await res.json()
        setTrades(data.trades || [])
        setTotal(data.total || 0)
      }
    } catch (e) {
      console.error('Failed to fetch trades', e)
    } finally {
      setLoading(false)
    }
  }, [statusFilter, walletFilter, directionFilter, timeRange, search, page, proxyWallet])

  useEffect(() => { fetchTrades() }, [fetchTrades])

  const handleSearch = () => {
    setPage(0)
    setSearch(searchInput.trim())
  }

  const handleExpand = async (eventId: string) => {
    if (expandedId === eventId) {
      setExpandedId(null)
      setSteps([])
      return
    }
    setExpandedId(eventId)
    setStepsLoading(true)
    try {
      const res = await apiFetch(`/api/strategy/events/${eventId}`)
      if (res.ok) {
        const data = await res.json()
        setSteps(data.steps || [])
      }
    } catch (e) {
      console.error('Failed to fetch steps', e)
    } finally {
      setStepsLoading(false)
    }
  }

  const formatTime = (ts: string | null) => {
    if (!ts) return '--'
    const utc = ts.endsWith('Z') ? ts : ts + 'Z'
    return new Date(utc).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }

  const formatDuration = (ms: number | null) => {
    if (!ms) return '--'
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
    if (ms < 3600000) return `${Math.floor(ms / 60000)}m${Math.floor((ms % 60000) / 1000)}s`
    return `${Math.floor(ms / 3600000)}h${Math.floor((ms % 3600000) / 60000)}m`
  }

  const pnlColor = (pnl: string | null) => {
    if (!pnl) return textSecondary
    const v = parseFloat(pnl)
    if (v > 0) return '#22c55e'
    if (v < 0) return '#ef4444'
    return textSecondary
  }

  const bg = darkMode ? '#0f172a' : '#f8fafc'
  const cardBg = darkMode ? '#1e293b' : '#ffffff'
  const border = darkMode ? '#334155' : '#e2e8f0'
  const textPrimary = darkMode ? '#f8fafc' : '#0f172a'
  const textSecondary = darkMode ? '#94a3b8' : '#64748b'


  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div style={{ flex: 1, overflow: 'auto', padding: '24px', background: bg }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', margin: '0 0 16px' }}>
        {onBack && (
          <button onClick={onBack} style={{
            background: 'transparent', border: `1px solid ${border}`, borderRadius: '6px',
            padding: '4px 10px', cursor: 'pointer', color: textSecondary, fontSize: '13px',
          }}>← 返回</button>
        )}
        <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: textPrimary }}>
          交易记录
        </h2>
      </div>

      {/* Filters */}
      <div style={{ display: 'flex', gap: '8px', marginBottom: '16px', alignItems: 'center', flexWrap: 'wrap' }}>
        <select
          value={statusFilter}
          onChange={e => { setStatusFilter(e.target.value); setPage(0) }}
          style={{ padding: '6px 10px', borderRadius: '6px', border: `1px solid ${border}`, background: darkMode ? '#334155' : '#f1f5f9', color: textPrimary, fontSize: '13px' }}
        >
          <option value="">全部状态</option>
          <option value="entry_working">入场中</option>
          <option value="exit_working">出场中</option>
          <option value="closed">已结束</option>
          <option value="completed">完成</option>
          <option value="failed">失败</option>
          <option value="needs_attention">需处理</option>
        </select>
        {!proxyWallet && accounts.length > 0 && (
          <select
            value={walletFilter}
            onChange={e => { setWalletFilter(e.target.value); setPage(0) }}
            style={{ padding: '6px 10px', borderRadius: '6px', border: `1px solid ${border}`, background: darkMode ? '#334155' : '#f1f5f9', color: textPrimary, fontSize: '13px' }}
          >
            <option value="">全部账户</option>
            {accounts.map(a => (
              <option key={a.id} value={a.proxy_wallet}>{a.name || a.proxy_wallet.slice(0, 8)}</option>
            ))}
          </select>
        )}
        <select
          value={directionFilter}
          onChange={e => { setDirectionFilter(e.target.value); setPage(0) }}
          style={{ padding: '6px 10px', borderRadius: '6px', border: `1px solid ${border}`, background: darkMode ? '#334155' : '#f1f5f9', color: textPrimary, fontSize: '13px' }}
        >
          <option value="">全部方向</option>
          <option value="highest">最高温</option>
          <option value="lowest">最低温</option>
        </select>
        <select
          value={timeRange}
          onChange={e => { setTimeRange(e.target.value); setPage(0) }}
          style={{ padding: '6px 10px', borderRadius: '6px', border: `1px solid ${border}`, background: darkMode ? '#334155' : '#f1f5f9', color: textPrimary, fontSize: '13px' }}
        >
          <option value="">全部时间</option>
          <option value="24h">24小时</option>
          <option value="7d">7天</option>
          <option value="30d">30天</option>
        </select>
        <div style={{ flex: 1 }} />
        <input
          value={searchInput}
          onChange={e => setSearchInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSearch()}
          placeholder="搜索 event_slug..."
          style={{
            padding: '6px 12px', borderRadius: '6px', border: `1px solid ${border}`,
            background: darkMode ? '#334155' : '#f1f5f9', color: textPrimary, fontSize: '13px', width: '200px',
          }}
        />
        <button onClick={handleSearch} style={{ padding: '6px 14px', borderRadius: '6px', border: 'none', background: '#2563eb', color: '#fff', fontSize: '13px', cursor: 'pointer' }}>
          搜索
        </button>
      </div>

      {/* Stats */}
      <div style={{ fontSize: '12px', color: textSecondary, marginBottom: '12px' }}>
        共 {total} 笔交易
        {search && <span> | 筛选: <span style={{ color: textPrimary }}>{search}</span></span>}
      </div>

      {/* Trade List */}
      {loading ? (
        <div style={{ color: textSecondary, padding: '40px', textAlign: 'center' }}>加载中...</div>
      ) : trades.length === 0 ? (
        <div style={{ color: textSecondary, padding: '40px', textAlign: 'center' }}>暂无交易记录</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {trades.map(t => {
            const lifecycle = lifecycleBadge(t)
            const exit = exitBadge(t)
            return (
            <div key={t.event_id}>
              <div
                onClick={() => handleExpand(t.event_id)}
                style={{
                  background: expandedId === t.event_id ? (darkMode ? '#334155' : '#eff6ff') : cardBg,
                  borderRadius: '8px',
                  padding: '12px 16px',
                  border: `1px solid ${expandedId === t.event_id ? '#3b82f6' : border}`,
                  cursor: 'pointer',
                  transition: 'all 0.15s',
                }}
              >
                {/* Row 1: status + market + time */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '6px' }}>
                  <span style={badgeStyle(lifecycle.tone, darkMode)}>
                    <span style={iconStyle(lifecycle.tone, darkMode)}>{lifecycle.icon}</span>
                    {lifecycle.label}
                  </span>
                  {exit && (
                    <span style={badgeStyle(exit.tone, darkMode)} title={[t.close_reason, t.failure_reason].filter(Boolean).join(' / ')}>
                      <span style={iconStyle(exit.tone, darkMode)}>{exit.icon}</span>
                      {exit.label}
                      {exit.detail && <span style={{ opacity: 0.75 }}>{exit.detail}</span>}
                    </span>
                  )}
                  <span style={{ fontSize: '13px', fontWeight: 600, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
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
                  <span style={{ fontSize: '12px', color: textSecondary }}>{formatTime(t.started_at)}</span>
                </div>

                {/* Row 2: city + direction + entry/exit + pnl + duration */}
                <div style={{ display: 'flex', gap: '16px', fontSize: '12px', color: textSecondary, flexWrap: 'wrap', alignItems: 'center' }}>
                  {t.city && <span>{t.city} {t.direction === 'highest' ? '↑' : t.direction === 'lowest' ? '↓' : ''}</span>}
                  <span>买入: {t.entry_price ? `$${t.entry_price}` : '--'}{' '}
                    {t.entry_order_size ? (
                      <span style={{ color: t.entry_shares && parseFloat(t.entry_shares) >= parseFloat(t.entry_order_size) ? '#22c55e' : '#f59e0b' }}>
                        {t.entry_shares || '0'}/{t.entry_order_size}
                      </span>
                    ) : t.entry_shares ? t.entry_shares : ''}
                  </span>
                  <span>卖出: {t.exit_price ? `$${t.exit_price}` : '--'}{' '}
                    {t.exit_order_size ? (
                      <span style={{ color: t.exit_shares && parseFloat(t.exit_shares) >= parseFloat(t.exit_order_size) ? '#22c55e' : '#f59e0b' }}>
                        {t.exit_shares || '0'}/{t.exit_order_size}
                      </span>
                    ) : t.exit_shares ? t.exit_shares : ''}
                  </span>
                  {t.pnl !== null && (
                    <span style={{ fontWeight: 600, color: pnlColor(t.pnl) }}>
                      PnL: {parseFloat(t.pnl) > 0 ? '+' : ''}{parseFloat(t.pnl).toFixed(4)}
                      {t.pnl_pct && ` (${parseFloat(t.pnl_pct) > 0 ? '+' : ''}${parseFloat(t.pnl_pct).toFixed(2)}%)`}
                    </span>
                  )}
                  <span>耗时: {formatDuration(t.duration_ms)}</span>
                </div>
              </div>

              {/* Expanded: step timeline */}
              {expandedId === t.event_id && (
                <div style={{
                  background: darkMode ? '#0f172a' : '#f8fafc',
                  border: `1px solid ${border}`,
                  borderTop: 'none',
                  borderRadius: '0 0 8px 8px',
                  padding: '12px 16px',
                  marginTop: '-2px',
                }}>
                  {stepsLoading ? (
                    <div style={{ color: textSecondary, textAlign: 'center', padding: '12px' }}>加载详情...</div>
                  ) : steps.length === 0 ? (
                    <div style={{ color: textSecondary, textAlign: 'center', padding: '12px' }}>无步骤记录</div>
                  ) : (
                    <div style={{ position: 'relative', paddingLeft: '24px' }}>
                      <div style={{
                        position: 'absolute', left: '8px', top: '4px', bottom: '4px',
                        width: '2px', background: darkMode ? '#475569' : '#cbd5e1',
                      }} />
                      {steps.map((step, idx) => (
                        <div key={step.id || idx} style={{ position: 'relative', marginBottom: '14px' }}>
                          <div style={{
                            position: 'absolute', left: '-20px', top: '6px',
                            width: '10px', height: '10px', borderRadius: '50%',
                            background: PHASE_COLORS[step.phase] || '#64748b',
                            border: `2px solid ${darkMode ? '#1e293b' : '#ffffff'}`,
                          }} />
                          <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px', marginBottom: '3px' }}>
                            <span style={{
                              fontSize: '11px', padding: '1px 6px', borderRadius: '3px',
                              background: `${PHASE_COLORS[step.phase] || '#64748b'}20`,
                              color: PHASE_COLORS[step.phase] || '#64748b',
                              fontWeight: 500,
                            }}>
                              {PHASE_LABELS[step.phase] || step.phase}
                            </span>
                            <span style={{ fontSize: '13px', fontWeight: 600, color: textPrimary }}>{step.step}</span>
                            <span style={{ fontSize: '11px', color: textSecondary, marginLeft: 'auto' }}>
                              #{step.sequence_no} &middot; {formatTime(step.occurred_at)}
                            </span>
                          </div>
                          {step.detail && Object.keys(step.detail).length > 0 && (
                            <div style={{
                              fontSize: '11px', color: textSecondary,
                              background: darkMode ? '#1e293b' : '#f1f5f9',
                              padding: '6px 10px', borderRadius: '5px',
                              fontFamily: 'monospace', wordBreak: 'break-all', lineHeight: '1.5',
                            }}>
                              {Object.entries(step.detail).map(([k, v]) => (
                                <div key={k}>
                                  <span style={{ color: darkMode ? '#93c5fd' : '#2563eb' }}>{k}</span>: <span style={{ color: textPrimary }}>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )})}
        </div>
      )}

      {/* Pagination */}
      {!loading && total > 0 && (
        <div style={{ display: 'flex', justifyContent: 'center', gap: '12px', marginTop: '20px' }}>
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            style={{ padding: '6px 16px', borderRadius: '6px', border: `1px solid ${border}`, background: 'transparent', color: page === 0 ? textSecondary : textPrimary, fontSize: '13px', cursor: page === 0 ? 'not-allowed' : 'pointer', opacity: page === 0 ? 0.5 : 1 }}
          >
            上一页
          </button>
          <span style={{ fontSize: '13px', color: textSecondary, lineHeight: '32px' }}>
            {page + 1} / {totalPages || 1}
          </span>
          <button
            onClick={() => setPage(p => p + 1)}
            disabled={page >= totalPages - 1}
            style={{ padding: '6px 16px', borderRadius: '6px', border: `1px solid ${border}`, background: 'transparent', color: page >= totalPages - 1 ? textSecondary : textPrimary, fontSize: '13px', cursor: page >= totalPages - 1 ? 'not-allowed' : 'pointer', opacity: page >= totalPages - 1 ? 0.5 : 1 }}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
