-- V10 账户 Relayer API Key（用于 split/merge 操作）
ALTER TABLE accounts ADD COLUMN relayer_api_key VARCHAR(128) DEFAULT NULL COMMENT 'Polymarket Relayer API Key' AFTER builder_code;
