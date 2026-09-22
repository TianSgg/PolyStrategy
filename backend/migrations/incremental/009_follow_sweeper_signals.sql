-- 009: Follow Weather Sweeper 信号记录表
-- 记录 Predexon WS 推送的所有 leader 操作（BUY + SELL），用于审计和分析

CREATE TABLE IF NOT EXISTS strategy_follow_weather_sweeper_signals (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- Predexon 事件标识
  tx_hash VARCHAR(128) NOT NULL,
  log_index VARCHAR(32) NULL,
  order_hash VARCHAR(128) NULL,

  -- leader 信息
  leader_wallet VARCHAR(128) NOT NULL COMMENT 'maker address (user)',
  taker VARCHAR(128) NULL,
  role ENUM('maker','taker') NOT NULL DEFAULT 'maker',

  -- 交易方向和价格
  side ENUM('BUY','SELL') NOT NULL,
  price DECIMAL(10,6) NOT NULL,
  shares BIGINT UNSIGNED NULL COMMENT 'raw shares (6 decimals)',
  shares_normalized DECIMAL(20,6) NULL,
  fee DECIMAL(20,6) NULL,

  -- token 信息
  token_id VARCHAR(256) NOT NULL,
  token_label VARCHAR(32) NULL,
  outcome VARCHAR(32) NULL,
  outcome_index TINYINT UNSIGNED NULL,
  complement_token_id VARCHAR(256) NULL,
  complement_token_label VARCHAR(32) NULL,

  -- 市场信息
  condition_id VARCHAR(128) NULL,
  market_slug VARCHAR(255) NULL,
  market_id VARCHAR(128) NULL,
  title VARCHAR(512) NULL,
  is_neg_risk TINYINT(1) NOT NULL DEFAULT 0,

  -- 元数据
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
