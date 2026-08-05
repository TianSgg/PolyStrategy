# 持仓缓存全面审计报告

## 数据结构现状

```
_leader_positions: Dict[str, Dict[str, float]]   # {leader_addr: {asset_id: size}}
_follower_positions: Dict[str, Dict[str, float]] # {follower_addr: {asset_id: size}}
```

## `_leader_positions` 读写路径

### 持久化（写 DB）

| 路径 | 操作 | 状态 |
|------|------|------|
| `_handle_buy` L169 | `upsert_leader_position(config_id, asset_id, size)` | ✅ 正常 |
| `_handle_sell` L214 | 同上 | ✅ 正常 |
| `_sync_leader_positions_by_address` L484 | 同上（全量覆盖） | ✅ 正常 |

### 内存读写

| 路径 | 操作 | 状态 |
|------|------|------|
| `_load_configs` L106 | 从 DB 加载，按 addr 覆盖 | ✅ 正常 |
| `_get_leader_num_shares` | 内存优先 → DB 回退 | ✅ 正常 |
| `_handle_buy` L168 | 写内存 | ✅ 正常 |
| `_handle_sell` L213 | 写内存 | ✅ 正常 |
| `_sync_leader_positions_by_address` L482 | 写内存（全量覆盖） | ✅ 正常 |
| `get_positions` L561 | 读内存 | ✅ 正常 |

## `_follower_positions` 读写路径

### 持久化

| 路径 | 操作 | 状态 |
|------|------|------|
| 全部写路径 | **仅写内存，不写 DB** | ⚠️ 设计如此（以 Polymarket API 为准） |
| 10s poller | `sync_follower_positions` 只写内存 | ⚠️ 同上 |

### 内存读写

| 路径 | 操作 | 状态 |
|------|------|------|
| `_load_configs` → `_sync_follower_positions_sync` | 初始化 | ✅ 正常 |
| `_handle_buy` L190-192 | matched 时写内存 | ⚠️ order 失败/skipped 不写 |
| `_handle_sell` L220-221,244 | matched 时写内存 | ⚠️ 同上 |
| `_handle_sell` L220 | 读内存（而非 API） | ⚠️ 见下 |
| `sync_follower_positions` L420 | API 覆盖内存 | ✅ 正常 |

---

## 问题逐项分析

### 1. `create_config` 不初始化 leader 现有持仓

**结论：正常**

`create_config` L354 只初始化了 `_follower_positions`，没有初始化 `_leader_positions`。Leader 持仓按 addr 共享，同一 leader 的后续 config 会复用已有缓存。**不影响运行时正确性**。

### 2. `_get_leader_num_shares` DB 回退用传入的 `config_id`

**结论：正常**

```python
def _get_leader_num_shares(self, leader_addr: str, asset_id: str, config_id: int):
    if addr in _leader_positions and asset_id in _leader_positions[addr]:
        return _leader_positions[addr][asset_id]  # ✅ 内存优先
    db_val = get_leader_position(config_id, asset_id)  # ✅ 正确查该 config 的 DB
```

同一 leader 多 config 共用同一 addr，内存是聚合值，DB 是 per-config 值。回退时用 `config_id` 查 DB 命中的是该 config 记录的 leader 持仓，**正确**。

### 3. `_handle_sell` 读 `follower_pos` 从内存而非 API

**结论：可接受（最终一致性代价）**

L220 读的是程序记录的持仓，非 API 实时值。若此时 follower 的 match 尚未被 WS/轮询确认，实际持仓会偏大，导致 follow_sell 偏大。但 order 提交后有保护（size 超过持仓会被 skip），且 10s 轮询会修正。**可接受**。

### 4. order 失败/skipped 时 follower 持仓不同步

**结论：低风险**

- `_handle_buy` L188：order 非 matched → follower 持仓不变
- `_handle_sell` L226：ratio 超过持仓被跳过 → leader 减了，follower 没减

两者都会在下次轮询（≤10s）被 API 值覆盖。**低概率**，最终一致性可接受。

### 5. disabled config 的 follower 持仓无轮询同步

**结论：可接受**

L78-81 的 poller 对所有 config 调用 `sync_follower_positions`，**不区分 enabled**，即 disabled 的 follower 也会被轮询同步 → 10s 后 follower 持仓会从 API 校准回来。**可接受**。

### 6. `_leader_positions` 无 periodic poller

**结论：正常**

Leader 只能通过本系统交易，convert 时已通过 `_sync_leader_positions_by_address` 从 API 校准。无需额外轮询。

### 7. `_leader_positions` entry 永不删除

**结论：可接受**

当 `size <= 0.001` 时 DB 会 delete（`upsert_leader_position` L176），但内存中 entry 保留 key，value 为 0。内存占用可忽略，不影响正确性（`get_leader_num_shares` 返回 0 会正确阻止后续 SELL）。

### 8. `create_config` 不调用 `sync_leader_events`

> **已移除**：`_leader_existing_events` 功能已于 2026-04-11 移除，不再有"首涉才跟"的限制。

---

## 结论

**无严重 bug，核心逻辑正确。** 主要风险：

1. **disabled config 的 follower 持仓漂移**：leader 更新了但 follower order 跳过，下次轮询（≤10s）才校准

两者都是低概率、低-impact 的最终一致性场景，当前设计可接受。
