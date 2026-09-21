-- 将两张 config 表的 UNIQUE KEY uq_owner_name 改为普通索引
-- 允许同一用户创建→删除→再创建同名 config（每次新行新 ID）

ALTER TABLE strategy_weather_sweep_configs
  DROP INDEX uq_owner_name,
  ADD INDEX idx_owner_name (owner_user_id, name);
