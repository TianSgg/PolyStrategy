ALTER TABLE copy_trading_configs
  ADD COLUMN size_mode VARCHAR(10) NOT NULL DEFAULT 'fixed' COMMENT '份额模式: fixed=固定数量, ratio=按leader比例' AFTER buy_size,
  ADD COLUMN size_ratio DECIMAL(10, 4) NOT NULL DEFAULT 1.0000 COMMENT '比例模式下的倍率（如0.5=leader的一半）' AFTER size_mode,
  ADD COLUMN size_min DECIMAL(20, 4) NOT NULL DEFAULT 0.0000 COMMENT '比例模式下最小下单份额，低于则跳过' AFTER size_ratio;
