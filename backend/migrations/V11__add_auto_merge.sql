-- V11 跟单配置增加 auto merge 功能
ALTER TABLE copy_trading_configs
    ADD COLUMN auto_merge_enabled TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否开启自动 merge' AFTER sell_follow_taker,
    ADD COLUMN auto_merge_threshold DECIMAL(20, 4) NOT NULL DEFAULT 100.0000 COMMENT '双边持仓超过此阈值时触发 merge（份额数）' AFTER auto_merge_enabled;
