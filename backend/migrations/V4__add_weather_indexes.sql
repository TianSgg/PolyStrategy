-- V4: 天气通知表增加 is_from_main 生成列 + 高频查询索引
-- 对应参考项目 v2_add_indexes_and_is_from_main.sql
-- 安全: 可在有数据的表上执行

-- Step 1: 添加 is_from_main 生成列 (NULL-safe 比较)
ALTER TABLE weather_notifications
  ADD COLUMN is_from_main TINYINT(1) GENERATED ALWAYS AS (main_market_slug <=> market_slug) STORED
  COMMENT '是否来自主监控器（生成列）'
  AFTER main_outcome;

-- Step 2: 添加高频查询索引
CREATE INDEX idx_weather_notifications_recent
  ON weather_notifications (occurred_at DESC, id DESC);

CREATE INDEX idx_weather_notifications_market
  ON weather_notifications (market_slug, event_type, occurred_at DESC);

CREATE INDEX idx_weather_notifications_main_market
  ON weather_notifications (main_market_slug, occurred_at DESC);

CREATE INDEX idx_weather_notifications_main_temp
  ON weather_notifications (main_temperature_label, occurred_at DESC);

CREATE INDEX idx_weather_notifications_filter_combo
  ON weather_notifications (is_from_main, reason, outcome, direction, occurred_at DESC);

CREATE INDEX idx_weather_notifications_reason
  ON weather_notifications (reason, occurred_at DESC);
