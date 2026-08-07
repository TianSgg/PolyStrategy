import { useState, useEffect, useMemo } from 'react'
import { apiFetch } from '../api'
import { useBalance } from '../contexts/BalanceContext'
import { toast } from '../components/Toast'
import PositionHistoryChart, { type PositionHistoryChartPoint } from '../components/PositionHistoryChart'
import TradeScatterChart, { type Trade, parseUtc8Timestamp as parseTs } from '../components/TradeScatterChart'
import './CopyTrading.css'

interface CopyTradingConfig {
  id: number
  leader_proxy_wallet: string
  leader_name: string
  follower_proxy_wallet: string
  follower_name: string
  enabled: boolean
  gtd_expiration_sec: number
  buy_size: number
}

interface Account {
  id: number
  name: string
  wallet_address: string
  proxy_wallet: string
}

interface Leader {
  id: number
  proxy_wallet: string
  name: string
  created_at: string | null
  updated_at: string | null
}

interface PositionHistoryPoint {
  id: number
  created_at: string
  asset_id: string
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

interface PositionAssetOption {
  asset_id: string
  question: string
  outcome: string
  last_seen_at?: string
}

interface PositionHistoryState {
  assetId: string
  start: string
  end: string
  normalized: boolean
  loading: boolean
  error: string
  points: PositionHistoryPoint[]
}



interface Props {
  darkMode: boolean
  visible?: boolean
}

const defaultHistoryState = (): PositionHistoryState => ({
  assetId: '',
  start: '',
  end: '',
  normalized: false,
  loading: false,
  error: '',
  points: [],
})

const parseUtc8Timestamp = (value: unknown) => {
  if (!value) return 0
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : 0
  }
  const text = String(value)
  const normalized = text.includes('T') ? text : text.replace(' ', 'T')
  const withTimezone = /(?:Z|[+-]\d{2}:\d{2})$/.test(normalized)
    ? normalized
    : `${normalized}+08:00`
  const time = Date.parse(withTimezone)
  return Number.isNaN(time) ? 0 : time
}

const formatAssetOptionLabel = (asset: PositionAssetOption) => {
  const title = asset.question.trim()
  const outcome = asset.outcome.trim()
  const shortAsset = asset.asset_id.length > 18
    ? `${asset.asset_id.slice(0, 10)}...${asset.asset_id.slice(-6)}`
    : asset.asset_id
  if (title && outcome) return `${title} / ${outcome}`
  if (title) return title
  if (outcome) return `${outcome} (${shortAsset})`
  return shortAsset
}

const normalizePositionAsset = (asset: unknown): PositionAssetOption | null => {
  if (typeof asset === 'string') {
    return { asset_id: asset, question: '', outcome: '' }
  }
  if (!asset || typeof asset !== 'object') {
    return null
  }
  const item = asset as Record<string, unknown>
  const assetId = String(item.asset_id || item.assetId || '')
  if (!assetId) {
    return null
  }
  return {
    asset_id: assetId,
    question: String(item.question || ''),
    outcome: String(item.outcome || ''),
    last_seen_at: item.last_seen_at ? String(item.last_seen_at) : undefined,
  }
}

export default function CopyTrading({ darkMode, visible }: Props) {
  const { accountBalances, refreshAllAccounts } = useBalance()
  const [configs, setConfigs] = useState<CopyTradingConfig[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [leaders, setLeaders] = useState<Leader[]>([])
  const [showAddForm, setShowAddForm] = useState(false)
  const [refreshingConfigIds, setRefreshingConfigIds] = useState<Set<number>>(new Set())
  const [expandedHistoryIds, setExpandedHistoryIds] = useState<Set<number>>(new Set())
  const [positionAssets, setPositionAssets] = useState<Record<number, PositionAssetOption[]>>({})
  const [positionHistory, setPositionHistory] = useState<Record<number, PositionHistoryState>>({})
  const [assetFilterDays, setAssetFilterDays] = useState<Record<number, number>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [activeTab, setActiveTab] = useState<'configs' | 'leaders'>('configs')


  const configBalances = useMemo(() => {
    const map: Record<string, { total: number; position: number } | null> = {}
    for (const c of configs) {
      const fw = c.follower_proxy_wallet.toLowerCase()
      if (accountBalances[fw]) map[fw] = { total: accountBalances[fw].total_value, position: accountBalances[fw].total_position_value }
    }
    return map
  }, [configs, accountBalances])

  // Leader 管理状态
  const [showAddLeaderForm, setShowAddLeaderForm] = useState(false)
  const [editingLeaderId, setEditingLeaderId] = useState<number | null>(null)
  const [editingLeaderName, setEditingLeaderName] = useState('')
  const [newLeaderWallet, setNewLeaderWallet] = useState('')
  const [leaderError, setLeaderError] = useState('')

  // Form fields
  const [leaderAddr, setLeaderAddr] = useState('')
  const [followerAccountId, setFollowerAccountId] = useState<number | ''>('')
  const [showCustomFollowerInput, setShowCustomFollowerInput] = useState(false)
  const [customFollowerPrivateKey, setCustomFollowerPrivateKey] = useState('')
  const [showCustomLeaderInput, setShowCustomLeaderInput] = useState(false)

  useEffect(() => {
    fetchConfigs()
    fetchAccounts()
    fetchLeaders()
  }, [])

  // 当页面变为可见时刷新配置和 Leader 列表
  useEffect(() => {
    if (visible) {
      fetchConfigs()
      fetchLeaders()
    }
  }, [visible])

  // GTD expiration editing
  const [editingGtdConfigId, setEditingGtdConfigId] = useState<number | null>(null)
  const [editingGtdValue, setEditingGtdValue] = useState('')
  const [editingBuySizeConfigId, setEditingBuySizeConfigId] = useState<number | null>(null)
  const [editingBuySizeValue, setEditingBuySizeValue] = useState('')


  // 曲线时间范围
  const [historyRangeDays, setHistoryRangeDays] = useState<Record<number, number | 'custom'>>({})
  const [showCustomRange, setShowCustomRange] = useState<Set<number>>(new Set())

  // 右侧图表面板
  const [selectedConfigId, setSelectedConfigId] = useState<number | null>(null)
  const [scatterAssets, setScatterAssets] = useState<Record<number, PositionAssetOption[]>>({})
  const [scatterState, setScatterState] = useState<Record<number, { assetId: string; loading: boolean; trades: Trade[]; error: string; midPrice?: number | null }>>({})
  const [scatterAssetFilterDays, setScatterAssetFilterDays] = useState<Record<number, number>>({})
  const [scatterRangeDays, setScatterRangeDays] = useState<Record<number, number | 'custom'>>({})
  const [scatterCustomRange, setScatterCustomRange] = useState<Record<number, { start: string; end: string }>>({})
  const [scatterShowZeroMatched, setScatterShowZeroMatched] = useState<Record<number, boolean>>({})
  const [scatterNormalize, setScatterNormalize] = useState<Record<number, boolean>>({})



  const fetchConfigs = async () => {
    try {
      const res = await apiFetch('/api/copy-trading/configs')
      if (!res.ok) return
      const data = await res.json()
      const cfgs = data.configs || []
      setConfigs(cfgs)
      fetchConfigBalances(cfgs)
    } catch (e) {
      console.error('Failed to fetch configs:', e)
    }
  }

  const fetchAccounts = async () => {
    try {
      const res = await apiFetch('/api/account/list')
      if (!res.ok) return
      const data = await res.json()
      setAccounts(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error('Failed to fetch accounts:', e)
    }
  }

  const fetchLeaders = async () => {
    try {
      const res = await apiFetch('/api/leaders')
      if (!res.ok) return
      const data = await res.json()
      setLeaders(data.leaders || [])
    } catch (e) {
      console.error('Failed to fetch leaders:', e)
    }
  }

  const fetchConfigBalances = async (cfgs: CopyTradingConfig[]) => {
    const leaderWallets: string[] = []
    const followerWallets: string[] = []
    for (const c of cfgs) {
      leaderWallets.push(c.leader_proxy_wallet.toLowerCase())
      followerWallets.push(c.follower_proxy_wallet.toLowerCase())
    }
    await refreshAllAccounts([...new Set(followerWallets)])
  }

  const fetchLeaderBalances = async () => {}

  const handleAddConfig = async () => {
    setError('')

    if (!leaderAddr || !leaderAddr.startsWith('0x') || leaderAddr.length !== 42) {
      setError('请输入有效的 leader 地址')
      return
    }

    let followerWallet = ''

    if (showCustomFollowerInput) {
      if (!customFollowerPrivateKey || !customFollowerPrivateKey.startsWith('0x') || customFollowerPrivateKey.length !== 66) {
        setError('请输入有效的私钥（0x 开头，66 位）')
        return
      }
    } else {
      if (followerAccountId === '') {
        setError('请选择 follower 账户')
        return
      }
      const selectedAccount = accounts.find(a => a.id === followerAccountId)
      if (!selectedAccount) {
        setError('选择的账户不存在')
        return
      }
      if (selectedAccount.proxy_wallet.toLowerCase() === leaderAddr.toLowerCase()) {
        setError('Leader 和 Follower 不能是同一个地址')
        return
      }
      followerWallet = selectedAccount.proxy_wallet
    }

    setLoading(true)
    try {
      // 如果是手动输入的新 Leader 地址，先创建 Leader 记录
      if (showCustomLeaderInput) {
        const leaderRes = await apiFetch('/api/leaders', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ proxy_wallet: leaderAddr })
        })
        // 409 表示已存在，忽略；其他错误抛出
        if (!leaderRes.ok && leaderRes.status !== 409) {
          const data = await leaderRes.json()
          throw new Error(data.detail || 'Leader 创建失败')
        }
      }

      let followerWalletForConfig = followerWallet

      if (showCustomFollowerInput) {
        // 先创建账户
        const accountRes = await apiFetch('/api/account/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ private_key: customFollowerPrivateKey })
        })
        if (!accountRes.ok) {
          const data = await accountRes.json()
          throw new Error(data.detail || '账户创建失败')
        }
        const newAccount = await accountRes.json()
        followerWalletForConfig = newAccount.proxy_wallet
      }

      const res = await apiFetch('/api/copy-trading/configs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          leader_proxy_wallet: leaderAddr,
          follower_proxy_wallet: followerWalletForConfig,
        })
      })

      if (!res.ok) {
        const data = await res.json()
        throw new Error(data.detail || '创建失败')
      }

      setLeaderAddr('')
      setFollowerAccountId('')
      setShowAddForm(false)
      setShowCustomLeaderInput(false)
      setShowCustomFollowerInput(false)
      setCustomFollowerPrivateKey('')
      fetchConfigs()
      fetchLeaders()
      fetchAccounts()
    } catch (e: any) {
      setError(e.message || '创建失败')
    } finally {
      setLoading(false)
    }
  }


  const handleToggleEnabled = async (config: CopyTradingConfig) => {
    try {
      await apiFetch(`/api/copy-trading/configs/${config.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !config.enabled })
      })
      setConfigs(prev => prev.map(c =>
        c.id === config.id ? { ...c, enabled: !c.enabled } : c
      ))
    } catch (e) {
      console.error('Failed to toggle config:', e)
    }
  }


  const handleGtdClick = (config: CopyTradingConfig) => {
    setEditingGtdConfigId(config.id)
    setEditingGtdValue(String(Math.round((config.gtd_expiration_sec || 1800) / 60)))
  }

  const handleGtdSave = async (configId: number) => {
    const minutes = parseInt(editingGtdValue)
    if (isNaN(minutes) || minutes < 1 || minutes > 1440) {
      toast('过期时间需在 1 ~ 1440 分钟之间')
      return
    }
    const sec = minutes * 60
    try {
      await apiFetch(`/api/copy-trading/configs/${configId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gtd_expiration_sec: sec })
      })
      setConfigs(prev => prev.map(c =>
        c.id === configId ? { ...c, gtd_expiration_sec: sec } : c
      ))
    } catch (e) {
      console.error('Failed to update gtd expiration:', e)
    } finally {
      setEditingGtdConfigId(null)
    }
  }

  const handleGtdCancel = () => {
    setEditingGtdConfigId(null)
  }

  const handleBuySizeClick = (config: CopyTradingConfig) => {
    setEditingBuySizeConfigId(config.id)
    setEditingBuySizeValue(String(config.buy_size || 100))
  }

  const handleBuySizeSave = async (configId: number) => {
    const size = parseFloat(editingBuySizeValue)
    if (isNaN(size) || size < 1) {
      toast('买入数量需 >= 1')
      return
    }
    try {
      await apiFetch(`/api/copy-trading/configs/${configId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ buy_size: size })
      })
      setConfigs(prev => prev.map(c =>
        c.id === configId ? { ...c, buy_size: size } : c
      ))
    } catch (e) {
      console.error('Failed to update buy_size:', e)
    } finally {
      setEditingBuySizeConfigId(null)
    }
  }

  const handleBuySizeCancel = () => {
    setEditingBuySizeConfigId(null)
  }



  const handleDeleteConfig = async (configId: number) => {
    if (!confirm('确定删除此跟单配置？')) return
    try {
      await apiFetch(`/api/copy-trading/configs/${configId}`, {
        method: 'DELETE'
      })
      setConfigs(prev => prev.filter(c => c.id !== configId))
    } catch (e) {
      console.error('Failed to delete config:', e)
    }
  }

  const updateHistoryState = (configId: number, patch: Partial<PositionHistoryState>) => {
    setPositionHistory(prev => ({
      ...prev,
      [configId]: {
        ...(prev[configId] || defaultHistoryState()),
        ...patch,
      },
    }))
  }

  const loadPositionAssets = async (configId: number, days?: number) => {
    try {
      let url = `/api/copy-trading/configs/${configId}/position-assets`
      if (days && days > 0) {
        const since = new Date(Date.now() - days * 86400000).toISOString().slice(0, 19).replace('T', ' ')
        url += `?since=${encodeURIComponent(since)}`
      }
      const res = await apiFetch(url)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '获取 asset 失败')
      const seen = new Set<string>()
      const rawAssets: unknown[] = Array.isArray(data.assets) ? data.assets : []
      const assets: PositionAssetOption[] = rawAssets
        .map(normalizePositionAsset)
        .filter((asset): asset is PositionAssetOption => {
          if (!asset || seen.has(asset.asset_id)) {
            return false
          }
          seen.add(asset.asset_id)
          return true
        })
      setPositionAssets(prev => ({ ...prev, [configId]: assets }))
      return assets
    } catch (e) {
      console.error('Failed to fetch positions:', e)
      return []
    }
  }

  const handleAssetFilterChange = async (configId: number, days: number) => {
    setAssetFilterDays(prev => ({ ...prev, [configId]: days }))
    const assets = await loadPositionAssets(configId, days)
    const currentState = positionHistory[configId] || defaultHistoryState()
    const assetId = assets[0]?.asset_id || ''
    const nextState = { ...currentState, assetId }
    updateHistoryState(configId, nextState)
    if (assetId) {
      void fetchPositionHistory(configId, nextState)
    }
  }

  const handleHistoryRangeChange = (configId: number, days: number | 'custom') => {
    setHistoryRangeDays(prev => ({ ...prev, [configId]: days }))
    if (days === 'custom') {
      setShowCustomRange(prev => new Set(prev).add(configId))
      return
    }
    setShowCustomRange(prev => { const n = new Set(prev); n.delete(configId); return n })
    const start = new Date(Date.now() - days * 86400000).toISOString().slice(0, 16)
    const end = ''
    const currentState = positionHistory[configId] || defaultHistoryState()
    const nextState = { ...currentState, start, end }
    updateHistoryState(configId, nextState)
    void fetchPositionHistory(configId, nextState)
  }

  const fetchPositionHistory = async (configId: number, stateOverride?: PositionHistoryState) => {
    const state = stateOverride || positionHistory[configId] || defaultHistoryState()
    const assetId = state.assetId.trim()
    if (!assetId) {
      updateHistoryState(configId, { error: '请输入 asset id', points: [] })
      return
    }

    updateHistoryState(configId, { loading: true, error: '' })
    try {
      const params = new URLSearchParams({
        asset_id: assetId,
        normalized: String(state.normalized),
        limit: '2000',
      })
      if (state.start) params.set('start', state.start)
      if (state.end) params.set('end', state.end)
      const res = await apiFetch(`/api/copy-trading/configs/${configId}/position-history?${params.toString()}`)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '查询失败')
      updateHistoryState(configId, { loading: false, points: data.points || [] })
    } catch (e: any) {
      updateHistoryState(configId, { loading: false, error: e.message || '查询失败', points: [] })
    }
  }

  const handlePositionAssetChange = (configId: number, assetId: string) => {
    const currentState = positionHistory[configId] || defaultHistoryState()
    const nextState = { ...currentState, assetId }
    updateHistoryState(configId, nextState)
    if (assetId) {
      void fetchPositionHistory(configId, nextState)
    }
  }

  const handleTogglePositionHistory = async (configId: number) => {
    const willOpen = !expandedHistoryIds.has(configId)
    setExpandedHistoryIds(prev => {
      const next = new Set(prev)
      if (willOpen) next.add(configId)
      else next.delete(configId)
      return next
    })
    if (!willOpen) return

    const existingState = positionHistory[configId] || defaultHistoryState()
    updateHistoryState(configId, {})
    const assets = await loadPositionAssets(configId)
    const assetId = existingState.assetId || assets[0]?.asset_id || ''
    const nextState = { ...existingState, assetId }
    updateHistoryState(configId, nextState)
    if (assetId) {
      await fetchPositionHistory(configId, nextState)
    }
  }

  const handleViewOrders = async (configId: number) => {
    try {
      const res = await apiFetch(`/api/copy-trading/configs/${configId}/orders`)
      const data = await res.json()
      console.log('Orders:', data)
    } catch (e) {
      console.error('Failed to fetch orders:', e)
    }
  }

  const handleRefreshConfig = async (configId: number) => {
    if (refreshingConfigIds.has(configId)) return
    setRefreshingConfigIds(prev => new Set(prev).add(configId))
    try {
      const res = await apiFetch(`/api/copy-trading/configs/${configId}/sync`, { method: 'POST' })
      const data = await res.json()
      console.log('[Refresh] Sync result:', data)
    } catch (e) {
      console.error('Failed to refresh config:', e)
    } finally {
      setRefreshingConfigIds(prev => {
        const next = new Set(prev)
        next.delete(configId)
        return next
      })
    }
  }

  // ==================== 散点图面板 ====================

  const loadScatterAssets = async (configId: number, days?: number) => {
    try {
      let url = `/api/copy-trading/configs/${configId}/position-assets`
      if (days && days > 0) {
        const since = new Date(Date.now() - days * 86400000).toISOString().slice(0, 19).replace('T', ' ')
        url += `?since=${encodeURIComponent(since)}`
      }
      const res = await apiFetch(url)
      const data = await res.json()
      if (!res.ok) return []
      const seen = new Set<string>()
      const rawAssets: unknown[] = Array.isArray(data.assets) ? data.assets : []
      const assets: PositionAssetOption[] = rawAssets
        .map(normalizePositionAsset)
        .filter((asset): asset is PositionAssetOption => {
          if (!asset || seen.has(asset.asset_id)) return false
          seen.add(asset.asset_id)
          return true
        })
      setScatterAssets(prev => ({ ...prev, [configId]: assets }))
      return assets
    } catch {
      return []
    }
  }

  const handleScatterAssetFilterChange = async (configId: number, days: number) => {
    setScatterAssetFilterDays(prev => ({ ...prev, [configId]: days }))
    const assets = await loadScatterAssets(configId, days)
    const assetId = assets[0]?.asset_id || ''
    setScatterState(prev => ({ ...prev, [configId]: { assetId, loading: false, trades: [], error: '' } }))
    if (assetId) {
      const { start, end } = getScatterRange(configId)
      fetchTradeScatter(configId, assetId, start, end)
    }
  }

  const handleSelectConfig = async (configId: number) => {
    if (selectedConfigId === configId) {
      setSelectedConfigId(null)
      return
    }
    setSelectedConfigId(configId)
    if (!scatterAssets[configId]) {
      const days = scatterAssetFilterDays[configId] ?? 0
      const assets = await loadScatterAssets(configId, days)
      if (assets.length > 0 && !scatterState[configId]?.assetId) {
        const assetId = assets[0].asset_id
        setScatterState(prev => ({ ...prev, [configId]: { assetId, loading: false, trades: [], error: '' } }))
        const { start, end } = getScatterRange(configId)
        fetchTradeScatter(configId, assetId, start, end)
      }
    } else if (scatterState[configId]?.assetId) {
      const { start, end } = getScatterRange(configId)
      fetchTradeScatter(configId, scatterState[configId].assetId, start, end)
    }
  }

  const fetchMidPrice = async (assetId: string): Promise<number | null> => {
    try {
      const res = await apiFetch(`/api/market/price?asset_id=${assetId}`)
      if (res.ok) {
        const data = await res.json()
        return data.price ?? null
      }
    } catch {}
    return null
  }

  const fetchTradeScatter = async (configId: number, assetId: string, start?: string, end?: string) => {
    if (!assetId) return
    setScatterState(prev => ({ ...prev, [configId]: { ...prev[configId], assetId, loading: true, error: '' } }))
    try {
      const params = new URLSearchParams({ asset_id: assetId })
      if (start) params.set('start', start)
      if (end) params.set('end', end)
      const [res, midPrice] = await Promise.all([
        apiFetch(`/api/copy-trading/configs/${configId}/trade-scatter?${params.toString()}`),
        fetchMidPrice(assetId),
      ])
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const trades: Trade[] = (data.trades || []).map((t: any) => ({
        ...t,
        createdAtMs: parseTs(t.created_at),
      }))
      setScatterState(prev => ({ ...prev, [configId]: { assetId, loading: false, trades, error: '', midPrice } }))
    } catch (e: any) {
      setScatterState(prev => ({ ...prev, [configId]: { assetId, loading: false, trades: [], error: e.message || '加载失败' } }))
    }
  }

  const getScatterRange = (configId: number): { start?: string; end?: string } => {
    const days = scatterRangeDays[configId]
    if (!days || days === 'custom') {
      const custom = scatterCustomRange[configId]
      return { start: custom?.start || undefined, end: custom?.end || undefined }
    }
    return { start: new Date(Date.now() - days * 86400000).toISOString().slice(0, 16) }
  }

  const handleScatterRangeChange = (configId: number, days: number | 'custom') => {
    setScatterRangeDays(prev => ({ ...prev, [configId]: days }))
    if (days === 'custom') return
    const assetId = scatterState[configId]?.assetId
    if (!assetId) return
    const start = new Date(Date.now() - days * 86400000).toISOString().slice(0, 16)
    fetchTradeScatter(configId, assetId, start)
  }


  const handleScatterAssetChange = (configId: number, assetId: string) => {
    setScatterState(prev => ({ ...prev, [configId]: { ...prev[configId], assetId, loading: false, trades: [], error: '' } }))
    if (assetId) {
      const { start, end } = getScatterRange(configId)
      fetchTradeScatter(configId, assetId, start, end)
    }
  }

  // ==================== Leader 管理 ====================

  const handleAddLeader = async () => {
    setLeaderError('')
    if (!newLeaderWallet || !newLeaderWallet.startsWith('0x') || newLeaderWallet.length !== 42) {
      setLeaderError('请输入有效的钱包地址')
      return
    }

    setLoading(true)
    try {
      const res = await apiFetch('/api/leaders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ proxy_wallet: newLeaderWallet })
      })
      if (!res.ok) {
        const data = await res.json()
        throw new Error(data.detail || '添加失败')
      }
      setNewLeaderWallet('')
      setShowAddLeaderForm(false)
      fetchLeaders()
    } catch (e: any) {
      setLeaderError(e.message || '添加失败')
    } finally {
      setLoading(false)
    }
  }

  const handleUpdateLeader = async (id: number) => {
    if (!editingLeaderName.trim()) return
    try {
      await apiFetch(`/api/leaders/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: editingLeaderName.trim() })
      })
      setEditingLeaderId(null)
      fetchLeaders()
    } catch (e) {
      console.error('Failed to update leader:', e)
    }
  }

  const handleDeleteLeader = async (id: number) => {
    if (!confirm('确定删除此 Leader？')) return
    try {
      await apiFetch(`/api/leaders/${id}`, { method: 'DELETE' })
      fetchLeaders()
    } catch (e) {
      console.error('Failed to delete leader:', e)
    }
  }

  const formatAddress = (addr: string) => {
    if (!addr) return ''
    return `${addr.slice(0, 6)}...${addr.slice(-4)}`
  }

  const getAccountName = (accountId: number) => {
    const account = accounts.find(a => a.id === accountId)
    if (!account) return `#${accountId}`
    return account.name ? `${account.name} (${formatAddress(account.proxy_wallet)})` : formatAddress(account.proxy_wallet)
  }

  const handleLeaderSelectChange = (value: string) => {
    if (value === '__custom__') {
      setLeaderAddr('')
      setShowCustomLeaderInput(true)
    } else {
      setLeaderAddr(value)
      setShowCustomLeaderInput(false)
    }
  }

  const handleFollowerSelectChange = (value: string) => {
    if (value === '__custom__') {
      setCustomFollowerPrivateKey('')
      setShowCustomFollowerInput(true)
      setFollowerAccountId('')
    } else {
      setFollowerAccountId(value ? Number(value) : '')
      setShowCustomFollowerInput(false)
      setCustomFollowerPrivateKey('')
    }
  }

  return (
    <div className="copytrading-page" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="copytrading-header-wrap">
        <div className="page-header">
          <h2 className="page-title">跟单</h2>
          {activeTab === 'configs' && (
            <button onClick={() => setShowAddForm(true)} className="btn btn-primary">
              + 添加配置
            </button>
          )}
          {activeTab === 'leaders' && !showAddLeaderForm && (
            <button onClick={() => setShowAddLeaderForm(true)} className="btn btn-primary">
              + 添加 Leader
            </button>
          )}
        </div>
        <div className="tab-bar" style={{ marginBottom: 24, display: 'flex', gap: 8 }}>
          <button
            className={`btn ${activeTab === 'configs' ? 'btn-primary' : 'btn-outline'}`}
            onClick={() => { setActiveTab('configs'); fetchConfigs() }}
          >
            跟单配置
          </button>
          <button
            className={`btn ${activeTab === 'leaders' ? 'btn-primary' : 'btn-outline'}`}
            onClick={() => { setActiveTab('leaders'); fetchLeaders(); fetchLeaderBalances() }}
          >
            Leader 管理
          </button>
        </div>
      </div>

        {/* ==================== 跟单配置 Tab ==================== */}
        {activeTab === 'configs' && (
          <div className={selectedConfigId ? 'configs-tab-content configs-tab-content--wide' : 'configs-tab-content'}>
            {showAddForm && (
              <div className="card">
                <h3 className="card-title">添加跟单配置</h3>
                <div className="form-group">
                  <label className="form-label">Leader</label>
                  {!showCustomLeaderInput ? (
                    <select
                      value={leaderAddr}
                      onChange={e => handleLeaderSelectChange(e.target.value)}
                      onClick={fetchLeaders}
                      className="form-select"
                    >
                      <option value="">选择 Leader...</option>
                      {leaders.map(l => (
                        <option key={l.id} value={l.proxy_wallet}>
                          {l.name} ({formatAddress(l.proxy_wallet)})
                        </option>
                      ))}
                      <option value="__custom__">+ 输入新地址...</option>
                    </select>
                  ) : (
                    <input
                      type="text"
                      value={leaderAddr}
                      onChange={e => setLeaderAddr(e.target.value)}
                      className="form-input"
                      placeholder="0x..."
                    />
                  )}
                  {showCustomLeaderInput && (
                    <button
                      onClick={() => setShowCustomLeaderInput(false)}
                      className="btn btn-outline"
                      style={{ marginTop: 8, fontSize: 12, padding: '4px 8px' }}
                    >
                      返回选择已有 Leader
                    </button>
                  )}
                </div>
                <div className="form-group">
                  <label className="form-label">Follower 账户</label>
                  {!showCustomFollowerInput ? (
                    <select
                      value={followerAccountId}
                      onChange={e => handleFollowerSelectChange(e.target.value)}
                      onClick={fetchAccounts}
                      className="form-select"
                    >
                      <option value="">选择账户...</option>
                      {accounts.map(account => (
                        <option key={account.id} value={account.id}>
                          {getAccountName(account.id)}
                        </option>
                      ))}
                      <option value="__custom__">+ 输入新地址...</option>
                    </select>
                  ) : (
                    <input
                      type="text"
                      value={customFollowerPrivateKey}
                      onChange={e => setCustomFollowerPrivateKey(e.target.value)}
                      className="form-input"
                      placeholder="0x 私钥..."
                    />
                  )}
                  {showCustomFollowerInput && (
                    <button
                      onClick={() => { setShowCustomFollowerInput(false); setCustomFollowerPrivateKey('') }}
                      className="btn btn-outline"
                      style={{ marginTop: 8, fontSize: 12, padding: '4px 8px' }}
                    >
                      返回选择已有账户
                    </button>
                  )}
                </div>
                {error && <div className="error-msg">{error}</div>}
                <div className="form-actions">
                  <button onClick={handleAddConfig} disabled={loading} className="btn btn-primary">
                    {loading ? '创建中...' : '创建'}
                  </button>
                  <button onClick={() => { setShowAddForm(false); setError(''); setShowCustomLeaderInput(false) }} className="btn btn-outline">
                    取消
                  </button>
                </div>
              </div>
            )}

            {configs.length === 0 && !showAddForm ? (
              <div className="empty-state">
                <p>暂无跟单配置</p>
                <p className="empty-hint">点击上方「添加配置」创建第一个跟单配置</p>
              </div>
            ) : (
              <div className={`copy-trading-split ${selectedConfigId ? 'split-active' : ''}`}>
              <div className={`configs-list ${selectedConfigId ? 'configs-list--narrow' : ''}`}>
                {configs.map(config => {
                  const historyState = positionHistory[config.id] || defaultHistoryState()
                  const historyExpanded = expandedHistoryIds.has(config.id)
                  const assets = positionAssets[config.id] || []
                  const chartPoints: PositionHistoryChartPoint[] = historyState.points
                    .map(point => ({
                      ...point,
                      createdAtMs: parseUtc8Timestamp(point.created_at),
                      leader_value: point.leader_position,
                      follower_value: point.follower_position,
                    }))
                    .filter(point => point.createdAtMs > 0)
                    .sort((a, b) => a.createdAtMs - b.createdAtMs)
                  return (
                  <div key={config.id} className={`card config-card ${!config.enabled ? 'config-card--disabled' : ''} ${selectedConfigId === config.id ? 'config-card--selected' : ''}`}>
                    <div className="config-header">
                      <div className="config-info">
                        <div className="config-addresses">
                          <div className="address-row">
                            <span className="address-label">Leader:</span>
                            <a
                              href={`https://polymarket.com/profile/${config.leader_proxy_wallet}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              style={{ fontWeight: 600, color: 'var(--text-color)', textDecoration: 'none' }}
                              title={config.leader_proxy_wallet}
                            >
                              {config.leader_name}
                            </a>
                            {(() => {
                              const b = configBalances[config.leader_proxy_wallet.toLowerCase()]
                              return b ? (
                                <span className="address-value" style={{ color: '#4caf50' }}>
                                  持仓 ${b.position.toFixed(2)} / 总 ${b.total.toFixed(2)}
                                </span>
                              ) : <span className="address-value">...</span>
                            })()}
                          </div>
                          <div className="address-row">
                            <span className="address-label">Follower:</span>
                            <a
                              href={`https://polymarket.com/profile/${config.follower_proxy_wallet}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              style={{ fontWeight: 600, color: 'var(--text-color)', textDecoration: 'none' }}
                              title={config.follower_proxy_wallet}
                            >
                              {config.follower_name}
                            </a>
                            {(() => {
                              const b = configBalances[config.follower_proxy_wallet.toLowerCase()]
                              return b ? (
                                <span className="address-value" style={{ color: '#4caf50' }}>
                                  持仓 ${b.position.toFixed(2)} / 总 ${b.total.toFixed(2)}
                                </span>
                              ) : <span className="address-value">...</span>
                            })()}
                          </div>
                        </div>
                      </div>
                      <div className="config-actions">
                        <label className="toggle-switch">
                          <input
                            type="checkbox"
                            checked={config.enabled}
                            onChange={() => handleToggleEnabled(config)}
                          />
                          <span className="toggle-slider"></span>
                        </label>
                        <button onClick={() => handleSelectConfig(config.id)} className={`btn ${selectedConfigId === config.id ? 'btn-primary' : 'btn-outline'}`}>
                          图表
                        </button>
                        <button onClick={() => handleTogglePositionHistory(config.id)} className={`btn ${historyExpanded ? 'btn-primary' : 'btn-outline'}`}>
                          持仓
                        </button>
                        <button onClick={() => handleViewOrders(config.id)} className="btn btn-outline">订单</button>
                        <button
                          onClick={() => handleRefreshConfig(config.id)}
                          className={`btn btn-outline ${refreshingConfigIds.has(config.id) ? 'btn-loading' : ''}`}
                          disabled={refreshingConfigIds.has(config.id)}
                        >
                          {refreshingConfigIds.has(config.id) ? '刷新中...' : '刷新'}
                        </button>
                        <button onClick={() => handleDeleteConfig(config.id)} className="btn btn-danger">删除</button>
                      </div>
                    </div>
                    <div className="config-meta">
                      <span className="meta-item">ID: <strong>#{config.id}</strong></span>
                          <span className="meta-item price-anchor">
                            GTD:{' '}
                            <strong
                              className="ratio-text"
                              onClick={() => handleGtdClick(config)}
                              title="点击修改 GTD 过期时间"
                            >
                              {Math.round((config.gtd_expiration_sec || 1800) / 60)}min
                            </strong>
                            {editingGtdConfigId === config.id && (
                              <div className="price-popover">
                                <div className="price-popover-row">
                                  <input
                                    type="number"
                                    step="1"
                                    min="1"
                                    max="1440"
                                    value={editingGtdValue}
                                    onChange={e => setEditingGtdValue(e.target.value)}
                                    onKeyDown={e => {
                                      if (e.key === 'Enter') handleGtdSave(config.id)
                                      if (e.key === 'Escape') handleGtdCancel()
                                    }}
                                    autoFocus
                                  />
                                  <span style={{ fontSize: 11 }}>min</span>
                                </div>
                                <div className="price-popover-actions">
                                  <button onClick={() => handleGtdSave(config.id)} className="btn-save">✓</button>
                                  <button onClick={handleGtdCancel} className="btn-cancel">✕</button>
                                </div>
                              </div>
                            )}
                          </span>
                          <span className="meta-item price-anchor">
                            Size:{' '}
                            <strong
                              className="ratio-text"
                              onClick={() => handleBuySizeClick(config)}
                              title="点击修改固定买入数量"
                            >
                              {config.buy_size || 100}
                            </strong>
                            {editingBuySizeConfigId === config.id && (
                              <div className="price-popover">
                                <div className="price-popover-row">
                                  <input
                                    type="number"
                                    step="1"
                                    min="1"
                                    value={editingBuySizeValue}
                                    onChange={e => setEditingBuySizeValue(e.target.value)}
                                    onKeyDown={e => {
                                      if (e.key === 'Enter') handleBuySizeSave(config.id)
                                      if (e.key === 'Escape') handleBuySizeCancel()
                                    }}
                                    autoFocus
                                  />
                                  <span style={{ fontSize: 11 }}>shares</span>
                                </div>
                                <div className="price-popover-actions">
                                  <button onClick={() => handleBuySizeSave(config.id)} className="btn-save">✓</button>
                                  <button onClick={handleBuySizeCancel} className="btn-cancel">✕</button>
                                </div>
                              </div>
                            )}
                          </span>
                    </div>
                    {historyExpanded && (
                      <div className="position-history-panel">
                        <div className="position-history-controls">
                          <div className="ph-row">
                            <span className="ph-label">Asset</span>
                            <div className="ph-filter-buttons">
                              {[1, 2, 3, 0].map(d => (
                                <button
                                  key={d}
                                  className={`btn btn-sm ${(assetFilterDays[config.id] ?? 0) === d ? 'btn-primary' : 'btn-secondary'}`}
                                  onClick={() => handleAssetFilterChange(config.id, d)}
                                >
                                  {d === 0 ? '全部' : `${d}天`}
                                </button>
                              ))}
                            </div>
                            <select
                              className="form-select ph-asset-select"
                              value={historyState.assetId}
                              onChange={e => handlePositionAssetChange(config.id, e.target.value)}
                              disabled={assets.length === 0}
                            >
                              {assets.length === 0 && (
                                <option value="">暂无 asset</option>
                              )}
                              {assets.map(asset => (
                                <option key={asset.asset_id} value={asset.asset_id}>
                                  {formatAssetOptionLabel(asset)}
                                </option>
                              ))}
                            </select>
                          </div>
                          <div className="ph-row">
                            <span className="ph-label">范围</span>
                            <div className="ph-filter-buttons">
                              {[1, 3, 7].map(d => (
                                <button
                                  key={d}
                                  className={`btn btn-sm ${historyRangeDays[config.id] === d ? 'btn-primary' : 'btn-secondary'}`}
                                  onClick={() => handleHistoryRangeChange(config.id, d)}
                                >
                                  {d}天
                                </button>
                              ))}
                              <button
                                className={`btn btn-sm ${historyRangeDays[config.id] === 'custom' ? 'btn-primary' : 'btn-secondary'}`}
                                onClick={() => handleHistoryRangeChange(config.id, 'custom')}
                              >
                                {showCustomRange.has(config.id) && historyState.start
                                  ? `${historyState.start.slice(5, 10)} ~ ${historyState.end ? historyState.end.slice(5, 10) : '现在'}`
                                  : '自定义'}
                              </button>
                              {showCustomRange.has(config.id) && (
                                <>
                                  <input
                                    className="form-input form-input--sm"
                                    type="date"
                                    value={historyState.start ? historyState.start.slice(0, 10) : ''}
                                    onChange={e => {
                                      const start = e.target.value ? `${e.target.value}T00:00` : ''
                                      updateHistoryState(config.id, { start })
                                    }}
                                  />
                                  <span className="range-sep">~</span>
                                  <input
                                    className="form-input form-input--sm"
                                    type="date"
                                    value={historyState.end ? historyState.end.slice(0, 10) : ''}
                                    onChange={e => {
                                      const end = e.target.value ? `${e.target.value}T23:59` : ''
                                      updateHistoryState(config.id, { end })
                                    }}
                                  />
                                </>
                              )}
                            </div>
                            <label className="position-history-check">
                              <input
                                type="checkbox"
                                checked={historyState.normalized}
                                onChange={e => updateHistoryState(config.id, { normalized: e.target.checked })}
                              />
                              归一化
                            </label>
                            <button
                              className="btn btn-primary btn-sm"
                              disabled={historyState.loading}
                              onClick={() => fetchPositionHistory(config.id)}
                            >
                              {historyState.loading ? '查询中...' : '刷新'}
                            </button>
                          </div>
                        </div>
                        {historyState.error && <div className="error-msg">{historyState.error}</div>}
                        {!historyState.error && historyState.points.length === 0 && (
                          <div className="position-history-empty">暂无历史点</div>
                        )}
                        {chartPoints.length > 0 && (
                          <div className="position-history-chart">
                            <PositionHistoryChart
                              points={chartPoints}
                              normalized={historyState.normalized}
                              darkMode={darkMode}
                            />
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  )
                })}
              </div>
              {selectedConfigId && (
                <div className="chart-panel">
                  <div className="position-history-controls">
                    <div className="ph-row">
                      <span className="ph-label">Asset</span>
                      <div className="ph-filter-buttons">
                        {[1, 2, 3, 0].map(d => (
                          <button
                            key={d}
                            className={`btn btn-sm ${(scatterAssetFilterDays[selectedConfigId] ?? 0) === d ? 'btn-primary' : 'btn-secondary'}`}
                            onClick={() => handleScatterAssetFilterChange(selectedConfigId, d)}
                          >
                            {d === 0 ? '全部' : `${d}天`}
                          </button>
                        ))}
                      </div>
                      <select
                        className="form-select ph-asset-select"
                        value={scatterState[selectedConfigId]?.assetId || ''}
                        onChange={e => handleScatterAssetChange(selectedConfigId, e.target.value)}
                      >
                        {(scatterAssets[selectedConfigId] || []).length === 0 && (
                          <option value="">暂无 asset</option>
                        )}
                        {(scatterAssets[selectedConfigId] || []).map(asset => (
                          <option key={asset.asset_id} value={asset.asset_id}>
                            {formatAssetOptionLabel(asset)}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="ph-row">
                      <span className="ph-label">范围</span>
                      <div className="ph-filter-buttons">
                        {[1, 3, 7].map(d => (
                          <button
                            key={d}
                            className={`btn btn-sm ${scatterRangeDays[selectedConfigId] === d ? 'btn-primary' : 'btn-secondary'}`}
                            onClick={() => handleScatterRangeChange(selectedConfigId, d)}
                          >
                            {d}天
                          </button>
                        ))}
                        <button
                          className={`btn btn-sm ${scatterRangeDays[selectedConfigId] === 'custom' ? 'btn-primary' : 'btn-secondary'}`}
                          onClick={() => handleScatterRangeChange(selectedConfigId, 'custom')}
                        >
                          {scatterRangeDays[selectedConfigId] === 'custom' && scatterCustomRange[selectedConfigId]?.start
                            ? `${scatterCustomRange[selectedConfigId].start.slice(5, 10)} ~ ${scatterCustomRange[selectedConfigId]?.end ? scatterCustomRange[selectedConfigId].end.slice(5, 10) : '现在'}`
                            : '自定义'}
                        </button>
                        {scatterRangeDays[selectedConfigId] === 'custom' && (
                          <>
                            <input
                              className="form-input form-input--sm"
                              type="date"
                              value={scatterCustomRange[selectedConfigId]?.start?.slice(0, 10) || ''}
                              onChange={e => {
                                const start = e.target.value ? `${e.target.value}T00:00` : ''
                                setScatterCustomRange(prev => ({ ...prev, [selectedConfigId]: { ...prev[selectedConfigId], start, end: prev[selectedConfigId]?.end || '' } }))
                              }}
                            />
                            <span className="range-sep">~</span>
                            <input
                              className="form-input form-input--sm"
                              type="date"
                              value={scatterCustomRange[selectedConfigId]?.end?.slice(0, 10) || ''}
                              onChange={e => {
                                const end = e.target.value ? `${e.target.value}T23:59` : ''
                                setScatterCustomRange(prev => ({ ...prev, [selectedConfigId]: { ...prev[selectedConfigId], start: prev[selectedConfigId]?.start || '', end } }))
                              }}
                            />
                          </>
                        )}
                      </div>
                      <button
                        className={`ph-pill${(scatterShowZeroMatched[selectedConfigId] ?? false) ? ' ph-pill--active' : ''}`}
                        onClick={() => setScatterShowZeroMatched(prev => ({ ...prev, [selectedConfigId]: !(prev[selectedConfigId] ?? false) }))}
                      >含未成交</button>
                      <button
                        className={`ph-pill${(scatterNormalize[selectedConfigId] ?? false) ? ' ph-pill--active' : ''}`}
                        onClick={() => setScatterNormalize(prev => ({ ...prev, [selectedConfigId]: !(prev[selectedConfigId] ?? false) }))}
                      >归一化</button>
                      <button
                        className="btn btn-primary btn-sm"
                        disabled={scatterState[selectedConfigId]?.loading}
                        onClick={() => {
                          const assetId = scatterState[selectedConfigId]?.assetId
                          if (assetId) {
                            const { start, end } = getScatterRange(selectedConfigId)
                            fetchTradeScatter(selectedConfigId, assetId, start, end)
                          }
                        }}
                      >
                        {scatterState[selectedConfigId]?.loading ? '加载中...' : '刷新'}
                      </button>
                    </div>
                  </div>
                  {scatterState[selectedConfigId]?.error && (
                    <div className="error-msg">{scatterState[selectedConfigId].error}</div>
                  )}
                  {(() => {
                    const allTrades = scatterState[selectedConfigId]?.trades || []
                    const filteredTrades = (scatterShowZeroMatched[selectedConfigId] ?? false)
                      ? allTrades
                      : allTrades.filter(t => t.size_matched > 0)
                    if (!scatterState[selectedConfigId]?.error && filteredTrades.length === 0 && !scatterState[selectedConfigId]?.loading) {
                      return <div className="position-history-empty">暂无交易数据</div>
                    }
                    if (filteredTrades.length > 0) {
                      return (
                        <div className="chart-panel-chart" style={{ height: (scatterShowZeroMatched[selectedConfigId] ?? false) ? 520 : 360 }}>
                          <TradeScatterChart trades={filteredTrades} darkMode={darkMode} midPrice={scatterState[selectedConfigId]?.midPrice} showLeader={scatterShowZeroMatched[selectedConfigId] ?? false} normalizePosition={scatterNormalize[selectedConfigId] ?? false} shareRatio={undefined} />
                        </div>
                      )
                    }
                    return null
                  })()}
                </div>
              )}
              </div>
            )}
          </div>
        )}

        {/* ==================== Leader 管理 Tab ==================== */}
        {activeTab === 'leaders' && (
          <div className="copytrading-container">
            {showAddLeaderForm && (
              <div className="card">
                <h3 className="card-title">添加 Leader</h3>
                <div className="form-group">
                  <label className="form-label">钱包地址</label>
                  <input
                    type="text"
                    value={newLeaderWallet}
                    onChange={e => setNewLeaderWallet(e.target.value)}
                    className="form-input"
                    placeholder="0x..."
                  />
                </div>
                {leaderError && <div className="error-msg">{leaderError}</div>}
                <div className="form-actions">
                  <button onClick={handleAddLeader} disabled={loading} className="btn btn-primary">
                    {loading ? '添加中...' : '添加'}
                  </button>
                  <button onClick={() => { setShowAddLeaderForm(false); setLeaderError('') }} className="btn btn-outline">
                    取消
                  </button>
                </div>
              </div>
            )}

            {leaders.length === 0 && !showAddLeaderForm ? (
              <div className="empty-state">
                <p>暂无 Leader</p>
                <p className="empty-hint">点击上方「添加 Leader」添加第一个 Leader</p>
              </div>
            ) : (
              <div className="card">
                <div className="leader-table">
                  <div className="leader-table-header">
                    <span>名字</span>
                    <span>仓位资产</span>
                    <span>总余额</span>
                    <span>操作</span>
                  </div>
                  {leaders.map(leader => (
                    <div key={leader.id} className="leader-table-row">
                      {editingLeaderId === leader.id ? (
                        <>
                          <input
                            type="text"
                            value={editingLeaderName}
                            onChange={e => setEditingLeaderName(e.target.value)}
                            onKeyDown={e => {
                              if (e.key === 'Enter') handleUpdateLeader(leader.id)
                              if (e.key === 'Escape') setEditingLeaderId(null)
                            }}
                            className="form-input"
                            style={{ flex: 1 }}
                            autoFocus
                          />
                          <div style={{ display: 'flex', gap: 8 }}>
                            <button onClick={() => handleUpdateLeader(leader.id)} className="btn-save" style={{ fontSize: 14, padding: '4px 8px' }}>✓</button>
                            <button onClick={() => setEditingLeaderId(null)} className="btn-cancel" style={{ fontSize: 14, padding: '4px 8px' }}>✕</button>
                          </div>
                        </>
                      ) : (
                        <>
                          <div>
                            <a
                              href={`https://polymarket.com/profile/${leader.proxy_wallet}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              style={{ fontWeight: 600, color: 'var(--text-color)', textDecoration: 'none' }}
                              onClick={e => e.stopPropagation()}
                            >
                              {leader.name}
                            </a>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginTop: 2 }}>
                              <span style={{ fontFamily: 'ui-monospace', fontSize: 12, color: 'var(--text-secondary)' }}>
                                {formatAddress(leader.proxy_wallet)}
                              </span>
                              <button
                                onClick={() => navigator.clipboard.writeText(leader.proxy_wallet)}
                                className="btn btn-outline"
                                style={{ fontSize: 11, padding: '1px 5px' }}
                                title="复制地址"
                              >
                                复制
                              </button>
                            </div>
                          </div>
                          <span style={{ fontFamily: 'ui-monospace', fontSize: 13 }}>-</span>
                          <span style={{ fontFamily: 'ui-monospace', fontSize: 13 }}>-</span>
                          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                            <button
                              onClick={() => { setEditingLeaderId(leader.id); setEditingLeaderName(leader.name) }}
                              className="btn btn-outline"
                              style={{ fontSize: 12, padding: '4px 8px' }}
                            >
                              编辑
                            </button>
                            <button
                              onClick={() => handleDeleteLeader(leader.id)}
                              className="btn btn-danger"
                              style={{ fontSize: 12, padding: '4px 8px' }}
                            >
                              删除
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
    </div>
  )
}
