import type { CSSProperties } from 'react'

export type TradeLike = {
  status?: string | null
  lifecycle_status?: string | null
  trade_outcome?: string | null
  close_reason?: string | null
  failure_reason?: string | null
  needs_attention?: boolean | number | null
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
  tick_exit: '正常退出',
  stop_loss: '止损退出',
  force_exit: '强制退出',
  buy_failed: '买入失败',
  no_cash: '资金不足',
  timeout_no_fill: '入场超时',
  sell_failed: '卖出失败',
}

const FAILURE_LABELS: Record<string, string> = {
  buy_placement_failed: '买入挂单失败',
  sell_placement_failed: '卖出挂单失败',
  sell_fill_parse_error: '成交解析失败',
  exit_order_unfilled: '仓位未清',
  fill_reconcile_failed: '成交校准失败',
  buy_cancel_failed: '买单撤单失败',
  sell_cancel_failed: '卖单撤单失败',
  dust_position: '低于最小下单量',
  tick_refresh_failed: 'tick刷新失败',
  invalid_tick_retry_exhausted: 'tick重试耗尽',
  same_error_repeated: '重复错误',
  total_failures_exceeded: '失败过多',
  deadline_exceeded: '退出超时',
  unknown_failure: '未知失败',
}

function isAttention(value: TradeLike['needs_attention']) {
  return value === true || value === 1
}

export function lifecycleStatus(trade: TradeLike) {
  const raw = trade.lifecycle_status || trade.status || 'entry_working'
  return raw === 'exit_failed' ? 'closed' : raw
}

export function tradeOutcome(trade: TradeLike) {
  if (trade.trade_outcome) return trade.trade_outcome
  if (trade.close_reason === 'no_cash') return 'skipped'
  if (trade.status === 'exit_failed') return 'failed'
  if (trade.close_reason === 'buy_failed' || trade.close_reason === 'sell_failed') return 'failed'
  return lifecycleStatus(trade) === 'closed' ? 'completed' : null
}

export function lifecycleBadge(trade: TradeLike): Badge {
  const status = lifecycleStatus(trade)
  if (status === 'entry_working') return { icon: '→', label: '入场中', tone: 'blue' }
  if (status === 'exit_working') return { icon: '↗', label: '出场中', tone: 'amber' }
  return { icon: '■', label: '已结束', tone: 'slate' }
}

export function exitBadge(trade: TradeLike): Badge | null {
  if (lifecycleStatus(trade) !== 'closed') return null

  const reason = trade.close_reason || ''
  const failed = tradeOutcome(trade) === 'failed'
  const attention = isAttention(trade.needs_attention)
  const failureDetail = trade.failure_reason ? FAILURE_LABELS[trade.failure_reason] || trade.failure_reason : undefined

  if (reason === 'no_cash' || tradeOutcome(trade) === 'skipped') {
    return { icon: '∅', label: CLOSE_REASON_LABELS[reason] || '已跳过', detail: '未下单', tone: 'slate' }
  }

  if (failed || attention) {
    return {
      icon: '!',
      label: CLOSE_REASON_LABELS[reason] || '执行失败',
      detail: failureDetail || (attention ? '需要处理' : undefined),
      tone: 'red',
    }
  }

  if (reason === 'normal_exit' || reason === 'tick_exit') return { icon: '✓', label: '正常退出', tone: 'green' }
  if (reason === 'timeout_no_fill') return { icon: '∅', label: '入场超时', detail: '无成交', tone: 'slate' }
  if (reason === 'stop_loss') return { icon: '↓', label: '止损退出', tone: 'amber' }
  if (reason === 'force_exit') return { icon: '↯', label: '强制退出', tone: 'violet' }
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
