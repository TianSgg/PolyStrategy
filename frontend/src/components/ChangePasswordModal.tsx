import { useState } from 'react'
import { apiFetch } from '../api'
import { toast } from './Toast'

interface Props {
  open: boolean
  onClose: () => void
  darkMode?: boolean
}

export default function ChangePasswordModal({ open, onClose, darkMode }: Props) {
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const reset = () => {
    setOldPwd('')
    setNewPwd('')
    setConfirmPwd('')
    setError('')
  }

  const handleClose = () => {
    reset()
    onClose()
  }

  const handleSubmit = async () => {
    setError('')
    if (!oldPwd || !newPwd || !confirmPwd) {
      setError('请填写所有字段')
      return
    }
    if (newPwd !== confirmPwd) {
      setError('两次输入的新密码不一致')
      return
    }
    setLoading(true)
    try {
      const res = await apiFetch('/api/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ old_password: oldPwd, new_password: newPwd, confirm_password: confirmPwd }),
      })
      if (res.ok) {
        reset()
        onClose()
        toast('密码修改成功', 'success')
      } else {
        const data = await res.json()
        setError(data.detail || '修改失败')
      }
    } finally {
      setLoading(false)
    }
  }

  if (!open) return null

  return (
    <div className="cg-modal-overlay" onClick={handleClose} data-theme={darkMode ? 'dark' : 'light'}>
      <div className="cg-modal" onClick={e => e.stopPropagation()}>
        <div className="cg-modal-header">
          <h3>修改密码</h3>
          <button className="cg-modal-close" onClick={handleClose}>&times;</button>
        </div>
        <div className="cg-form-group">
          <label>旧密码</label>
          <input
            type="password"
            value={oldPwd}
            onChange={e => setOldPwd(e.target.value)}
            placeholder="请输入旧密码"
            autoFocus
          />
        </div>
        <div className="cg-form-group">
          <label>新密码</label>
          <input
            type="password"
            value={newPwd}
            onChange={e => setNewPwd(e.target.value)}
            placeholder="请输入新密码"
          />
        </div>
        <div className="cg-form-group">
          <label>确认新密码</label>
          <input
            type="password"
            value={confirmPwd}
            onChange={e => setConfirmPwd(e.target.value)}
            placeholder="再次输入新密码"
          />
        </div>
        {error && <div style={{ color: '#ef4444', fontSize: 13, margin: '8px 0' }}>{error}</div>}
        <div className="cg-actions">
          <button className="cg-btn cg-btn-primary" onClick={handleSubmit} disabled={loading}>
            {loading ? '提交中...' : '确认修改'}
          </button>
          <button className="cg-btn cg-btn-outline" onClick={handleClose}>取消</button>
        </div>
      </div>
    </div>
  )
}
