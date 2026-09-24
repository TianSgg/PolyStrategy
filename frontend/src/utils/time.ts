const TIMESTAMP_MS_KEYS = new Set([
  'captured_at_ms',
  'closed_at_ms',
  'enter_origin_ms',
  'occurred_at_ms',
  'order_response_at_ms',
  'ready_at_ms',
  'request_started_at_ms',
  'response_at_ms',
  'started_at_ms',
  'last_buy_fill_ms',
])

export function formatUtcTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '--'

  const stringValue = typeof value === 'string' ? value : undefined
  const normalizedString = stringValue && (
    stringValue.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(stringValue)
      ? stringValue
      : `${stringValue}Z`
  )
  const parsedValue = typeof value === 'number'
    ? value
    : /^\d+$/.test(value)
      ? Number(value)
      : normalizedString ?? value
  const date = new Date(parsedValue)

  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (part: number, width = 2) => String(part).padStart(width, '0')
  return [
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`,
  ].join(' ')
}

function isTimestampMsKey(key: string): boolean {
  return TIMESTAMP_MS_KEYS.has(key) || key.endsWith('_at_ms')
}

function removeRedundantBuyOrderTimes(key: string, value: unknown, step?: string): unknown {
  if (step !== 'buy_order_placed' || !value || typeof value !== 'object' || Array.isArray(value)) {
    return value
  }

  const hiddenKeys = key === 'order'
    ? new Set(['utc'])
    : key === 'pre_bbo' || key === 'aft_bbo'
      ? new Set(['utc', 'request_started_at', 'response_at'])
      : new Set<string>()

  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([childKey]) => !hiddenKeys.has(childKey)),
  )
}

function formatNestedValue(key: string, value: unknown, step?: string): unknown {
  value = removeRedundantBuyOrderTimes(key, value, step)

  if (isTimestampMsKey(key) && (typeof value === 'number' || typeof value === 'string')) {
    const numericValue = Number(value)
    if (Number.isFinite(numericValue)) return formatUtcTime(numericValue)
  }

  if (Array.isArray(value)) {
    return value.map(item => formatNestedValue(key, item, step))
  }

  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .map(([childKey, childValue]) => [childKey, formatNestedValue(childKey, childValue, step)]),
    )
  }

  return value
}

export function formatEventDetailValue(key: string, value: unknown, step?: string): string {
  const formatted = formatNestedValue(key, value, step)
  if (formatted && typeof formatted === 'object') return JSON.stringify(formatted)
  return String(formatted ?? '')
}
