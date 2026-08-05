# 仓位历史快照维护链路

## 数据模型

**DB 表**: `copy_trading_position_history`

每条记录包含：

| 字段 | 说明 |
|------|------|
| config_id | 跟单配置 ID |
| asset_id | 资产 token ID |
| leader_proxy_wallet | Leader 地址 |
| follower_proxy_wallet | Follower 地址 |
| share_ratio | 快照时刻的跟单比例 |
| leader_position | Leader 当前持仓 |
| follower_position | Follower 当前持仓 |
| follower_pending_buy | Follower BUY 待成交量 |
| follower_pending_sell | Follower SELL 待成交量 |
| source | 快照来源（见下方 4 种 source） |
| side | BUY / SELL（事件触发时） |
| event_size | 触发事件的交易数量 |
| event_price | 触发事件的价格 |
| order_id | 关联的 follower 订单 ID |
| leader_tx_hash | Leader 链上交易 hash |
| raw_context | 附加上下文 JSON |
| created_at | 快照时间（UTC+8） |

辅助表 `copy_trading_config_assets` 维护每个 config 参与过的 asset 列表及 `last_seen_at`。

---

## 写入入口（4 种 source）

### 1. `leader_signal` — Leader 交易信号触发

**位置**: `service.py` `process_signal()` 内，Leader 持仓更新后、Follower 下单前。

**触发条件**: 每次收到 Leader 的 BUY/SELL activity 信号。

**逻辑**: 遍历该 Leader 关联的所有 enabled config，各记录一条快照。同时 `upsert_config_asset()` 更新 asset 的 `last_seen_at`。

```python
self._record_position_snapshot(
    config, signal.asset, "leader_signal",
    side=signal.side, event_size=signal.size, event_price=signal.price,
    leader_tx_hash=signal.transaction_hash,
    raw_context={"source": signal.source},
)
```

### 2. `follower_trade_confirmed` — Follower 成交确认

**位置**: `service.py` `handle_trade_confirmed()` 内，follower 持仓和 pending 更新完成后。

**触发条件**: Follower WS 收到 trade CONFIRMED 消息。

**逻辑**: 通过 `_order_id_to_config_id` 运行时索引或订单表反查 config，找到后记录快照。

```python
self._record_position_snapshot(
    config_for_snapshot, asset_id, "follower_trade_confirmed",
    side=side, event_size=matched_amount, event_price=price,
    order_id=order_id, leader_tx_hash=order.leader_tx_hash,
    raw_context={"config_id": config_id},
)
```

### 3. `baseline` — 配置创建时的基线快照

**位置**: `service.py` `_record_config_baseline_snapshots()` → `create_config()` 末尾。

**触发条件**: 新建跟单配置、完成 leader/follower/pending 同步之后。

**逻辑**: 取该 config 下 leader 和 follower 当前所有有持仓/pending 的 asset，各记录一条 baseline 快照，作为历史曲线的起点。

### 4. `position_history_poller` — 定时轮询兜底

**位置**: `service.py` `_start_position_history_poller()` → `_record_all_position_history_snapshots()`。

**触发条件**: 每 120 秒定时执行。

**逻辑**: 遍历所有 enabled config，对 follower 有持仓或 pending 的 asset 各记录一条快照。用于：
- 补全事件驱动之间的空白时段
- 兜底 WS 丢消息导致的曲线断裂

---

## 核心采集方法

`_record_position_snapshot()` 从内存字典中读取当前状态：

```
leader_position  = self._leader_positions[l_addr][asset_id]
follower_position = self._follower_positions[f_addr][asset_id]
follower_pending_buy = self._pending_buy_orders[f_addr][asset_id]
follower_pending_sell = self._pending_sell_orders[f_addr][asset_id]
```

通过 `asyncio.create_task(asyncio.to_thread(record_position_history, ...))` fire-and-forget 写入 DB，不阻塞主流程。

---

## 数据流向

```
Leader WS 信号
  │
  ▼
process_signal()
  ├── 更新 leader 内存仓位
  ├── 记录 "leader_signal" 快照
  └── _handle_buy / _handle_sell → 下单
                                      │
                                      ▼
Follower WS trade CONFIRMED
  │
  ▼
handle_trade_confirmed()
  ├── 更新 follower 内存仓位 + pending
  └── 记录 "follower_trade_confirmed" 快照

Config 创建
  ├── sync leader/follower/pending from Polymarket API
  └── 记录 "baseline" 快照

Poller (每 120s)
  └── 记录 "position_history_poller" 快照
```

---

## 查询链路

### API 入口

`get_position_history(config_id, asset_id, start, end, normalized, limit)`

- `normalized=True` 时，`leader_value = leader_position * share_ratio`（与 follower_value 在同一量级对比）
- `normalized=False` 时，`leader_value = leader_position`（原始值）
- 默认 limit=2000，最大 5000

### Asset 列表查询

`get_assets_belongs_to_cfg(config_id, since)` → 查 `copy_trading_config_assets` 表

返回该 config 参与过的所有 asset（带 question/outcome/last_seen_at），支持 `since` 过滤。

---

## 注意事项

1. 快照记录的是**写入时刻的内存状态**，不是事件发生前的状态。`leader_signal` 记录的 leader_position 是更新后的值。
2. Poller 只记录 follower 有持仓或 pending 的 asset，leader 已清仓但 follower 仍有持仓的情况会被覆盖。
3. `_record_position_snapshot` 是 fire-and-forget，极端情况下（进程崩溃）可能丢失少量快照，由 poller 兜底补全。
4. `config_assets` 表的 `last_seen_at` 只在 `leader_signal` 路径更新，poller 不会刷新该时间戳。
