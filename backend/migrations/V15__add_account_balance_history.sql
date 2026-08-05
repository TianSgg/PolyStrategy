CREATE TABLE IF NOT EXISTS copy_trading_account_balance_history (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  proxy_wallet VARCHAR(128) NOT NULL,
  total_value DECIMAL(20,6) NOT NULL COMMENT '账户总价值(余额+持仓)',
  created_at DATETIME(3) NOT NULL COMMENT '记录时间(UTC+8)',
  INDEX idx_abh_wallet_time (proxy_wallet, created_at),
  INDEX idx_abh_time (created_at)
);
