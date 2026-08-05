-- V8 用户置顶分组
ALTER TABLE users ADD COLUMN pinned_portfolio_group_id INT DEFAULT NULL AFTER enabled;
