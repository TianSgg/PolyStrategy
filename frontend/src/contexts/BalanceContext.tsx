import { createContext, useContext, useState, useCallback, useRef, ReactNode } from 'react'
import { apiFetch } from '../api'

export interface AccountBalance {
  balance: number
  total_position_value: number
  total_value: number
}

export interface LeaderBalance {
  position_value: number
  available_balance: number
  total_balance: number
}

interface BalanceContextValue {
  accountBalances: Record<string, AccountBalance>
  leaderBalances: Record<string, LeaderBalance>
  refreshAccountBalance: (proxyWallet: string) => Promise<void>
  refreshLeaderBalance: (proxyWallet: string) => Promise<void>
  refreshAllAccounts: (wallets: string[]) => Promise<void>
  refreshAllLeaders: (wallets: string[]) => Promise<void>
}

const BalanceContext = createContext<BalanceContextValue | null>(null)

export function BalanceProvider({ children }: { children: ReactNode }) {
  const [accountBalances, setAccountBalances] = useState<Record<string, AccountBalance>>({})
  const [leaderBalances, setLeaderBalances] = useState<Record<string, LeaderBalance>>({})
  const inflightRef = useRef<Set<string>>(new Set())

  const refreshAccountBalance = useCallback(async (proxyWallet: string) => {
    const wallet = proxyWallet.toLowerCase()
    const key = `acc:${wallet}`
    if (inflightRef.current.has(key)) return
    inflightRef.current.add(key)
    try {
      const res = await apiFetch(`/api/account/${wallet}/balance`)
      if (res.ok) {
        const data = await res.json()
        setAccountBalances(prev => ({
          ...prev,
          [wallet]: {
            balance: data.balance,
            total_position_value: data.total_position_value,
            total_value: data.total_value,
          }
        }))
      }
    } finally {
      inflightRef.current.delete(key)
    }
  }, [])

  const refreshLeaderBalance = useCallback(async (proxyWallet: string) => {
    const wallet = proxyWallet.toLowerCase()
    const key = `ldr:${wallet}`
    if (inflightRef.current.has(key)) return
    inflightRef.current.add(key)
    try {
      const res = await apiFetch(`/api/leaders/balances?leader_address=${wallet}`)
      if (res.ok) {
        const data = await res.json()
        const info = data.balances?.[wallet]
        if (info) {
          setLeaderBalances(prev => ({
            ...prev,
            [wallet]: info,
          }))
        }
      }
    } finally {
      inflightRef.current.delete(key)
    }
  }, [])

  const refreshAllAccounts = useCallback(async (wallets: string[]) => {
    await Promise.all(wallets.map(w => refreshAccountBalance(w)))
  }, [refreshAccountBalance])

  const refreshAllLeaders = useCallback(async (wallets: string[]) => {
    await Promise.all(wallets.map(w => refreshLeaderBalance(w)))
  }, [refreshLeaderBalance])

  return (
    <BalanceContext.Provider value={{
      accountBalances,
      leaderBalances,
      refreshAccountBalance,
      refreshLeaderBalance,
      refreshAllAccounts,
      refreshAllLeaders,
    }}>
      {children}
    </BalanceContext.Provider>
  )
}

export function useBalance() {
  const ctx = useContext(BalanceContext)
  if (!ctx) throw new Error('useBalance must be used within BalanceProvider')
  return ctx
}
