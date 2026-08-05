CREATE TABLE IF NOT EXISTS copy_trading_balance_adjustments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  proxy_wallet VARCHAR(128) NOT NULL,
  delta DECIMAL(20,6) NOT NULL COMMENT '充值为正,提现为负',
  applied_at DATETIME(3) NOT NULL COMMENT '生效时间点(快照时间戳)',
  note VARCHAR(255) DEFAULT '' COMMENT '备注',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_ba_wallet (proxy_wallet),
  INDEX idx_ba_applied (applied_at)
);
