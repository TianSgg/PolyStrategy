import { useState } from 'react'
import { apiFetch } from '../api'
import './CreateGroupModal.css'

interface Props {
  open: boolean
  onClose: () => void
  onCreated: () => void
  accounts: { id: number; name: string; proxy_wallet: string }[]
  darkMode?: boolean
}

export default function CreateGroupModal({ open, onClose, onCreated, accounts, darkMode }: Props) {
  const [name, setName] = useState('')
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [loading, setLoading] = useState(false)

  const toggle = (id: number) => {
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  const handleCreate = async () => {
    if (!name.trim()) return
    setLoading(true)
    try {
      await apiFetch('/api/portfolio-group/create', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), account_ids: selectedIds }),
      })
      setName('')
      setSelectedIds([])
      onCreated()
      onClose()
    } finally {
      setLoading(false)
    }
  }

  if (!open) return null

  return (
    <div className="cg-modal-overlay" onClick={onClose} data-theme={darkMode ? 'dark' : 'light'}>
      <div className="cg-modal" onClick={e => e.stopPropagation()}>
        <div className="cg-modal-header">
          <h3>创建分组</h3>
          <button className="cg-modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="cg-form-group">
          <label>分组名称</label>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="输入名称"
            autoFocus
          />
        </div>
        <div className="cg-form-group">
          <label>选择账户</label>
          <div className="cg-account-list">
            {accounts.map(acc => (
              <label key={acc.id} className="cg-account-item">
                <input
                  type="checkbox"
                  checked={selectedIds.includes(acc.id)}
                  onChange={() => toggle(acc.id)}
                />
                <span>{acc.name || acc.proxy_wallet.slice(0, 10) + '...'}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="cg-actions">
          <button className="cg-btn cg-btn-primary" onClick={handleCreate} disabled={loading || !name.trim()}>
            {loading ? '创建中...' : '创建'}
          </button>
          <button className="cg-btn cg-btn-outline" onClick={onClose}>取消</button>
        </div>
      </div>
    </div>
  )
}
