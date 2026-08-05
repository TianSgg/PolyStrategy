ALTER TABLE copy_trading_configs
    ADD COLUMN buy_price_filter_min DECIMAL(10,4) DEFAULT 0.001 COMMENT 'BUY信号价格过滤下限' AFTER buy_only,
    ADD COLUMN buy_price_filter_max DECIMAL(10,4) DEFAULT 0.998 COMMENT 'BUY信号价格过滤上限' AFTER buy_price_filter_min;
