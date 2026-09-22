import type { CSSProperties } from 'react'

export type TradeLike = {
  phase?: 'entry' | 'exit' | 'closed' | string | null
  close_reason?: string | null
}

type Tone = 'blue' | 'amber' | 'green' | 'red' | 'slate' | 'violet'

type Badge = {
  icon: string
  label: string
  detail?: string
  tone: Tone
}

const TONES: Record<Tone, { fg: string; bg: string; border: string }> = {
  blue: { fg: '#2563eb', bg: '#dbeafe', border: '#bfdbfe' },
  amber: { fg: '#b45309', bg: '#fef3c7', border: '#fde68a' },
  green: { fg: '#15803d', bg: '#dcfce7', border: '#bbf7d0' },
  red: { fg: '#dc2626', bg: '#fee2e2', border: '#fecaca' },
  slate: { fg: '#475569', bg: '#f1f5f9', border: '#e2e8f0' },
  violet: { fg: '#7c3aed', bg: '#ede9fe', border: '#ddd6fe' },
}

const DARK_TONES: Record<Tone, { fg: string; bg: string; border: string }> = {
  blue: { fg: '#93c5fd', bg: '#1e3a8a66', border: '#2563eb88' },
  amber: { fg: '#fbbf24', bg: '#78350f66', border: '#d9770688' },
  green: { fg: '#86efac', bg: '#14532d66', border: '#16a34a88' },
  red: { fg: '#fca5a5', bg: '#7f1d1d66', border: '#dc262688' },
  slate: { fg: '#cbd5e1', bg: '#334155', border: '#475569' },
  violet: { fg: '#c4b5fd', bg: '#4c1d9566', border: '#7c3aed88' },
}

const CLOSE_REASON_LABELS: Record<string, string> = {
  normal_exit: '正常退出',
  stop_loss: '止损退出',
  force_exit: '强制关闭',
  buy_placement_failed: '买入未成交',
  no_cash: '资金不足',
  timeout_no_fill: '入场超时 · 无成交',
  dust_position: '低于最小下单量',
  exit_order_unfilled: '卖出未完成',
  market_settled: '市场已结算',
  sell_placement_failed: '卖出挂单失败',
  sell_fill_parse_error: '成交解析失败',
  fill_reconcile_failed: '成交校准失败',
  buy_cancel_failed: '买单撤单失败',
  sell_cancel_failed: '卖单撤单失败',
  tick_refresh_failed: 'tick 刷新失败',
  unknown_failure: '未知执行异常',
}

const AMBER_OUTCOME_REASONS = new Set([
  'stop_loss',
  'dust_position',
  'exit_order_unfilled',
])

const RED_FAILURE_REASONS = new Set([
  'buy_placement_failed',
  'sell_placement_failed',
  'sell_fill_parse_error',
  'fill_reconcile_failed',
  'buy_cancel_failed',
  'sell_cancel_failed',
  'tick_refresh_failed',
  'unknown_failure',
])

export function lifecycleStatus(trade: TradeLike) {
  return trade.phase || 'entry'
}

export function lifecycleBadge(trade: TradeLike): Badge {
  const phase = lifecycleStatus(trade)
  if (phase === 'entry') return { icon: '→', label: '入场中', tone: 'blue' }
  if (phase === 'exit') return { icon: '↗', label: '出场中', tone: 'amber' }
  return { icon: '■', label: '已结束', tone: 'slate' }
}

export function exitBadge(trade: TradeLike): Badge | null {
  if (trade.phase !== 'closed') return null

  const reason = trade.close_reason || ''
  if (RED_FAILURE_REASONS.has(reason)) {
    return {
      icon: '!',
      label: CLOSE_REASON_LABELS[reason] || '执行异常',
      tone: 'red',
    }
  }

  if (reason === 'normal_exit') return { icon: '✓', label: '正常退出', tone: 'green' }
  if (reason === 'force_exit') {
    return { icon: '■', label: CLOSE_REASON_LABELS[reason], tone: 'slate' }
  }
  if (reason === 'no_cash' || reason === 'timeout_no_fill' || reason === 'market_settled') {
    return { icon: '∅', label: CLOSE_REASON_LABELS[reason], tone: 'slate' }
  }
  if (AMBER_OUTCOME_REASONS.has(reason)) {
    const icon = reason === 'stop_loss' ? '↓' : '!'
    return { icon, label: CLOSE_REASON_LABELS[reason] || reason, tone: 'amber' }
  }
  if (reason) return { icon: '■', label: CLOSE_REASON_LABELS[reason] || reason, tone: 'slate' }
  return { icon: '■', label: '已结束', tone: 'slate' }
}

export function badgeStyle(tone: Tone, darkMode: boolean): CSSProperties {
  const palette = darkMode ? DARK_TONES[tone] : TONES[tone]
  return {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '5px',
    minHeight: '22px',
    maxWidth: '100%',
    padding: '2px 8px',
    borderRadius: '6px',
    border: `1px solid ${palette.border}`,
    background: palette.bg,
    color: palette.fg,
    fontSize: '11px',
    fontWeight: 650,
    lineHeight: 1.2,
    whiteSpace: 'nowrap',
    flexShrink: 0,
  }
}

export function iconStyle(tone: Tone, darkMode: boolean): CSSProperties {
  const palette = darkMode ? DARK_TONES[tone] : TONES[tone]
  return {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '16px',
    height: '16px',
    borderRadius: '4px',
    background: darkMode ? '#0f172a66' : '#ffffff99',
    color: palette.fg,
    fontSize: '10px',
    fontWeight: 800,
    letterSpacing: 0,
    flexShrink: 0,
  }
}
