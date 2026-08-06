-- V1 初始化 Schema
CREATE DATABASE IF NOT EXISTS weathertaker DEFAULT CHARACTER SET utf8mb4;
USE weathertaker;

-- ============================================================
-- 登录用户表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(128) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'user',
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  UNIQUE KEY idx_username (username)
);

-- ============================================================
-- 账户表：真实钱包全局唯一；owner_user_id 用于授权和列表过滤
-- ============================================================
CREATE TABLE IF NOT EXISTS accounts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  owner_user_id INT NULL,
  name VARCHAR(128),
  wallet_address VARCHAR(128) NOT NULL,
  proxy_wallet VARCHAR(128),
  encrypted_private_key TEXT NOT NULL,
  builder_api_key VARCHAR(128),
  encrypted_builder_secret TEXT,
  encrypted_builder_passphrase TEXT,
  builder_code VARCHAR(128) DEFAULT NULL COMMENT 'Polymarket Builder Program 归属码 (bytes32)',
  relayer_api_key VARCHAR(128) DEFAULT NULL COMMENT 'Polymarket Relayer API Key',
  signature_type TINYINT NOT NULL DEFAULT 2,
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  UNIQUE KEY idx_wallet_address (wallet_address),
  INDEX idx_accounts_owner_user_id (owner_user_id)
);

-- ============================================================
-- Leader 管理表
-- ============================================================
CREATE TABLE IF NOT EXISTS leaders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Leader 代理钱包地址',
  name VARCHAR(128) NOT NULL COMMENT 'Leader 显示名称',
  profile_image VARCHAR(512) DEFAULT '' COMMENT '头像 URL',
  bio VARCHAR(512) DEFAULT '' COMMENT '个人简介',
  pseudonym VARCHAR(128) DEFAULT '' COMMENT '匿名显示名',
  x_username VARCHAR(128) DEFAULT '' COMMENT 'X (Twitter) 用户名',
  verified_badge TINYINT(1) DEFAULT 0 COMMENT 'Polymarket 认证标识',
  display_username_public TINYINT(1) DEFAULT 0 COMMENT '是否公开显示用户名',
  poly_created_at DATETIME(3) DEFAULT NULL COMMENT 'Polymarket 创建时间（UTC+8）',
  owner_user_id INT NULL,
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  UNIQUE KEY idx_leaders_owner_proxy (owner_user_id, proxy_wallet),
  INDEX idx_leaders_owner_user_id (owner_user_id)
);

-- ============================================================
-- 跟单配置表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_configs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  leader_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Leader 代理钱包地址',
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  share_ratio DECIMAL(5, 4) NOT NULL DEFAULT 0.1000 COMMENT '跟单比例 0.0001-1.0000',
  threshold DECIMAL(20, 4) NOT NULL DEFAULT 0 COMMENT '最大投入 USDC，>=1e10=无穷',
  allowance DECIMAL(20, 4) NOT NULL DEFAULT 0 COMMENT '当前可用额度，0=耗尽，>=1e10=无穷',
  gtd_expiration_sec INT NOT NULL DEFAULT 1800 COMMENT 'GTD订单过期时间（秒），默认30分钟',
  buy_spread_thr DOUBLE NOT NULL DEFAULT 0.05 COMMENT 'BUY价差阈值，默认0.05',
  sell_spread_thr DOUBLE NOT NULL DEFAULT 0.05 COMMENT 'SELL价差阈值，默认0.05',
  buy_exceed_thr TINYINT(1) NOT NULL DEFAULT 1 COMMENT '超过阈值时BUY是否挂单',
  sell_exceed_thr TINYINT(1) NOT NULL DEFAULT 1 COMMENT '超过阈值时SELL是否挂单',
  buy_follow_taker TINYINT(1) NOT NULL DEFAULT 1 COMMENT '买入是否跟随leader的taker行为',
  sell_follow_taker TINYINT(1) NOT NULL DEFAULT 1 COMMENT '卖出是否跟随leader的taker行为',
  buy_only TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'buy only 模式：leader SELL 时转为 BUY 反向 token',
  buy_price_filter_min DECIMAL(10,4) DEFAULT 0.001 COMMENT 'BUY信号价格过滤下限',
  buy_price_filter_max DECIMAL(10,4) DEFAULT 0.998 COMMENT 'BUY信号价格过滤上限',
  buy_price_min DOUBLE NOT NULL DEFAULT 0.001 COMMENT 'BUY最低价格，低于此值截断',
  buy_price_max DOUBLE NOT NULL DEFAULT 0.999 COMMENT 'BUY最高价格，高于此值截断',
  sell_price_min DOUBLE NOT NULL DEFAULT 0.001 COMMENT 'SELL最低价格，低于此值截断',
  sell_price_max DOUBLE NOT NULL DEFAULT 0.999 COMMENT 'SELL最高价格，高于此值截断',
  enabled TINYINT(1) DEFAULT 0 COMMENT '是否启用',
  owner_user_id INT NULL,
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  UNIQUE KEY idx_leader_follower (leader_proxy_wallet, follower_proxy_wallet),
  INDEX idx_copy_trading_configs_owner_user_id (owner_user_id)
);

-- ============================================================
-- 跟单配置仓位历史快照表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_position_history (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  config_id INT NOT NULL,
  asset_id VARCHAR(128) NOT NULL,
  leader_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Leader 代理钱包地址',
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  share_ratio DECIMAL(10, 6) NOT NULL COMMENT '记录时的跟单比例',
  leader_position DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT 'Leader 钱包真实仓位',
  follower_position DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT 'Follower 钱包真实仓位',
  follower_pending_buy DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT 'Follower BUY 待成交',
  follower_pending_sell DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT 'Follower SELL 待成交',
  source VARCHAR(64) NOT NULL COMMENT '快照来源',
  side VARCHAR(16) DEFAULT NULL COMMENT '事件方向 BUY/SELL',
  event_size DECIMAL(20, 8) DEFAULT NULL COMMENT '事件数量',
  event_price DECIMAL(20, 8) DEFAULT NULL COMMENT '事件价格',
  order_id VARCHAR(128) DEFAULT NULL COMMENT '相关订单 ID',
  leader_tx_hash VARCHAR(128) DEFAULT NULL COMMENT 'Leader 交易 hash',
  raw_context TEXT COMMENT '扩展上下文 JSON 字符串',
  created_at DATETIME(3) NOT NULL,
  INDEX idx_config_asset_created (config_id, asset_id, created_at),
  INDEX idx_created_at (created_at)
);

-- ============================================================
-- 跟单配置关联 asset 列表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_config_assets (
  config_id INT NOT NULL,
  asset_id VARCHAR(128) NOT NULL,
  last_seen_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (config_id, asset_id),
  INDEX idx_config_last_seen (config_id, last_seen_at)
);

-- ============================================================
-- 跟单交易记录表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_orders (
  id VARCHAR(128) PRIMARY KEY COMMENT '0x开头，follower_order_hash',
  config_id INT NOT NULL,
  leader VARCHAR(128) NOT NULL COMMENT 'leader proxy_wallet',
  follower VARCHAR(128) NOT NULL COMMENT 'follower proxy_wallet',
  leader_tx_hash VARCHAR(128) NOT NULL COMMENT 'leader transaction hash',
  asset_id VARCHAR(128) NOT NULL,
  side VARCHAR(16) NOT NULL COMMENT 'BUY / SELL',
  leader_size DECIMAL(20,4) NOT NULL,
  leader_price DECIMAL(20,4) NOT NULL,
  follow_size DECIMAL(20,4) NOT NULL COMMENT 'follower 成交数量',
  follow_price DECIMAL(20,4) NOT NULL COMMENT 'follower 成交价（含tick调整）',
  size_matched DECIMAL(20,4) NOT NULL DEFAULT 0 COMMENT '已成交数量',
  status VARCHAR(32) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING / FILLED / CANCELLED / ERROR',
  leader_role VARCHAR(10) DEFAULT NULL COMMENT 'leader 角色: maker/taker',
  follower_role VARCHAR(10) DEFAULT NULL COMMENT 'follower 角色: maker/taker',
  err_msg VARCHAR(255) COMMENT '错误信息',
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  INDEX idx_config_id_asset_status (config_id, asset_id, status),
  INDEX idx_follower (follower),
  INDEX idx_created_at (created_at)
);

-- ============================================================
-- Asset 市场名称缓存表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_asset_questions (
  asset_id VARCHAR(128) NOT NULL PRIMARY KEY,
  question VARCHAR(512) NOT NULL,
  outcome VARCHAR(128) DEFAULT '' COMMENT 'Asset 对应 outcome',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR)
);

-- ============================================================
-- Follower 仓位表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_follower_positions (
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  asset_id VARCHAR(128) NOT NULL COMMENT '资产 ID',
  size DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT '持仓数量',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (follower_proxy_wallet, asset_id)
);

-- ============================================================
-- Follower 待成交 SELL 锁单
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_follower_pending_sell (
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  asset_id VARCHAR(128) NOT NULL COMMENT '资产 ID',
  pending DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT '锁定中的挂单数量',
  question VARCHAR(512) COMMENT '市场标题',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (follower_proxy_wallet, asset_id)
);

-- ============================================================
-- Follower 待成交 BUY 锁单
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_follower_pending_buy (
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  asset_id VARCHAR(128) NOT NULL COMMENT '资产 ID',
  pending DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT '锁定中的买单数量',
  question VARCHAR(512) COMMENT '市场标题',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (follower_proxy_wallet, asset_id)
);

-- ============================================================
-- 跟单份额债务缓冲（Share Debt Buffer）
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_share_debt (
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  asset_id VARCHAR(128) NOT NULL COMMENT '资产 ID',
  side VARCHAR(10) NOT NULL COMMENT 'BUY 或 SELL',
  debt DOUBLE NOT NULL DEFAULT 0 COMMENT '债务值，负值表示预支',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (follower_proxy_wallet, asset_id, side)
);

-- ============================================================
-- 账户余额历史表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_account_balance_history (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  proxy_wallet VARCHAR(128) NOT NULL,
  total_value DECIMAL(20,6) NOT NULL COMMENT '账户总价值(余额+持仓)',
  created_at DATETIME(3) NOT NULL COMMENT '记录时间(UTC+8)',
  INDEX idx_abh_wallet_time (proxy_wallet, created_at),
  INDEX idx_abh_time (created_at)
);

-- ============================================================
-- 余额调整表（充值/提现记录）
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_balance_adjustments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  proxy_wallet VARCHAR(128) NOT NULL,
  delta DECIMAL(20,6) NOT NULL COMMENT '充值为正,提现为负',
  applied_at DATETIME(3) NOT NULL COMMENT '生效时间点(快照时间戳)',
  note VARCHAR(255) DEFAULT '' COMMENT '备注',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_ba_wallet (proxy_wallet),
  INDEX idx_ba_applied (applied_at)
);

-- ============================================================
-- 跟单定时调度表
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_schedules (
  id INT AUTO_INCREMENT PRIMARY KEY,
  config_id INT NOT NULL,
  start_cron VARCHAR(100) DEFAULT NULL COMMENT '启动 cron 表达式（UTC+8）',
  stop_cron VARCHAR(100) DEFAULT NULL COMMENT '停止 cron 表达式（UTC+8）',
  enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '该调度规则是否启用',
  last_triggered_at DATETIME(3) DEFAULT NULL COMMENT '上次触发时间',
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  CONSTRAINT fk_schedule_config FOREIGN KEY (config_id) REFERENCES copy_trading_configs(id) ON DELETE CASCADE,
  UNIQUE KEY uk_config_id (config_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================
-- 跟单 Slug 过滤表（黑/白名单）
-- ============================================================
CREATE TABLE IF NOT EXISTS copy_trading_slug_filters (
  id INT AUTO_INCREMENT PRIMARY KEY,
  config_id INT NOT NULL,
  mode ENUM('blacklist', 'whitelist') NOT NULL DEFAULT 'blacklist' COMMENT '过滤模式',
  slugs JSON NOT NULL COMMENT 'slug 列表，JSON 数组',
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  CONSTRAINT fk_slug_filter_config FOREIGN KEY (config_id) REFERENCES copy_trading_configs(id) ON DELETE CASCADE,
  UNIQUE KEY uk_slug_filter_config_id (config_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
