-- Adopt the canonical trade/event storage contract from
-- doc/2026-09-02-trade-event-schema-and-ui-spec.md.
--
-- This migration intentionally drops the old presentation/status columns.
-- The project is still in construction and existing test data is disposable.

UPDATE strategy_weather_sweep_events
SET phase = CASE
  WHEN phase IN ('exit_risk', 'exit_force') THEN 'exit'
  ELSE 'entry'
END;

ALTER TABLE strategy_weather_sweep_events
  MODIFY COLUMN phase ENUM('entry', 'exit') NOT NULL DEFAULT 'entry';

ALTER TABLE strategy_weather_sweep_events
  DROP COLUMN owner_user_id,
  DROP COLUMN proxy_wallet,
  DROP COLUMN config_id,
  DROP COLUMN config_snapshot,
  DROP COLUMN signal_id,
  DROP COLUMN token_id,
  DROP COLUMN market_slug,
  DROP COLUMN event_slug,
  DROP INDEX idx_owner_wallet,
  DROP INDEX idx_config_event,
  DROP INDEX idx_token,
  DROP INDEX idx_event_slug;

ALTER TABLE strategy_weather_sweep_trades
  ADD COLUMN params_version INT UNSIGNED NOT NULL DEFAULT 1 AFTER config_id,
  ADD COLUMN config_snapshot JSON NULL AFTER params_version,
  ADD COLUMN phase_new ENUM('entry', 'exit', 'closed') NOT NULL DEFAULT 'entry' AFTER is_from_main;

UPDATE strategy_weather_sweep_trades
SET phase_new = CASE
  WHEN status = 'entry_working' THEN 'entry'
  WHEN status = 'exit_working' THEN 'exit'
  ELSE 'closed'
END;

ALTER TABLE strategy_weather_sweep_trades
  DROP INDEX idx_config_status,
  DROP INDEX idx_lifecycle_outcome,
  DROP INDEX idx_status,
  DROP COLUMN status,
  DROP COLUMN lifecycle_status,
  DROP COLUMN trade_outcome,
  DROP COLUMN failure_reason,
  DROP COLUMN needs_attention,
  CHANGE COLUMN phase_new phase ENUM('entry', 'exit', 'closed') NOT NULL DEFAULT 'entry',
  ADD COLUMN entry_started_at DATETIME(3) NULL AFTER entry_order_id,
  ADD COLUMN exit_started_at DATETIME(3) NULL AFTER exit_order_id,
  ADD KEY idx_config_phase (config_id, phase),
  ADD KEY idx_phase (phase);
