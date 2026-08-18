import { useState } from 'react'
import { apiFetch } from '../api'

interface SweepToLeader {
  count: number
  avg_ms: number
  min_ms: number
  max_ms: number
  p50_ms: number
  p90_ms: number
}

interface SignalQuality {
  sweep_signal_count: number
  sweep_entry_count: number
  sweep_confirmed_count: number
  sweep_timeout_count: number
  confirmation_rate: number
  sweep_to_leader: SweepToLeader | null
}

interface Execution {
  leader_signal_count: number
  leader_filled_count: number
  leader_fill_rate: number
  buy_fill_rate: number
  avg_latency_ms: number | null
  sweep_filled_total: number
}

interface Stats {
  days: number
  signal_quality: SignalQuality
  execution: Execution
}

interface Props {
  darkMode: boolean
}

export default function StrategyDashboard({ darkMode }: Props) {
  const [days, setDays] = useState(7)
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const fetchStats = async () => {
    setLoading(true)
    setError('')
    try {
      const res = await apiFetch(`/api/copy-trading/stats?days=${days}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setStats(data)
    } catch (e: any) {
      setError(e.message || 'Failed to fetch')
    } finally {
      setLoading(false)
    }
  }

  const card: React.CSSProperties = {
    background: darkMode ? '#1e293b' : '#ffffff',
    borderRadius: '12px',
    padding: '20px',
    border: `1px solid ${darkMode ? '#334155' : '#e2e8f0'}`,
  }

  const label: React.CSSProperties = {
    fontSize: '12px',
    color: darkMode ? '#94a3b8' : '#64748b',
    marginBottom: '4px',
  }

  const value: React.CSSProperties = {
    fontSize: '24px',
    fontWeight: 700,
    color: darkMode ? '#f8fafc' : '#0f172a',
  }

  const pct = (v: number) => `${(v * 100).toFixed(1)}%`

  return (
    <div style={{ flex: 1, overflow: 'auto', padding: '24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '24px' }}>
        <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: darkMode ? '#f8fafc' : '#0f172a' }}>
          Strategy Dashboard
        </h2>
        <select
          value={days}
          onChange={e => setDays(Number(e.target.value))}
          style={{
            padding: '6px 12px',
            borderRadius: '6px',
            border: `1px solid ${darkMode ? '#475569' : '#cbd5e1'}`,
            background: darkMode ? '#334155' : '#f1f5f9',
            color: darkMode ? '#f8fafc' : '#0f172a',
            fontSize: '14px',
          }}
        >
          <option value={1}>1 day</option>
          <option value={7}>7 days</option>
          <option value={14}>14 days</option>
          <option value={30}>30 days</option>
        </select>
        <button
          onClick={fetchStats}
          disabled={loading}
          style={{
            padding: '6px 16px',
            borderRadius: '6px',
            border: 'none',
            background: '#2563eb',
            color: '#fff',
            fontSize: '14px',
            cursor: loading ? 'not-allowed' : 'pointer',
            opacity: loading ? 0.6 : 1,
          }}
        >
          {loading ? 'Loading...' : 'Refresh'}
        </button>
        {error && <span style={{ color: '#ef4444', fontSize: '13px' }}>{error}</span>}
      </div>

      {stats && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
          {/* Signal Quality */}
          <div>
            <h3 style={{ margin: '0 0 12px', fontSize: '16px', fontWeight: 600, color: darkMode ? '#cbd5e1' : '#475569' }}>
              Signal Quality
            </h3>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '12px' }}>
              <div style={card}>
                <div style={label}>Sweep Signals</div>
                <div style={value}>{stats.signal_quality.sweep_signal_count}</div>
              </div>
              <div style={card}>
                <div style={label}>Sweep Entries</div>
                <div style={value}>{stats.signal_quality.sweep_entry_count}</div>
              </div>
              <div style={card}>
                <div style={label}>Confirmed</div>
                <div style={value}>{stats.signal_quality.sweep_confirmed_count}</div>
              </div>
              <div style={card}>
                <div style={label}>Timeout Exit</div>
                <div style={value}>{stats.signal_quality.sweep_timeout_count}</div>
              </div>
              <div style={card}>
                <div style={label}>Confirmation Rate</div>
                <div style={value}>{pct(stats.signal_quality.confirmation_rate)}</div>
              </div>
            </div>
          </div>

          {/* Sweep → Leader Delay */}
          {stats.signal_quality.sweep_to_leader && (
            <div>
              <h3
                style={{ margin: '0 0 12px', fontSize: '16px', fontWeight: 600, color: darkMode ? '#cbd5e1' : '#475569', cursor: 'help' }}
                title="Sweep 信号发出到 Leader 入场信号到达的时间差（仅统计 Leader 确认了的 sweep）。用于调优 sweep_confirm_window_ms 参数：window 应覆盖 P90 以保证大多数确认信号不被超时取消。"
              >
                Sweep → Leader Delay
              </h3>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '12px' }}>
                <div style={card}>
                  <div style={label}>Samples</div>
                  <div style={value}>{stats.signal_quality.sweep_to_leader.count}</div>
                </div>
                <div style={card}>
                  <div style={label}>Avg</div>
                  <div style={value}>{(stats.signal_quality.sweep_to_leader.avg_ms / 1000).toFixed(1)}s</div>
                </div>
                <div style={card}>
                  <div style={label}>P50</div>
                  <div style={value}>{(stats.signal_quality.sweep_to_leader.p50_ms / 1000).toFixed(1)}s</div>
                </div>
                <div style={card}>
                  <div style={label}>P90</div>
                  <div style={value}>{(stats.signal_quality.sweep_to_leader.p90_ms / 1000).toFixed(1)}s</div>
                </div>
                <div style={card}>
                  <div style={label}>Min</div>
                  <div style={value}>{(stats.signal_quality.sweep_to_leader.min_ms / 1000).toFixed(1)}s</div>
                </div>
                <div style={card}>
                  <div style={label}>Max</div>
                  <div style={value}>{(stats.signal_quality.sweep_to_leader.max_ms / 1000).toFixed(1)}s</div>
                </div>
              </div>
            </div>
          )}

          {/* Execution */}
          <div>
            <h3 style={{ margin: '0 0 12px', fontSize: '16px', fontWeight: 600, color: darkMode ? '#cbd5e1' : '#475569' }}>
              Execution
            </h3>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '12px' }}>
              <div style={card}>
                <div style={label}>Leader Signals</div>
                <div style={value}>{stats.execution.leader_signal_count}</div>
              </div>
              <div style={card}>
                <div style={label}>Leader Filled</div>
                <div style={value}>{stats.execution.leader_filled_count}</div>
              </div>
              <div style={card}>
                <div style={label}>Leader Fill Rate</div>
                <div style={value}>{pct(stats.execution.leader_fill_rate)}</div>
              </div>
              <div style={card}>
                <div style={label}>BUY Fill Rate</div>
                <div style={value}>{pct(stats.execution.buy_fill_rate)}</div>
              </div>
              <div style={card}>
                <div style={label}>Avg Latency</div>
                <div style={value}>{stats.execution.avg_latency_ms != null ? `${stats.execution.avg_latency_ms}ms` : '--'}</div>
              </div>
              <div style={card}>
                <div style={label}>Sweep Filled Total</div>
                <div style={value}>{stats.execution.sweep_filled_total}</div>
              </div>
            </div>
          </div>
        </div>
      )}

      {!stats && !loading && (
        <div style={{ color: darkMode ? '#64748b' : '#94a3b8', fontSize: '14px' }}>
          Click Refresh to load stats.
        </div>
      )}
    </div>
  )
}
