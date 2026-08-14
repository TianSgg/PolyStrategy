import { useEffect, useState } from 'react'
import { apiFetch } from '../api'

type Notification = {
  id: number
  occurred_at: string
  event_type: string
  event_slug: string
  city: string
  temperature_label: string | null
  outcome: string | null
  reason: string | null
  message: string
}

interface Props {
  limit: number
}

const TYPE_LABEL: Record<string, string> = {
  sweep: 'Sweep',
  no_longer_possible: 'Impossible',
  market_resolved: 'Resolved',
  event_started: 'Event Started',
}

const TYPE_STYLE: Record<string, string> = {
  sweep: 'danger',
  no_longer_possible: 'warning',
  market_resolved: 'success',
  event_started: 'primary',
}

function formatTimestamp(value: string | null): string {
  if (!value) return '-'
  try {
    const d = new Date(value)
    const fmt = (tz: string) => new Intl.DateTimeFormat('zh-CN', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(d).replace(/\//g, '-')
    const local = fmt(Intl.DateTimeFormat().resolvedOptions().timeZone)
    const utc = fmt('UTC')
    return `${local} (UTC ${utc})`
  } catch {
    return value
  }
}

export default function WeatherNotifications({ limit }: Props) {
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [expandedMessages, setExpandedMessages] = useState<Set<number>>(new Set())

  async function load(append = false) {
    if (append) setLoadingMore(true)
    else setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({ limit: String(limit) })
      if (append && nextBeforeId) params.set('before_id', String(nextBeforeId))
      const res = await apiFetch(`/api/weather/notifications/recent?${params}`)
      if (!res.ok) throw new Error('Failed to load')
      const data = await res.json()
      const items: Notification[] = data.notifications || []
      const next = append ? [...notifications, ...items] : items
      const bounded = next.slice(0, limit)
      setNotifications(bounded)
      setNextBeforeId(bounded.length >= limit ? null : data.next_before_id)
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
    load()
  }, [limit])

  function toggleMessage(id: number) {
    setExpandedMessages(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  if (loading) return <div className="wm-loading">Loading...</div>

  return (
    <div>
      {error && (
        <>
          <div className="wm-error">{error}</div>
          <button className="wm-btn" onClick={() => load()}>Retry</button>
        </>
      )}
      {!error && notifications.length === 0 && <div className="wm-empty">No notifications</div>}
      {notifications.length > 0 && (
        <ul className="wm-notification-list">
          {notifications.map(item => (
            <li key={item.id} className={`wm-notification-item type-${TYPE_STYLE[item.event_type] || 'info'}`}>
              <div className="wm-notification-card">
                <div className="wm-notification-title">
                  <span className={`wm-tag wm-tag-${TYPE_STYLE[item.event_type] || 'info'}`}>
                    {TYPE_LABEL[item.event_type] || item.event_type}
                  </span>
                  <strong>{item.city}</strong>
                  {item.temperature_label && <span>{item.temperature_label}</span>}
                  {item.outcome && <span>{item.outcome.toUpperCase()}</span>}
                </div>
                {item.event_slug && <p className="wm-cell-sub">{item.event_slug}</p>}
                {item.reason && <p>Reason: {item.reason}</p>}
                <div className="wm-notification-ts">{formatTimestamp(item.occurred_at)}</div>
                <button className="wm-btn-link" onClick={() => toggleMessage(item.id)} style={{ marginTop: 4, padding: '2px 0' }}>
                  {expandedMessages.has(item.id) ? 'Hide message' : 'Show message'}
                </button>
                {expandedMessages.has(item.id) && (
                  <div className="wm-notification-message">
                    <pre>{item.message}</pre>
                  </div>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {nextBeforeId && (
        <div className="wm-timeline-more">
          <button className="wm-btn" onClick={() => load(true)} disabled={loadingMore}>
            {loadingMore ? 'Loading...' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  )
}
