import { useEffect, useState } from 'react'
import { apiFetch } from '../api'
import './UserManagement.css'

type User = {
  id: number
  username: string
  role: 'root' | 'user'
  enabled: boolean
  created_at: string | null
}

interface Props {
  darkMode: boolean
  currentRole: 'root' | 'user'
  currentUserId: number
}

const ROLE_HIERARCHY: Record<string, number> = { root: 2, user: 1 }
const ALL_ROLES: ('root' | 'user')[] = ['user', 'root']

export default function UserManagement({ darkMode, currentRole, currentUserId }: Props) {
  const [users, setUsers] = useState<User[]>([])
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'root' | 'user'>('user')
  const [error, setError] = useState('')
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editUsername, setEditUsername] = useState('')

  const loadUsers = async () => {
    const res = await apiFetch('/api/auth/users')
    const data = await res.json()
    if (res.ok) setUsers(data.users || [])
  }

  useEffect(() => {
    loadUsers()
  }, [])

  const canOperate = (user: User) =>
    ROLE_HIERARCHY[user.role] < ROLE_HIERARCHY[currentRole]

  const createUser = async () => {
    setError('')
    const res = await apiFetch('/api/auth/users', {
      method: 'POST',
      body: JSON.stringify({ username, password, role }),
    })
    const data = await res.json()
    if (!res.ok) {
      setError(data.detail || '创建失败')
      return
    }
    setUsername('')
    setPassword('')
    setRole('user')
    loadUsers()
  }

  const setEnabled = async (user: User, enabled: boolean) => {
    const res = await apiFetch(`/api/auth/users/${user.id}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    })
    if (res.ok) {
      setUsers(prev => prev.map(u => u.id === user.id ? { ...u, enabled } : u))
    }
  }

  const startEdit = (user: User) => {
    setEditingId(user.id)
    setEditUsername(user.username)
  }

  const cancelEdit = () => {
    setEditingId(null)
    setEditUsername('')
  }

  const saveEdit = async (userId: number) => {
    const original = users.find(u => u.id === userId)
    if (!editUsername || editUsername === original?.username) {
      cancelEdit()
      return
    }
    const res = await apiFetch(`/api/auth/users/${userId}`, {
      method: 'PUT',
      body: JSON.stringify({ username: editUsername }),
    })
    if (res.ok) {
      cancelEdit()
      loadUsers()
    } else {
      const data = await res.json()
      setError(data.detail || '更新失败')
    }
  }

  const deleteUser = async (user: User) => {
    if (!confirm(`确定删除用户 "${user.username}" 吗？`)) return
    const res = await apiFetch(`/api/auth/users/${user.id}`, { method: 'DELETE' })
    if (res.ok) {
      loadUsers()
    } else {
      const data = await res.json()
      setError(data.detail || '删除失败')
    }
  }

  return (
    <div className="users-page" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="users-container">
        <div className="page-header">
          <h2 className="page-title">用户管理</h2>
        </div>

        <div className="card">
          <h3 className="card-title">创建用户</h3>
          <div className="create-user-form">
            <div className="form-group">
              <label className="form-label">用户名</label>
              <input
                value={username}
                onChange={e => setUsername(e.target.value)}
                className="form-input"
                placeholder="请输入用户名"
              />
            </div>
            <div className="form-group">
              <label className="form-label">密码</label>
              <input
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                className="form-input"
                placeholder="请输入密码"
              />
            </div>
            <div className="form-group">
              <label className="form-label">角色</label>
              <select
                value={role}
                onChange={e => setRole(e.target.value as 'root' | 'user')}
                className="form-select"
              >
                {ALL_ROLES.filter(r => ROLE_HIERARCHY[r] < ROLE_HIERARCHY[currentRole]).map(r => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
            </div>
            <div className="form-group" style={{ flex: '0 0 auto', minWidth: 'auto' }}>
              <label className="form-label" style={{ visibility: 'hidden' }}>占位</label>
              <button onClick={createUser} className="btn btn-primary">
                创建用户
              </button>
            </div>
          </div>
          {error && <div className="error-msg">{error}</div>}
        </div>

        <div className="card">
          <h3 className="card-title">用户列表</h3>
          <div className="users-list-table">
            <div className="users-table-header">
              <span>ID</span>
              <span>用户名</span>
              <span>角色</span>
              <span>状态</span>
              <span style={{ textAlign: 'right' }}>操作</span>
            </div>
            {users.length === 0 ? (
              <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-secondary)' }}>
                暂无用户数据
              </div>
            ) : (
              users.map(user => (
                <div key={user.id} className="users-table-row">
                  <span style={{ color: 'var(--text-secondary)', fontFamily: 'ui-monospace' }}>#{user.id}</span>
                  {editingId === user.id ? (
                    <input
                      value={editUsername}
                      onChange={e => setEditUsername(e.target.value)}
                      className="form-input form-input-inline"
                    />
                  ) : (
                    <strong className="user-username">
                      {user.username}
                      {user.id === currentUserId && <span style={{ color: 'var(--text-secondary)', fontSize: 12, marginLeft: 6 }}>(我)</span>}
                    </strong>
                  )}
                  <div>
                    <span className="user-role-badge">{user.role}</span>
                  </div>
                  <span className={`status-text ${user.enabled ? 'status-enabled' : 'status-disabled'}`}>
                    {user.enabled ? '已启用' : '已禁用'}
                  </span>
                  <div className="user-actions">
                    {editingId === user.id ? (
                      <>
                        <button onClick={() => saveEdit(user.id)} className="btn btn-sm btn-primary">保存</button>
                        <button onClick={cancelEdit} className="btn btn-sm btn-secondary">取消</button>
                      </>
                    ) : canOperate(user) ? (
                      <>
                        <label className="toggle-switch">
                          <input
                            type="checkbox"
                            checked={user.enabled}
                            onChange={() => setEnabled(user, !user.enabled)}
                          />
                          <span className="toggle-slider"></span>
                        </label>
                        <button onClick={() => startEdit(user)} className="btn btn-sm btn-secondary">编辑</button>
                        <button onClick={() => deleteUser(user)} className="btn btn-sm btn-danger">删除</button>
                      </>
                    ) : (
                      <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}>--</span>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
