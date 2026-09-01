# Event 字段说明

> 适用范围：`strategy_weather_sweep_events` 的表字段、`detail` JSON、主要 step，以及 `strategy_weather_sweep_trades` 摘要字段。
> 本文档是字段字典；完整生命周期和场景示例见 `2026-08-28-event-log-storage-design.md`。

## 1. 读取方式

一笔交易由一个 `event_id` 标识。查询完整过程时按：

```sql
SELECT *
FROM strategy_weather_sweep_events
WHERE event_id = ?
ORDER BY sequence_no ASC;
```

`sequence_no` 是唯一可靠的顺序字段；`occurred_at` 用于展示和按时间筛选，不用于替代顺序。

## 2. events 表字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT | 自增主键，仅作物理行 ID。 |
| `owner_user_id` | INT | 配置归属用户，用于权限过滤。 |
| `proxy_wallet` | VARCHAR | 执行本笔交易的代理钱包。 |
| `config_id` | BIGINT | 策略配置 ID。 |
| `config_snapshot` | JSON | 创建 event logger 时保存的配置快照，可为空。 |
| `event_id` | CHAR(36) | 一笔交易生命周期的 UUID；同一笔交易所有 step 相同。 |
| `signal_id` | VARCHAR | 触发交易的信号 ID，与信号源和 trades 表保持一致。 |
| `token_id` | VARCHAR | Polymarket outcome token ID。 |
| `market_slug` | VARCHAR | 市场 slug。 |
| `event_slug` | VARCHAR | Polymarket event slug。 |
| `phase` | ENUM | 执行阶段：`entry`、`monitor`、`exit`、`exit_risk`、`exit_force`。 |
| `step` | VARCHAR | 阶段内的具体动作或状态。 |
| `sequence_no` | INT | 同一 `event_id` 内递增的 step 序号。 |
| `detail` | JSON | step 详情，字段见下文。 |
| `occurred_at` | DATETIME(3) | 写入时间，UTC，毫秒精度。 |

## 3. phase 含义

| phase | 含义 |
|---|---|
| `entry` | 收到信号、买入、买入成交与入场终态。 |
| `monitor` | 等待 tick 变化、HTTP 验证、风控监控。 |
| `exit` | tick 验证后的正常卖出流程。 |
| `exit_risk` | 风控触发后的强制清仓流程。 |
| `exit_force` | 用户或配置禁用触发的强制退出流程。 |

`strategy_paused` 目前写入 `exit` phase。它表示某个 event 的 SELL 失败触发了策略级熔断，不表示新的数据库 phase。

## 4. detail 通用字段

### 4.1 时间与偏移

| 字段 | 格式 | 说明 |
|---|---|---|
| `utc` | `YYYY-MM-DDTHH:MM:SS.mmm` | detail 生成时刻，UTC 字符串，毫秒精度。 |
| `offset_ms` | integer | 相对信号源时间戳的毫秒偏移。 |
| `elapsed_ms` | integer | 相近动作开始后的耗时，例如挂单到完成。 |
| `wait_ms` | integer | 重试或等待的时间。 |
| `duration_ms` | integer | 整笔 event 从开始到终态的耗时。 |

### 4.2 order 对象

挂在 `order` 字段下：

| 字段 | 说明 |
|---|---|
| `order_id` | 本地生成的请求 ID 或 CLOB 返回的订单 ID；失败未获得 CLOB ID 时为本地 ID。 |
| `side` | `BUY` 或 `SELL`。 |
| `price` | 委托价格，字符串 Decimal。 |
| `size` | 原始委托份额，字符串 Decimal。 |
| `utc` | order 对象生成时间。 |
| `offset_ms` | 相对信号源时间的偏移。 |

### 4.3 BBO 快照

通常出现在 `pre_bbo` / `aft_bbo`：

| 字段 | 说明 |
|---|---|
| `best_bid` / `best_ask` | 最优买价 / 卖价；空侧为 `null`。 |
| `best_bid_size` / `best_ask_size` | 最优价位数量。 |
| `utc` | 快照时间。 |
| `offset_ms` | 相对信号源时间的偏移。 |
| `source` | 快照来源，例如 `signal_snapshot`。 |

### 4.4 CLOB 原始响应

| 字段 | 说明 |
|---|---|
| `clob_status` | CLOB 原始状态，例如 `matched`、`live`、错误状态。 |
| `clob_taking` | CLOB `takingAmount` 原始字符串。 |
| `clob_making` | CLOB `makingAmount` 原始字符串。 |

份额语义按 side 区分：

| side | 成交份额来源 | 另一个金额 |
|---|---|---|
| `BUY` | `takingAmount` | `makingAmount` 为 USDC 支出 |
| `SELL` | `makingAmount` | `takingAmount` 为 USDC 收入 |

策略内的 `filled_size` 已按上表解析，不再直接等同于 `clob_taking`。

### 4.5 成交字段

出现在 `buy_filled` / `sell_filled`：

| 字段 | 说明 |
|---|---|
| `order_id` | 成交所属订单。 |
| `filled_size` | 本次成交份额。 |
| `fill_price` | 本次成交价格。 |
| `total_position` | BUY 后累计持仓。 |
| `remaining_position` | SELL 后剩余持仓。 |
| `source` | `clob_response`、`ws_order_update`、`reconnect_reconcile`、`cancel_reconcile`。 |
| `trade_id` | User WS trade ID，可用于去重；非 WS 来源可为空。 |

### 4.6 撤单与回查

| 字段 | 说明 |
|---|---|
| `success` | 撤单请求是否成功。 |
| `cancelled` | `CancelResult.cancelled`，撤单请求成功为 true。 |
| `final_matched` | 回查到的最终成交份额；查询失败时为 `-1`，不能当成交量使用。 |
| `query_status` | 回查结果，例如 `CANCELED`、`LIVE`、`query_failed`。 |
| `clob_matched` | `fill_reconciled` 中 CLOB 回查的累计成交量。 |
| `memory_before` | 回查前策略内存中的累计成交量。 |
| `reconciled` | CLOB 与内存差值补记的成交量。 |

### 4.7 错误与重试

| 字段 | 说明 |
|---|---|
| `status` | 本地执行状态，例如 `failed`、`insufficient_balance`、`no_cash`。 |
| `error` | 原始错误文本。 |
| `error_signature` | 稳定错误签名，用于单 event 重试和跨 event 熔断。 |
| `attempt` | 当前尝试次数，从 1 开始。 |
| `stop_reason` | 重试停止原因。 |
| `failure_reason` | `event_closed` 失败终态的细分原因，见第 7 节。 |
| `manual_action_required` | 是否需要人工确认仓位或订单。 |
| `position_open` | 是否仍有持仓。 |

常见 `error_signature`：`insufficient_balance`、`invalid_tick_size`、`network_timeout`、`authentication_error`、`invalid_order_params`、`unknown_api_error`。

常见 `stop_reason`：`same_error_repeated`、`total_failures_exceeded`、`deadline_exceeded`、`invalid_tick_retry_exhausted`、`tick_refresh_failed`。

### 4.8 tick 与最小下单量

| 字段 | 说明 |
|---|---|
| `tick_size` | 当前使用的最小价格间隔。 |
| `ws_tick_size` | Market WS 推送或监听到的 tick。 |
| `http_tick_size` | HTTP 验证得到的 tick。 |
| `confirmed` | HTTP 是否确认 tick 变化。 |
| `min_order_size` | `/book` 返回的最小下单份额。 |
| `position_shares` | 当前剩余持仓。 |

## 5. 主要 step

| phase | step | 关键 detail |
|---|---|---|
| `entry` | `signal_received` | `signal_id`、`token_id`、`city`、`direction`、`utc`、`risk_ref_mid`、`risk_threshold`。 |
| `entry` | `buy_order_skipped` | `status=no_cash`、`requested_size`、`available_cash`。 |
| `entry` | `buy_order_failed` | `status`、`error`、`order`、`requested_size`、`available_cash`、BBO。 |
| `entry` | `buy_order_placed` | `order`、`clob_status`、`clob_taking`、`clob_making`、BBO。 |
| `entry` | `buy_filled` | `order_id`、`filled_size`、`fill_price`、`total_position`、`source`、`trade_id`。 |
| `entry` | `entry_complete` | `order_id`、`total_filled`、`total_position`、`fill_count`、`elapsed_ms`。 |
| `entry` | `entry_timeout` | `wait_ms`、`cancelled_order_id`、`final_position`、`unfilled_size`。 |
| `monitor` | `tick_verified` | `token_id`、`source`、`tick_size`、`confirmed`、`utc`。三源成功时只写 `market_ws`、`tick_size_api`、`book_api` 三条。 |
| `monitor` | `normal_exit_deferred` | `trigger`、`reason`、`position_shares`、`entry_order_id`。 |
| `exit` | `tick_refresh_failed` | `error`、`action`、`utc`。 |
| `exit` | `dust_position_detected` | `trigger`、`position_shares`、`min_order_size`、`manual_action_required`、`utc`。 |
| `exit` | `sell_order_failed` | `status`、`error`、`error_signature`、`order`、`attempt`。 |
| `exit` | `balance_settlement_retry` | `attempt`、`error`、`wait_ms`、`utc`。 |
| `exit` | `sell_retry_started` | `attempt`、`error`。 |
| `exit` | `tick_refreshed` | `old_tick_size`、`new_tick_size`、`reason`。 |
| `exit` | `sell_retry_exhausted` | `trigger`、`stop_reason`、错误统计。 |
| `exit` | `sell_order_placed` | `order`、CLOB 原始响应、`trigger`、`attempt`。 |
| `exit` | `sell_filled` | `order_id`、`filled_size`、`fill_price`、`remaining_position`、`source`、`trade_id`。 |
| `exit` | `fill_reconciled` | `side`、`order_id`、`clob_matched`、`memory_before`、`reconciled`。 |
| `exit` | `sell_complete` | `order_id`、`total_filled`、`remaining_position`、`fill_count`、`elapsed_ms`。 |
| `exit` | `fill_reconcile_failed` | `side`、`order_id`、`cancelled`、`query_status`、`final_matched`、`utc`。 |
| `exit` | `buy_cancel_failed` / `sell_cancel_failed` | `side`、`order_id`、`query_status`、`final_matched`、`utc`。 |
| `exit` | `strategy_paused` | `reason`、`trigger_token_id`、`error_signature`、`event_count`、`window_sec`、`config_disabled`。 |
| `exit` | `event_closed` | `outcome`、`close_reason`、`failure_reason`、`position_open`、`manual_action_required`、`duration_ms`。 |

`exit_risk` 会复用 `sell_order_failed`、`sell_filled`、`fill_reconciled`、`sell_complete` 等 step，另有关键 step：

| step | 关键 detail |
|---|---|
| `risk_triggered` | `reference_mid`、`threshold`、`stop_loss_ratio`、`state_at_trigger`、`pending_buy`、`pending_sell`、`position_shares`。 |
| `buy_cancelled` / `sell_cancelled` | `order_id`、`success`、`final_matched`、`trigger="stop_loss"`。 |
| `sell_order_placed` | `order`、CLOB 原始响应、`reason="stop_loss"`、`trigger="stop_loss"`、`attempt`。 |

`exit_force` 的关键 step：

| step | 关键 detail |
|---|---|
| `force_exit_requested` | `reason`、`trigger`、`state_at_exit`、`position_shares`。 |
| `buy_cancelled` / `sell_cancelled` | `order_id`、`success`、`final_matched`。 |

## 6. 终态字段语义

| 字段 | 出现位置 | 说明 |
|---|---|---|
| `entry_complete.total_filled` | entry | 入场阶段累计买入份额。 |
| `sell_complete.total_filled` | exit / exit_risk | 退出阶段累计卖出份额，不使用买入委托量反推。 |
| `exit_shares` | trades 摘要 | 退出阶段累计卖出份额。 |
| `remaining_position` | exit / exit_risk | 终态时剩余持仓。 |
| `reason` | `event_closed` | 摘要 close reason，例如 `normal_exit`、`timeout_no_fill`、`buy_failed`、`sell_failed`、`stop_loss`、`force_exit`。 |

## 7. 失败终态的 failure_reason

交易表保留旧 `status=exit_failed` 兼容字段；新语义使用 `lifecycle_status=closed`、`trade_outcome=failed`，细分原因写在 `event_closed.detail.failure_reason` 和 trades 的 `failure_reason`。

| failure_reason | 含义 | 处理结果 |
|---|---|---|
| `sell_placement_failed` | SELL 请求没有成功挂出，包括 CLOB 拒绝、网络失败、tick 刷新失败。 | 停止该 event，保留 `error_signature` 和 `stop_reason`。 |
| `exit_order_unfilled` | 卖单已挂出或曾尝试挂出，但截止时间后仍有仓位。 | 停止该 event，记录剩余仓位。 |
| `sell_fill_parse_error` | CLOB 返回 matched，但无法按 side 解析成交份额。 | 不把该响应当成交，停止该 event。 |
| `fill_reconcile_failed` | 撤单后无法通过 REST 回查确认最终成交。 | `final_matched=-1` 仅表示未知，停止该 event。 |
| `buy_cancel_failed` / `sell_cancel_failed` | 撤单请求失败，订单可能仍 LIVE。 | 停止该 event，人工确认订单。 |
| `dust_position` | 剩余仓位低于 `/book.min_order_size`。 | 不再重复挂卖，人工确认剩余仓位。 |

## 8. trades 摘要字段

| 字段 | 说明 |
|---|---|
| `status` | 旧兼容字段：`entry_working`、`exit_working`、`closed`、`exit_failed`。 |
| `lifecycle_status` | 生命周期字段：`entry_working`、`exit_working`、`closed`。 |
| `outcome` | Polymarket outcome：`yes` / `no`。 |
| `trade_outcome` | 交易最终结果：`completed` / `failed` / `skipped`。 |
| `close_reason` | 摘要级关闭原因；SELL 失败仍为 `sell_failed`，细分看 `failure_reason`。 |
| `failure_reason` / `needs_attention` | 失败细分和是否需要人工确认。 |
| `entry_order_size` / `entry_price` / `entry_shares` / `entry_cost` / `entry_order_id` / `entered_at` | 入场委托与累计成交。 |
| `exit_order_size` / `exit_price` / `exit_shares` / `exit_revenue` / `exit_order_id` / `exited_at` | 退出委托与累计卖出。 |
| `pnl` / `pnl_pct` | 有入场成本和退出收入时计算。 |
| `duration_ms` / `started_at` / `closed_at` | 生命周期时间。 |

## 9. 维护规则

1. 新增 event detail 字段时，同步更新本文档。
2. `Decimal` 序列化为字符串，避免 JSON 浮点精度问题。
3. `occurred_at` 与 `detail.utc` 均为 UTC；进程日志默认北京时间时需注意换算。
4. 挂单结果、成交通知、阶段终态保持三层分离；即使下单响应直接全部成交，也要分别记录挂单和成交。
5. 份额字段必须区分 side 语义；SELL 成交份额来自 `makingAmount`。
