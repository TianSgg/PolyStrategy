# Config Asset 维护链路

## 数据模型

**DB 表**: `copy_trading_config_assets`

| 字段 | 说明 |
|------|------|
| config_id | 跟单配置 ID（联合主键） |
| asset_id | 资产 token ID（联合主键） |
| last_seen_at | 最近一次 Leader 交易该 asset 的时间 |

该表记录每个 config 曾参与过的 asset，作为前端查询仓位历史曲线时的 asset 列表来源。

---

## 写入入口（唯一）

### Leader 交易信号触发

**位置**: `service.py` `process_signal()` 内，Leader 持仓更新之后。

**触发条件**: 每次收到 Leader 的 BUY/SELL activity 信号。

**逻辑**:
1. 收集该 Leader 下所有 **enabled** 的 config ID
2. 调用 `batch_upsert_config_asset(config_ids, asset_id)` 批量写入
3. fire-and-forget（`asyncio.create_task`）

```python
enabled_config_ids = [c.id for c in configs if c.enabled]
if enabled_config_ids:
    asyncio.create_task(asyncio.to_thread(batch_upsert_config_asset, enabled_config_ids, signal.asset))
```

### DB 写入实现

`batch_upsert_config_asset(config_ids, asset_id, last_seen_at=None)`

```sql
INSERT INTO copy_trading_config_assets (config_id, asset_id, last_seen_at)
VALUES (%s, %s, %s)
ON DUPLICATE KEY UPDATE
  last_seen_at = GREATEST(last_seen_at, VALUES(last_seen_at))
```

- 不传 `last_seen_at` 时默认取当前 UTC+8 时间
- `GREATEST` 保证幂等，重复信号或乱序到达不会把时间戳倒推
- 单条便捷入口 `upsert_config_asset()` 内部直接调用 batch 版本

---

## 不会触发写入的场景

| 场景 | 原因 |
|------|------|
| Config disabled | `enabled_config_ids` 列表过滤掉 |
| Poller 定时同步 | Poller 只写 `position_history`，不维护此表 |
| Follower trade confirmed | 只更新 follower 持仓和订单状态 |
| Config 创建时 baseline | 只写 `position_history` 快照 |
| Convert 事件 | 只触发持仓同步 |

---

## 查询链路

### API

`GET /configs/{config_id}/position-assets?since=...`

返回该 config 参与过的 asset 列表，用于前端仓位历史图表的 asset 选择器。

### Service

```python
def get_assets_belongs_to_cfg(self, config_id: int, since: Optional[str] = None) -> List[dict]:
    return get_assets_of_cfg_from_db(config_id, since=since)
```

### Model

`get_assets_of_cfg_from_db(config_id, since, limit=1000)`

```sql
SELECT ca.asset_id,
       COALESCE(aq.question, '') AS question,
       COALESCE(aq.outcome, '') AS outcome,
       ca.last_seen_at
FROM copy_trading_config_assets ca
LEFT JOIN copy_trading_asset_questions aq ON aq.asset_id = ca.asset_id
WHERE ca.config_id = %s [AND ca.last_seen_at >= %s]
ORDER BY ca.last_seen_at DESC
LIMIT %s
```

- LEFT JOIN `copy_trading_asset_questions` 获取可读的 question/outcome
- 支持 `since` 过滤（只返回最近活跃的 asset）
- 按 `last_seen_at DESC` 排序，最近交易的排在前面

---

## 数据流向

```
Leader WS 信号
  │
  ▼
process_signal()
  ├── 更新 leader 内存仓位
  ├── 收集 enabled config IDs
  └── batch_upsert_config_asset(config_ids, asset_id)
        │
        ▼
      DB: copy_trading_config_assets
        │  last_seen_at = GREATEST(old, new)
        │
        ▼
      前端查询: GET /configs/{id}/position-assets?since=...
        │
        ▼
      用户选择 asset → 查询 position_history 曲线
```

---

## 注意事项

1. **只有 enabled config 会被写入**。如果一个 config 在 disabled 期间 leader 交易了某个 asset，该 asset 不会出现在列表中。
2. **没有清理机制**。一旦写入，记录永久存在。如果需要清理历史 asset，需要手动删除。
3. **last_seen_at 只反映 leader 信号时间**，不反映 follower 实际成交时间。
4. **删除 config 时不会级联删除**此表记录（config 删除只清理 positions/pending）。
