-- PolyStrategy 全量建表
-- 新环境直接执行此文件即可
-- ⚠️ 会 DROP 已有表，勿在生产环境直接执行

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
-- 账户表
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
  deleted_at DATETIME(3) DEFAULT NULL,
  UNIQUE KEY idx_wallet_address (wallet_address),
  INDEX idx_accounts_owner_user_id (owner_user_id)
);


-- ============================================================
-- 天气城市监听配置
-- ============================================================
CREATE TABLE weather_cities (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '城市配置主键',
  city_name VARCHAR(100) NOT NULL COMMENT '城市展示名，例如 Miami',
  city_slug VARCHAR(100) NOT NULL COMMENT 'Polymarket 城市 slug，例如 miami',
  timezone VARCHAR(64) NOT NULL COMMENT 'IANA 时区，例如 America/New_York',

  has_highest_market TINYINT(1) NOT NULL DEFAULT 0 COMMENT '该城市是否存在最高温市场',
  has_lowest_market TINYINT(1) NOT NULL DEFAULT 0 COMMENT '该城市是否存在最低温市场',
  monitor_highest TINYINT(1) NOT NULL DEFAULT 0 COMMENT '本项目是否监听最高温市场',
  monitor_lowest TINYINT(1) NOT NULL DEFAULT 0 COMMENT '本项目是否监听最低温市场',
  enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '城市总开关；0 时不监听任何方向',

  metadata JSON NULL COMMENT '预留扩展信息',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间 UTC，毫秒级',
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '最后修改时间 UTC，毫秒级',

  PRIMARY KEY (id),
  UNIQUE KEY uq_weather_cities_city_slug (city_slug),
  KEY idx_weather_cities_enabled (enabled),
  CONSTRAINT chk_monitor_highest_requires_market CHECK (monitor_highest = 0 OR has_highest_market = 1),
  CONSTRAINT chk_monitor_lowest_requires_market CHECK (monitor_lowest = 0 OR has_lowest_market = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='天气城市监听配置';

-- ============================================================
-- 天气信号记录 (signal_weather_orderbook)
-- ============================================================
CREATE TABLE weather_orderbook_signals (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id VARCHAR(512) NOT NULL COMMENT '信号唯一标识',
  signal_type ENUM('sweep', 'no_longer_possible', 'market_resolved', 'event_started') NOT NULL COMMENT '信号类型',
  occurred_at DATETIME(3) NOT NULL COMMENT '信号发生时间 UTC',
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
  payload JSON NOT NULL COMMENT '订单簿快照等详情',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  UNIQUE KEY uq_signal_id (signal_id),
  KEY idx_event_time (event_slug, occurred_at DESC, id DESC),
  KEY idx_city_date (city_slug, direction, local_date),
  KEY idx_recent (occurred_at DESC, id DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='天气信号记录';

-- ============================================================
-- 天气城市种子数据
-- ============================================================
INSERT INTO weather_cities (
  city_name, city_slug, timezone,
  has_highest_market, has_lowest_market,
  monitor_highest, monitor_lowest, enabled
) VALUES
  ('Taipei', 'taipei', 'Asia/Taipei', 1, 0, 1, 0, 1),
  ('Miami', 'miami', 'America/New_York', 1, 1, 1, 1, 1),
  ('Kuala Lumpur', 'kuala-lumpur', 'Asia/Kuala_Lumpur', 1, 0, 1, 0, 1),
  ('Paris', 'paris', 'Europe/Paris', 1, 1, 1, 1, 1),
  ('Mexico City', 'mexico-city', 'America/Mexico_City', 1, 0, 1, 0, 1),
  ('New York City', 'nyc', 'America/New_York', 1, 1, 1, 1, 1),
  ('Panama City', 'panama-city', 'America/Panama', 1, 0, 1, 0, 1),
  ('Sao Paulo', 'sao-paulo', 'America/Sao_Paulo', 1, 1, 1, 1, 1),
  ('Buenos Aires', 'buenos-aires', 'America/Argentina/Buenos_Aires', 1, 0, 1, 0, 1),
  ('Lucknow', 'lucknow', 'Asia/Kolkata', 1, 0, 1, 0, 1),
  ('Cape Town', 'cape-town', 'Africa/Johannesburg', 1, 0, 1, 0, 1),
  ('Karachi', 'karachi', 'Asia/Karachi', 1, 0, 1, 0, 1),
  ('London', 'london', 'Europe/London', 1, 1, 1, 1, 1),
  ('Wellington', 'wellington', 'Pacific/Auckland', 1, 0, 1, 0, 1),
  ('Tel Aviv', 'tel-aviv', 'Asia/Jerusalem', 1, 0, 1, 0, 1),
  ('Tokyo', 'tokyo', 'Asia/Tokyo', 1, 1, 1, 1, 1),
  ('Denver', 'denver', 'America/Denver', 1, 0, 1, 0, 1),
  ('Manila', 'manila', 'Asia/Manila', 1, 0, 1, 0, 1),
  ('Toronto', 'toronto', 'America/Toronto', 1, 0, 1, 0, 1),
  ('Amsterdam', 'amsterdam', 'Europe/Amsterdam', 1, 0, 1, 0, 1),
  ('Ankara', 'ankara', 'Europe/Istanbul', 1, 0, 1, 0, 1),
  ('Atlanta', 'atlanta', 'America/New_York', 1, 0, 1, 0, 1),
  ('Austin', 'austin', 'America/Chicago', 1, 0, 1, 0, 1),
  ('Beijing', 'beijing', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('Busan', 'busan', 'Asia/Seoul', 1, 0, 1, 0, 1),
  ('Chengdu', 'chengdu', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('Chicago', 'chicago', 'America/Chicago', 1, 0, 1, 0, 1),
  ('Chongqing', 'chongqing', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('Dallas', 'dallas', 'America/Chicago', 1, 0, 1, 0, 1),
  ('Guangzhou', 'guangzhou', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('Helsinki', 'helsinki', 'Europe/Helsinki', 1, 0, 1, 0, 1),
  ('Hong Kong', 'hong-kong', 'Asia/Hong_Kong', 1, 1, 1, 1, 1),
  ('Houston', 'houston', 'America/Chicago', 1, 0, 1, 0, 1),
  ('Istanbul', 'istanbul', 'Europe/Istanbul', 1, 0, 1, 0, 1),
  ('Jeddah', 'jeddah', 'Asia/Riyadh', 1, 0, 1, 0, 1),
  ('Jinan', 'jinan', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('Los Angeles', 'los-angeles', 'America/Los_Angeles', 1, 0, 1, 0, 1),
  ('Madrid', 'madrid', 'Europe/Madrid', 1, 0, 1, 0, 1),
  ('Milan', 'milan', 'Europe/Rome', 1, 0, 1, 0, 1),
  ('Moscow', 'moscow', 'Europe/Moscow', 1, 0, 1, 0, 1),
  ('Munich', 'munich', 'Europe/Berlin', 1, 0, 1, 0, 1),
  ('Qingdao', 'qingdao', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('San Francisco', 'san-francisco', 'America/Los_Angeles', 1, 0, 1, 0, 1),
  ('Seattle', 'seattle', 'America/Los_Angeles', 1, 0, 1, 0, 1),
  ('Shanghai', 'shanghai', 'Asia/Shanghai', 1, 1, 1, 1, 1),
  ('Shenzhen', 'shenzhen', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('Singapore', 'singapore', 'Asia/Singapore', 1, 0, 1, 0, 1),
  ('Warsaw', 'warsaw', 'Europe/Warsaw', 1, 0, 1, 0, 1),
  ('Wuhan', 'wuhan', 'Asia/Shanghai', 1, 0, 1, 0, 1),
  ('Zhengzhou', 'zhengzhou', 'Asia/Shanghai', 1, 0, 1, 0, 1);


-- ============================================================
-- 策略配置: Weather Sweep
-- ============================================================
CREATE TABLE strategy_weather_sweep_configs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  owner_user_id INT NOT NULL,
  account_id INT NOT NULL,
  name VARCHAR(128) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 0,

  fixed_entry_shares DECIMAL(20,4) NOT NULL DEFAULT 100.0000,
  entry_wait_ms INT NOT NULL DEFAULT 1200000,
  sweep_outcome_filter VARCHAR(8) NOT NULL DEFAULT 'no',
  signal_source_filter VARCHAR(8) NOT NULL DEFAULT 'all',
  signal_threshold_filter VARCHAR(8) NOT NULL DEFAULT 'all',
  direction_filter VARCHAR(8) NOT NULL DEFAULT 'all',
  stop_loss_ratio DECIMAL(5,4) NOT NULL DEFAULT 0.6000,
  exit_wait_ms INT NOT NULL DEFAULT 5000,

  params_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  deleted_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  UNIQUE KEY uq_owner_name (owner_user_id, name),
  KEY idx_account (account_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Weather Sweep 策略配置（一个配置 = 一个实例）';

-- ============================================================
-- Weather Sweep 执行事件日志（一个 event = 多个 step）
-- ============================================================
CREATE TABLE strategy_weather_sweep_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- ① 配置相关
  owner_user_id INT NOT NULL,
  proxy_wallet VARCHAR(128) NOT NULL,
  config_id BIGINT UNSIGNED NOT NULL,
  config_snapshot JSON NULL,

  -- ② event 上下文
  event_id CHAR(36) NOT NULL,
  signal_id VARCHAR(512) NULL,
  token_id VARCHAR(128) NULL,
  market_slug VARCHAR(255) NULL,
  event_slug VARCHAR(255) NULL,

  -- ③ 执行记录本身
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Weather Sweep 执行事件日志';

-- ============================================================
-- Weather Sweep 交易摘要（每笔交易一行，实时更新状态和盈亏）
-- ============================================================
CREATE TABLE strategy_weather_sweep_trades (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- 关联
  event_id CHAR(36) NOT NULL,
  config_id BIGINT UNSIGNED NOT NULL,
  owner_user_id INT NOT NULL,
  proxy_wallet VARCHAR(128) NOT NULL,

  -- 市场信息
  signal_id VARCHAR(512) NULL,
  token_id VARCHAR(128) NULL,
  market_slug VARCHAR(255) NULL,
  event_slug VARCHAR(255) NULL,
  city VARCHAR(100) NULL,
  direction VARCHAR(16) NULL,

  -- 状态
  status ENUM('entry_working', 'exit_working', 'closed', 'exit_failed') NOT NULL DEFAULT 'entry_working',
  close_reason VARCHAR(64) NULL,

  -- 入场
  entry_price DECIMAL(10,4) NULL,
  entry_shares DECIMAL(20,4) NULL,
  entry_cost DECIMAL(20,6) NULL,
  entry_order_id VARCHAR(128) NULL,
  entered_at DATETIME(3) NULL,

  -- 出场
  exit_price DECIMAL(10,4) NULL,
  exit_shares DECIMAL(20,4) NULL,
  exit_revenue DECIMAL(20,6) NULL,
  exit_order_id VARCHAR(128) NULL,
  exited_at DATETIME(3) NULL,

  -- 盈亏
  pnl DECIMAL(20,6) NULL,
  pnl_pct DECIMAL(8,4) NULL,

  -- 时间
  duration_ms INT UNSIGNED NULL,
  started_at DATETIME(3) NOT NULL,
  closed_at DATETIME(3) NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_event_id (event_id),
  KEY idx_config_status (config_id, status),
  KEY idx_owner (owner_user_id, closed_at DESC),
  KEY idx_event_slug (event_slug, started_at DESC),
  KEY idx_status (status),
  KEY idx_closed_at (closed_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Weather Sweep 交易摘要';
