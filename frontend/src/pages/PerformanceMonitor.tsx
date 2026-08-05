import { useEffect, useState } from 'react'
import { WS_BASE, apiFetch } from '../api'
import './PerformanceMonitor.css'

type CacheSummary = {
  service: string
  cache: string
  label: string
  total: number
  summary: Record<string, unknown>
  sample: unknown[]
}

type ServiceSummary = {
  service: string
  label: string
  status: string
  total_items: number
  caches: CacheSummary[]
}

type SummaryPayload = {
  generated_at: string
  services: ServiceSummary[]
}

type DetailPayload = {
  service: string
  cache: string
  total: number
  limit: number
  offset: number
  truncated: boolean
  generated_at: string
  items: unknown[]
}

type SelectedCache = {
  service: string
  cache: string
  label: string
}

interface Props {
  darkMode: boolean
  visible?: boolean
}

function formatTime(ts?: string) {
  if (!ts) return '--'
  return ts
}

function compactJson(value: unknown) {
  if (value == null) return '--'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return JSON.stringify(value)
}

function JsonPreview({ value }: { value: unknown }) {
  return <pre className="perf-json">{JSON.stringify(value, null, 2)}</pre>
}

export default function PerformanceMonitor({ darkMode, visible }: Props) {
  const [summary, setSummary] = useState<SummaryPayload | null>(null)
  const [connected, setConnected] = useState(false)
  const [selected, setSelected] = useState<SelectedCache | null>(null)
  const [detail, setDetail] = useState<DetailPayload | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [error, setError] = useState('')
  const [offset, setOffset] = useState(0)
  const limit = 20


  const fetchSummary = async () => {
    const res = await apiFetch('/api/performance/cache/summary')
    const data = await res.json()
    if (res.ok && data.data) {
      setSummary(data.data)
    }
  }

  const fetchDetail = async (target: SelectedCache, nextOffset = 0) => {
    setLoadingDetail(true)
    setError('')
    try {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(nextOffset),
      })
      const res = await apiFetch(`/api/performance/cache/${target.service}/${target.cache}?${params.toString()}`)
      const data = await res.json()
      if (!res.ok) {
        throw new Error(data.detail || '详情加载失败')
      }
      setDetail(data.data)
      setOffset(nextOffset)
    } catch (e) {
      setError(e instanceof Error ? e.message : '详情加载失败')
      setDetail(null)
    } finally {
      setLoadingDetail(false)
    }
  }

  const selectCache = (cache: CacheSummary) => {
    const target = { service: cache.service, cache: cache.cache, label: cache.label }
    setSelected(target)
    void fetchDetail(target, 0)
  }

  useEffect(() => {
    if (!visible) return
    void fetchSummary()
  }, [visible])

  useEffect(() => {
    if (!visible) {
      setConnected(false)
      return
    }
    const client = new (window as any).PolymarketWSClient(`${WS_BASE}/ws/performance`)
    client.on('open', () => setConnected(true))
    client.on('close', () => setConnected(false))
    client.on('cache_summary', (message: { data?: SummaryPayload }) => {
      if (message.data) setSummary(message.data)
    })
    client.connect()
    return () => {
      client.disconnect()
      setConnected(false)
    }
  }, [visible])

  return (
    <div className="perf-page" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="perf-container">
        <div className="perf-header">
          <div>
            <h2 className="perf-title">性能监控</h2>
            <div className="perf-subtitle">Last update: {formatTime(summary?.generated_at)}</div>
          </div>
          <div className="perf-header-actions">
            <span className={`perf-conn ${connected ? 'connected' : 'disconnected'}`}>
              {connected ? 'Live' : 'Disconnected'}
            </span>
            <button className="perf-btn" onClick={fetchSummary}>刷新概览</button>
          </div>
        </div>

        <div className="perf-layout">
          <div className="perf-services">
            {(summary?.services || []).map(service => (
              <section className="perf-section" key={service.service}>
                <div className="perf-section-head">
                  <h3>{service.label}</h3>
                  <span>{service.total_items} items</span>
                </div>
                <div className="perf-cache-grid">
                  {service.caches.map(cache => (
                    <button
                      key={`${cache.service}.${cache.cache}`}
                      className={`perf-cache-card ${selected?.service === cache.service && selected?.cache === cache.cache ? 'active' : ''}`}
                      onClick={() => selectCache(cache)}
                    >
                      <span className="cache-name">{cache.label}</span>
                      <strong>{cache.total}</strong>
                      <span className="cache-meta">{compactJson(cache.summary)}</span>
                    </button>
                  ))}
                </div>
              </section>
            ))}
          </div>

          <aside className="perf-detail">
            <div className="perf-detail-head">
              <div>
                <h3>{selected ? selected.label : '缓存详情'}</h3>
                <span>
                  {detail ? `${detail.total} total · ${detail.offset + 1}-${Math.min(detail.offset + detail.items.length, detail.total)}` : '选择一个缓存查看详情'}
                </span>
              </div>
              {selected && (
                <button className="perf-btn" onClick={() => fetchDetail(selected, offset)} disabled={loadingDetail}>
                  {loadingDetail ? '加载中' : '刷新'}
                </button>
              )}
            </div>

            {error && <div className="perf-error">{error}</div>}

            {detail && (
              <>
                <div className="perf-items-scroll">
                  {detail.items.length === 0 ? (
                    <div className="perf-empty">暂无数据</div>
                  ) : (
                    detail.items.map((item, idx) => (
                      <div className="perf-item" key={`${detail.offset}-${idx}`}>
                        <JsonPreview value={item} />
                      </div>
                    ))
                  )}
                </div>
                <div className="perf-pager">
                  <button
                    className="perf-btn"
                    disabled={!selected || offset === 0 || loadingDetail}
                    onClick={() => selected && fetchDetail(selected, Math.max(0, offset - limit))}
                  >
                    上一页
                  </button>
                  <span className="perf-pager-info">
                    {detail.offset + 1}-{Math.min(detail.offset + detail.items.length, detail.total)} / {detail.total}
                  </span>
                  <button
                    className="perf-btn"
                    disabled={!selected || !detail.truncated || loadingDetail}
                    onClick={() => selected && fetchDetail(selected, offset + limit)}
                  >
                    下一页
                  </button>
                </div>
              </>
            )}
          </aside>
        </div>
      </div>
    </div>
  )
}
