import './StatusBar.css';

interface StatusBarProps {
  darkMode: boolean;
  setDarkMode: (value: boolean) => void;
  totalBalance?: string;
  onBalanceRefresh?: () => void;
  balanceLoading?: boolean;
  username?: string;
  onLogout?: () => void;
  onChangePassword?: () => void;
}

export default function StatusBar({
  darkMode,
  setDarkMode,
  totalBalance = "--",
  onBalanceRefresh,
  balanceLoading = false,
  username,
  onLogout,
  onChangePassword,
}: StatusBarProps) {

  return (
    <div className="global-status-bar" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="status-left">
      </div>
      <div className="status-actions">
        <div className="portfolio-container">
          <div className="portfolio-display" onClick={onBalanceRefresh} title="点击刷新">
            <span className="portfolio-label">Portfolio</span>
            <span className="portfolio-amount">
              {balanceLoading ? '...' : totalBalance}
            </span>
          </div>
        </div>
        <div className="status-divider"></div>
        <div className="user-menu-container">
          <div className="user-menu-trigger">
            <span className="user-name">{username}</span>
            <svg className="user-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
          </div>
          <div className="user-dropdown">
            <div className="dropdown-item" onClick={() => alert('通知设置开发中')}>
              <span className="item-icon">🔔</span>
              <span>通知设置</span>
            </div>
            <div className="dropdown-item" onClick={() => alert('语言选择开发中')}>
              <span className="item-icon">🌐</span>
              <span>语言</span>
              <span className="item-value">简体中文</span>
            </div>
            <div className="dropdown-divider"></div>
            <label className="dropdown-item theme-toggle-item">
              <span className="item-icon">🌙</span>
              <span>深色模式</span>
              <div className="item-action">
                <span className="toggle-switch small">
                  <input
                    type="checkbox"
                    checked={darkMode}
                    onChange={() => setDarkMode(!darkMode)}
                  />
                  <span className="toggle-slider"></span>
                </span>
              </div>
            </label>
            {onChangePassword && (
              <div className="dropdown-item" onClick={onChangePassword}>
                <span className="item-icon">🔑</span>
                <span>修改密码</span>
              </div>
            )}
            {onLogout && (
              <div className="dropdown-item logout-item" onClick={onLogout}>
                <span className="item-icon">🚪</span>
                <span>退出登陆</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
