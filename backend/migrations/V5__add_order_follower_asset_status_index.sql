-- 统一 status 为大写（BINARY 避免 ci collation 下大小写不敏感）
UPDATE copy_trading_orders SET status = UPPER(status) WHERE BINARY status != UPPER(status);

-- 统一 id 前缀为大写
UPDATE copy_trading_orders SET id = CONCAT('ERROR_', SUBSTRING(id, 7)) WHERE id LIKE BINARY 'error_%';
UPDATE copy_trading_orders SET id = CONCAT('SKIPPED_', SUBSTRING(id, 9)) WHERE id LIKE BINARY 'skipped_%';

-- 升级索引
ALTER TABLE copy_trading_orders
  DROP INDEX idx_config_id,
  ADD INDEX idx_config_id_asset_status (config_id, asset_id, status);
