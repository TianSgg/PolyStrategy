ALTER TABLE copy_trading_configs
    ADD COLUMN buy_spread_thr DOUBLE NOT NULL DEFAULT 0.05 COMMENT 'BUY价差阈值，默认0.05' AFTER gtd_expiration_sec,
    ADD COLUMN sell_spread_thr DOUBLE NOT NULL DEFAULT 0.05 COMMENT 'SELL价差阈值，默认0.05' AFTER buy_spread_thr,
    ADD COLUMN buy_exceed_thr TINYINT(1) NOT NULL DEFAULT 1 COMMENT '超过阈值时BUY是否挂单' AFTER sell_spread_thr,
    ADD COLUMN sell_exceed_thr TINYINT(1) NOT NULL DEFAULT 1 COMMENT '超过阈值时SELL是否挂单' AFTER buy_exceed_thr;
