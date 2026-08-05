import { useState, useCallback, useEffect, useRef } from 'react'
import './Toast.css'

interface ToastItem {
  id: number
  message: string
  type: 'error' | 'info' | 'success'
  exiting?: boolean
}

let addToastGlobal: ((message: string, type?: 'error' | 'info' | 'success') => void) | null = null

export function toast(message: string, type: 'error' | 'info' | 'success' = 'error') {
  addToastGlobal?.(message, type)
}

let idCounter = 0
const EXIT_DURATION = 250

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map())

  const startExit = useCallback((id: number) => {
    setToasts(prev => prev.map(t => t.id === id ? { ...t, exiting: true } : t))
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id))
    }, EXIT_DURATION)
  }, [])

  const addToast = useCallback((message: string, type: 'error' | 'info' | 'success' = 'error') => {
    const id = ++idCounter
    setToasts(prev => [...prev, { id, message, type }])
    const timer = setTimeout(() => {
      startExit(id)
      timersRef.current.delete(id)
    }, 3000)
    timersRef.current.set(id, timer)
  }, [startExit])

  useEffect(() => {
    addToastGlobal = addToast
    return () => { addToastGlobal = null }
  }, [addToast])

  const dismiss = (id: number) => {
    const timer = timersRef.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timersRef.current.delete(id)
    }
    startExit(id)
  }

  if (toasts.length === 0) return null

  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div
          key={t.id}
          className={`toast-item toast-${t.type}${t.exiting ? ' toast-exit' : ''}`}
          onClick={() => dismiss(t.id)}
        >
          {t.message}
        </div>
      ))}
    </div>
  )
}
