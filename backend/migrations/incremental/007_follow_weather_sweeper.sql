-- ============================================================
-- 007: Follow Weather Sweeper 全部表
-- ============================================================

-- This migration targets the database used by the services. Keeping the
-- database selection here also makes it safe to run directly from a SQL GUI.
USE polystrategy;

-- ============================================================
-- strategy_follow_weather_sweeper 配置表
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_follow_weather_sweeper_configs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  owner_user_id INT NOT NULL,
  account_id INT NOT NULL,
  name VARCHAR(128) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 0,

  params JSON NOT NULL DEFAULT ('{}'),

  params_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  deleted_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  KEY idx_owner_name (owner_user_id, name),
  KEY idx_account (account_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Follow Weather Sweeper 策略配置';

-- ============================================================
-- strategy_follow_weather_sweeper 执行事件日志
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_follow_weather_sweeper_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  event_id CHAR(36) NOT NULL,

  phase ENUM('entry','exit') NOT NULL DEFAULT 'entry',
  step VARCHAR(64) NOT NULL,
  sequence_no INT UNSIGNED NOT NULL,
  detail JSON NOT NULL,
  occurred_at DATETIME(3) NOT NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_event_seq (event_id, sequence_no),
  KEY idx_phase (phase),
  KEY idx_occurred (occurred_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Follow Weather Sweeper 执行事件日志';

-- ============================================================
-- strategy_follow_weather_sweeper 交易摘要
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_follow_weather_sweeper_trades (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  event_id CHAR(36) NOT NULL,
  config_id BIGINT UNSIGNED NOT NULL,
  params_version INT UNSIGNED NOT NULL DEFAULT 1,
  config_snapshot JSON NULL,
  owner_user_id INT NOT NULL,
  proxy_wallet VARCHAR(128) NOT NULL,

  signal_id VARCHAR(512) NULL,
  token_id VARCHAR(128) NULL,
  market_slug VARCHAR(255) NULL,
  event_slug VARCHAR(255) NULL,
  city VARCHAR(100) NULL,
  direction VARCHAR(16) NULL,
  outcome VARCHAR(8) NULL COMMENT 'Polymarket outcome: yes/no',
  temperature_label VARCHAR(100) NULL,
  is_from_main TINYINT(1) NOT NULL DEFAULT 1,

  phase ENUM('entry', 'exit', 'closed') NOT NULL DEFAULT 'entry',
  close_reason VARCHAR(64) NULL,

  entry_price DECIMAL(10,4) NULL,
  entry_shares DECIMAL(20,4) NULL,
  entry_cost DECIMAL(20,6) NULL,
  entry_order_size DECIMAL(20,4) NULL,
  entry_order_id VARCHAR(128) NULL,
  entry_started_at DATETIME(3) NULL,
  entered_at DATETIME(3) NULL,

  exit_price DECIMAL(10,4) NULL,
  exit_shares DECIMAL(20,4) NULL,
  exit_revenue DECIMAL(20,6) NULL,
  exit_order_size DECIMAL(20,4) NULL,
  exit_order_id VARCHAR(128) NULL,
  exit_started_at DATETIME(3) NULL,
  exited_at DATETIME(3) NULL,

  pnl DECIMAL(20,6) NULL,
  pnl_pct DECIMAL(8,4) NULL,

  duration_ms INT UNSIGNED NULL,
  started_at DATETIME(3) NOT NULL,
  closed_at DATETIME(3) NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_event_id (event_id),
  KEY idx_config_phase (config_id, phase),
  KEY idx_owner (owner_user_id, closed_at DESC),
  KEY idx_event_slug (event_slug, started_at DESC),
  KEY idx_phase (phase),
  KEY idx_closed_at (closed_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Follow Weather Sweeper 交易摘要';

-- ============================================================
-- Follow Weather Sweeper leader 信号记录（BUY+SELL）
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_follow_weather_sweeper_signals (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  tx_hash VARCHAR(128) NOT NULL,
  log_index VARCHAR(32) NULL,
  order_hash VARCHAR(128) NULL,

  leader_wallet VARCHAR(128) NOT NULL COMMENT 'maker address (user)',
  taker VARCHAR(128) NULL,
  role ENUM('maker','taker') NOT NULL DEFAULT 'maker',

  side ENUM('BUY','SELL') NOT NULL,
  price DECIMAL(10,6) NOT NULL,
  shares BIGINT UNSIGNED NULL COMMENT 'raw shares (6 decimals)',
  shares_normalized DECIMAL(20,6) NULL,
  fee DECIMAL(20,6) NULL,

  token_id VARCHAR(256) NOT NULL,
  token_label VARCHAR(32) NULL,
  outcome VARCHAR(32) NULL,
  outcome_index TINYINT UNSIGNED NULL,
  complement_token_id VARCHAR(256) NULL,
  complement_token_label VARCHAR(32) NULL,

  condition_id VARCHAR(128) NULL,
  market_slug VARCHAR(255) NULL,
  market_id VARCHAR(128) NULL,
  title VARCHAR(512) NULL,
  is_neg_risk TINYINT(1) NOT NULL DEFAULT 0,

  status ENUM('pending','confirmed') NOT NULL DEFAULT 'pending',
  version TINYINT UNSIGNED NULL COMMENT 'V1 or V2 contracts',
  event_timestamp INT UNSIGNED NULL COMMENT 'Predexon unix timestamp',
  received_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  UNIQUE KEY uq_tx_token (tx_hash, token_id, role),
  KEY idx_leader (leader_wallet, received_at DESC),
  KEY idx_market (market_slug, received_at DESC),
  KEY idx_side (side),
  KEY idx_received (received_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Follow Weather Sweeper leader 信号记录（BUY+SELL）';
