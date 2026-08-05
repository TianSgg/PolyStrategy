import { type CSSProperties, useState } from 'react'
import { apiFetch } from '../api'

type AuthUser = {
  id: number
  username: string
  role: 'root' | 'admin' | 'user'
  enabled: boolean
}

interface Props {
  onLogin: (user: AuthUser) => void
}

async function readJsonOrNull(res: Response) {
  const text = await res.text()
  if (!text.trim()) return null
  return JSON.parse(text)
}

export default function Login({ onLogin }: Props) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleLogin = async () => {
    setLoading(true)
    setError('')
    try {
      const res = await apiFetch('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      })
      const data = await readJsonOrNull(res)
      if (!res.ok) {
        throw new Error(data?.detail || (res.status === 401 ? '用户名或密码错误' : '登录失败'))
      }

      if (data?.user) {
        onLogin(data.user)
        return
      }

      const meRes = await apiFetch('/api/auth/me')
      const meData = await readJsonOrNull(meRes)
      if (!meRes.ok || !meData?.user) {
        throw new Error('登录成功，但获取用户信息失败')
      }
      onLogin(meData.user)
    } catch (e) {
      setError(e instanceof Error ? e.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={styles.page}>
      <div style={styles.panel}>
        <h1 style={styles.title}>WeatherTaker</h1>
        <div style={styles.subtitle}>登录后继续</div>
        <label style={styles.label}>用户名</label>
        <input
          value={username}
          onChange={e => setUsername(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleLogin() }}
          style={styles.input}
          autoFocus
        />
        <label style={styles.label}>密码</label>
        <input
          type="password"
          value={password}
          onChange={e => setPassword(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleLogin() }}
          style={styles.input}
        />
        {error && <div style={styles.error}>{error}</div>}
        <button onClick={handleLogin} disabled={loading} style={styles.button}>
          {loading ? '登录中...' : '登录'}
        </button>
      </div>
    </div>
  )
}

const styles: Record<string, CSSProperties> = {
  page: {
    height: '100vh',
    width: '100vw',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: '#f8fafc',
    color: '#0f172a',
  },
  panel: {
    width: 360,
    background: '#fff',
    border: '1px solid #e2e8f0',
    borderRadius: 8,
    padding: 28,
    boxShadow: '0 12px 32px rgba(15, 23, 42, 0.08)',
  },
  title: { margin: '0 0 4px', fontSize: 24 },
  subtitle: { marginBottom: 24, color: '#64748b', fontSize: 14 },
  label: { display: 'block', margin: '12px 0 6px', fontSize: 13, color: '#475569' },
  input: {
    width: '100%',
    boxSizing: 'border-box',
    border: '1px solid #cbd5e1',
    borderRadius: 6,
    padding: '10px 12px',
    fontSize: 14,
  },
  button: {
    width: '100%',
    marginTop: 18,
    border: 'none',
    borderRadius: 6,
    padding: '11px 12px',
    background: '#2563eb',
    color: '#fff',
    fontSize: 14,
    cursor: 'pointer',
  },
  error: { marginTop: 12, color: '#dc2626', fontSize: 13 },
}
