import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'

export interface Trade {
  side: string
  size: number
  price: number
  size_matched: number
  status: string
  created_at: string
  createdAtMs: number
  leader_size: number
  leader_price: number
  err_msg?: string
}

interface TradeScatterChartProps {
  trades: Trade[]
  darkMode: boolean
  midPrice?: number | null
  showLeader?: boolean
  normalizePosition?: boolean
  shareRatio?: number
}

const font =
  'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'

const parseUtc8Timestamp = (value: unknown) => {
  if (!value) return 0
  const text = String(value)
  const normalized = text.replace(' ', 'T') + '+08:00'
  const ms = Date.parse(normalized)
  return Number.isFinite(ms) ? ms : 0
}

const buyColor = '#16a34a'
const sellColor = '#dc2626'
const midColor = '#3b82f6'
const leaderColor = '#f59e0b'
const followerColor = '#8b5cf6'

function buildOption(trades: Trade[], darkMode: boolean, midPrice?: number | null, showLeader?: boolean, normalizePosition?: boolean, shareRatio?: number): EChartsOption {
  const mutedColor = darkMode ? '#94a3b8' : '#64748b'
  const borderColor = darkMode ? 'rgba(148, 163, 184, 0.22)' : 'rgba(15, 23, 42, 0.1)'
  const gridColor = darkMode ? 'rgba(148, 163, 184, 0.16)' : 'rgba(15, 23, 42, 0.08)'
  const tooltipBg = darkMode ? 'rgba(15, 23, 42, 0.96)' : 'rgba(255, 255, 255, 0.96)'
  const textColor = darkMode ? '#f8fafc' : '#0f172a'

  const sorted = [...trades].sort((a, b) => a.createdAtMs - b.createdAtMs)
  const categories = sorted.map(t => t.created_at)

  const followerData: any[] = sorted.map((t, i) => ({
    value: [i, t.price],
    trade: t,
    itemStyle: {
      color: t.size_matched > 0 ? (t.side === 'BUY' ? buyColor : sellColor) : 'transparent',
      borderColor: t.size_matched > 0 ? undefined : (t.side === 'BUY' ? buyColor : sellColor),
      borderWidth: t.size_matched > 0 ? 0 : 2,
    },
  }))

  const leaderData: any[] = showLeader ? sorted.map((t, i) => ({
    value: [i, t.leader_price || null],
    trade: t,
  })) : []

  // 累计仓位
  let fPosAccum = 0
  let lPosAccum = 0
  const fPositionRaw: number[] = sorted.map(t => {
    if (t.size_matched > 0) fPosAccum += t.side === 'BUY' ? t.size_matched : -t.size_matched
    return Math.max(0, fPosAccum)
  })
  const lPositionRaw: number[] = sorted.map(t => {
    if (t.leader_size > 0) lPosAccum += t.side === 'BUY' ? t.leader_size : -t.leader_size
    return Math.max(0, lPosAccum)
  })
  const ratio = shareRatio || 1
  const positionData = fPositionRaw
  const lPositionData = normalizePosition ? lPositionRaw.map(v => v * ratio) : lPositionRaw
  const posColor = darkMode ? 'rgba(99, 102, 241, 0.5)' : 'rgba(99, 102, 241, 0.35)'
  const lPosColor = darkMode ? 'rgba(251, 146, 60, 0.5)' : 'rgba(251, 146, 60, 0.35)'

  if (midPrice != null) {
    categories.push('当前')
    followerData.push({
      value: [categories.length - 1, midPrice],
      trade: { side: 'MID', size: 0, price: midPrice, size_matched: 0, status: 'MIDPOINT', created_at: '当前', createdAtMs: 0, leader_size: 0, leader_price: 0 },
      itemStyle: { color: midColor },
      symbolSize: 12,
      symbol: 'diamond',
    })
    positionData.push(Math.max(0, fPosAccum))
    lPositionData.push(normalizePosition ? Math.max(0, lPosAccum) * ratio : Math.max(0, lPosAccum))
    if (showLeader) {
      leaderData.push({
        value: [categories.length - 1, midPrice],
        trade: { side: 'MID', size: 0, price: midPrice, size_matched: 0, status: 'MIDPOINT', created_at: '当前', createdAtMs: 0, leader_size: 0, leader_price: midPrice },
        itemStyle: { color: midColor },
        symbolSize: 12,
        symbol: 'diamond',
      })
    }
  }

  const hasSlider = sorted.length > 20
  const xLabelFormatter = (value: string) => {
    const parts = value.split(' ')
    return parts.length > 1 ? parts[1].slice(0, 5) : value.slice(5, 10)
  }

  const makeXAxis = (gridIdx: number, showLabel: boolean) => ({
    type: 'category' as const,
    data: categories,
    gridIndex: gridIdx,
    axisLine: { lineStyle: { color: borderColor } },
    axisTick: { show: false },
    axisLabel: showLabel
      ? { color: mutedColor, fontSize: 11, fontFamily: font, rotate: sorted.length > 10 ? 30 : 0, formatter: xLabelFormatter }
      : { show: false },
    splitLine: { show: false },
  })

  const makeYAxis = (gridIdx: number, name: string, nameColor: string) => ({
    type: 'value' as const,
    scale: true,
    gridIndex: gridIdx,
    name,
    nameTextStyle: { color: nameColor, fontSize: 11, fontFamily: font },
    nameLocation: 'end' as const,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: mutedColor, fontSize: 11, fontFamily: font },
    splitLine: { lineStyle: { color: gridColor, type: 'dashed' as const } },
  })

  const tooltipFormatter = (params: any) => {
    const items = Array.isArray(params) ? params : [params]
    const followerItem = items.find((p: any) => p.seriesName === 'Follower')
    const trade = followerItem?.data?.trade as Trade | undefined
    if (!trade) {
      const leaderItem = items.find((p: any) => p.seriesName === 'Leader')
      const lt = leaderItem?.data?.trade as Trade | undefined
      if (!lt) return ''
      return `<div style="line-height:1.6"><div style="font-weight:700">${lt.created_at}</div><div><span style="color:${leaderColor}">L</span> ${lt.leader_size.toFixed(2)} @ ${lt.leader_price.toFixed(4)}</div></div>`
    }
    if (trade.side === 'MID') {
      return `<div style="line-height:1.6"><span style="color:${midColor};font-weight:700">Midpoint</span> ${trade.price.toFixed(4)}</div>`
    }
    const sideColor = trade.side === 'BUY' ? buyColor : sellColor
    let html = `<div style="min-width:130px;line-height:1.6"><div style="font-weight:700;margin-bottom:2px">${trade.created_at}</div><div><span style="color:${sideColor};font-weight:700">${trade.side}</span></div>`
    if (showLeader && trade.leader_price > 0) {
      html += `<div><span style="color:${leaderColor}">L</span> ${trade.leader_size.toFixed(2)} @ ${trade.leader_price.toFixed(4)}</div>`
    }
    html += `<div><span style="color:${followerColor}">F</span> ${trade.size.toFixed(2)} @ ${trade.price.toFixed(4)}</div>`
    html += `<div style="opacity:0.7">成交 ${trade.size_matched.toFixed(2)} · ${trade.status}</div>`
    if (trade.err_msg) {
      html += `<div style="opacity:0.6;font-size:11px;color:#f59e0b">${trade.err_msg}</div>`
    }
    html += `</div>`
    return html
  }

  if (showLeader) {
    return {
      backgroundColor: 'transparent',
      textStyle: { fontFamily: font, color: textColor },
      axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
      tooltip: {
        trigger: 'axis',
        backgroundColor: tooltipBg,
        borderColor,
        borderWidth: 1,
        padding: [8, 10],
        extraCssText: 'box-shadow: 0 4px 16px rgba(0,0,0,0.12); border-radius: 6px;',
        textStyle: { color: textColor, fontSize: 12, fontFamily: font },
        axisPointer: { type: 'cross', crossStyle: { color: mutedColor } },
        formatter: tooltipFormatter,
      },
      grid: [
        { left: 50, right: 50, top: 24, bottom: '53%' },
        { left: 50, right: 50, top: '53%', bottom: hasSlider ? 52 : 28 },
      ],
      xAxis: [makeXAxis(0, false), makeXAxis(1, true)],
      yAxis: [
        makeYAxis(0, 'Leader', leaderColor),
        makeYAxis(1, 'Follower', followerColor),
        { type: 'value' as const, gridIndex: 1, name: '仓位', nameTextStyle: { color: mutedColor, fontSize: 11, fontFamily: font }, nameLocation: 'end' as const, position: 'right' as const, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: mutedColor, fontSize: 11, fontFamily: font }, splitLine: { show: false }, min: 0 },
      ],
      dataZoom: hasSlider
        ? [
            { type: 'inside', xAxisIndex: [0, 1] },
            { type: 'slider', xAxisIndex: [0, 1], height: 18, bottom: 4, borderColor, fillerColor: darkMode ? 'rgba(37, 99, 235, 0.2)' : 'rgba(37, 99, 235, 0.1)' },
          ]
        : [{ type: 'inside', xAxisIndex: [0, 1] }],
      series: [
        {
          name: 'Leader',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: leaderData,
          symbol: 'circle',
          symbolSize: 6,
          lineStyle: { color: leaderColor, width: 1.5, opacity: 0.6 },
          itemStyle: { color: leaderColor, borderWidth: 0 },
          connectNulls: false,
        },
        {
          name: 'Follower',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: followerData,
          symbol: 'circle',
          symbolSize: 8,
          lineStyle: { color: followerColor, width: 1.5, opacity: 0.6 },
          itemStyle: { borderWidth: 0 },
        },
        {
          name: 'L仓位',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: lPositionData,
          step: 'end',
          symbol: 'none',
          lineStyle: { color: lPosColor, width: 1.5 },
          areaStyle: { color: lPosColor, opacity: 0.15 },
          silent: true,
          z: 0,
        },
        {
          name: 'F仓位',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: positionData,
          step: 'end',
          symbol: 'none',
          lineStyle: { color: posColor, width: 1.5 },
          areaStyle: { color: posColor, opacity: 0.15 },
          silent: true,
          z: 0,
        },
      ],
    }
  }

  // Single chart (no leader)
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: font, color: textColor },
    tooltip: {
      trigger: 'axis',
      backgroundColor: tooltipBg,
      borderColor,
      borderWidth: 1,
      padding: [8, 10],
      extraCssText: 'box-shadow: 0 4px 16px rgba(0,0,0,0.12); border-radius: 6px;',
      textStyle: { color: textColor, fontSize: 12, fontFamily: font },
      axisPointer: { type: 'cross', crossStyle: { color: mutedColor } },
      formatter: tooltipFormatter,
    },
    grid: { left: 50, right: 50, top: 16, bottom: hasSlider ? 52 : 28 },
    xAxis: makeXAxis(0, true),
    yAxis: [
      makeYAxis(0, '', mutedColor),
      { type: 'value' as const, gridIndex: 0, name: '仓位', nameTextStyle: { color: mutedColor, fontSize: 11, fontFamily: font }, nameLocation: 'end' as const, position: 'right' as const, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: mutedColor, fontSize: 11, fontFamily: font }, splitLine: { show: false }, min: 0 },
    ],
    dataZoom: hasSlider
      ? [
          { type: 'inside', xAxisIndex: [0] },
          { type: 'slider', xAxisIndex: [0], height: 18, bottom: 4, borderColor, fillerColor: darkMode ? 'rgba(37, 99, 235, 0.2)' : 'rgba(37, 99, 235, 0.1)' },
        ]
      : [{ type: 'inside', xAxisIndex: [0] }],
    series: [
      {
        name: 'Follower',
        type: 'line',
        data: followerData,
        symbol: 'circle',
        symbolSize: 8,
        lineStyle: { color: followerColor, width: 1.5, opacity: 0.6 },
        itemStyle: { borderWidth: 0 },
      },
      {
        name: '仓位',
        type: 'line',
        yAxisIndex: 1,
        data: positionData,
        step: 'end',
        symbol: 'none',
        lineStyle: { color: posColor, width: 1.5 },
        areaStyle: { color: posColor, opacity: 0.15 },
        silent: true,
        z: 0,
      },
    ],
  }
}

function calcProfit(trades: Trade[], midPrice?: number | null, useLeader?: boolean) {
  const sorted = [...trades].sort((a, b) => a.createdAtMs - b.createdAtMs)
  const buyQueue: { qty: number; price: number }[] = []
  let realized = 0

  for (const t of sorted) {
    const qty = useLeader ? t.leader_size : t.size_matched
    const price = useLeader ? t.leader_price : t.price
    if (!qty || qty <= 0 || !price) continue
    if (t.side === 'BUY') {
      buyQueue.push({ qty, price })
    } else {
      let remaining = qty
      while (remaining > 0 && buyQueue.length > 0) {
        const front = buyQueue[0]
        const matched = Math.min(remaining, front.qty)
        realized += matched * (price - front.price)
        front.qty -= matched
        remaining -= matched
        if (front.qty <= 0) buyQueue.shift()
      }
    }
  }

  let unrealized = 0
  if (midPrice != null) {
    for (const lot of buyQueue) {
      unrealized += lot.qty * (midPrice - lot.price)
    }
  }

  return { realized, unrealized, total: realized + unrealized }
}

export default function TradeScatterChart({ trades, darkMode, midPrice, showLeader, normalizePosition, shareRatio }: TradeScatterChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!containerRef.current) return
    const chart = echarts.init(containerRef.current)
    chartRef.current = chart

    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(containerRef.current)

    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  const tradesKey = useRef('')
  useEffect(() => {
    if (!chartRef.current || trades.length === 0) return
    const key = trades.map(t => t.created_at + t.side).join('|') + `|mid:${midPrice}|leader:${showLeader}|norm:${normalizePosition}|ratio:${shareRatio}`
    const fullReset = key !== tradesKey.current
    tradesKey.current = key
    const option = buildOption(trades, darkMode, midPrice, showLeader, normalizePosition, shareRatio)
    chartRef.current.setOption(option, fullReset)
  }, [trades, darkMode, midPrice, showLeader, normalizePosition, shareRatio])

  const followerProfit = calcProfit(trades, midPrice, false)
  const leaderProfit = showLeader ? calcProfit(trades, midPrice, true) : null

  const fmt = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(4)}`
  const clr = (v: number) => v >= 0 ? '#16a34a' : '#dc2626'

  const hasMid = midPrice != null
  const cols = showLeader
    ? (hasMid ? 'auto auto auto auto' : 'auto auto auto')
    : (hasMid ? 'auto auto auto' : 'auto auto')

  return (
    <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'grid', gridTemplateColumns: cols, justifyContent: 'end', padding: '2px 8px', fontSize: 11, fontWeight: 600, flexShrink: 0, gap: '1px 12px', fontVariantNumeric: 'tabular-nums' }}>
        {leaderProfit && <>
          <span style={{ color: leaderColor }}>L</span>
          <span>已实现 <span style={{ color: clr(leaderProfit.realized) }}>{fmt(leaderProfit.realized)}</span></span>
          {hasMid && <span>未实现 <span style={{ color: clr(leaderProfit.unrealized) }}>{fmt(leaderProfit.unrealized)}</span></span>}
          <span>总计 <span style={{ color: clr(leaderProfit.total) }}>{fmt(leaderProfit.total)}</span></span>
        </>}
        {showLeader && <span style={{ color: followerColor }}>F</span>}
        <span>已实现 <span style={{ color: clr(followerProfit.realized) }}>{fmt(followerProfit.realized)}</span></span>
        {hasMid && <span>未实现 <span style={{ color: clr(followerProfit.unrealized) }}>{fmt(followerProfit.unrealized)}</span></span>}
        <span>总计 <span style={{ color: clr(followerProfit.total) }}>{fmt(followerProfit.total)}</span></span>
      </div>
      <div ref={containerRef} style={{ flex: 1, minHeight: 0 }} />
    </div>
  )
}

export { parseUtc8Timestamp }
