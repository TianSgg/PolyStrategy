-- V12 跟单配置增加 buy_only 模式（应对 convert 型 leader）
ALTER TABLE copy_trading_configs
    ADD COLUMN buy_only TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'buy only 模式：leader SELL 时转为 BUY 反向 token' AFTER auto_merge_threshold;
