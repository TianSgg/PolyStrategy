import { useState, useEffect } from 'react'
import { apiFetch } from '../api'
import './CreateGroupModal.css'

interface Props {
  open: boolean
  groupId: number | null
  groups: { id: number; name: string; account_ids: number[] }[]
  onClose: () => void
  onSaved: () => void
  accounts: { id: number; name: string; proxy_wallet: string }[]
  darkMode?: boolean
}

export default function EditGroupModal({ open, groupId, groups, onClose, onSaved, accounts, darkMode }: Props) {
  const [name, setName] = useState('')
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (open && groupId != null) {
      const group = groups.find(g => g.id === groupId)
      if (group) {
        setName(group.name)
        setSelectedIds([...group.account_ids])
      }
    }
  }, [open, groupId, groups])

  const toggle = (id: number) => {
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  const handleSave = async () => {
    if (!name.trim() || groupId == null) return
    setLoading(true)
    try {
      await apiFetch(`/api/portfolio-group/${groupId}`, {
        method: 'PUT',
        body: JSON.stringify({ name: name.trim(), account_ids: selectedIds }),
      })
      onSaved()
      onClose()
    } finally {
      setLoading(false)
    }
  }

  if (!open || groupId == null) return null

  return (
    <div className="cg-modal-overlay" onClick={onClose} data-theme={darkMode ? 'dark' : 'light'}>
      <div className="cg-modal" onClick={e => e.stopPropagation()}>
        <div className="cg-modal-header">
          <h3>编辑分组</h3>
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
          <button className="cg-btn cg-btn-primary" onClick={handleSave} disabled={loading || !name.trim()}>
            {loading ? '保存中...' : '保存'}
          </button>
          <button className="cg-btn cg-btn-outline" onClick={onClose}>取消</button>
        </div>
      </div>
    </div>
  )
}
