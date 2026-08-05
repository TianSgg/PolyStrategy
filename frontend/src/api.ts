function defaultWsBase() {
  if (typeof window === 'undefined') {
    return 'ws://localhost:8000'
  }
  return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`
}

export const API_BASE = import.meta.env.VITE_API_BASE as string || ''
export const WS_BASE = import.meta.env.VITE_WS_BASE as string || defaultWsBase()

let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler
}

export async function apiFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers || {})
  const hasBody = init.body != null
  if (hasBody && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    credentials: 'include',
  })

  if (response.status === 401 && unauthorizedHandler) {
    unauthorizedHandler()
  }

  return response
}
