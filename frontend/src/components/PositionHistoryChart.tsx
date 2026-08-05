import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'

export interface PositionHistoryChartPoint {
  created_at: string
  createdAtMs: number
  share_ratio: number
  leader_position: number
  follower_position: number
  follower_pending_buy: number
  follower_pending_sell: number
  leader_value: number
  follower_value: number
  source: string
  side: string | null
  event_size: number | null
  event_price: number | null
  order_id: string | null
  leader_tx_hash: string | null
}

interface PositionHistoryChartProps {
  points: PositionHistoryChartPoint[]
  normalized: boolean
  darkMode: boolean
}

const font =
  'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'

const toFiniteNumber = (value: unknown, fallback = 0) => {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : fallback
}

const safeString = (value: unknown) => String(value ?? '')

const formatNumber = (value?: unknown) => {
  if (value == null) return '-'
  const n = toFiniteNumber(value, Number.NaN)
  if (Number.isNaN(n)) return '-'
  return n.toFixed(4)
}

const formatAxisTime = (timestamp: number | string) => {
  if (!timestamp) return ''
  const time = typeof timestamp === 'number' ? timestamp : Date.parse(timestamp)
  if (!Number.isFinite(time)) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(time))
}

const escapeHtml = (value: unknown) =>
  safeString(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')

const buildOptionKey = (points: PositionHistoryChartPoint[], normalized: boolean, darkMode: boolean) =>
  `${normalized ? '1' : '0'}:${darkMode ? '1' : '0'}:${points
    .map(point => [
      toFiniteNumber(point.createdAtMs),
      toFiniteNumber(point.leader_value),
      toFiniteNumber(point.follower_value),
      toFiniteNumber(point.leader_position),
      toFiniteNumber(point.follower_position),
      toFiniteNumber(point.follower_pending_buy),
      toFiniteNumber(point.follower_pending_sell),
      toFiniteNumber(point.share_ratio),
      safeString(point.source),
      safeString(point.side),
      safeString(point.order_id),
      safeString(point.leader_tx_hash),
    ].join('|'))
    .join(';')}`

const getCurrentZoomRange = (chart: echarts.ECharts) => {
  try {
    const option = chart.getOption() as {
      dataZoom?: Array<{ start?: number; end?: number }>
    }
    const sliderZoom = option.dataZoom?.[1] ?? option.dataZoom?.[0]
    if (!sliderZoom) return undefined
    return {
      start: sliderZoom.start,
      end: sliderZoom.end,
    }
  } catch {
    return undefined
  }
}

function buildChartOption(points: PositionHistoryChartPoint[], normalized: boolean, darkMode: boolean): EChartsOption {
  const textColor = darkMode ? '#f8fafc' : '#0f172a'
  const mutedColor = darkMode ? '#94a3b8' : '#64748b'
  const borderColor = darkMode ? 'rgba(148, 163, 184, 0.22)' : 'rgba(15, 23, 42, 0.1)'
  const gridColor = darkMode ? 'rgba(148, 163, 184, 0.16)' : 'rgba(15, 23, 42, 0.08)'
  const tooltipBg = darkMode ? 'rgba(15, 23, 42, 0.96)' : 'rgba(255, 255, 255, 0.96)'
  const leaderColor = '#2563eb'
  const followerColor = '#16a34a'
  const leaderName = normalized ? 'Leader × ratio' : 'Leader'

  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: font, color: textColor },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: borderColor, width: 1 } },
      backgroundColor: tooltipBg,
      borderColor,
      borderWidth: 1,
      padding: [10, 12],
      extraCssText: 'box-shadow: 0 10px 28px rgba(0,0,0,0.16); border-radius: 8px;',
      textStyle: { color: textColor, fontSize: 12, fontFamily: font },
      formatter: (params: unknown) => {
        const arr = Array.isArray(params) ? params as Array<{ dataIndex: number; marker: string; seriesName: string; value: [number, number] }> : []
        const first = arr[0]
        const point = first ? points[first.dataIndex] : undefined
        if (!point) return ''

        const seriesRows = arr.map(item => {
          const value = Array.isArray(item.value) ? item.value[1] : undefined
          return `<div style="display:flex;align-items:center;gap:8px;margin-top:4px">
            ${item.marker}
            <span style="color:${mutedColor}">${escapeHtml(item.seriesName)}</span>
            <span style="margin-left:auto;font-variant-numeric:tabular-nums;font-weight:700">${formatNumber(value)}</span>
          </div>`
        }).join('')

        return `<div style="min-width:220px;line-height:1.55;font-family:${font}">
          <div style="font-weight:700;margin-bottom:6px">${escapeHtml(point.created_at)}</div>
          ${seriesRows}
          <div style="height:1px;background:${borderColor};margin:8px 0"></div>
          <div>leader raw: ${formatNumber(point.leader_position)}</div>
          <div>follower raw: ${formatNumber(point.follower_position)}</div>
          <div>ratio: ${point.share_ratio}</div>
          <div>pending buy/sell: ${formatNumber(point.follower_pending_buy)} / ${formatNumber(point.follower_pending_sell)}</div>
          <div>source: ${escapeHtml(point.source)}${point.side ? ` ${escapeHtml(point.side)}` : ''}</div>
          ${point.event_size != null ? `<div>event: ${formatNumber(point.event_size)} @ ${formatNumber(point.event_price)}</div>` : ''}
          ${point.order_id ? `<div>order: ${escapeHtml(point.order_id.slice(0, 12))}...</div>` : ''}
          ${point.leader_tx_hash ? `<div>tx: ${escapeHtml(point.leader_tx_hash.slice(0, 12))}...</div>` : ''}
        </div>`
      },
    },
    legend: {
      top: 4,
      right: 4,
      textStyle: { color: mutedColor, fontSize: 12, fontFamily: font },
      itemWidth: 16,
      itemHeight: 10,
    },
    grid: { left: 8, right: 18, top: 42, bottom: points.length > 12 ? 58 : 30, containLabel: true },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: borderColor } },
      axisTick: { show: false },
      axisLabel: {
        color: mutedColor,
        fontSize: 11,
        fontFamily: font,
        formatter: (value: number) => formatAxisTime(value),
      },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: mutedColor,
        fontSize: 11,
        fontFamily: font,
        formatter: (value: number) => {
          const abs = Math.abs(value)
          if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
          if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`
          return value.toFixed(0)
        },
      },
      splitLine: { lineStyle: { color: gridColor, type: 'solid' } },
      scale: true,
    },
    series: [
      {
        name: leaderName,
        type: 'line',
        smooth: 0.35,
        showSymbol: points.length <= 36,
        symbol: 'circle',
        symbolSize: 5,
        data: points.map(point => [toFiniteNumber(point.createdAtMs), toFiniteNumber(point.leader_value)]),
        lineStyle: { width: 2.4, color: leaderColor, cap: 'round', join: 'round' },
        itemStyle: { color: leaderColor },
        emphasis: { focus: 'series', lineStyle: { width: 3 } },
      },
      {
        name: 'Follower',
        type: 'line',
        smooth: 0.35,
        showSymbol: points.length <= 36,
        symbol: 'circle',
        symbolSize: 5,
        data: points.map(point => [toFiniteNumber(point.createdAtMs), toFiniteNumber(point.follower_value)]),
        lineStyle: { width: 2.4, color: followerColor, cap: 'round', join: 'round' },
        itemStyle: { color: followerColor },
        emphasis: { focus: 'series', lineStyle: { width: 3 } },
      },
    ],
    dataZoom:
      points.length > 12
        ? [
            { type: 'inside', start: 0, end: 100, filterMode: 'none' },
            {
              type: 'slider',
              start: 0,
              end: 100,
              filterMode: 'none',
              height: 24,
              bottom: 10,
              borderColor: 'transparent',
              backgroundColor: darkMode ? 'rgba(148, 163, 184, 0.12)' : 'rgba(15, 23, 42, 0.05)',
              fillerColor: darkMode ? 'rgba(96, 165, 250, 0.22)' : 'rgba(37, 99, 235, 0.16)',
              handleStyle: { color: leaderColor, borderColor: leaderColor },
              textStyle: { color: mutedColor, fontSize: 10, fontFamily: font },
              dataBackground: {
                areaStyle: { color: darkMode ? 'rgba(148, 163, 184, 0.12)' : 'rgba(15, 23, 42, 0.05)' },
                lineStyle: { opacity: 0.25, color: mutedColor },
              },
              selectedDataBackground: {
                areaStyle: { color: darkMode ? 'rgba(96, 165, 250, 0.16)' : 'rgba(37, 99, 235, 0.12)' },
                lineStyle: { opacity: 0.35, color: leaderColor },
              },
            },
          ]
        : [],
    animationDuration: 420,
    animationEasing: 'cubicOut',
  }
}

export default function PositionHistoryChart({ points, normalized, darkMode }: PositionHistoryChartProps) {
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInstance = useRef<echarts.ECharts | null>(null)
  const resizeObserverRef = useRef<ResizeObserver | null>(null)
  const lastOptionKeyRef = useRef('')
  const [chartError, setChartError] = useState('')

  useEffect(() => {
    return () => {
      resizeObserverRef.current?.disconnect()
      resizeObserverRef.current = null
      chartInstance.current?.dispose()
      chartInstance.current = null
    }
  }, [])

  useLayoutEffect(() => {
    const el = chartRef.current
    if (!el) return

    if (!chartInstance.current) {
      try {
        chartInstance.current = echarts.init(el, undefined, { renderer: 'canvas' })
      } catch (e) {
        console.error('[PositionHistoryChart] init failed:', e)
        setChartError('图表初始化失败')
        return
      }
    }
    const chart = chartInstance.current

    resizeObserverRef.current?.disconnect()
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    resizeObserverRef.current = ro

    const optionKey = buildOptionKey(points, normalized, darkMode)

    try {
      if (points.length && optionKey !== lastOptionKeyRef.current) {
        const zoomRange = getCurrentZoomRange(chart)
        const option = buildChartOption(points, normalized, darkMode)
        if (zoomRange && Array.isArray(option.dataZoom)) {
          option.dataZoom = option.dataZoom.map(zoom => ({ ...zoom, ...zoomRange }))
        }
        chart.setOption(option, { notMerge: true })
        lastOptionKeyRef.current = optionKey
        setChartError('')
      } else {
        if (!points.length) {
          chart.clear()
          lastOptionKeyRef.current = ''
          setChartError('')
        }
      }
    } catch (e) {
      console.error('[PositionHistoryChart] render failed:', e)
      setChartError('图表渲染失败')
    }

    const raf = requestAnimationFrame(() => chart.resize())

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      if (resizeObserverRef.current === ro) {
        resizeObserverRef.current = null
      }
    }
  }, [points, normalized, darkMode])

  return (
    <>
      <div ref={chartRef} className="position-history-chart-canvas" />
      {chartError && <div className="position-history-chart-error">{chartError}</div>}
    </>
  )
}
