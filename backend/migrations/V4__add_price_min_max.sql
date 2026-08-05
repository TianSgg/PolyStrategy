ALTER TABLE copy_trading_configs
    ADD COLUMN buy_price_min DOUBLE NOT NULL DEFAULT 0.001 COMMENT 'BUY最低价格，低于此值截断' AFTER sell_exceed_thr,
    ADD COLUMN buy_price_max DOUBLE NOT NULL DEFAULT 0.999 COMMENT 'BUY最高价格，高于此值截断' AFTER buy_price_min,
    ADD COLUMN sell_price_min DOUBLE NOT NULL DEFAULT 0.001 COMMENT 'SELL最低价格，低于此值截断' AFTER buy_price_max,
    ADD COLUMN sell_price_max DOUBLE NOT NULL DEFAULT 0.999 COMMENT 'SELL最高价格，高于此值截断' AFTER sell_price_min;
