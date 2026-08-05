-- V7 Portfolio 分组
-- 分组表
CREATE TABLE IF NOT EXISTS copy_trading_portfolio_groups (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(128) NOT NULL COMMENT '分组名称',
  owner_user_id INT NOT NULL COMMENT '所属用户',
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  INDEX idx_copy_trading_portfolio_groups_owner (owner_user_id)
);

-- 分组-账户 多对多关联表
CREATE TABLE IF NOT EXISTS copy_trading_portfolio_group_accounts (
  group_id INT NOT NULL,
  account_id INT NOT NULL,
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (group_id, account_id),
  INDEX idx_pga_account (account_id)
);
