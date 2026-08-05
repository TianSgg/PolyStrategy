import './StatusBar.css';

interface StatusBarProps {
  connected: boolean;
  wsLatency: {
    ws_market: string;
    ws_user: string;
    polygon_ws: string;
    poly_rtds: string;
    predexon: string;
  };
  httpLatency: {
    data_api: string;
    clob_api: string;
    gamma_api: string;
    polygon_http: string;
  };
  darkMode: boolean;
  setDarkMode: (value: boolean) => void;
  totalBalance?: string;
  onBalanceRefresh?: () => void;
  balanceLoading?: boolean;
  portfolioGroups?: { id: number; name: string; account_ids: number[] }[];
  accountSummary?: Record<number, { total_value: number }>;
  pinnedGroupId?: number | null;
  onCreateGroup?: () => void;
  onEditGroup?: (groupId: number) => void;
  onDeleteGroup?: (groupId: number) => void;
  onPinGroup?: (groupId: number | null) => void;
  latencyLoading?: boolean;
  onLatencyRefresh?: () => void;
  username?: string;
  onLogout?: () => void;
  onChangePassword?: () => void;
}

function getLatencyColor(ms: string, darkMode: boolean): string {
  if (ms === "--") return 'var(--text-secondary)';
  const n = Number(ms);
  if (n === 0) return 'var(--text-secondary)';
  const ratio = Math.min(n / 500, 1);
  if (darkMode) {
    const r = Math.round(200 * ratio + 100);
    const g = Math.round(200 * (1 - ratio));
    const b = Math.round(100 * (1 - ratio));
    return `rgb(${r}, ${g}, ${b})`;
  }
  const r = Math.round(200 * ratio);
  const g = Math.round(150 * (1 - ratio) + 80 * ratio);
  const b = Math.round(80 * ratio);
  return `rgb(${r}, ${g}, ${b})`;
}

function LatencyItem({ label, value, darkMode, loading, width }: { label: string; value: string; darkMode: boolean; loading?: boolean; width?: string }) {
  return (
    <span className="latency-item" style={{ width }}>
      <span className="latency-label">{label}</span>
      <span style={{ color: loading ? 'var(--text-secondary)' : getLatencyColor(value, darkMode) }}>{loading ? "--" : value === "--" ? "--" : `${value}ms`}</span>
    </span>
  );
}

function PortfolioDropdown({ groups, accountSummary, pinnedGroupId, onCreateGroup, onEditGroup, onDeleteGroup, onPinGroup }: {
  groups: { id: number; name: string; account_ids: number[] }[];
  accountSummary: Record<number, { total_value: number }>;
  pinnedGroupId?: number | null;
  onCreateGroup?: () => void;
  onEditGroup?: (groupId: number) => void;
  onDeleteGroup?: (groupId: number) => void;
  onPinGroup?: (groupId: number | null) => void;
}) {
  return (
    <div className="portfolio-dropdown">
      {groups.map(group => {
        const groupTotal = group.account_ids.reduce(
          (sum, id) => sum + (accountSummary[id]?.total_value || 0), 0
        );
        const isPinned = pinnedGroupId === group.id;
        return (
          <div key={group.id} className="portfolio-dropdown-row">
            <div className="portfolio-dropdown-item">
              <span className="portfolio-dropdown-name">
                {isPinned && <span className="portfolio-pin-indicator">*</span>}
                {group.name}
              </span>
              <span className="portfolio-dropdown-value">${groupTotal.toFixed(2)}</span>
            </div>
            <div className="portfolio-dropdown-actions">
              <div className="portfolio-action-btn" onClick={(e) => { e.stopPropagation(); onPinGroup?.(isPinned ? null : group.id); }}>
                {isPinned ? '取消置顶' : '置顶'}
              </div>
              <div className="portfolio-action-btn" onClick={(e) => { e.stopPropagation(); onEditGroup?.(group.id); }}>
                编辑
              </div>
              <div className="portfolio-action-btn portfolio-action-danger" onClick={(e) => { e.stopPropagation(); onDeleteGroup?.(group.id); }}>
                删除
              </div>
            </div>
          </div>
        );
      })}
      <div className="dropdown-divider"></div>
      <div className="portfolio-dropdown-item portfolio-dropdown-create-btn" onClick={(e) => { e.stopPropagation(); onCreateGroup?.(); }}>
        <span className="portfolio-dropdown-name">+ 创建分组</span>
      </div>
    </div>
  );
}

export default function StatusBar({
  connected,
  wsLatency,
  httpLatency,
  darkMode,
  setDarkMode,
  totalBalance = "--",
  onBalanceRefresh,
  balanceLoading = false,
  portfolioGroups = [],
  accountSummary = {},
  pinnedGroupId,
  onCreateGroup,
  onEditGroup,
  onDeleteGroup,
  onPinGroup,
  latencyLoading = false,
  onLatencyRefresh,
  username,
  onLogout,
  onChangePassword,
}: StatusBarProps) {
  const pinnedGroup = pinnedGroupId != null ? portfolioGroups.find(g => g.id === pinnedGroupId) : null;
  const displayLabel = pinnedGroup ? pinnedGroup.name : 'Portfolio';
  const displayValue = pinnedGroup
    ? `$${pinnedGroup.account_ids.reduce((sum, id) => sum + (accountSummary[id]?.total_value || 0), 0).toFixed(2)}`
    : totalBalance;

  return (
    <div className="global-status-bar" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="status-left">
        <div className={`status-badge ${connected ? 'status-connected' : 'status-disconnected'}`}>
          {connected ? 'Connected' : 'Disconnected'}
        </div>
        <div className="latency-info" onClick={onLatencyRefresh} title="Click to test latency">
          <LatencyItem label="WS-mkt" value={wsLatency.ws_market} darkMode={darkMode} loading={latencyLoading} width="50px" />
          <LatencyItem label="WS-user" value={wsLatency.ws_user} darkMode={darkMode} loading={latencyLoading} width="60px" />
          <LatencyItem label="RTDS" value={wsLatency.poly_rtds} darkMode={darkMode} loading={latencyLoading} width="50px" />
          <LatencyItem label="Predexon" value={wsLatency.predexon} darkMode={darkMode} loading={latencyLoading} width="60px" />
          <LatencyItem label="PolygonWS" value={wsLatency.polygon_ws} darkMode={darkMode} loading={latencyLoading} width="70px" />
          <LatencyItem label="Data" value={httpLatency.data_api} darkMode={darkMode} loading={latencyLoading} width="50px" />
          <LatencyItem label="CLOB" value={httpLatency.clob_api} darkMode={darkMode} loading={latencyLoading} width="50px" />
          <LatencyItem label="Gamma" value={httpLatency.gamma_api} darkMode={darkMode} loading={latencyLoading} width="50px" />
          <LatencyItem label="PolygonHTTP" value={httpLatency.polygon_http} darkMode={darkMode} loading={latencyLoading} width="80px" />
        </div>
      </div>
      <div className="status-actions">
        <div className="portfolio-container">
          <div className="portfolio-display" onClick={onBalanceRefresh} title="点击刷新">
            <span className="portfolio-label">{displayLabel}</span>
            <span className="portfolio-amount">
              {balanceLoading ? '...' : displayValue}
            </span>
            <svg className="portfolio-arrow" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
          </div>
          <PortfolioDropdown
            groups={portfolioGroups}
            accountSummary={accountSummary}
            pinnedGroupId={pinnedGroupId}
            onCreateGroup={onCreateGroup}
            onEditGroup={onEditGroup}
            onDeleteGroup={onDeleteGroup}
            onPinGroup={onPinGroup}
          />
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
