import { useState, useEffect, useMemo } from 'react'
import Account from './pages/Account'
import CopyTrading from './pages/CopyTrading'
import Login from './pages/Login'
import WeatherMonitor from './pages/WeatherMonitor'
import StrategyDashboard from './pages/StrategyDashboard'
import UserManagement from './pages/UserManagement'
import StatusBar from './components/StatusBar'
import ChangePasswordModal from './components/ChangePasswordModal'
import { ToastContainer } from './components/Toast'
import { apiFetch, setUnauthorizedHandler } from './api'
import { useBalance } from './contexts/BalanceContext'

type Page = 'account' | 'copytrading' | 'users' | 'weather' | 'dashboard'
type AuthUser = { id: number; username: string; role: 'root' | 'admin' | 'user'; enabled: boolean }

const DARK_MODE_STORAGE_KEY = 'weathertaker:dark-mode'

function getInitialDarkMode(): boolean {
  try {
    return localStorage.getItem(DARK_MODE_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

function App() {
  const [currentPage, setCurrentPage] = useState<Page>('account')
  const [darkMode, setDarkMode] = useState(getInitialDarkMode)
  const [authUser, setAuthUser] = useState<AuthUser | null>(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [showChangePwd, setShowChangePwd] = useState(false)
  const [strategyExpanded, setStrategyExpanded] = useState(true)

  const { accountBalances } = useBalance()
  const [balanceRefreshKey, setBalanceRefreshKey] = useState(0)
  const [accountsList, setAccountsList] = useState<{ id: number; name: string; proxy_wallet: string }[]>([])

  const totalBalance = useMemo(() => {
    const total = accountsList.reduce((sum, acc) => {
      const b = accountBalances[acc.proxy_wallet.toLowerCase()]
      return sum + (b?.total_value || 0)
    }, 0)
    return total > 0 ? `$${total.toFixed(2)}` : "--"
  }, [accountsList, accountBalances])


  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuthUser(null)
      setCurrentPage('account')
    })
    apiFetch('/api/auth/me')
      .then(async res => {
        if (!res.ok) return null
        const data = await res.json()
        setAuthUser(data.user)
      })
      .finally(() => setAuthLoading(false))
    return () => setUnauthorizedHandler(null)
  }, [])

  const handleLogout = async () => {
    await apiFetch('/api/auth/logout', { method: 'POST' })
    setAuthUser(null)
    setCurrentPage('account')
  }

  useEffect(() => {
    try {
      localStorage.setItem(DARK_MODE_STORAGE_KEY, String(darkMode))
    } catch {
      // Ignore storage failures; theme still works for the current session.
    }
    document.body.style.backgroundColor = darkMode ? '#0f172a' : '#f8fafc'
    document.body.style.color = darkMode ? '#f8fafc' : '#0f172a'
    document.body.style.margin = '0'
    document.documentElement.style.height = '100%'
    document.body.style.height = '100%'
  }, [darkMode])

  const theme = darkMode ? {
    container: styles.containerDark,
    sidebar: styles.sidebarDark,
    navItem: styles.navItemDark,
    navItemActive: styles.navItemActiveDark,
    title: styles.titleDark
  } : {
    container: styles.container,
    sidebar: styles.sidebar,
    navItem: styles.navItem,
    navItemActive: styles.navItemActive,
    title: styles.title
  }

  if (authLoading) {
    return <div style={{ height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>加载中...</div>
  }

  if (!authUser) {
    return <Login onLogin={setAuthUser} />
  }

  return (
    <div style={theme.container}>
      <ToastContainer />
      <ChangePasswordModal
        open={showChangePwd}
        onClose={() => setShowChangePwd(false)}
        darkMode={darkMode}
      />
      {/* 左侧导航栏 */}
      <div style={theme.sidebar}>
        <h1 style={theme.title}>PolyStrategy</h1>
        <nav style={styles.nav}>
          <button
            onClick={() => setCurrentPage('account')}
            style={currentPage === 'account' ? theme.navItemActive : theme.navItem}
          >
            <span style={styles.navIcon}>💳</span>
            账户管理
          </button>

          {/* 策略（可展开） */}
          <button
            onClick={() => setStrategyExpanded(!strategyExpanded)}
            style={['copytrading', 'dashboard'].includes(currentPage) && !strategyExpanded
              ? theme.navItemActive : theme.navItem}
          >
            <span style={styles.navIcon}>🎯</span>
            策略
            <span style={{
              marginLeft: 'auto',
              fontSize: '12px',
              transition: 'transform 0.2s',
              transform: strategyExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
            }}>▶</span>
          </button>
          {strategyExpanded && (
            <div style={styles.subNav}>
              <button
                onClick={() => setCurrentPage('copytrading')}
                style={currentPage === 'copytrading' ? theme.navItemActive : theme.navItem}
              >
                <span style={styles.navIcon}>📋</span>
                跟单策略
              </button>
              <button
                onClick={() => setCurrentPage('dashboard')}
                style={currentPage === 'dashboard' ? theme.navItemActive : theme.navItem}
              >
                <span style={styles.navIcon}>🌡</span>
                Weather Sweep
              </button>
            </div>
          )}

          <button
            onClick={() => setCurrentPage('weather')}
            style={currentPage === 'weather' ? theme.navItemActive : theme.navItem}
          >
            <span style={styles.navIcon}>🌤</span>
            天气监控
          </button>
          {(authUser.role === 'admin' || authUser.role === 'root') && (
            <button
              onClick={() => setCurrentPage('users')}
              style={currentPage === 'users' ? theme.navItemActive : theme.navItem}
            >
              <span style={styles.navIcon}>👥</span>
              用户管理
            </button>
          )}
        </nav>
      </div>

      {/* 右侧内容区域 */}
      <div style={styles.content}>
        {/* 全局状态栏 */}
        <StatusBar
          darkMode={darkMode}
          setDarkMode={setDarkMode}
          totalBalance={totalBalance}
          onBalanceRefresh={() => setBalanceRefreshKey(k => k + 1)}
          username={authUser.username}
          onLogout={handleLogout}
          onChangePassword={() => setShowChangePwd(true)}
        />

        {/* 页面路由及渲染 */}
        <div style={{ display: currentPage === 'account' ? 'flex' : 'none', flex: 1, overflow: 'hidden' }}>
          <Account
            darkMode={darkMode}
            setDarkMode={setDarkMode}
            visible={currentPage === 'account'}
            refreshKey={balanceRefreshKey}
            onAccountsLoaded={setAccountsList}
          />
        </div>
        <div style={{ display: currentPage === 'copytrading' ? 'flex' : 'none', flex: 1, overflow: 'hidden' }}>
          <CopyTrading darkMode={darkMode} visible={currentPage === 'copytrading'} />
        </div>
        <div style={{ display: currentPage === 'users' ? 'flex' : 'none', flex: 1, overflow: 'hidden' }}>
          <UserManagement darkMode={darkMode} currentRole={authUser.role} currentUserId={authUser.id} />
        </div>
        <div style={{ display: currentPage === 'weather' ? 'flex' : 'none', flex: 1, overflow: 'hidden' }}>
          <WeatherMonitor darkMode={darkMode} visible={currentPage === 'weather'} />
        </div>
        <div style={{ display: currentPage === 'dashboard' ? 'flex' : 'none', flex: 1, overflow: 'hidden' }}>
          <StrategyDashboard darkMode={darkMode} />
        </div>
      </div>
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    height: '100vh',
    width: '100vw',
    background: '#f8fafc',
    overflow: 'visible'
  },
  containerDark: {
    display: 'flex',
    height: '100vh',
    width: '100vw',
    background: '#0f172a',
    overflow: 'visible'
  },
  sidebar: {
    width: '240px',
    background: '#ffffff',
    padding: '24px 0',
    display: 'flex',
    flexDirection: 'column',
    borderRight: '1px solid #e2e8f0',
    flexShrink: 0
  },
  sidebarDark: {
    width: '240px',
    background: '#1e293b',
    padding: '24px 0',
    display: 'flex',
    flexDirection: 'column',
    borderRight: '1px solid #334155',
    flexShrink: 0
  },
  title: {
    textAlign: 'center',
    margin: '0 0 40px 0',
    fontSize: '22px',
    fontWeight: '700',
    color: '#0f172a',
    padding: '0 15px'
  },
  titleDark: {
    textAlign: 'center',
    margin: '0 0 40px 0',
    fontSize: '22px',
    fontWeight: '700',
    color: '#f8fafc',
    padding: '0 15px'
  },
  nav: {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
    padding: '0 16px'
  },
  navItem: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '12px 16px',
    background: 'transparent',
    border: 'none',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '15px',
    color: '#64748b',
    textAlign: 'left',
    transition: 'all 0.2s'
  },
  navItemDark: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '12px 16px',
    background: 'transparent',
    border: 'none',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '15px',
    color: '#94a3b8',
    textAlign: 'left',
    transition: 'all 0.2s'
  },
  navItemActive: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '12px 16px',
    background: '#eff6ff',
    border: 'none',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '15px',
    color: '#2563eb',
    fontWeight: 600,
    textAlign: 'left',
    transition: 'all 0.2s'
  },
  navItemActiveDark: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '12px 16px',
    background: '#1e3a8a',
    border: 'none',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '15px',
    color: '#60a5fa',
    fontWeight: 600,
    textAlign: 'left',
    transition: 'all 0.2s'
  },
  navIcon: {
    fontSize: '18px'
  },
  subNav: {
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
    paddingLeft: '16px',
  },
  content: {
    flex: 1,
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column',
    position: 'relative'
  }
}

export default App
