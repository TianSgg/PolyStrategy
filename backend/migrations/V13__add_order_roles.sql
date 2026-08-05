ALTER TABLE copy_trading_orders
    ADD COLUMN leader_role VARCHAR(10) DEFAULT NULL COMMENT 'leader 角色: maker/taker' AFTER status,
    ADD COLUMN follower_role VARCHAR(10) DEFAULT NULL COMMENT 'follower 角色: maker/taker' AFTER leader_role;
