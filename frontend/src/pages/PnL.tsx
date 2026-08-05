import { useEffect, useState, useRef, useCallback, useMemo } from 'react'
import { WS_BASE, apiFetch } from '../api'
import './PnL.css'

interface BalanceRecord {
  proxy_wallet: string
  total_value: number
  created_at: string
}

interface Adjustment {
  id: number
  proxy_wallet: string
  delta: number
  applied_at: string
  note: string
}

type TimeRange = '1D' | '1W' | '1M' | '1Y'

interface AccountInfo {
  id: number
  name: string
  proxy_wallet: string
}

interface LeaderInfo {
  id: number
  name: string
  proxy_wallet: string
}

type PnLTab = 'follower' | 'leader'

interface PnLProps {
  darkMode: boolean
  visible: boolean
  accounts: AccountInfo[]
}

const ACCOUNT_COLORS = [
  '#3b82f6', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4',
  '#84cc16', '#f97316', '#6366f1', '#14b8a6', '#e11d48',
]

const TOTAL_KEY = '__total__'

export default function PnL({ darkMode, visible, accounts }: PnLProps) {
  const [tab, setTab] = useState<PnLTab>('follower')
  const [range, setRange] = useState<TimeRange>('1D')
  const [records, setRecords] = useState<BalanceRecord[]>([])
  const [leaderRecords, setLeaderRecords] = useState<BalanceRecord[]>([])
  const [leaders, setLeaders] = useState<LeaderInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [tooltip, setTooltip] = useState<{ x: number; y: number; time: string; totalPnl: number; walletPnls: Record<string, number> } | null>(null)
  const [selectedFollowers, setSelectedFollowers] = useState<Set<string>>(new Set([TOTAL_KEY]))
  const [selectedLeaders, setSelectedLeaders] = useState<Set<string>>(new Set([TOTAL_KEY]))
  const [resizeKey, setResizeKey] = useState(0)
  const [adjustments, setAdjustments] = useState<Adjustment[]>([])
  const [adjModal, setAdjModal] = useState<{ wallet: string; time: string } | null>(null)
  const [adjDelta, setAdjDelta] = useState('')
  const [adjNote, setAdjNote] = useState('')
  const wsRef = useRef<WebSocket | null>(null)
  const chartRef = useRef<HTMLCanvasElement | null>(null)
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const chartLayoutRef = useRef<{ padding: { top: number; bottom: number; left: number; right: number }; chartW: number; chartH: number; minVal: number; valRange: number; w: number; h: number } | null>(null)
  const totalPnlPointsRef = useRef<Map<string, number>>(new Map())
  const walletPnlPointsRef = useRef<Map<string, Map<string, number>>>(new Map())

  // Active selection based on tab
  const selectedAccounts = tab === 'follower' ? selectedFollowers : selectedLeaders
  const setSelectedAccounts = tab === 'follower' ? setSelectedFollowers : setSelectedLeaders

  // Active records based on tab
  const activeRecords = tab === 'follower' ? records : leaderRecords

  const walletToName = useMemo(() => {
    const map: Record<string, string> = {}
    if (tab === 'follower') {
      for (const acc of accounts) {
        map[acc.proxy_wallet] = acc.name
      }
    } else {
      for (const l of leaders) {
        map[l.proxy_wallet] = l.name
      }
    }
    return map
  }, [accounts, leaders, tab])

  const walletToColor = useMemo(() => {
    const map: Record<string, string> = {}
    const items = tab === 'follower' ? accounts : leaders
    items.forEach((item, i) => {
      map[item.proxy_wallet] = ACCOUNT_COLORS[i % ACCOUNT_COLORS.length]
    })
    return map
  }, [accounts, leaders, tab])

  const fetchHistory = useCallback(async (r: TimeRange) => {
    setLoading(true)
    try {
      const res = await apiFetch(`/api/pnl/history?range=${r}`)
      if (res.ok) {
        const data = await res.json()
        setRecords(data.records || [])
      }
    } catch (e) {
      console.error('[PnL] fetch error', e)
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchLeaderHistory = useCallback(async (r: TimeRange) => {
    setLoading(true)
    try {
      const res = await apiFetch(`/api/pnl/leader-history?range=${r}`)
      if (res.ok) {
        const data = await res.json()
        setLeaderRecords(data.records || [])
      }
    } catch (e) {
      console.error('[PnL] fetch leader error', e)
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchLeaders = useCallback(async () => {
    try {
      const res = await apiFetch('/api/leaders')
      if (res.ok) {
        const data = await res.json()
        setLeaders((data.leaders || []).map((l: any) => ({ id: l.id, name: l.name, proxy_wallet: l.proxy_wallet })))
      }
    } catch (e) {
      console.error('[PnL] fetch leaders error', e)
    }
  }, [])

  const fetchAdjustments = useCallback(async () => {
    try {
      const res = await apiFetch('/api/pnl/adjustments')
      if (res.ok) {
        const data = await res.json()
        setAdjustments(data.adjustments || [])
      }
    } catch (e) {
      console.error('[PnL] fetch adjustments error', e)
    }
  }, [])

  useEffect(() => {
    if (visible) {
      if (tab === 'follower') {
        fetchHistory(range)
        fetchAdjustments()
      } else {
        fetchLeaderHistory(range)
        fetchLeaders()
      }
    }
  }, [visible, range, tab, fetchHistory, fetchLeaderHistory, fetchLeaders, fetchAdjustments])

  useEffect(() => {
    if (!visible) return
    const ws = new WebSocket(`${WS_BASE}/ws/pnl`)
    wsRef.current = ws

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.event_type === 'balance_update' && Array.isArray(msg.data)) {
          setRecords(prev => [...prev, ...msg.data])
        } else if (msg.event_type === 'leader_balance_update' && Array.isArray(msg.data)) {
          setLeaderRecords(prev => [...prev, ...msg.data])
        }
      } catch { /* ignore */ }
    }

    return () => {
      ws.close()
      wsRef.current = null
    }
  }, [visible])

  const { chartLines, pnl, walletPnls } = useMemo(() => {
    const cutoff = new Date(Date.now() - RANGE_MS[range]).toISOString()

    // Adjustments only apply to follower tab
    const adjByWallet = new Map<string, { applied_at: string; delta: number }[]>()
    if (tab === 'follower') {
      for (const adj of adjustments) {
        if (!adjByWallet.has(adj.proxy_wallet)) adjByWallet.set(adj.proxy_wallet, [])
        adjByWallet.get(adj.proxy_wallet)!.push({ applied_at: adj.applied_at, delta: adj.delta })
      }
      for (const arr of adjByWallet.values()) arr.sort((a, b) => a.applied_at.localeCompare(b.applied_at))
    }

    const getCumulativeDelta = (wallet: string, time: string): number => {
      const adjs = adjByWallet.get(wallet)
      if (!adjs) return 0
      let sum = 0
      for (const a of adjs) {
        if (a.applied_at <= time) sum += a.delta
        else break
      }
      return sum
    }

    const walletTimeMaps = new Map<string, Map<string, number>>()
    const allTimes = new Set<string>()

    for (const r of activeRecords) {
      if (r.created_at < cutoff) continue
      const adjusted = r.total_value - getCumulativeDelta(r.proxy_wallet, r.created_at)
      if (!walletTimeMaps.has(r.proxy_wallet)) walletTimeMaps.set(r.proxy_wallet, new Map())
      walletTimeMaps.get(r.proxy_wallet)!.set(r.created_at, adjusted)
      allTimes.add(r.created_at)
    }

    // Forward-fill: for each wallet, fill missing time points with its first known value
    const sortedTimes = Array.from(allTimes).sort()
    for (const [, wMap] of walletTimeMaps) {
      let firstVal: number | undefined
      for (const t of sortedTimes) {
        const v = wMap.get(t)
        if (v !== undefined) { firstVal = v; break }
      }
      if (firstVal === undefined) continue
      for (const t of sortedTimes) {
        if (wMap.has(t)) break
        wMap.set(t, firstVal)
      }
    }

    // Compute total by summing all wallets at each time point
    const totalTimeMap = new Map<string, number>()
    for (const t of sortedTimes) {
      let sum = 0
      for (const [, wMap] of walletTimeMaps) {
        const v = wMap.get(t)
        if (v !== undefined) sum += v
      }
      totalTimeMap.set(t, sum)
    }

    // Total: downsample + baseline + pnl
    const totalPoints = Array.from(totalTimeMap.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([time, value]) => ({ time, value }))
    const totalSampled = downsample(totalPoints, range)

    const pnlMap = new Map<string, number>()
    if (totalSampled.length > 0) {
      const baseline = totalSampled[0].value
      for (const p of totalSampled) pnlMap.set(p.time, p.value - baseline)
    }
    totalPnlPointsRef.current = pnlMap
    const totalPnl = totalSampled.length >= 2 ? totalSampled[totalSampled.length - 1].value - totalSampled[0].value : 0

    // Per-wallet: downsample + baseline + pnl
    const wPnls: Record<string, number> = {}
    const wPnlPoints = new Map<string, Map<string, number>>()
    const walletSampled = new Map<string, { time: string; value: number }[]>()
    for (const [wallet, wMap] of walletTimeMaps) {
      const wPoints = Array.from(wMap.entries())
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([time, value]) => ({ time, value }))
      const wSamp = downsample(wPoints, range)
      walletSampled.set(wallet, wSamp)
      wPnls[wallet] = wSamp.length >= 2 ? wSamp[wSamp.length - 1].value - wSamp[0].value : 0
      if (wSamp.length > 0) {
        const baseline = wSamp[0].value
        const m = new Map<string, number>()
        for (const p of wSamp) m.set(p.time, p.value - baseline)
        wPnlPoints.set(wallet, m)
      }
    }
    walletPnlPointsRef.current = wPnlPoints

    // Build chart lines (only selected)
    const lines: ChartLine[] = []
    if (selectedAccounts.has(TOTAL_KEY) && totalSampled.length > 0) {
      const baseline = totalSampled[0].value
      lines.push({ key: TOTAL_KEY, points: totalSampled.map(p => ({ time: p.time, value: p.value - baseline })) })
    }
    for (const wallet of selectedAccounts) {
      if (wallet === TOTAL_KEY) continue
      const wSamp = walletSampled.get(wallet)
      if (wSamp && wSamp.length > 0) {
        const baseline = wSamp[0].value
        lines.push({ key: wallet, points: wSamp.map(p => ({ time: p.time, value: p.value - baseline })) })
      }
    }

    return { chartLines: lines, pnl: totalPnl, walletPnls: wPnls }
  }, [activeRecords, range, selectedAccounts, adjustments, tab])

  const timePoints = useMemo(() => {
    const set = new Set<string>()
    for (const line of chartLines) {
      for (const p of line.points) set.add(p.time)
    }
    return Array.from(set).sort()
  }, [chartLines])

  // Draw chart
  useEffect(() => {
    const canvas = chartRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * dpr
    canvas.height = rect.height * dpr
    ctx.scale(dpr, dpr)
    const w = rect.width
    const h = rect.height
    ctx.clearRect(0, 0, w, h)

    if (timePoints.length < 2 || chartLines.length === 0) return

    let minVal = Infinity, maxVal = -Infinity
    for (const line of chartLines) {
      for (const p of line.points) {
        if (p.value < minVal) minVal = p.value
        if (p.value > maxVal) maxVal = p.value
      }
    }
    if (!isFinite(minVal)) { minVal = 0; maxVal = 1 }
    const yPad = (maxVal - minVal) * 0.1 || 1
    minVal -= yPad
    maxVal += yPad

    const padding = { top: 20, bottom: 30, left: 60, right: 20 }
    const chartW = w - padding.left - padding.right
    const chartH = h - padding.top - padding.bottom
    const valRange = maxVal - minVal || 1

    // Grid lines
    ctx.strokeStyle = darkMode ? '#334155' : '#e2e8f0'
    ctx.lineWidth = 0.5
    for (let i = 0; i <= 4; i++) {
      const y = padding.top + (chartH * i) / 4
      ctx.beginPath()
      ctx.moveTo(padding.left, y)
      ctx.lineTo(w - padding.right, y)
      ctx.stroke()
    }

    // Y-axis labels
    ctx.fillStyle = darkMode ? '#94a3b8' : '#64748b'
    ctx.font = '11px monospace'
    ctx.textAlign = 'right'
    for (let i = 0; i <= 4; i++) {
      const val = maxVal - (valRange * i) / 4
      const y = padding.top + (chartH * i) / 4
      ctx.fillText(`$${val.toFixed(0)}`, padding.left - 8, y + 4)
    }

    // Time index map
    const timeToIdx = new Map<string, number>()
    timePoints.forEach((t, i) => timeToIdx.set(t, i))

    // Draw lines
    for (const line of chartLines) {
      if (line.points.length < 2) continue
      const color = line.key === TOTAL_KEY
        ? (pnl >= 0 ? '#22c55e' : '#ef4444')
        : (walletToColor[line.key] || '#94a3b8')

      ctx.strokeStyle = color
      ctx.lineWidth = line.key === TOTAL_KEY ? 2.5 : 1.8
      ctx.lineJoin = 'round'
      ctx.beginPath()

      for (let i = 0; i < line.points.length; i++) {
        const tIdx = timeToIdx.get(line.points[i].time) ?? 0
        const x = padding.left + (chartW * tIdx) / (timePoints.length - 1)
        const y = padding.top + chartH - ((line.points[i].value - minVal) / valRange) * chartH
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()

      // Fill gradient only for total line
      if (line.key === TOTAL_KEY) {
        const gradient = ctx.createLinearGradient(0, padding.top, 0, h - padding.bottom)
        gradient.addColorStop(0, pnl >= 0 ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)')
        gradient.addColorStop(1, 'rgba(0,0,0,0)')
        const lastTIdx = timeToIdx.get(line.points[line.points.length - 1].time) ?? 0
        const lastX = padding.left + (chartW * lastTIdx) / (timePoints.length - 1)
        const firstTIdx = timeToIdx.get(line.points[0].time) ?? 0
        const firstX = padding.left + (chartW * firstTIdx) / (timePoints.length - 1)
        ctx.lineTo(lastX, padding.top + chartH)
        ctx.lineTo(firstX, padding.top + chartH)
        ctx.closePath()
        ctx.fillStyle = gradient
        ctx.fill()
      }
    }

    // X-axis time labels
    ctx.fillStyle = darkMode ? '#94a3b8' : '#64748b'
    ctx.font = '10px monospace'
    ctx.textAlign = 'center'
    const labelCount = Math.min(6, timePoints.length)
    for (let i = 0; i < labelCount; i++) {
      const idx = Math.floor((i * (timePoints.length - 1)) / (labelCount - 1))
      const x = padding.left + (chartW * idx) / (timePoints.length - 1)
      const d = new Date(timePoints[idx])
      const label = range === '1D'
        ? `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
        : `${(d.getMonth() + 1)}/${d.getDate()}`
      ctx.fillText(label, x, h - 8)
    }

    chartLayoutRef.current = { padding, chartW, chartH, minVal, valRange, w, h }
  }, [chartLines, timePoints, darkMode, range, pnl, walletToColor, resizeKey])

  useEffect(() => {
    const wrapper = wrapperRef.current
    if (!wrapper) return
    const ro = new ResizeObserver(() => setResizeKey(k => k + 1))
    ro.observe(wrapper)
    return () => ro.disconnect()
  }, [])

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = chartRef.current
    const layout = chartLayoutRef.current
    if (!canvas || !layout || timePoints.length < 2) {
      setTooltip(null)
      return
    }
    const rect = canvas.getBoundingClientRect()
    const mouseX = e.clientX - rect.left
    const { padding, chartW, chartH, minVal, valRange } = layout

    const relX = mouseX - padding.left
    if (relX < 0 || relX > chartW) {
      setTooltip(null)
      return
    }

    const idx = Math.round((relX / chartW) * (timePoints.length - 1))
    const clampedIdx = Math.max(0, Math.min(timePoints.length - 1, idx))
    const hoveredTime = timePoints[clampedIdx]

    const hoveredTotalPnl = totalPnlPointsRef.current.get(hoveredTime) ?? pnl
    const hoveredWalletPnls: Record<string, number> = {}
    for (const [wallet, m] of walletPnlPointsRef.current) {
      const v = m.get(hoveredTime)
      if (v !== undefined) hoveredWalletPnls[wallet] = v
    }

    const px = padding.left + (chartW * clampedIdx) / (timePoints.length - 1)
    const firstVal = hoveredTotalPnl
    const py = padding.top + chartH - ((firstVal - minVal) / valRange) * chartH

    setTooltip({ x: px, y: py, time: hoveredTime, totalPnl: hoveredTotalPnl, walletPnls: hoveredWalletPnls })
  }, [timePoints, pnl])

  const handleMouseLeave = useCallback(() => {
    setTooltip(null)
  }, [])

  const handleChartClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    if (tab !== 'follower') return
    const canvas = chartRef.current
    const layout = chartLayoutRef.current
    if (!canvas || !layout || timePoints.length < 2) return
    const rect = canvas.getBoundingClientRect()
    const mouseX = e.clientX - rect.left
    const { padding, chartW } = layout

    const relX = mouseX - padding.left
    if (relX < 0 || relX > chartW) return

    const idx = Math.round((relX / chartW) * (timePoints.length - 1))
    const clampedIdx = Math.max(0, Math.min(timePoints.length - 1, idx))
    const clickedTime = timePoints[clampedIdx]

    setAdjModal({ wallet: '', time: clickedTime })
    setAdjDelta('')
    setAdjNote('')
  }, [timePoints, tab])

  const handleAdjSubmit = async () => {
    if (!adjModal || !adjModal.wallet || !adjDelta) return
    const delta = parseFloat(adjDelta)
    if (isNaN(delta)) return
    await apiFetch('/api/pnl/adjustments', {
      method: 'POST',
      body: JSON.stringify({ proxy_wallet: adjModal.wallet, delta, applied_at: adjModal.time, note: adjNote }),
    })
    setAdjModal(null)
    fetchAdjustments()
  }

  const handleAdjDelete = async (id: number) => {
    await apiFetch(`/api/pnl/adjustments/${id}`, { method: 'DELETE' })
    fetchAdjustments()
  }

  const toggleAccount = (key: string) => {
    setSelectedAccounts(prev => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })
  }

  const availableWallets = useMemo(() => {
    const set = new Set<string>()
    for (const r of activeRecords) set.add(r.proxy_wallet)
    return Array.from(set)
  }, [activeRecords])

  return (
    <div className={`pnl-container ${darkMode ? 'dark' : ''}`}>
      <div className="pnl-header">
        <div className="pnl-summary">
          <div className="pnl-tab-switcher">
            <button className={`pnl-tab-btn ${tab === 'follower' ? 'active' : ''}`} onClick={() => setTab('follower')}>账户</button>
            <button className={`pnl-tab-btn ${tab === 'leader' ? 'active' : ''}`} onClick={() => setTab('leader')}>Leader</button>
          </div>
          <span className="pnl-label">Total PnL</span>
          {(() => {
            const displayPnl = tooltip ? tooltip.totalPnl : pnl
            return (
              <span className={`pnl-value ${displayPnl >= 0 ? 'positive' : 'negative'}`}>
                {displayPnl >= 0 ? '+$' : '-$'}{Math.abs(displayPnl).toFixed(2)}
              </span>
            )
          })()}
        </div>
        <div className="pnl-range-selector">
          {(['1D', '1W', '1M', '1Y'] as TimeRange[]).map(r => (
            <button
              key={r}
              className={`range-btn ${range === r ? 'active' : ''}`}
              onClick={() => setRange(r)}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      {/* Account/Leader filter */}
      <div className="pnl-account-filter">
        {(() => {
          const displayTotal = tooltip ? tooltip.totalPnl : pnl
          return (
            <div
              className={`pnl-account-chip ${selectedAccounts.has(TOTAL_KEY) ? 'active' : ''}`}
              style={selectedAccounts.has(TOTAL_KEY) ? { borderColor: displayTotal >= 0 ? '#22c55e' : '#ef4444' } : undefined}
              onClick={() => toggleAccount(TOTAL_KEY)}
            >
              <span className="pnl-chip-dot" style={{ background: displayTotal >= 0 ? '#22c55e' : '#ef4444' }} />
              <span className="pnl-chip-name">总计</span>
              <span className="pnl-chip-pnl" style={{ color: displayTotal >= 0 ? '#22c55e' : '#ef4444' }}>
                {displayTotal >= 0 ? '+' : '-'}${Math.abs(displayTotal).toFixed(2)}
              </span>
            </div>
          )
        })()}
        {availableWallets.map(wallet => {
          const name = walletToName[wallet] || wallet.slice(0, 8)
          const color = walletToColor[wallet] || '#94a3b8'
          const wp = tooltip?.walletPnls[wallet] ?? walletPnls[wallet] ?? 0
          return (
            <div
              key={wallet}
              className={`pnl-account-chip ${selectedAccounts.has(wallet) ? 'active' : ''}`}
              style={selectedAccounts.has(wallet) ? { borderColor: color } : undefined}
              onClick={() => toggleAccount(wallet)}
            >
              <span className="pnl-chip-dot" style={{ background: color }} />
              <span className="pnl-chip-name">{name}</span>
              <span className="pnl-chip-pnl" style={{ color: wp >= 0 ? '#22c55e' : '#ef4444' }}>
                {wp >= 0 ? '+' : '-'}${Math.abs(wp).toFixed(2)}
              </span>
            </div>
          )
        })}
      </div>

      <div className="pnl-chart-wrapper" ref={wrapperRef}>
        {loading && <div className="pnl-loading">Loading...</div>}
        {!loading && timePoints.length < 2 && <div className="pnl-empty">暂无数据</div>}
        <canvas
          ref={chartRef}
          className="pnl-chart-canvas"
          onMouseMove={handleMouseMove}
          onMouseLeave={handleMouseLeave}
          onClick={handleChartClick}
        />
        {tooltip && (
          <>
            <div className="pnl-crosshair-v" style={{ left: tooltip.x }} />
            <div className="pnl-crosshair-h" style={{ top: tooltip.y }} />
            <div className="pnl-tooltip" style={{ left: tooltip.x, top: tooltip.y }}>
              <div className="pnl-tooltip-time">
                {new Date(tooltip.time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
              </div>
              <div className="pnl-tooltip-row">
                <span className="pnl-tooltip-dot" style={{ background: tooltip.totalPnl >= 0 ? '#22c55e' : '#ef4444' }} />
                <span className="pnl-tooltip-label">总计</span>
                <span className="pnl-tooltip-val">${tooltip.totalPnl.toFixed(2)}</span>
              </div>
              {Object.entries(tooltip.walletPnls).filter(([wallet]) => selectedAccounts.has(wallet)).map(([wallet, val]) => (
                <div key={wallet} className="pnl-tooltip-row">
                  <span className="pnl-tooltip-dot" style={{ background: walletToColor[wallet] || '#94a3b8' }} />
                  <span className="pnl-tooltip-label">{walletToName[wallet] || wallet.slice(0, 8)}</span>
                  <span className="pnl-tooltip-val">${val.toFixed(2)}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Adjustment modal (follower only) */}
      {tab === 'follower' && adjModal && (
        <div className="pnl-adj-overlay" onClick={() => setAdjModal(null)}>
          <div className="pnl-adj-modal" onClick={e => e.stopPropagation()}>
            <div className="pnl-adj-title">添加余额调整</div>
            <div className="pnl-adj-time">
              时间点: {new Date(adjModal.time).toLocaleString('zh-CN')}
            </div>
            <label className="pnl-adj-field">
              <span>账户</span>
              <select value={adjModal.wallet} onChange={e => setAdjModal({ ...adjModal, wallet: e.target.value })}>
                <option value="">选择账户</option>
                {availableWallets.map(w => (
                  <option key={w} value={w}>{walletToName[w] || w.slice(0, 8)}</option>
                ))}
              </select>
            </label>
            <label className="pnl-adj-field">
              <span>金额 (充值正/提现负)</span>
              <input type="number" step="any" value={adjDelta} onChange={e => setAdjDelta(e.target.value)} placeholder="如 100 或 -50" />
            </label>
            <label className="pnl-adj-field">
              <span>备注</span>
              <input type="text" value={adjNote} onChange={e => setAdjNote(e.target.value)} placeholder="可选" />
            </label>
            <div className="pnl-adj-actions">
              <button className="pnl-adj-btn cancel" onClick={() => setAdjModal(null)}>取消</button>
              <button className="pnl-adj-btn confirm" onClick={handleAdjSubmit}>确认</button>
            </div>
          </div>
        </div>
      )}

      {/* Adjustments list (follower only) */}
      {tab === 'follower' && adjustments.length > 0 && (
        <div className="pnl-adj-list">
          <div className="pnl-adj-list-title">余额调整记录</div>
          {adjustments.map(adj => (
            <div key={adj.id} className="pnl-adj-item">
              <span className="pnl-adj-item-wallet">{walletToName[adj.proxy_wallet] || adj.proxy_wallet.slice(0, 8)}</span>
              <span className={`pnl-adj-item-delta ${adj.delta >= 0 ? 'positive' : 'negative'}`}>
                {adj.delta >= 0 ? '+' : ''}{adj.delta.toFixed(2)}
              </span>
              <span className="pnl-adj-item-time">{new Date(adj.applied_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>
              {adj.note && <span className="pnl-adj-item-note">{adj.note}</span>}
              <button className="pnl-adj-item-del" onClick={() => handleAdjDelete(adj.id)}>×</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const RANGE_MS: Record<TimeRange, number> = {
  '1D': 24 * 60 * 60 * 1000,
  '1W': 7 * 24 * 60 * 60 * 1000,
  '1M': 30 * 24 * 60 * 60 * 1000,
  '1Y': 365 * 24 * 60 * 60 * 1000,
}

const BUCKET_MS: Record<TimeRange, number> = {
  '1D': 0,
  '1W': 30 * 60 * 1000,
  '1M': 2 * 60 * 60 * 1000,
  '1Y': 24 * 60 * 60 * 1000,
}

interface ChartLine {
  key: string
  points: { time: string; value: number }[]
}

function bucketTime(isoTime: string, bucketMs: number): string {
  const ts = new Date(isoTime).getTime()
  const bucketed = Math.floor(ts / bucketMs) * bucketMs
  return new Date(bucketed).toISOString()
}

function downsample(points: { time: string; value: number }[], range: TimeRange): { time: string; value: number }[] {
  const bucket = BUCKET_MS[range]
  if (!bucket) return points
  const map = new Map<string, { time: string; value: number }>()
  for (const p of points) {
    const key = bucketTime(p.time, bucket)
    map.set(key, p)
  }
  return Array.from(map.values()).sort((a, b) => a.time.localeCompare(b.time))
}
