-- WeatherTaker 全量建表 (V1 + V2 + V3 + V4 合并)
-- 新环境直接执行此文件即可，无需逐个跑增量迁移
-- ⚠️ 会 DROP 已有表，勿在生产环境直接执行

DROP DATABASE IF EXISTS weathertaker;
CREATE DATABASE weathertaker DEFAULT CHARACTER SET utf8mb4;
USE weathertaker;

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
  UNIQUE KEY idx_wallet_address (wallet_address),
  INDEX idx_accounts_owner_user_id (owner_user_id)
);

-- ============================================================
-- Leader 管理表
-- ============================================================
CREATE TABLE leaders (
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
-- 跟单配置表 (含 V2 size_mode + V3 sweep_confirm_window)
-- ============================================================
CREATE TABLE copy_trading_configs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  leader_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Leader 代理钱包地址',
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  gtd_expiration_sec INT NOT NULL DEFAULT 1800 COMMENT 'GTD订单过期时间（秒），默认30分钟',
  buy_size DECIMAL(20, 4) NOT NULL DEFAULT 100.0000 COMMENT '固定买入数量（shares）',
  size_mode VARCHAR(10) NOT NULL DEFAULT 'fixed' COMMENT '份额模式: fixed=固定数量, ratio=按leader比例',
  size_ratio DECIMAL(10, 4) NOT NULL DEFAULT 1.0000 COMMENT '比例模式下的倍率',
  size_min DECIMAL(20, 4) NOT NULL DEFAULT 0.0000 COMMENT '比例模式下最小下单份额',
  sweep_confirm_window_ms INT NOT NULL DEFAULT 0 COMMENT '天气扫单确认窗口(ms)，0=不参与天气扫单入场',
  enabled TINYINT(1) DEFAULT 0 COMMENT '是否启用',
  owner_user_id INT NULL,
  created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  UNIQUE KEY idx_leader_follower (leader_proxy_wallet, follower_proxy_wallet),
  INDEX idx_copy_trading_configs_owner_user_id (owner_user_id)
);

-- ============================================================
-- 跟单配置关联 asset 列表
-- ============================================================
CREATE TABLE copy_trading_config_assets (
  config_id INT NOT NULL,
  asset_id VARCHAR(128) NOT NULL,
  last_seen_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (config_id, asset_id),
  INDEX idx_config_last_seen (config_id, last_seen_at)
);

-- ============================================================
-- 跟单交易记录表
-- ============================================================
CREATE TABLE copy_trading_orders (
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
  err_msg VARCHAR(255) COMMENT '错误信息',
  signal_latency_ms INT DEFAULT NULL COMMENT '信号到达→下单结果耗时(ms)',
  sweep_to_leader_ms INT DEFAULT NULL COMMENT 'sweep入场→leader信号确认延迟(ms)',
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  INDEX idx_config_id_asset_status (config_id, asset_id, status),
  INDEX idx_follower (follower),
  INDEX idx_created_at (created_at)
);

-- ============================================================
-- Asset 市场名称缓存表
-- ============================================================
CREATE TABLE copy_trading_asset_questions (
  asset_id VARCHAR(128) NOT NULL PRIMARY KEY,
  question VARCHAR(512) NOT NULL,
  outcome VARCHAR(128) DEFAULT '' COMMENT 'Asset 对应 outcome',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR)
);

-- ============================================================
-- Follower 仓位表
-- ============================================================
CREATE TABLE copy_trading_follower_positions (
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  asset_id VARCHAR(128) NOT NULL COMMENT '资产 ID',
  size DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT '持仓数量',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (follower_proxy_wallet, asset_id)
);

-- ============================================================
-- Follower 待成交 SELL 锁单
-- ============================================================
CREATE TABLE copy_trading_follower_pending_sell (
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
CREATE TABLE copy_trading_follower_pending_buy (
  follower_proxy_wallet VARCHAR(128) NOT NULL COMMENT 'Follower 代理钱包地址',
  asset_id VARCHAR(128) NOT NULL COMMENT '资产 ID',
  pending DECIMAL(20, 8) NOT NULL DEFAULT 0 COMMENT '锁定中的买单数量',
  question VARCHAR(512) COMMENT '市场标题',
  updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  PRIMARY KEY (follower_proxy_wallet, asset_id)
);

-- ============================================================
-- 账户余额历史表
-- ============================================================
CREATE TABLE copy_trading_account_balance_history (
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
CREATE TABLE copy_trading_balance_adjustments (
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

  sort_order INT NOT NULL DEFAULT 0 COMMENT '前端排序，值越小越靠前',
  metadata JSON NULL COMMENT '预留扩展信息',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间 UTC，毫秒级',
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '最后修改时间 UTC，毫秒级',

  PRIMARY KEY (id),
  UNIQUE KEY uq_weather_cities_city_slug (city_slug),
  KEY idx_weather_cities_enabled_sort (enabled, sort_order),
  CONSTRAINT chk_monitor_highest_requires_market CHECK (monitor_highest = 0 OR has_highest_market = 1),
  CONSTRAINT chk_monitor_lowest_requires_market CHECK (monitor_lowest = 0 OR has_lowest_market = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='天气城市监听配置';

-- ============================================================
-- 天气通知历史 (含 is_from_main 生成列 + 全部索引)
-- ============================================================
CREATE TABLE weather_notifications (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '通知历史主键',
  notification_key VARCHAR(512) NOT NULL COMMENT '全局幂等键，防止重复记录',
  occurred_at DATETIME(3) NOT NULL COMMENT '事件发生时间 UTC，毫秒级',
  event_type ENUM('sweep', 'no_longer_possible', 'market_resolved', 'event_started') NOT NULL COMMENT '天气通知类型',
  event_slug VARCHAR(255) NOT NULL COMMENT 'Polymarket Event slug',
  city VARCHAR(100) NOT NULL COMMENT '城市展示名',
  city_slug VARCHAR(100) NOT NULL COMMENT '城市 slug',
  direction ENUM('highest', 'lowest') NOT NULL COMMENT '最高温或最低温市场',
  local_date DATE NOT NULL COMMENT '城市当地时区的市场日期',
  market_slug VARCHAR(255) NULL COMMENT '触发通知的温度区间 market slug',
  temperature_label VARCHAR(100) NULL COMMENT '温度区间显示名，例如 88-89°F',
  outcome ENUM('yes', 'no') NULL COMMENT '触发通知的 token 方向',
  main_market_slug VARCHAR(255) NULL COMMENT '触发时主监控器的 market slug',
  main_temperature_label VARCHAR(100) NULL COMMENT '触发时主监控器的温度区间标签',
  main_outcome ENUM('yes', 'no') NULL COMMENT '触发时主监控器的概率偏向（mid-price 较高的一方）',
  is_from_main TINYINT(1) GENERATED ALWAYS AS (main_market_slug <=> market_slug) STORED COMMENT '是否来自主监控器（生成列）',
  token_id VARCHAR(100) NULL COMMENT 'Polymarket CLOB token ID',
  status ENUM('monitoring', 'resolved', 'exhausted') NULL COMMENT '该 Event 当时的监听状态',
  reason VARCHAR(255) NULL COMMENT '触发原因，例如 ask_level_removed_0.98',
  message TEXT NOT NULL COMMENT '完整通知正文',
  payload JSON NOT NULL COMMENT '订单簿快照及其他结构化详情',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '写入数据库时间 UTC，毫秒级',

  PRIMARY KEY (id),
  UNIQUE KEY uq_weather_notifications_notification_key (notification_key),
  KEY idx_weather_notifications_event_time (event_slug, occurred_at DESC, id DESC),
  KEY idx_weather_notifications_city_date (city_slug, direction, local_date),
  KEY idx_weather_notifications_recent (occurred_at DESC, id DESC),
  KEY idx_weather_notifications_market (market_slug, event_type, occurred_at DESC),
  KEY idx_weather_notifications_main_market (main_market_slug, occurred_at DESC),
  KEY idx_weather_notifications_main_temp (main_temperature_label, occurred_at DESC),
  KEY idx_weather_notifications_filter_combo (is_from_main, reason, outcome, direction, occurred_at DESC),
  KEY idx_weather_notifications_reason (reason, occurred_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='天气市场通知历史';

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
  ('Sao Paulo', 'sao-paulo', 'America/Sao_Paulo', 1, 0, 1, 0, 1, 80),
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
