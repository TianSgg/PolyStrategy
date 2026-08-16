-- 跟单配置：天气扫单确认窗口
ALTER TABLE copy_trading_configs
  ADD COLUMN sweep_confirm_window_ms INT NOT NULL DEFAULT 0 COMMENT '天气扫单确认窗口(ms)，0=不参与天气扫单入场' AFTER size_min;

-- ============================================================
-- 天气城市监听配置
-- ============================================================
CREATE TABLE IF NOT EXISTS weather_cities (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  city_name VARCHAR(100) NOT NULL COMMENT '城市展示名',
  city_slug VARCHAR(100) NOT NULL COMMENT 'Polymarket 城市 slug',
  timezone VARCHAR(64) NOT NULL COMMENT 'IANA 时区',

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
-- 天气通知历史
-- ============================================================
CREATE TABLE IF NOT EXISTS weather_notifications (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  notification_key VARCHAR(512) NOT NULL COMMENT '全局幂等键',
  occurred_at DATETIME(3) NOT NULL COMMENT '事件发生时间 UTC',
  event_type ENUM('sweep', 'no_longer_possible', 'market_resolved', 'event_started') NOT NULL,
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
  token_id VARCHAR(100) NULL,
  status ENUM('monitoring', 'resolved', 'exhausted') NULL,
  reason VARCHAR(255) NULL,
  message TEXT NOT NULL,
  payload JSON NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id),
  UNIQUE KEY uq_weather_notifications_notification_key (notification_key),
  KEY idx_weather_notifications_event_time (event_slug, occurred_at DESC, id DESC),
  KEY idx_weather_notifications_city_date (city_slug, direction, local_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- 天气城市种子数据
-- ============================================================
INSERT IGNORE INTO weather_cities (
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
