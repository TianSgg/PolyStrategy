CREATE TABLE copy_trading_slug_filters (
    id INT AUTO_INCREMENT PRIMARY KEY,
    config_id INT NOT NULL,
    mode ENUM('blacklist', 'whitelist') NOT NULL DEFAULT 'blacklist' COMMENT '过滤模式',
    slugs JSON NOT NULL COMMENT 'slug 列表，JSON 数组',
    created_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
    updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
    CONSTRAINT fk_slug_filter_config FOREIGN KEY (config_id) REFERENCES copy_trading_configs(id) ON DELETE CASCADE,
    UNIQUE KEY uk_slug_filter_config_id (config_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
