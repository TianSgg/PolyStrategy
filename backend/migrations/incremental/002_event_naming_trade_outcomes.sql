-- 002_event_naming_trade_outcomes.sql
-- Align Weather Sweep event names and trade summary fields with
-- doc/2026-08-31-event-naming-convention.md.

ALTER TABLE strategy_weather_sweep_trades
  ADD COLUMN outcome VARCHAR(8) NULL COMMENT 'Polymarket outcome: yes/no' AFTER direction,
  ADD COLUMN lifecycle_status ENUM('entry_working', 'exit_working', 'closed')
    NOT NULL DEFAULT 'entry_working' AFTER status,
  ADD COLUMN trade_outcome ENUM('completed', 'failed') NULL AFTER lifecycle_status,
  ADD COLUMN failure_reason VARCHAR(64) NULL AFTER close_reason,
  ADD COLUMN needs_attention TINYINT(1) NOT NULL DEFAULT 0 AFTER failure_reason,
  ADD COLUMN entry_order_size DECIMAL(20,4) NULL AFTER entry_cost,
  ADD COLUMN exit_order_size DECIMAL(20,4) NULL AFTER exit_revenue,
  ADD COLUMN temperature_label VARCHAR(100) NULL AFTER exited_at,
  ADD COLUMN is_from_main TINYINT(1) NOT NULL DEFAULT 1 AFTER temperature_label,
  ADD KEY idx_lifecycle_outcome (lifecycle_status, trade_outcome, needs_attention);

UPDATE strategy_weather_sweep_trades
SET lifecycle_status = CASE
    WHEN status = 'exit_failed' THEN 'closed'
    ELSE status
  END,
  trade_outcome = CASE
    WHEN status IN ('entry_working', 'exit_working') THEN NULL
    WHEN status = 'exit_failed' THEN 'failed'
    WHEN close_reason IN ('buy_failed', 'sell_failed') THEN 'failed'
    ELSE 'completed'
  END,
  failure_reason = CASE
    WHEN status = 'exit_failed' AND close_reason = 'force_exit' THEN 'exit_order_unfilled'
    WHEN close_reason = 'buy_failed' THEN 'buy_placement_failed'
    WHEN close_reason = 'sell_failed' THEN 'unknown_failure'
    ELSE NULL
  END,
  needs_attention = CASE
    WHEN status = 'exit_failed' THEN 1
    ELSE 0
  END;

UPDATE strategy_weather_sweep_trades
SET close_reason = 'normal_exit'
WHERE close_reason = 'tick_exit';

UPDATE strategy_weather_sweep_events SET step = 'buy_order_placed'
WHERE phase = 'entry' AND step = 'order_placed';

UPDATE strategy_weather_sweep_events SET step = 'buy_order_failed'
WHERE phase = 'entry' AND step = 'order_failed';

UPDATE strategy_weather_sweep_events SET step = 'fill_reconciled'
WHERE step = 'fill_reconcile';

UPDATE strategy_weather_sweep_events SET step = 'sell_retry_started'
WHERE step = 'sell_retry_start';

UPDATE strategy_weather_sweep_events SET step = 'sell_order_placed'
WHERE step = 'risk_sell_order_placed';

UPDATE strategy_weather_sweep_events SET step = 'buy_cancelled'
WHERE step = 'risk_cancel_buy';

UPDATE strategy_weather_sweep_events SET step = 'sell_cancelled'
WHERE step = 'risk_cancel_sell';

UPDATE strategy_weather_sweep_events SET step = 'dust_position_detected'
WHERE step = 'dust_position';

UPDATE strategy_weather_sweep_events SET step = 'force_exit_requested'
WHERE step = 'force_exit';

UPDATE strategy_weather_sweep_events
SET step = 'fill_reconcile_failed',
    detail = JSON_SET(detail, '$.failure_reason', 'fill_reconcile_failed')
WHERE step = 'exit_reconcile_failed';

UPDATE strategy_weather_sweep_events
SET step = CASE
    WHEN JSON_UNQUOTE(JSON_EXTRACT(detail, '$.side')) = 'BUY' THEN 'buy_cancel_failed'
    ELSE 'sell_cancel_failed'
  END,
  detail = JSON_SET(
    detail,
    '$.failure_reason',
    CASE
      WHEN JSON_UNQUOTE(JSON_EXTRACT(detail, '$.side')) = 'BUY' THEN 'buy_cancel_failed'
      ELSE 'sell_cancel_failed'
    END
  )
WHERE step = 'cancel_failed';

UPDATE strategy_weather_sweep_events
SET step = 'event_closed',
    detail = JSON_SET(
      detail,
      '$.outcome', 'failed',
      '$.close_reason', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.reason')), 'sell_failed')
    )
WHERE step IN ('exit_failed', 'sell_give_up');

UPDATE strategy_weather_sweep_events
SET detail = JSON_SET(
      detail,
      '$.outcome', 'completed',
      '$.close_reason', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.reason')), 'normal_exit')
    )
WHERE step = 'event_closed'
  AND JSON_EXTRACT(detail, '$.outcome') IS NULL;

UPDATE strategy_weather_sweep_events
SET detail = JSON_SET(detail, '$.close_reason', 'normal_exit')
WHERE JSON_UNQUOTE(JSON_EXTRACT(detail, '$.reason')) = 'tick_exit';
