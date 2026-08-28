import { useState, useEffect, useMemo } from 'react'
import { apiFetch } from '../api'
import { useBalance } from '../contexts/BalanceContext'
import './Account.css'

type Account = {
  id: number
  name: string
  wallet_address: string
  proxy_wallet: string
  builder_api_key: string
  builder_code: string | null
  relayer_api_key: string | null
  signature_type: number
  created_at: string | null
}

type Position = {
  asset: string
  size: number
  avgPrice: number
  curPrice: number
  currentValue: number
  cashPnl: number
  percentPnl: number
  title: string
  outcome: string
  endDate: string
}

interface Props {
  darkMode: boolean
  setDarkMode: (v: boolean) => void
  visible?: boolean
  refreshKey?: number
  onAccountsLoaded?: (accounts: { id: number; name: string; proxy_wallet: string }[]) => void
}

export default function Account({ darkMode, visible, refreshKey = 0, onAccountsLoaded }: Props) {
  const { accountBalances, refreshAccountBalance, refreshAllAccounts } = useBalance()
  const [accounts, setAccounts] = useState<Account[]>([])
  const [positions, setPositions] = useState<Record<number, Position[]>>({})
  const [showAddForm, setShowAddForm] = useState(false)
  const [newPrivateKey, setNewPrivateKey] = useState('')
  const [showBuilderCodeModal, setShowBuilderCodeModal] = useState(false)
  const [builderCodeModalAccount, setBuilderCodeModalAccount] = useState<Account | null>(null)
  const [builderCodeInput, setBuilderCodeInput] = useState('')
  const [showRelayerKeyModal, setShowRelayerKeyModal] = useState(false)
  const [relayerKeyModalAccount, setRelayerKeyModalAccount] = useState<Account | null>(null)
  const [relayerKeyInput, setRelayerKeyInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [positionsCollapsed, setPositionsCollapsed] = useState<Record<number, boolean>>({})
  const [refreshingAccounts, setRefreshingAccounts] = useState<Set<number>>(new Set())


  useEffect(() => {
    fetchAccounts()
  }, [])

  useEffect(() => {
    if (visible) {
      fetchAccounts()
    }
  }, [visible])

  useEffect(() => {
    if (refreshKey > 0) {
      fetchAccounts()
    }
  }, [refreshKey])

  // 从 context 派生 accountSummary (by account id)
  const accountSummary = useMemo(() => {
    const map: Record<number, { balance: number; totalPositionValue: number; total_value: number }> = {}
    for (const acc of accounts) {
      const b = accountBalances[acc.proxy_wallet.toLowerCase()]
      if (b) map[acc.id] = { balance: b.balance, totalPositionValue: b.total_position_value, total_value: b.total_value }
    }
    return map
  }, [accounts, accountBalances])


  const fetchAccounts = async () => {
    try {
      const res = await apiFetch('/api/account/list')
      if (!res.ok) return
      const data = await res.json()
      const list = Array.isArray(data) ? data : []
      setAccounts(list)
      onAccountsLoaded?.(list.map((a: Account) => ({ id: a.id, name: a.name, proxy_wallet: a.proxy_wallet })))
      // 自动加载持仓和余额
      const wallets: string[] = []
      list.forEach((account: Account) => {
        if (account.proxy_wallet) {
          fetchPositions(account.proxy_wallet, account.id)
          wallets.push(account.proxy_wallet)
        }
      })
      refreshAllAccounts(wallets)
    } catch (e) {
      console.error('Failed to fetch accounts:', e)
    }
  }


  const handleAddAccount = async () => {
    if (!newPrivateKey) {
      setError('请输入私钥')
      return
    }
    setLoading(true)
    setError('')
    try {
      const res = await apiFetch('/api/account/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ private_key: newPrivateKey })
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to add account')
      }
      setNewPrivateKey('')
      setShowAddForm(false)
      fetchAccounts()
    } catch (e: any) {
      setError('添加失败: ' + (e.message || e))
    } finally {
        setLoading(false)
    }
  }

  const handleDeleteAccount = async (proxyWallet: string) => {
    if (!confirm('确定删除此账户？')) return
    await apiFetch(`/api/account/${proxyWallet}`, { method: 'DELETE' })
    fetchAccounts()
  }

  const handleUpdateBuilderCode = async () => {
    if (!builderCodeModalAccount) return
    try {
      const res = await apiFetch(`/api/account/${builderCodeModalAccount.id}/builder-code`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ builder_code: builderCodeInput.trim() })
      })
      if (!res.ok) throw new Error('Failed to update builder code')
      setShowBuilderCodeModal(false)
      setBuilderCodeInput('')
      setBuilderCodeModalAccount(null)
      fetchAccounts()
    } catch (e) {
      console.error('Failed to update builder code:', e)
    }
  }

  const startEditingBuilderCode = (account: Account) => {
    setBuilderCodeModalAccount(account)
    setBuilderCodeInput(account.builder_code || '')
    setShowBuilderCodeModal(true)
  }

  const handleUpdateRelayerKey = async () => {
    if (!relayerKeyModalAccount) return
    try {
      const res = await apiFetch(`/api/account/${relayerKeyModalAccount.id}/relayer-api-key`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ relayer_api_key: relayerKeyInput.trim() })
      })
      if (!res.ok) throw new Error('Failed to update relayer api key')
      setShowRelayerKeyModal(false)
      setRelayerKeyInput('')
      setRelayerKeyModalAccount(null)
      fetchAccounts()
    } catch (e) {
      console.error('Failed to update relayer api key:', e)
    }
  }

  const startEditingRelayerKey = (account: Account) => {
    setRelayerKeyModalAccount(account)
    setRelayerKeyInput(account.relayer_api_key || '')
    setShowRelayerKeyModal(true)
  }

  const fetchPositions = async (proxyWallet: string, accountId: number) => {
    try {
      const res = await fetch(`https://data-api.polymarket.com/positions?user=${proxyWallet}`)
      const data = await res.json()
      setPositions(prev => ({ ...prev, [accountId]: data }))
    } catch (e) {
      console.error('Failed to fetch positions:', e)
    }
  }

  const togglePositions = (accountId: number) => {
    setPositionsCollapsed(prev => ({ ...prev, [accountId]: prev[accountId] === false ? true : false }))
  }

  return (
    <div className="account-page" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="account-container">
        <div className="account-header">
          <h2 className="account-title">账户管理</h2>
          <button onClick={() => setShowAddForm(true)} className="btn btn-primary">
            + 添加账户
          </button>
        </div>

        {showAddForm && (
          <div className="card">
            <h3 style={{ margin: '0 0 20px 0' }}>添加新账户</h3>
            <div className="form-group">
              <label className="form-label">私钥（0x 开头）</label>
              <input
                type="password"
                value={newPrivateKey}
                onChange={e => setNewPrivateKey(e.target.value)}
                className="form-input"
                placeholder="0x..."
              />
              <span style={{ fontSize: '12px', color: '#94a3b8', marginTop: '4px', display: 'block' }}>
                输入 Polymarket 账户的私钥，签名类型将自动检测
              </span>
            </div>
            {error && <div className="error-msg">{error}</div>}
            <div className="form-actions">
              <button onClick={handleAddAccount} disabled={loading} className="btn btn-primary">
                {loading ? '添加中...' : '添加'}
              </button>
              <button onClick={() => { setShowAddForm(false); setError('') }} className="btn btn-outline">
                取消
              </button>
            </div>
          </div>
        )}

        {accounts.length === 0 ? (
          <div className="empty-state">
            暂无账户，请添加账户
          </div>
        ) : (
          <div className="accounts-list">
            {accounts.map(account => (
              <div key={account.id} className="card">
                <div className="account-item-header">
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flex: 1 }}>
                    <div>
                      <div className="account-name">
                        {account.name || '未命名'}
                        <span style={{ marginLeft: '8px', fontSize: '11px', padding: '2px 6px', borderRadius: '4px', background: account.signature_type === 3 ? '#7c3aed22' : '#3b82f622', color: account.signature_type === 3 ? '#7c3aed' : '#3b82f6' }}>
                          {account.signature_type === 3 ? 'POLY_1271' : 'SAFE'}
                        </span>
                      </div>
                      <span
                        className="account-meta"
                        style={{ cursor: 'pointer' }}
                        onClick={() => navigator.clipboard.writeText(account.wallet_address)}
                        title="点击复制地址"
                      >
                        {account.wallet_address.slice(0, 6)}...{account.wallet_address.slice(-4)}
                      </span>
                    </div>
                    {accountSummary[account.id] && (
                      <div style={{ display: 'flex', gap: '16px', marginLeft: '16px', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', fontSize: '13px' }}>
                        <span style={{ color: 'var(--success-text)', fontWeight: 500 }}>
                          Portfolio ${accountSummary[account.id].total_value?.toFixed(2)}
                        </span>
                        <span style={{ color: 'var(--success-text)' }}>
                          Cash ${accountSummary[account.id].balance?.toFixed(2)}
                        </span>
                      </div>
                    )}
                  </div>
                  <div className="account-actions">
                    <button onClick={() => startEditingBuilderCode(account)} className="btn btn-outline">
                      {account.builder_code ? '✓ Builder Code' : '+ Builder Code'}
                    </button>
                    <button onClick={() => startEditingRelayerKey(account)} className="btn btn-outline">
                      {account.relayer_api_key ? '✓ Relayer Key' : '+ Relayer Key'}
                    </button>
                    <button
                      disabled={refreshingAccounts.has(account.id)}
                      onClick={async () => {
                        setRefreshingAccounts(prev => new Set(prev).add(account.id))
                        try {
                          await Promise.all([
                            fetchPositions(account.proxy_wallet, account.id),
                            refreshAccountBalance(account.proxy_wallet),
                          ])
                        } finally {
                          setRefreshingAccounts(prev => {
                            const next = new Set(prev)
                            next.delete(account.id)
                            return next
                          })
                        }
                      }}
                      className="btn btn-primary"
                    >
                      {refreshingAccounts.has(account.id) ? '加载中...' : '刷新持仓'}
                    </button>
                    <button onClick={() => handleDeleteAccount(account.proxy_wallet)} className="btn btn-danger">
                      删除
                    </button>
                  </div>
                </div>
                {positions[account.id] && positions[account.id].length > 0 && (
                  <div className="positions-section">
                    <div className="positions-title" onClick={() => togglePositions(account.id)} style={{ cursor: 'pointer' }}>
                      持仓 ({positions[account.id]?.length}) {positionsCollapsed[account.id] === false ? '▼' : '▶'}
                    </div>
                    {positionsCollapsed[account.id] === false && (
                      <div className="positions-list">
                        {positions[account.id].map((pos, i) => {
                          const isYes = pos.outcome === 'Yes'
                          return (
                            <div key={i} className="position-item">
                              <div className="position-info">
                                <div className="position-name">
                                  {pos.title || pos.asset}
                                </div>
                                <div className="position-details">
                                  <span className={`badge ${isYes ? 'badge-yes' : 'badge-no'}`}>
                                    {pos.outcome}
                                  </span>
                                  <span className={`price-range ${isYes ? 'yes' : 'no'}`}>
                                    {(pos.avgPrice * 100).toFixed(2)}¢ → {(pos.curPrice * 100).toFixed(2)}¢
                                  </span>
                                  <span className="shares-amt">
                                    {pos.size?.toFixed(2)} shares
                                  </span>
                                </div>
                              </div>

                              <div className="position-pnl">
                                <span className={pos.cashPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                                  ${pos.cashPnl?.toFixed(2)}
                                </span>
                                <span className={pos.percentPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                                  {pos.percentPnl >= 0 ? '+' : ''}{pos.percentPnl?.toFixed(2)}%
                                </span>
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {showBuilderCodeModal && builderCodeModalAccount && (
          <div className="modal-overlay" onClick={() => setShowBuilderCodeModal(false)}>
            <div className="card" style={{ minWidth: '400px' }} onClick={e => e.stopPropagation()}>
              <h3 style={{ margin: '0 0 20px 0' }}>
                {builderCodeModalAccount.builder_code ? '编辑 Builder Code' : '添加 Builder Code'}
              </h3>
              <div className="form-group">
                <label className="form-label">账户</label>
                <div style={{ fontSize: '13px', color: 'var(--text-secondary)', fontFamily: 'ui-monospace, monospace' }}>
                  {builderCodeModalAccount.name || builderCodeModalAccount.wallet_address}
                </div>
              </div>
              <div className="form-group">
                <label className="form-label">Builder Code（bytes32）</label>
                <input
                  type="text"
                  value={builderCodeInput}
                  onChange={e => setBuilderCodeInput(e.target.value)}
                  className="form-input"
                  placeholder="0x..."
                />
              </div>
              <div className="form-actions">
                <button onClick={handleUpdateBuilderCode} className="btn btn-primary">保存</button>
                <button onClick={() => setShowBuilderCodeModal(false)} className="btn btn-outline">取消</button>
              </div>
            </div>
          </div>
        )}

        {showRelayerKeyModal && relayerKeyModalAccount && (
          <div className="modal-overlay" onClick={() => setShowRelayerKeyModal(false)}>
            <div className="card" style={{ minWidth: '400px' }} onClick={e => e.stopPropagation()}>
              <h3 style={{ margin: '0 0 20px 0' }}>
                {relayerKeyModalAccount.relayer_api_key ? '编辑 Relayer API Key' : '添加 Relayer API Key'}
              </h3>
              <div className="form-group">
                <label className="form-label">账户</label>
                <div style={{ fontSize: '13px', color: 'var(--text-secondary)', fontFamily: 'ui-monospace, monospace' }}>
                  {relayerKeyModalAccount.name || relayerKeyModalAccount.wallet_address}
                </div>
              </div>
              <div className="form-group">
                <label className="form-label">Relayer API Key</label>
                <input
                  type="text"
                  value={relayerKeyInput}
                  onChange={e => setRelayerKeyInput(e.target.value)}
                  className="form-input"
                  placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                />
              </div>
              <div className="form-actions">
                <button onClick={handleUpdateRelayerKey} className="btn btn-primary">保存</button>
                <button onClick={() => setShowRelayerKeyModal(false)} className="btn btn-outline">取消</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
