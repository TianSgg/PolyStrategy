import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../api'

interface EventSummary {
  event_id: string
  config_id: number
  owner_user_id: number
  proxy_wallet: string
  signal_id: string | null
  token_id: string | null
  market_slug: string | null
  event_slug: string | null
  started_at: string
  ended_at: string
  step_count: number
  final_phase: string | null
}

interface EventStep {
  id: number
  event_id: string
  phase: string
  step: string
  sequence_no: number
  detail: Record<string, any>
  occurred_at: string
  market_slug: string | null
  event_slug: string | null
  token_id: string | null
}

interface Props {
  darkMode: boolean
  onBack?: () => void
}

const PHASE_COLORS: Record<string, string> = {
  entry: '#3b82f6',
  monitor: '#8b5cf6',
  exit: '#22c55e',
  exit_risk: '#ef4444',
  exit_force: '#f59e0b',
}

const PHASE_LABELS: Record<string, string> = {
  entry: '入场',
  monitor: '监控',
  exit: '退出',
  exit_risk: '风控退出',
  exit_force: '强制退出',
}

export default function SweepTradeEvents({ darkMode, onBack }: Props) {
  const [events, setEvents] = useState<EventSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [steps, setSteps] = useState<EventStep[]>([])
  const [stepsLoading, setStepsLoading] = useState(false)
  const [page, setPage] = useState(0)
  const PAGE_SIZE = 30

  const fetchEvents = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (search) params.set('search', search)
      params.set('limit', String(PAGE_SIZE))
      params.set('offset', String(page * PAGE_SIZE))
      const res = await apiFetch(`/api/strategy/events?${params}`)
      if (res.ok) {
        const data = await res.json()
        setEvents(data.events || [])
      }
    } catch (e) {
      console.error('Failed to fetch events', e)
    } finally {
      setLoading(false)
    }
  }, [search, page])

  useEffect(() => { fetchEvents() }, [fetchEvents])

  const handleSearch = () => {
    setPage(0)
    setSearch(searchInput.trim())
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleSearch()
  }

  const handleSelectEvent = async (eventId: string) => {
    if (selectedEventId === eventId) {
      setSelectedEventId(null)
      setSteps([])
      return
    }
    setSelectedEventId(eventId)
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
    const d = new Date(ts)
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }

  const formatDuration = (start: string, end: string) => {
    const ms = new Date(end).getTime() - new Date(start).getTime()
    if (ms < 0) return '--'
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
    if (ms < 3600000) return `${Math.floor(ms / 60000)}m${Math.floor((ms % 60000) / 1000)}s`
    return `${Math.floor(ms / 3600000)}h${Math.floor((ms % 3600000) / 60000)}m`
  }

  const bg = darkMode ? '#0f172a' : '#f8fafc'
  const cardBg = darkMode ? '#1e293b' : '#ffffff'
  const border = darkMode ? '#334155' : '#e2e8f0'
  const textPrimary = darkMode ? '#f8fafc' : '#0f172a'
  const textSecondary = darkMode ? '#94a3b8' : '#64748b'

  const inputStyle: React.CSSProperties = {
    padding: '8px 12px',
    borderRadius: '6px',
    border: `1px solid ${border}`,
    background: darkMode ? '#334155' : '#f1f5f9',
    color: textPrimary,
    fontSize: '14px',
    flex: 1,
    minWidth: '200px',
  }

  return (
    <div style={{ flex: 1, overflow: 'auto', padding: '24px', background: bg }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px' }}>
        {onBack && (
          <button onClick={onBack} style={{
            padding: '6px 12px', borderRadius: '6px', border: `1px solid ${border}`,
            background: 'transparent', color: textPrimary, fontSize: '13px', cursor: 'pointer',
          }}>
            &larr; 返回
          </button>
        )}
        <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: textPrimary }}>
          交易记录
        </h2>
      </div>

      {/* Search */}
      <div style={{ display: 'flex', gap: '8px', marginBottom: '20px', alignItems: 'center' }}>
        <input
          value={searchInput}
          onChange={e => setSearchInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="搜索 event_slug 关键字..."
          style={inputStyle}
        />
        <button onClick={handleSearch} style={{
          padding: '8px 20px', borderRadius: '6px', border: 'none',
          background: '#2563eb', color: '#fff', fontSize: '14px', cursor: 'pointer', whiteSpace: 'nowrap',
        }}>
          搜索
        </button>
        {search && (
          <button onClick={() => { setSearchInput(''); setSearch(''); setPage(0) }} style={{
            padding: '8px 14px', borderRadius: '6px', border: `1px solid ${border}`,
            background: 'transparent', color: textSecondary, fontSize: '13px', cursor: 'pointer', whiteSpace: 'nowrap',
          }}>
            清除
          </button>
        )}
      </div>

      {search && (
        <div style={{ marginBottom: '12px', fontSize: '13px', color: textSecondary }}>
          筛选: <span style={{ color: textPrimary, fontWeight: 500 }}>{search}</span>
        </div>
      )}

      {/* Event List */}
      {loading ? (
        <div style={{ color: textSecondary, padding: '40px', textAlign: 'center' }}>加载中...</div>
      ) : events.length === 0 ? (
        <div style={{ color: textSecondary, padding: '40px', textAlign: 'center' }}>
          {search ? '无匹配的交易记录' : '暂无交易记录'}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {events.map(evt => (
            <div key={evt.event_id}>
              {/* Summary row */}
              <div
                onClick={() => handleSelectEvent(evt.event_id)}
                style={{
                  background: selectedEventId === evt.event_id ? (darkMode ? '#334155' : '#eff6ff') : cardBg,
                  borderRadius: '10px',
                  padding: '16px 20px',
                  border: `1px solid ${selectedEventId === evt.event_id ? '#3b82f6' : border}`,
                  cursor: 'pointer',
                  transition: 'all 0.15s',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
                  {/* Phase badge */}
                  <span style={{
                    fontSize: '11px', padding: '2px 8px', borderRadius: '4px', fontWeight: 500,
                    background: `${PHASE_COLORS[evt.final_phase || 'entry']}20`,
                    color: PHASE_COLORS[evt.final_phase || 'entry'],
                  }}>
                    {PHASE_LABELS[evt.final_phase || ''] || evt.final_phase || '--'}
                  </span>
                  <span style={{ fontSize: '14px', fontWeight: 600, color: textPrimary, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {evt.event_slug || evt.market_slug || evt.event_id.slice(0, 8)}
                  </span>
                  <span style={{ fontSize: '12px', color: textSecondary }}>
                    {formatTime(evt.started_at)}
                  </span>
                </div>
                <div style={{ display: 'flex', gap: '16px', fontSize: '12px', color: textSecondary, flexWrap: 'wrap' }}>
                  <span>Token: {evt.token_id ? evt.token_id.slice(0, 10) + '...' : '--'}</span>
                  <span>步骤: {evt.step_count}</span>
                  <span>耗时: {formatDuration(evt.started_at, evt.ended_at)}</span>
                  <span>钱包: {evt.proxy_wallet ? evt.proxy_wallet.slice(0, 8) + '...' : '--'}</span>
                </div>
              </div>

              {/* Expanded detail */}
              {selectedEventId === evt.event_id && (
                <div style={{
                  background: darkMode ? '#0f172a' : '#f8fafc',
                  border: `1px solid ${border}`,
                  borderTop: 'none',
                  borderRadius: '0 0 10px 10px',
                  padding: '16px 20px',
                  marginTop: '-4px',
                }}>
                  {stepsLoading ? (
                    <div style={{ color: textSecondary, textAlign: 'center', padding: '12px' }}>加载详情...</div>
                  ) : steps.length === 0 ? (
                    <div style={{ color: textSecondary, textAlign: 'center', padding: '12px' }}>无步骤记录</div>
                  ) : (
                    <div style={{ position: 'relative', paddingLeft: '24px' }}>
                      {/* Timeline line */}
                      <div style={{
                        position: 'absolute', left: '8px', top: '4px', bottom: '4px',
                        width: '2px', background: darkMode ? '#475569' : '#cbd5e1',
                      }} />
                      {steps.map((step, idx) => (
                        <div key={step.id || idx} style={{ position: 'relative', marginBottom: '16px' }}>
                          {/* Timeline dot */}
                          <div style={{
                            position: 'absolute', left: '-20px', top: '6px',
                            width: '10px', height: '10px', borderRadius: '50%',
                            background: PHASE_COLORS[step.phase] || '#64748b',
                            border: `2px solid ${darkMode ? '#1e293b' : '#ffffff'}`,
                          }} />
                          <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px', marginBottom: '4px' }}>
                            <span style={{
                              fontSize: '11px', padding: '1px 6px', borderRadius: '3px',
                              background: `${PHASE_COLORS[step.phase] || '#64748b'}20`,
                              color: PHASE_COLORS[step.phase] || '#64748b',
                              fontWeight: 500,
                            }}>
                              {PHASE_LABELS[step.phase] || step.phase}
                            </span>
                            <span style={{ fontSize: '13px', fontWeight: 600, color: textPrimary }}>
                              {step.step}
                            </span>
                            <span style={{ fontSize: '11px', color: textSecondary, marginLeft: 'auto' }}>
                              #{step.sequence_no} &middot; {formatTime(step.occurred_at)}
                            </span>
                          </div>
                          {/* Detail JSON */}
                          {step.detail && Object.keys(step.detail).length > 0 && (
                            <div style={{
                              fontSize: '12px', color: textSecondary,
                              background: darkMode ? '#1e293b' : '#f1f5f9',
                              padding: '8px 12px', borderRadius: '6px',
                              fontFamily: 'monospace', wordBreak: 'break-all',
                              lineHeight: '1.5',
                            }}>
                              {Object.entries(step.detail).map(([k, v]) => (
                                <div key={k}>
                                  <span style={{ color: darkMode ? '#93c5fd' : '#2563eb' }}>{k}</span>
                                  {': '}
                                  <span style={{ color: textPrimary }}>
                                    {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                                  </span>
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
          ))}
        </div>
      )}

      {/* Pagination */}
      {!loading && events.length > 0 && (
        <div style={{ display: 'flex', justifyContent: 'center', gap: '12px', marginTop: '20px' }}>
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            style={{
              padding: '6px 16px', borderRadius: '6px', border: `1px solid ${border}`,
              background: 'transparent', color: page === 0 ? textSecondary : textPrimary,
              fontSize: '13px', cursor: page === 0 ? 'not-allowed' : 'pointer', opacity: page === 0 ? 0.5 : 1,
            }}
          >
            上一页
          </button>
          <span style={{ fontSize: '13px', color: textSecondary, lineHeight: '32px' }}>
            第 {page + 1} 页
          </span>
          <button
            onClick={() => setPage(p => p + 1)}
            disabled={events.length < PAGE_SIZE}
            style={{
              padding: '6px 16px', borderRadius: '6px', border: `1px solid ${border}`,
              background: 'transparent', color: events.length < PAGE_SIZE ? textSecondary : textPrimary,
              fontSize: '13px', cursor: events.length < PAGE_SIZE ? 'not-allowed' : 'pointer',
              opacity: events.length < PAGE_SIZE ? 0.5 : 1,
            }}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
