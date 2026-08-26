-- PolyStrategy 全量建表
-- 新环境直接执行此文件即可建立完整数据库
-- ⚠️ 会 DROP 已有库，勿在生产环境直接执行

DROP DATABASE IF EXISTS polystrategy;
CREATE DATABASE polystrategy DEFAULT CHARACTER SET utf8mb4;
USE polystrategy;

-- ============================================================
-- 登录用户表
-- ============================================================
CREATE TABLE users (
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
-- 账户表（钱包 + API 密钥）
-- ============================================================
CREATE TABLE accounts (
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
-- 天气城市监听配置
-- ============================================================
CREATE TABLE weather_cities (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  city_name VARCHAR(100) NOT NULL,
  city_slug VARCHAR(100) NOT NULL,
  timezone VARCHAR(64) NOT NULL,

  has_highest_market TINYINT(1) NOT NULL DEFAULT 0,
  has_lowest_market TINYINT(1) NOT NULL DEFAULT 0,
  monitor_highest TINYINT(1) NOT NULL DEFAULT 0,
  monitor_lowest TINYINT(1) NOT NULL DEFAULT 0,
  enabled TINYINT(1) NOT NULL DEFAULT 1,

  sort_order INT NOT NULL DEFAULT 0,
  metadata JSON NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  UNIQUE KEY uq_weather_cities_city_slug (city_slug),
  KEY idx_weather_cities_enabled_sort (enabled, sort_order),
  CONSTRAINT chk_monitor_highest_requires_market CHECK (monitor_highest = 0 OR has_highest_market = 1),
  CONSTRAINT chk_monitor_lowest_requires_market CHECK (monitor_lowest = 0 OR has_lowest_market = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- 天气信号记录
-- ============================================================
CREATE TABLE weather_orderbook_signals (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id VARCHAR(512) NOT NULL,
  signal_type ENUM('sweep', 'no_longer_possible', 'market_resolved', 'event_started') NOT NULL,
  occurred_at DATETIME(3) NOT NULL,
  event_slug VARCHAR(255) NOT NULL,
  city VARCHAR(100) NOT NULL,
  city_slug VARCHAR(100) NOT NULL,
  direction ENUM('highest', 'lowest') NOT NULL,
  local_date DATE NOT NULL,
  market_slug VARCHAR(255) NULL,
  temperature_label VARCHAR(100) NULL,
  outcome ENUM('yes', 'no') NULL,
  main_market_slug VARCHAR(255) NULL,
  main_temperature_label VARCHAR(100) NULL,
  main_outcome ENUM('yes', 'no') NULL,
  is_from_main TINYINT(1) GENERATED ALWAYS AS (main_market_slug <=> market_slug) STORED,
  token_id VARCHAR(100) NULL,
  status ENUM('monitoring', 'resolved', 'exhausted') NULL,
  reason VARCHAR(255) NULL,
  payload JSON NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  UNIQUE KEY uq_signal_id (signal_id),
  KEY idx_event_time (event_slug, occurred_at DESC, id DESC),
  KEY idx_city_date (city_slug, direction, local_date),
  KEY idx_recent (occurred_at DESC, id DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Weather Sweep 策略配置
-- ============================================================
CREATE TABLE weather_sweep_configs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  owner_user_id INT NOT NULL,
  account_id INT NOT NULL,
  name VARCHAR(128) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 0,

  fixed_entry_shares DECIMAL(20,4) NOT NULL DEFAULT 100.0000,
  entry_wait_ms INT NOT NULL DEFAULT 30000,
  sweep_outcome_filter VARCHAR(8) NOT NULL DEFAULT 'no',
  signal_source_filter VARCHAR(8) NOT NULL DEFAULT 'main',
  signal_threshold_filter VARCHAR(8) NOT NULL DEFAULT 'all',
  stop_loss_ratio DECIMAL(5,4) NOT NULL DEFAULT 0.6000,
  exit_wait_ms INT NOT NULL DEFAULT 5000,
  tick_verify_retries SMALLINT UNSIGNED NOT NULL DEFAULT 3,
  tick_verify_backoff_ms INT NOT NULL DEFAULT 1000,

  params_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  deleted_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  UNIQUE KEY uq_owner_name (owner_user_id, name),
  KEY idx_account (account_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- Weather Sweep 执行事件日志
-- ============================================================
CREATE TABLE weather_sweep_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  owner_user_id INT NOT NULL,
  proxy_wallet VARCHAR(128) NOT NULL,
  config_id BIGINT UNSIGNED NOT NULL,
  config_snapshot JSON NULL,

  event_id CHAR(36) NOT NULL,
  signal_id VARCHAR(512) NULL,
  token_id VARCHAR(128) NULL,
  market_slug VARCHAR(255) NULL,
  event_slug VARCHAR(255) NULL,

  phase ENUM('entry','monitor','exit','exit_risk','exit_force') NOT NULL DEFAULT 'entry',
  step VARCHAR(64) NOT NULL,
  sequence_no INT UNSIGNED NOT NULL,
  detail JSON NOT NULL,
  occurred_at DATETIME(3) NOT NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_event_seq (event_id, sequence_no),
  KEY idx_owner_wallet (owner_user_id, proxy_wallet, occurred_at DESC),
  KEY idx_config_event (config_id, event_id),
  KEY idx_phase (phase),
  KEY idx_token (token_id, occurred_at DESC),
  KEY idx_event_slug (event_slug, occurred_at DESC),
  KEY idx_occurred (occurred_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- 天气城市种子数据
-- ============================================================
INSERT INTO weather_cities (
  city_name, city_slug, timezone,
  has_highest_market, has_lowest_market,
  monitor_highest, monitor_lowest, enabled, sort_order
) VALUES
  ('Taipei', 'taipei', 'Asia/Taipei', 1, 0, 1, 0, 1, 10),
  ('Miami', 'miami', 'America/New_York', 1, 1, 1, 1, 1, 20),
  ('Kuala Lumpur', 'kuala-lumpur', 'Asia/Kuala_Lumpur', 1, 0, 1, 0, 1, 30),
  ('Paris', 'paris', 'Europe/Paris', 1, 1, 1, 1, 1, 40),
  ('Mexico City', 'mexico-city', 'America/Mexico_City', 1, 0, 1, 0, 1, 50),
  ('New York City', 'nyc', 'America/New_York', 1, 1, 1, 1, 1, 60),
  ('Panama City', 'panama-city', 'America/Panama', 1, 0, 1, 0, 1, 70),
  ('Sao Paulo', 'sao-paulo', 'America/Sao_Paulo', 1, 1, 1, 1, 1, 80),
  ('Buenos Aires', 'buenos-aires', 'America/Argentina/Buenos_Aires', 1, 0, 1, 0, 1, 90),
  ('Lucknow', 'lucknow', 'Asia/Kolkata', 1, 0, 1, 0, 1, 100),
  ('Cape Town', 'cape-town', 'Africa/Johannesburg', 1, 0, 1, 0, 1, 110),
  ('Karachi', 'karachi', 'Asia/Karachi', 1, 0, 1, 0, 1, 120),
  ('London', 'london', 'Europe/London', 1, 1, 1, 1, 1, 130),
  ('Wellington', 'wellington', 'Pacific/Auckland', 1, 0, 1, 0, 1, 140),
  ('Tel Aviv', 'tel-aviv', 'Asia/Jerusalem', 1, 0, 1, 0, 1, 150),
  ('Tokyo', 'tokyo', 'Asia/Tokyo', 1, 1, 1, 1, 1, 160),
  ('Denver', 'denver', 'America/Denver', 1, 0, 1, 0, 1, 170),
  ('Manila', 'manila', 'Asia/Manila', 1, 0, 1, 0, 1, 180),
  ('Toronto', 'toronto', 'America/Toronto', 1, 0, 1, 0, 1, 190),
  ('Amsterdam', 'amsterdam', 'Europe/Amsterdam', 1, 0, 1, 0, 1, 200),
  ('Ankara', 'ankara', 'Europe/Istanbul', 1, 0, 1, 0, 1, 210),
  ('Atlanta', 'atlanta', 'America/New_York', 1, 0, 1, 0, 1, 220),
  ('Austin', 'austin', 'America/Chicago', 1, 0, 1, 0, 1, 230),
  ('Beijing', 'beijing', 'Asia/Shanghai', 1, 0, 1, 0, 1, 240),
  ('Busan', 'busan', 'Asia/Seoul', 1, 0, 1, 0, 1, 250),
  ('Chengdu', 'chengdu', 'Asia/Shanghai', 1, 0, 1, 0, 1, 260),
  ('Chicago', 'chicago', 'America/Chicago', 1, 0, 1, 0, 1, 270),
  ('Chongqing', 'chongqing', 'Asia/Shanghai', 1, 0, 1, 0, 1, 280),
  ('Dallas', 'dallas', 'America/Chicago', 1, 0, 1, 0, 1, 290),
  ('Guangzhou', 'guangzhou', 'Asia/Shanghai', 1, 0, 1, 0, 1, 300),
  ('Helsinki', 'helsinki', 'Europe/Helsinki', 1, 0, 1, 0, 1, 310),
  ('Hong Kong', 'hong-kong', 'Asia/Hong_Kong', 1, 1, 1, 1, 1, 320),
  ('Houston', 'houston', 'America/Chicago', 1, 0, 1, 0, 1, 330),
  ('Istanbul', 'istanbul', 'Europe/Istanbul', 1, 0, 1, 0, 1, 340),
  ('Jeddah', 'jeddah', 'Asia/Riyadh', 1, 0, 1, 0, 1, 350),
  ('Jinan', 'jinan', 'Asia/Shanghai', 1, 0, 1, 0, 1, 360),
  ('Los Angeles', 'los-angeles', 'America/Los_Angeles', 1, 0, 1, 0, 1, 370),
  ('Madrid', 'madrid', 'Europe/Madrid', 1, 0, 1, 0, 1, 380),
  ('Milan', 'milan', 'Europe/Rome', 1, 0, 1, 0, 1, 390),
  ('Moscow', 'moscow', 'Europe/Moscow', 1, 0, 1, 0, 1, 400),
  ('Munich', 'munich', 'Europe/Berlin', 1, 0, 1, 0, 1, 410),
  ('Qingdao', 'qingdao', 'Asia/Shanghai', 1, 0, 1, 0, 1, 420),
  ('San Francisco', 'san-francisco', 'America/Los_Angeles', 1, 0, 1, 0, 1, 430),
  ('Seattle', 'seattle', 'America/Los_Angeles', 1, 0, 1, 0, 1, 440),
  ('Shanghai', 'shanghai', 'Asia/Shanghai', 1, 1, 1, 1, 1, 450),
  ('Shenzhen', 'shenzhen', 'Asia/Shanghai', 1, 0, 1, 0, 1, 460),
  ('Singapore', 'singapore', 'Asia/Singapore', 1, 0, 1, 0, 1, 470),
  ('Warsaw', 'warsaw', 'Europe/Warsaw', 1, 0, 1, 0, 1, 480),
  ('Wuhan', 'wuhan', 'Asia/Shanghai', 1, 0, 1, 0, 1, 490),
  ('Zhengzhou', 'zhengzhou', 'Asia/Shanghai', 1, 0, 1, 0, 1, 500);


-- ============================================================
-- ↓↓↓ 以下表暂未使用，保留供后续启用 ↓↓↓
-- ============================================================

-- -- Leader 管理表
-- CREATE TABLE leaders ( ... );

-- -- Leader 信号记录
-- CREATE TABLE leader_signals ( ... );

-- -- 策略 2 配置: Leader
-- CREATE TABLE strategy_leader_configs ( ... );

-- -- 策略 3 配置: Sweep + Leader 确认
-- CREATE TABLE strategy_sweep_leader_configs ( ... );

-- -- 跟单配置表
-- CREATE TABLE copy_trading_configs ( ... );

-- -- 跟单配置关联 asset 列表
-- CREATE TABLE copy_trading_config_assets ( ... );

-- -- 跟单交易记录表
-- CREATE TABLE copy_trading_orders ( ... );

-- -- Asset 市场名称缓存表
-- CREATE TABLE copy_trading_asset_questions ( ... );

-- -- Follower 仓位表
-- CREATE TABLE copy_trading_follower_positions ( ... );

-- -- Follower 待成交 SELL 锁单
-- CREATE TABLE copy_trading_follower_pending_sell ( ... );

-- -- Follower 待成交 BUY 锁单
-- CREATE TABLE copy_trading_follower_pending_buy ( ... );

-- -- 账户余额历史表
-- CREATE TABLE copy_trading_account_balance_history ( ... );

-- -- 余额调整表
-- CREATE TABLE copy_trading_balance_adjustments ( ... );

-- -- 资金账本
-- CREATE TABLE strategy_account_ledger ( ... );
