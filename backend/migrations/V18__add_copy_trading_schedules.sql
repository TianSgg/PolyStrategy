CREATE TABLE copy_trading_schedules (
    id INT AUTO_INCREMENT PRIMARY KEY,
    config_id INT NOT NULL,
    start_cron VARCHAR(100) DEFAULT NULL COMMENT '启动 cron 表达式（UTC+8）',
    stop_cron VARCHAR(100) DEFAULT NULL COMMENT '停止 cron 表达式（UTC+8）',
    enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '该调度规则是否启用',
    last_triggered_at DATETIME(3) DEFAULT NULL COMMENT '上次触发时间',
    created_at DATETIME(3) NOT NULL DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
    updated_at DATETIME(3) DEFAULT (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR),
    CONSTRAINT fk_schedule_config FOREIGN KEY (config_id) REFERENCES copy_trading_configs(id) ON DELETE CASCADE,
    UNIQUE KEY uk_config_id (config_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
