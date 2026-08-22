-- V8: 策略执行领域表 — 配置、运行、订单与账本
-- 每种策略有独立的配置表（显式列），运行/订单/账本共用

-- ============================================================
-- 策略 1 配置: Sweep
-- ============================================================
CREATE TABLE strategy_sweep_configs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  owner_user_id INT NOT NULL,
  account_id INT NOT NULL,
  name VARCHAR(128) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 0,

  fixed_entry_shares DECIMAL(20,4) NOT NULL DEFAULT 100.0000,
  entry_wait_ms INT NOT NULL DEFAULT 30000,
  sweep_outcome_filter VARCHAR(8) NOT NULL DEFAULT 'no',
  stop_loss_ratio DECIMAL(5,4) NOT NULL DEFAULT 0.6000,
  exit_wait_ms INT NOT NULL DEFAULT 5000,
  tick_verify_retries SMALLINT UNSIGNED NOT NULL DEFAULT 3,
  tick_verify_backoff_ms INT NOT NULL DEFAULT 1000,

  params_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  deleted_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),

  PRIMARY KEY (id),
  UNIQUE KEY uq_sweep_configs_owner_name (owner_user_id, name),
  KEY idx_sweep_configs_account (account_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='策略 1 (Sweep) 配置';

-- ============================================================
-- 策略 2 配置: Leader
-- ============================================================
CREATE TABLE strategy_leader_configs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  owner_user_id INT NOT NULL,
  account_id INT NOT NULL,
  name VARCHAR(128) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 0,

  entry_size_mode VARCHAR(16) NOT NULL DEFAULT 'fixed',
  fixed_entry_shares DECIMAL(20,4) NOT NULL DEFAULT 100.0000,
  entry_wait_ms INT NOT NULL DEFAULT 30000,
  leader_proxy_wallet VARCHAR(128) NOT NULL COMMENT '要跟踪的 leader 地址',
  leader_outcome_filter VARCHAR(8) NOT NULL DEFAULT 'all',
  stop_loss_ratio DECIMAL(5,4) NOT NULL DEFAULT 0.6000,
  exit_wait_ms INT NOT NULL DEFAULT 5000,
  tick_verify_retries SMALLINT UNSIGNED NOT NULL DEFAULT 3,
  tick_verify_backoff_ms INT NOT NULL DEFAULT 1000,

  params_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  deleted_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),

  PRIMARY KEY (id),
  UNIQUE KEY uq_leader_configs_owner_name (owner_user_id, name),
  KEY idx_leader_configs_account (account_id, enabled),
  KEY idx_leader_configs_leader (leader_proxy_wallet, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='策略 2 (Leader) 配置';

-- ============================================================
-- 策略 3 配置: Sweep + Leader 确认
-- ============================================================
CREATE TABLE strategy_sweep_leader_configs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  owner_user_id INT NOT NULL,
  account_id INT NOT NULL,
  name VARCHAR(128) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 0,

  -- 入场
  fixed_probe_shares DECIMAL(20,4) NOT NULL DEFAULT 50.0000,
  entry_wait_ms INT NOT NULL DEFAULT 30000,
  sweep_outcome_filter VARCHAR(8) NOT NULL DEFAULT 'no',
  leader_proxy_wallet VARCHAR(128) NOT NULL COMMENT '确认用 leader 地址',
  leader_outcome_filter VARCHAR(8) NOT NULL DEFAULT 'all',
  leader_confirm_window_ms INT NOT NULL DEFAULT 60000,
  post_confirm_wait_ms INT NOT NULL DEFAULT 30000,

  -- 退场
  exit_mode VARCHAR(32) NOT NULL DEFAULT 'risk_tick_exit',
  exit_wait_ms INT NOT NULL DEFAULT 5000,

  -- 风控
  stop_loss_ratio DECIMAL(5,4) NOT NULL DEFAULT 0.6000,
  tick_verify_retries SMALLINT UNSIGNED NOT NULL DEFAULT 3,
  tick_verify_backoff_ms INT NOT NULL DEFAULT 1000,

  params_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
  deleted_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),

  PRIMARY KEY (id),
  UNIQUE KEY uq_sweep_leader_configs_owner_name (owner_user_id, name),
  KEY idx_sweep_leader_configs_account (account_id, enabled),
  KEY idx_sweep_leader_configs_leader (leader_proxy_wallet, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='策略 3 (Sweep+Leader) 配置';

-- ============================================================
-- 策略运行 (所有策略共用)
-- ============================================================
CREATE TABLE strategy_runs (
  id CHAR(36) NOT NULL COMMENT 'UUID',
  strategy_type VARCHAR(32) NOT NULL COMMENT 'sweep / leader / sweep_leader',
  strategy_config_id BIGINT UNSIGNED NOT NULL COMMENT '指向对应策略的配置表 ID',
  token_id VARCHAR(128) NOT NULL,
  market_slug VARCHAR(255) DEFAULT NULL,
  question VARCHAR(512) DEFAULT NULL,
  outcome VARCHAR(8) DEFAULT NULL,

  state VARCHAR(32) NOT NULL DEFAULT 'CREATED',
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
  active_key VARCHAR(256) DEFAULT NULL COMMENT '活跃时 {type}:{config_id}:{token_id}',

  params_snapshot_json JSON NOT NULL COMMENT '创建时冻结的配置参数',
  started_by_signal_id CHAR(36) DEFAULT NULL,

  avg_entry_price DECIMAL(36,18) DEFAULT NULL,
  entry_shares DECIMAL(36,18) NOT NULL DEFAULT 0,
  exited_shares DECIMAL(36,18) NOT NULL DEFAULT 0,
  risk_state VARCHAR(32) NOT NULL DEFAULT 'OFF',
  close_reason VARCHAR(64) DEFAULT NULL,

  version INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '乐观锁',
  started_at DATETIME(3) DEFAULT NULL,
  ended_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),

  PRIMARY KEY (id),
  UNIQUE KEY uq_strategy_runs_active (active_key),
  KEY idx_strategy_runs_config_status (strategy_type, strategy_config_id, status, started_at),
  KEY idx_strategy_runs_token (token_id, status, started_at),
  KEY idx_strategy_runs_signal (started_by_signal_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='策略运行聚合根';

-- ============================================================
-- 运行与信号关联
-- ============================================================
CREATE TABLE strategy_run_signals (
  run_id CHAR(36) NOT NULL,
  signal_id CHAR(36) NOT NULL,
  role VARCHAR(32) NOT NULL COMMENT 'entry_sweep / leader_confirm / ignored',
  linked_at DATETIME(3) NOT NULL,

  PRIMARY KEY (run_id, signal_id),
  KEY idx_run_signals_signal (signal_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='运行与信号 N:N 关联';

-- ============================================================
-- 运行时间线事件 (不可变)
-- ============================================================
CREATE TABLE strategy_run_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id CHAR(36) NOT NULL,
  sequence_no INT UNSIGNED NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  occurred_at DATETIME(3) NOT NULL,
  monotonic_ns BIGINT UNSIGNED DEFAULT NULL,
  payload_json JSON NOT NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_run_events_seq (run_id, sequence_no),
  KEY idx_run_events_type_time (event_type, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='运行时间线';

-- ============================================================
-- 策略订单
-- ============================================================
CREATE TABLE strategy_orders (
  id CHAR(36) NOT NULL COMMENT '内部 UUID',
  run_id CHAR(36) NOT NULL,
  clob_order_id VARCHAR(128) DEFAULT NULL,
  client_order_id VARCHAR(128) NOT NULL,
  purpose VARCHAR(32) NOT NULL COMMENT 'entry / add / exit_tick / exit_099 / risk_exit',
  execution_mode VARCHAR(16) NOT NULL DEFAULT 'normal',
  side VARCHAR(8) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
  limit_price DECIMAL(36,18) NOT NULL,
  requested_size DECIMAL(36,18) NOT NULL,
  matched_size DECIMAL(36,18) NOT NULL DEFAULT 0,
  avg_matched_price DECIMAL(36,18) DEFAULT NULL,
  error_code VARCHAR(64) DEFAULT NULL,
  error_message VARCHAR(512) DEFAULT NULL,
  sent_at DATETIME(3) DEFAULT NULL,
  responded_at DATETIME(3) DEFAULT NULL,
  closed_at DATETIME(3) DEFAULT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  updated_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),

  PRIMARY KEY (id),
  UNIQUE KEY uq_strategy_orders_clob (clob_order_id),
  UNIQUE KEY uq_strategy_orders_client (client_order_id),
  KEY idx_strategy_orders_run (run_id, created_at),
  KEY idx_strategy_orders_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='策略订单';

-- ============================================================
-- 资金账本 (不可变)
-- ============================================================
CREATE TABLE strategy_account_ledger (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id INT NOT NULL,
  run_id CHAR(36) DEFAULT NULL,
  order_id CHAR(36) DEFAULT NULL,
  token_id VARCHAR(128) DEFAULT NULL,
  entry_type VARCHAR(32) NOT NULL,
  cash_delta DECIMAL(36,18) NOT NULL DEFAULT 0,
  shares_delta DECIMAL(36,18) NOT NULL DEFAULT 0,
  reserved_cash_delta DECIMAL(36,18) NOT NULL DEFAULT 0,
  reserved_shares_delta DECIMAL(36,18) NOT NULL DEFAULT 0,
  dedupe_key VARCHAR(256) NOT NULL,
  occurred_at DATETIME(3) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
  metadata_json JSON DEFAULT NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_ledger_dedupe (dedupe_key),
  KEY idx_ledger_account_time (account_id, occurred_at),
  KEY idx_ledger_run (run_id, occurred_at),
  KEY idx_ledger_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='策略资金账本';

-- ============================================================
-- 运行诊断快照 (可选)
-- ============================================================
CREATE TABLE strategy_run_snapshots (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id CHAR(36) NOT NULL,
  snapshot_at DATETIME(3) NOT NULL,
  position_shares DECIMAL(36,18) NOT NULL DEFAULT 0,
  pending_buy_shares DECIMAL(36,18) NOT NULL DEFAULT 0,
  pending_sell_shares DECIMAL(36,18) NOT NULL DEFAULT 0,
  avg_entry_price DECIMAL(36,18) DEFAULT NULL,
  best_bid DECIMAL(36,18) DEFAULT NULL,
  best_ask DECIMAL(36,18) DEFAULT NULL,
  risk_state VARCHAR(32) NOT NULL,
  payload_json JSON DEFAULT NULL,

  PRIMARY KEY (id),
  KEY idx_run_snapshots_run_time (run_id, snapshot_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='运行诊断快照';
