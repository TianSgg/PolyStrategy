-- 001_initial_baseline.sql
-- 基线迁移：标记当前 schema.sql 的状态为起点
-- 对已存在的数据库无实际操作，仅作为增量迁移的序号起点
--
-- 后续迁移文件命名规则：
--   NNN_简短描述.sql
--   例如：002_add_column_xxx.sql, 003_create_table_yyy.sql
--
-- 使用方式：
--   在已有数据的数据库上，按序号从上次执行过的下一个文件开始执行
--   新环境直接使用 schema.sql 全量建表即可，无需执行 incremental/

SELECT 'baseline: schema matches schema.sql as of 2026-08-26' AS migration_note;
