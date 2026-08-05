-- V9 账户签名类型
ALTER TABLE accounts ADD COLUMN signature_type TINYINT NOT NULL DEFAULT 2 AFTER builder_code;
