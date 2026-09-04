import { useEffect, useState } from 'react'
import { apiFetch } from '../api'

type Notification = {
  id: number
  occurred_at: string
  signal_type: string
  event_slug: string
  market_slug: string | null
  temperature_label: string | null
  outcome: string | null
  status: string | null
  reason: string | null
  payload: Record<string, unknown>
}

interface Props {
  eventSlug: string
}

const TYPE_LABEL: Record<string, string> = {
  sweep: '扫单',
  no_longer_possible: '不再可能',
  market_resolved: '已结算',
  event_started: '事件开始',
}

const TYPE_STYLE: Record<string, string> = {
  sweep: 'danger',
  no_longer_possible: 'warning',
  market_resolved: 'success',
  event_started: 'primary',
}

const MAX_LOADED = 500

function formatTimestamp(value: string | null): string {
  if (!value) return '-'
  try {
    const d = new Date(value)
    const fmt = (tz: string) => new Intl.DateTimeFormat('zh-CN', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(d).replace(/\//g, '-') + `.${String(d.getMilliseconds()).padStart(3, '0')}`
    const local = fmt(Intl.DateTimeFormat().resolvedOptions().timeZone)
    const utc = fmt('UTC')
    return `${local} (UTC ${utc})`
  } catch {
    return value
  }
}

export default function WeatherTimeline({ eventSlug }: Props) {
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [expandedMessages, setExpandedMessages] = useState<Set<number>>(new Set())

  async function load(append = false) {
    if (!eventSlug) return
    if (append) setLoadingMore(true)
    else setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({ limit: '100' })
      if (append && nextBeforeId) params.set('before_id', String(nextBeforeId))
      const res = await apiFetch(`/api/weather/events/${encodeURIComponent(eventSlug)}/signals?${params}`)
      if (!res.ok) throw new Error('Failed to load')
      const data = await res.json()
      const items: Notification[] = data.signals || []
      const next = append ? [...notifications, ...items] : items
      const bounded = next.slice(0, MAX_LOADED)
      setNotifications(bounded)
      setNextBeforeId(bounded.length >= MAX_LOADED ? null : data.next_before_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setLoading(false)
      setLoadingMore(false)
    }
  }

  useEffect(() => {
    setNotifications([])
    setNextBeforeId(null)
    if (eventSlug) load()
  }, [eventSlug])

  function toggleMessage(id: number) {
    setExpandedMessages(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  if (loading) return <div className="wm-loading">加载中...</div>

  return (
    <div className="wm-timeline">
      <div className="wm-timeline-heading">
        <strong>信号历史</strong>
        <button className="wm-btn-link" onClick={() => load()} disabled={loading}>刷新</button>
      </div>
      {error && (
        <>
          <div className="wm-error">{error}</div>
          <button className="wm-btn" onClick={() => load()}>重试</button>
        </>
      )}
      {!error && notifications.length === 0 && <div className="wm-empty">暂无信号记录</div>}
      {notifications.length > 0 && (
        <ul className="wm-notification-list">
          {notifications.map(item => (
            <li key={item.id} className={`wm-notification-item type-${TYPE_STYLE[item.signal_type] || 'info'}`}>
              <div className="wm-notification-card">
                <div className="wm-notification-title">
                  <span className={`wm-tag wm-tag-${TYPE_STYLE[item.signal_type] || 'info'}`}>
                    {TYPE_LABEL[item.signal_type] || item.signal_type}
                  </span>
                  <strong>{item.temperature_label || '事件'}</strong>
                  {item.outcome && <span>{item.outcome.toUpperCase()}</span>}
                  {item.payload?.is_from_main !== undefined && (
                    <span className={`wm-tag wm-tag-${item.payload.is_from_main ? 'primary' : 'info'}`}>
                      {item.payload.is_from_main ? 'Main' : 'Next'}
                    </span>
                  )}
                  {item.status && <span>{item.status}</span>}
                </div>
                {item.market_slug && <p>市场: {item.market_slug}</p>}
                {item.reason && <p>原因: {item.reason}</p>}
                <div className="wm-notification-ts">{formatTimestamp(item.occurred_at)}</div>
                <button className="wm-btn-link" onClick={() => toggleMessage(item.id)} style={{ marginTop: 4, padding: '2px 0' }}>
                  {expandedMessages.has(item.id) ? '收起消息' : '展开消息'}
                </button>
                {expandedMessages.has(item.id) && (
                  <div className="wm-notification-message">
                    <pre>{JSON.stringify(item.payload, null, 2)}</pre>
                  </div>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {!nextBeforeId && notifications.length >= MAX_LOADED && (
        <p className="wm-timeline-limit">显示最近 {MAX_LOADED} 条记录</p>
      )}
      {nextBeforeId && (
        <div className="wm-timeline-more">
          <button className="wm-btn" onClick={() => load(true)} disabled={loadingMore}>
            {loadingMore ? '加载中...' : '加载更多'}
          </button>
        </div>
      )}
    </div>
  )
}
