ALTER TABLE copy_trading_orders
  ADD COLUMN sweep_to_leader_ms INT DEFAULT NULL COMMENT 'sweep入场→leader信号确认延迟(ms)'
  AFTER signal_latency_ms;

-- 迁移历史数据：sweep 订单的 signal_latency_ms 实际记录的是 sweep→leader 延迟
UPDATE copy_trading_orders
SET sweep_to_leader_ms = signal_latency_ms,
    signal_latency_ms = NULL
WHERE leader_tx_hash = 'WEATHER_SWEEP'
  AND signal_latency_ms IS NOT NULL;
