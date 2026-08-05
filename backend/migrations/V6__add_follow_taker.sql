ALTER TABLE copy_trading_configs
    ADD COLUMN buy_follow_taker TINYINT(1) NOT NULL DEFAULT 1 COMMENT '买入是否跟随leader的taker行为' AFTER sell_exceed_thr,
    ADD COLUMN sell_follow_taker TINYINT(1) NOT NULL DEFAULT 1 COMMENT '卖出是否跟随leader的taker行为' AFTER buy_follow_taker;
