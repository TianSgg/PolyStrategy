import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE, apiFetch } from '../api'
import WeatherOrderbook from './WeatherOrderbook'
import WeatherTimeline from './WeatherTimeline'
import WeatherNotifications from './WeatherNotifications'
import './WeatherMonitor.css'

type Direction = {
  direction: string
  status: string
  event_slug: string | null
  main_market_slug: string | null
  main_temperature_label: string | null
  notification_count: number
  markets: Market[]
}

type City = {
  name: string
  slug: string
  timezone: string
  directions: Direction[]
}

type Market = {
  market_slug: string
  temperature_label: string
  yes_token_id: string
  no_token_id: string
  selected?: boolean
}

type DirectionRow = Direction & {
  city: string
  citySlug: string
  timezone: string
  rowKey: string
}

type LiveBooks = Record<string, Record<string, unknown>>

interface Props {
  darkMode: boolean
  visible?: boolean
  onManageCities?: () => void
}

function directionLabel(direction: string) {
  return direction === 'highest' ? '最高温' : '最低温'
}

function statusLabel(status: string) {
  return ({ monitoring: '监控中', resolved: '已结算', exhausted: '已耗尽', discovering: '发现中' } as Record<string, string>)[status] || status
}

function statusType(status: string) {
  return ({ monitoring: 'success', resolved: 'info', exhausted: 'warning', discovering: 'warning' } as Record<string, string>)[status] || 'info'
}

function localTime(timezone: string | null): string {
  if (!timezone) return '-'
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium', timeStyle: 'medium', hour12: false, timeZone: timezone,
    }).format(new Date())
  } catch {
    return '-'
  }
}

export default function WeatherMonitor({ darkMode, visible, onManageCities }: Props) {
  const [cities, setCities] = useState<City[]>([])
  const [keyword, setKeyword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState('')
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set())
  const [expandedMarkets, setExpandedMarkets] = useState<Set<string>>(new Set())
  const [liveOrderbooks, setLiveOrderbooks] = useState<LiveBooks>({})
  const [liveConnected, setLiveConnected] = useState(false)
  const [notificationCounts, setNotificationCounts] = useState<Record<string, number>>({})
  const [showRecent, setShowRecent] = useState(false)
  const [recentLimit, setRecentLimit] = useState(100)
  const [historySlug, setHistorySlug] = useState('')
  const [querySlug, setQuerySlug] = useState('')
  const [expandedNotifRows, setExpandedNotifRows] = useState<Set<string>>(new Set())
  const [, setNow] = useState(Date.now())
  const [latencyMs, setLatencyMs] = useState<number | null>(null)

  const liveRef = useRef<EventSource | null>(null)
  const notifRef = useRef<EventSource | null>(null)

  const directionRows: DirectionRow[] = useMemo(() =>
    cities.flatMap(city => city.directions.map(dir => ({
      ...dir,
      city: city.name,
      citySlug: city.slug,
      timezone: city.timezone,
      rowKey: `${city.slug}:${dir.direction}`,
    }))),
    [cities]
  )

  const visibleRows = useMemo(() => {
    const term = keyword.trim().toLowerCase()
    if (!term) return directionRows
    return directionRows.filter(row =>
      [row.city, row.citySlug, row.timezone, row.direction, row.event_slug, row.main_market_slug, row.main_temperature_label]
        .filter(Boolean)
        .some(v => v!.toLowerCase().includes(term))
    )
  }, [directionRows, keyword])


  async function refresh() {
    setLoading(true)
    setError('')
    try {
      const res = await apiFetch('/api/weather/cities')
      if (!res.ok) throw new Error('Failed to load')
      const data = await res.json()
      setCities(data.cities || [])
      setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setLoading(false)
    }
  }

  function connectLive() {
    if (liveRef.current) { liveRef.current.close(); liveRef.current = null }
    const es = new EventSource(`${API_BASE}/api/weather/live`)
    liveRef.current = es
    es.onopen = () => { setLiveConnected(true); refresh() }
    es.onerror = () => {
      setLiveConnected(false)
      es.close()
      liveRef.current = null
      setTimeout(connectLive, 3000)
    }
    es.addEventListener('snapshot', (event) => {
      const next: LiveBooks = {}
      for (const payload of JSON.parse(event.data)) next[payload.market_slug] = payload.books
      setLiveOrderbooks(next)
      setLiveConnected(true)
    })
    es.addEventListener('orderbook', (event) => {
      const payload = JSON.parse(event.data)
      setLiveOrderbooks(prev => ({ ...prev, [payload.market_slug]: payload.books }))
    })
  }

  function connectNotificationCounts() {
    if (notifRef.current) { notifRef.current.close(); notifRef.current = null }
    const es = new EventSource(`${API_BASE}/api/weather/signal-counts/live`)
    notifRef.current = es
    es.onerror = () => {
      es.close()
      notifRef.current = null
      setTimeout(connectNotificationCounts, 3000)
    }
    es.addEventListener('snapshot', (event) => {
      setNotificationCounts(JSON.parse(event.data))
    })
    es.addEventListener('notification-count', (event) => {
      const payload = JSON.parse(event.data)
      if (payload.type === 'snapshot') setNotificationCounts(payload.counts || {})
      if (payload.type === 'update') {
        setNotificationCounts(prev => ({ ...prev, [payload.event_slug]: payload.count }))
      }
    })
  }

  async function measureLatency() {
    const start = performance.now()
    try {
      const res = await fetch(`${API_BASE}/health`, { cache: 'no-store' })
      if (res.ok) setLatencyMs(Math.round(performance.now() - start))
      else setLatencyMs(null)
    } catch {
      setLatencyMs(null)
    }
  }

  useEffect(() => {
    if (!visible) return
    refresh()
    connectLive()
    connectNotificationCounts()
    measureLatency()
    const clockId = window.setInterval(() => setNow(Date.now()), 1000)
    const pingId = window.setInterval(measureLatency, 60000)
    return () => {
      window.clearInterval(clockId)
      window.clearInterval(pingId)
      liveRef.current?.close()
      notifRef.current?.close()
    }
  }, [visible])

  function toggleRow(rowKey: string) {
    setExpandedRows(prev => {
      const next = new Set(prev)
      if (next.has(rowKey)) next.delete(rowKey)
      else next.add(rowKey)
      return next
    })
  }

  function toggleMarket(key: string) {
    setExpandedMarkets(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function toggleNotifRow(rowKey: string) {
    setExpandedNotifRows(prev => {
      const next = new Set(prev)
      if (next.has(rowKey)) next.delete(rowKey)
      else next.add(rowKey)
      return next
    })
  }

  function probability(row: DirectionRow, outcome: 'yes' | 'no'): string {
    const book = (liveOrderbooks[row.main_market_slug!] as Record<string, { best_bid?: { price: string }; best_ask?: { price: string } }> | undefined)?.[outcome]
    if (!book) return '-'
    const bid = Number(book.best_bid?.price)
    const ask = Number(book.best_ask?.price)
    const price = Number.isFinite(bid) && Number.isFinite(ask)
      ? (bid + ask) / 2
      : (Number.isFinite(bid) ? bid : (Number.isFinite(ask) ? ask : null))
    return price === null ? '-' : `${(price * 100).toFixed(1)}%`
  }

  function notificationCount(row: DirectionRow): number {
    return notificationCounts[row.event_slug!] ?? row.notification_count ?? 0
  }

  const DEFAULT_COL_WIDTHS = [50, 160, 80, 130, 150, 60, 70, 160, 90, 60]
  const [colWidths, setColWidths] = useState<number[]>(DEFAULT_COL_WIDTHS)
  const resizingRef = useRef<{ colIdx: number; startX: number; startW: number } | null>(null)

  const onResizeStart = useCallback((colIdx: number, e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = colWidths[colIdx]
    resizingRef.current = { colIdx, startX, startW }

    const onMove = (ev: MouseEvent) => {
      if (!resizingRef.current) return
      const diff = ev.clientX - resizingRef.current.startX
      const newW = Math.max(40, resizingRef.current.startW + diff)
      setColWidths(prev => {
        const next = [...prev]
        next[resizingRef.current!.colIdx] = newW
        return next
      })
    }
    const onUp = () => {
      resizingRef.current = null
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [colWidths])

  return (
    <div className="wm-page" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="wm-header">
        <div>
          <h2 className="wm-title">天气监控</h2>
          <div className="wm-subtitle">{updatedAt ? `更新于: ${updatedAt}` : ''}</div>
        </div>
        <div className="wm-header-actions">
          <span className={`wm-latency-badge ${liveConnected ? '' : 'disconnected'}`} onClick={measureLatency} title="点击刷新延迟">
            <span className="wm-latency-label">{liveConnected ? 'WS-mkt' : '已断开'}</span>
            <span className={`wm-latency-value ${!liveConnected ? 'off' : latencyMs == null ? 'off' : latencyMs < 100 ? 'good' : latencyMs < 300 ? 'warn' : 'bad'}`}>
              {latencyMs == null ? '--' : `${latencyMs}ms`}
            </span>
          </span>
          {onManageCities && (
            <button className="wm-btn" onClick={onManageCities}>城市管理</button>
          )}
        </div>
      </div>

      {/* Recent notifications collapsible */}
      <div className="wm-recent-section">
        <button className="wm-collapse-header" onClick={() => setShowRecent(!showRecent)} style={{ border: 'none', background: 'none', padding: 0 }}>
          <strong>最近信号</strong>
          <span>最近 {recentLimit} 条 {showRecent ? '(收起)' : '(展开)'}</span>
        </button>
        {showRecent && (
          <div style={{ marginTop: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12, fontSize: 13, color: 'var(--wm-text-secondary)' }}>
              <span>数量:</span>
              <input
                type="number"
                min={10}
                max={500}
                step={10}
                value={recentLimit}
                onChange={e => setRecentLimit(Number(e.target.value))}
                style={{ width: 80, background: 'var(--wm-card-bg)', border: '1px solid var(--wm-card-border)', borderRadius: 4, color: 'var(--wm-text)', padding: '4px 8px' }}
              />
            </div>
            <WeatherNotifications limit={recentLimit} />
          </div>
        )}
      </div>

      {/* Toolbar */}
      <div className="wm-toolbar">
        <input
          placeholder="搜索城市、事件、市场或时区..."
          value={keyword}
          onChange={e => setKeyword(e.target.value)}
        />
        <span className="wm-toolbar-info">{visibleRows.length} / {directionRows.length}</span>
        <input
          placeholder="输入事件 slug 查询历史..."
          value={historySlug}
          onChange={e => setHistorySlug(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') setQuerySlug(historySlug.trim()) }}
          style={{ width: 'min(320px, 100%)' }}
        />
        <button className="wm-btn" onClick={() => setQuerySlug(historySlug.trim())}>查询</button>
      </div>

      {/* History query section */}
      {querySlug && (
        <div className="wm-history-section">
          <div className="wm-history-header">
            <strong>历史记录: {querySlug}</strong>
            <button className="wm-btn-link" onClick={() => { setQuerySlug(''); setHistorySlug('') }}>关闭</button>
          </div>
          <WeatherTimeline eventSlug={querySlug} />
        </div>
      )}

      {error && <div className="wm-error">{error}</div>}

      {/* Main table */}
      {!loading && visibleRows.length === 0 && <div className="wm-empty">无匹配结果</div>}
      {visibleRows.length > 0 && (
        <div className="wm-table-wrap">
          <table className="wm-table wm-table-resizable">
            <colgroup>
              {colWidths.map((w, i) => <col key={i} style={{ width: w }} />)}
            </colgroup>
            <thead>
              <tr>
                {['', '事件', '城市', '时区', '当地时间', '类型', '状态', '当前市场', '概率', '信号数'].map((label, i) => (
                  <th key={i}>
                    {label}
                    {i > 0 && <span className="wm-resize-handle" onMouseDown={e => onResizeStart(i, e)} />}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map(row => (
                <>
                  <tr key={row.rowKey}>
                    <td>
                      <button className="wm-btn-link" onClick={() => toggleRow(row.rowKey)}>
                        {expandedRows.has(row.rowKey) ? '收起' : '详情'}
                      </button>
                    </td>
                    <td>
                      {row.event_slug ? (
                        <a
                          className="wm-event-link"
                          href={`https://polymarket.com/event/${encodeURIComponent(row.event_slug)}`}
                          target="_blank"
                          rel="noopener noreferrer"
                        >{row.event_slug}</a>
                      ) : '-'}
                    </td>
                    <td>
                      <span className="wm-cell-main">{row.city}</span>
                      <span className="wm-cell-sub">{row.citySlug}</span>
                    </td>
                    <td>{row.timezone || '-'}</td>
                    <td>{localTime(row.timezone)}</td>
                    <td>{directionLabel(row.direction)}</td>
                    <td>
                      <span className={`wm-tag wm-tag-${statusType(row.status)}`}>
                        {statusLabel(row.status)}
                      </span>
                    </td>
                    <td>
                      <span className="wm-cell-main">{row.main_temperature_label || '-'}</span>
                      <span className="wm-cell-sub">{row.main_market_slug || '未选定'}</span>
                    </td>
                    <td>
                      {row.status === 'monitoring' ? (
                        <div className="wm-live-prob">
                          <span>YES <strong>{probability(row, 'yes')}</strong></span>
                          <span>NO <strong>{probability(row, 'no')}</strong></span>
                        </div>
                      ) : <span style={{ color: 'var(--wm-text-secondary)' }}>-</span>}
                    </td>
                    <td>
                      <strong className="wm-notif-count">{notificationCount(row)}</strong>
                    </td>
                  </tr>
                  {expandedRows.has(row.rowKey) && (
                    <tr key={`${row.rowKey}-detail`}>
                      <td colSpan={10} style={{ padding: 0 }}>
                        <div className="wm-detail-panel">
                          <div className="wm-detail-heading">
                            <div className="wm-detail-heading-info">
                              <strong>{row.city} - {directionLabel(row.direction)}</strong>
                              <span>{row.timezone || '-'} - {localTime(row.timezone)}</span>
                            </div>
                            <span style={{ color: 'var(--wm-text-secondary)', fontSize: 12 }}>{row.markets.length} 个区间</span>
                          </div>

                          {/* Markets / Orderbooks */}
                          <div className="wm-collapse">
                            <button className="wm-collapse-header" onClick={() => toggleMarket(`${row.rowKey}:ranges`)}>
                              <strong>温度区间 & 订单簿</strong>
                              <span>{row.markets.length} 个区间</span>
                            </button>
                            {expandedMarkets.has(`${row.rowKey}:ranges`) && (
                              <div className="wm-collapse-body">
                                {row.markets.map(market => {
                                  const mkey = `${row.rowKey}:${market.market_slug}`
                                  return (
                                    <div className="wm-market-item" key={market.market_slug}>
                                      <button className="wm-market-header" onClick={() => toggleMarket(mkey)}>
                                        <strong>{market.temperature_label}</strong>
                                        <span>{market.market_slug}</span>
                                        {market.selected && <span className="wm-tag wm-tag-success">Live L2</span>}
                                      </button>
                                      {expandedMarkets.has(mkey) && (
                                        <div style={{ padding: '0 14px 14px' }}>
                                          <WeatherOrderbook
                                            market={market}
                                            liveBooks={market.selected ? liveOrderbooks[market.market_slug] as any : null}
                                          />
                                        </div>
                                      )}
                                    </div>
                                  )
                                })}
                              </div>
                            )}
                          </div>

                          {/* Notification history for this direction */}
                          <div className="wm-collapse" style={{ marginTop: 12 }}>
                            <button className="wm-collapse-header" onClick={() => toggleNotifRow(row.rowKey)}>
                              <strong>信号历史</strong>
                              <span>{notificationCount(row)} 条信号</span>
                            </button>
                            {expandedNotifRows.has(row.rowKey) && row.event_slug && (
                              <div className="wm-collapse-body">
                                <WeatherTimeline eventSlug={row.event_slug} />
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
