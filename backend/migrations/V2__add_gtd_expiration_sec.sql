ALTER TABLE copy_trading_configs
    ADD COLUMN gtd_expiration_sec INT NOT NULL DEFAULT 1800 COMMENT 'GTD订单过期时间（秒），默认30分钟' AFTER allowance;
