-- V7: 信号数据领域表 — 两个信号服务（天气/Leader）共同写入
-- 先于 V8 策略执行表独立上线，验证信号保存与查询

-- ============================================================
-- 规范化业务信号
-- ============================================================
CREATE TABLE signal_events (
  id CHAR(36) NOT NULL COMMENT '信号服务生成的 UUID',
  source_type VARCHAR(32) NOT NULL COMMENT 'weather / leader',
  source_event_id VARCHAR(256) NOT NULL COMMENT '上游稳定幂等键',
  event_type VARCHAR(64) NOT NULL COMMENT 'sweep / leader_buy 等',
  token_id VARCHAR(128) NOT NULL,
  outcome VARCHAR(8) DEFAULT NULL COMMENT 'yes / no',
  side VARCHAR(8) DEFAULT NULL COMMENT 'BUY / SELL',
  leader_proxy_wallet VARCHAR(128) DEFAULT NULL,
  occurred_at DATETIME(3) NOT NULL COMMENT '上游事件发生时间',
  received_at DATETIME(3) NOT NULL COMMENT '本服务收到时间',
  received_monotonic_ns BIGINT UNSIGNED DEFAULT NULL COMMENT '延迟审计用',
  payload_json JSON NOT NULL COMMENT '原始来源 payload',
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),

  PRIMARY KEY (id),
  UNIQUE KEY uq_signal_events_source (source_type, source_event_id),
  KEY idx_signal_events_token_time (token_id, received_at),
  KEY idx_signal_events_leader_time (leader_proxy_wallet, received_at),
  KEY idx_signal_events_type_time (event_type, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='规范化业务信号';

-- ============================================================
-- 信号触发时的盘口上下文
-- ============================================================
CREATE TABLE signal_market_contexts (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id CHAR(36) NOT NULL COMMENT '关联 signal_events.id',
  token_id VARCHAR(128) NOT NULL,
  observed_at DATETIME(3) NOT NULL COMMENT '盘口观测时间',
  tick_size DECIMAL(18,8) DEFAULT NULL,
  best_bid DECIMAL(36,18) DEFAULT NULL,
  best_ask DECIMAL(36,18) DEFAULT NULL,
  bid_depth DECIMAL(36,18) DEFAULT NULL,
  ask_depth DECIMAL(36,18) DEFAULT NULL,
  bid_levels INT UNSIGNED DEFAULT NULL,
  ask_levels INT UNSIGNED DEFAULT NULL,
  book_summary_json JSON DEFAULT NULL,

  PRIMARY KEY (id),
  KEY idx_signal_market_ctx_signal (signal_id, observed_at),
  KEY idx_signal_market_ctx_token (token_id, observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='信号触发时盘口上下文';

-- ============================================================
-- 可版本化的分析特征
-- ============================================================
CREATE TABLE signal_analysis_features (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id CHAR(36) NOT NULL,
  feature_set VARCHAR(64) NOT NULL COMMENT '如 weather_sweep_v1',
  feature_version SMALLINT UNSIGNED NOT NULL,
  computed_at DATETIME(3) NOT NULL,
  features_json JSON NOT NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_signal_features_set_ver (signal_id, feature_set, feature_version),
  KEY idx_signal_features_set_time (feature_set, computed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='信号分析特征';
