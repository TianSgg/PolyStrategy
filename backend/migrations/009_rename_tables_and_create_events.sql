-- Migration 009: 表重命名 + 字段重命名 + 新建 weather_sweep_events
-- 执行前确认无活跃写入

-- ① 重命名信号表
RENAME TABLE signal_weather_events TO weather_orderbook_signals;
RENAME TABLE leader_signal_events TO leader_signals;

-- ② weather_orderbook_signals 字段重命名
ALTER TABLE weather_orderbook_signals
  CHANGE COLUMN notification_key signal_id VARCHAR(512) NOT NULL COMMENT '信号唯一标识',
  CHANGE COLUMN event_type signal_type ENUM('sweep', 'no_longer_possible', 'market_resolved', 'event_started') NOT NULL COMMENT '信号类型';

-- 更新索引名（DROP + ADD，因为 RENAME INDEX 需要 MySQL 5.7+）
ALTER TABLE weather_orderbook_signals
  DROP KEY uq_signal_weather_events_notification_key,
  ADD UNIQUE KEY uq_signal_id (signal_id);

ALTER TABLE weather_orderbook_signals
  DROP KEY idx_signal_weather_events_event_time,
  ADD KEY idx_event_time (event_slug, occurred_at DESC, id DESC);

ALTER TABLE weather_orderbook_signals
  DROP KEY idx_signal_weather_events_city_date,
  ADD KEY idx_city_date (city_slug, direction, local_date);

ALTER TABLE weather_orderbook_signals
  DROP KEY idx_signal_weather_events_recent,
  ADD KEY idx_recent (occurred_at DESC, id DESC);

-- 删除不再需要的冗余索引（精简）
ALTER TABLE weather_orderbook_signals
  DROP KEY IF EXISTS idx_signal_weather_events_market,
  DROP KEY IF EXISTS idx_signal_weather_events_main_market,
  DROP KEY IF EXISTS idx_signal_weather_events_main_temp,
  DROP KEY IF EXISTS idx_signal_weather_events_filter_combo,
  DROP KEY IF EXISTS idx_signal_weather_events_reason;

-- ③ leader_signals: 改为自增 id + 独立 signal_id
ALTER TABLE leader_signals
  ADD COLUMN _new_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT FIRST,
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (_new_id);

ALTER TABLE leader_signals
  CHANGE COLUMN id signal_id VARCHAR(256) NOT NULL COMMENT '信号唯一标识',
  CHANGE COLUMN _new_id id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT FIRST,
  CHANGE COLUMN event_type signal_type VARCHAR(64) NOT NULL DEFAULT 'leader_buy';

ALTER TABLE leader_signals
  ADD UNIQUE KEY uq_signal_id (signal_id);

ALTER TABLE leader_signals
  DROP KEY IF EXISTS idx_leader_signal_token_time,
  ADD KEY idx_token_time (token_id, received_at);

ALTER TABLE leader_signals
  DROP KEY IF EXISTS idx_leader_signal_wallet_time,
  ADD KEY idx_wallet_time (leader_proxy_wallet, received_at);

-- ④ 重命名策略配置表
RENAME TABLE strategy_sweep_configs TO weather_sweep_configs;

ALTER TABLE weather_sweep_configs
  DROP KEY IF EXISTS uq_sweep_configs_owner_name,
  ADD UNIQUE KEY uq_owner_name (owner_user_id, name);

ALTER TABLE weather_sweep_configs
  DROP KEY IF EXISTS idx_sweep_configs_account,
  ADD KEY idx_account (account_id, enabled);

-- ⑤ 新建 weather_sweep_events 表
CREATE TABLE IF NOT EXISTS weather_sweep_events (
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
  step VARCHAR(64) NOT NULL,
  sequence_no INT UNSIGNED NOT NULL,
  detail JSON NOT NULL,
  occurred_at DATETIME(3) NOT NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_event_seq (event_id, sequence_no),
  KEY idx_owner_wallet (owner_user_id, proxy_wallet, occurred_at DESC),
  KEY idx_config_event (config_id, event_id),
  KEY idx_token (token_id, occurred_at DESC),
  KEY idx_event_slug (event_slug, occurred_at DESC),
  KEY idx_occurred (occurred_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Weather Sweep 执行事件日志';

-- ⑥ 删除旧策略表（已被 weather_sweep_events 替代）
DROP TABLE IF EXISTS strategy_run_events;
DROP TABLE IF EXISTS strategy_orders;
DROP TABLE IF EXISTS strategy_runs;
