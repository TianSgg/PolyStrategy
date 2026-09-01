-- 003_no_cash_skipped_outcome.sql
-- Treat insufficient cash before BUY as a skipped trade, not a failed trade.

ALTER TABLE strategy_weather_sweep_trades
  MODIFY COLUMN trade_outcome ENUM('completed', 'failed', 'skipped') NULL;

UPDATE strategy_weather_sweep_trades t
JOIN strategy_weather_sweep_events e ON e.event_id = t.event_id
SET t.close_reason = 'no_cash',
    t.trade_outcome = 'skipped',
    t.failure_reason = NULL,
    t.needs_attention = 0
WHERE t.close_reason = 'buy_failed'
  AND e.phase = 'entry'
  AND e.step IN ('order_failed', 'buy_order_failed', 'buy_order_skipped')
  AND JSON_UNQUOTE(JSON_EXTRACT(e.detail, '$.status')) = 'no_cash';

UPDATE strategy_weather_sweep_events
SET step = 'buy_order_skipped'
WHERE phase = 'entry'
  AND step IN ('order_failed', 'buy_order_failed')
  AND JSON_UNQUOTE(JSON_EXTRACT(detail, '$.status')) = 'no_cash';

UPDATE strategy_weather_sweep_events
SET detail = JSON_SET(
    JSON_REMOVE(detail, '$.failure_reason', '$.error'),
    '$.reason', 'no_cash',
    '$.close_reason', 'no_cash',
    '$.outcome', 'skipped'
  )
WHERE step = 'event_closed'
  AND JSON_UNQUOTE(JSON_EXTRACT(detail, '$.reason')) = 'buy_failed'
  AND JSON_UNQUOTE(JSON_EXTRACT(detail, '$.error')) LIKE 'no_cash:%';
