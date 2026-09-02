# Event 与交易日志命名规范

> 日期：2026-08-31  
> 状态：设计方案，待实施  
> 适用范围：`strategy_weather_sweep_events`、`strategy_weather_sweep_trades`、后端事件写入、前端事件展示。

## 1. 目标

这套规范解决三类问题：

1. `stage`、`step`、`status`、`close_reason` 的语义混杂。
2. `force_exit`、`exit_failed` 等同一个词同时表示 phase、step、状态或结局。
3. 交易摘要表无法同时回答“是否还在运行”“最终结果是什么”“为什么结束”“是否需要人工处理”。

核心目标是让每一层只回答一个问题：

```text
phase            这个动作发生在哪个执行阶段？
step             这个动作本身是什么？
lifecycle_status 这笔交易现在是否还在运行？
trade_outcome    这笔交易最终是完成还是失败？
close_reason     这笔交易为什么结束？
failure_reason   如果失败，具体失败在哪里？
```

## 2. 核心层级模型

一笔交易对应一个 `event_id`。`event_id` 是策略执行生命周期 ID，不是 Polymarket 市场 ID；Polymarket 市场用 `market_slug`、`event_slug`、`token_id` 表达。

```text
一笔交易 / 一个 event_id
    │
    ├── strategy_weather_sweep_events：过程日志，多行
    │   ├── phase：这条记录发生在哪个阶段
    │   ├── step：具体动作
    │   ├── sequence_no：动作顺序
    │   └── detail：动作上下文
    │
    └── strategy_weather_sweep_trades：交易摘要，一行
        ├── lifecycle_status：当前生命周期状态
        ├── trade_outcome：最终结果
        ├── close_reason：最终关闭原因
        ├── failure_reason：失败细分原因
        └── needs_attention：是否需要人工处理
```

推荐心智模型：

| 名称 | 层级 | 回答的问题 | 取值时机 |
|---|---|---|---|
| `phase` | event 行字段 | 动作发生在哪个阶段 | 每条 event 行 |
| `step` | event 行字段 | 发生了什么动作 | 每条 event 行 |
| `sequence_no` | event 行字段 | 动作顺序 | 每条 event 行 |
| `lifecycle_status` | trades 行字段 | 交易是否仍在执行 | 实时更新 |
| `trade_outcome` | trades 行字段 | 最终是完成还是失败 | 终态时写入 |
| `close_reason` | trades 行字段 | 为什么关闭 | 终态时写入 |
| `failure_reason` | trades 行字段 / event detail | 失败细分原因 | 失败终态时写入 |
| `needs_attention` | trades 行字段 | 是否有未解决问题 | 终态时写入 |

`stage` 不再使用。数据库实际字段是 `phase`，文档、前端和接口都不应引入第二套阶段名称。

## 3. 命名原则

### 3.1 通用规则

1. 名称使用小写 ASCII `snake_case`。
2. 名称只表达一层语义，不在 step 里复用 close_reason。
3. 同一个动作在多个 phase 出现时使用同一个 step，差异放在 `phase` 和 `detail.trigger`。
4. 请求类动作使用 `*_requested`，例如 `force_exit_requested`。
5. 结果类动作使用完成态或结果态，例如 `buy_order_placed`、`buy_order_failed`、`buy_filled`。
6. 终态日志统一使用 `event_closed`，不再使用 `exit_failed` 作为最终 step。
7. 终态字段写在 `event_closed.detail` 和 trades 摘要，不复制到每条 event 行。
8. `Decimal`、价格、份额在 JSON 中使用字符串，避免浮点精度问题。

### 3.2 买入与卖出对称

买入和卖出都遵循同一三层结构：

```text
挂单结果 -> 成交通知 -> 阶段终态
```

即使下单响应直接全部成交，也必须分开记录：

```text
buy_order_placed -> buy_filled -> entry_complete
sell_order_placed -> sell_filled -> sell_complete
```

这样统计成交量只需要查 `buy_filled` / `sell_filled`，统计挂单结果只需要查 `*_order_placed` / `*_order_failed`。

## 4. phase 规范

phase 保持 5 个，不建议合并：

| phase | 含义 |
|---|---|
| `entry` | 收到信号、买入、买入成交、入场阶段终态。 |
| `monitor` | 等待 tick 变化、验证 tick、延迟正常退出。 |
| `exit` | 正常卖出流程。 |
| `exit_risk` | 风控触发后的清仓流程。 |
| `exit_force` | 用户或配置禁用触发的强制退出流程。 |

`exit_risk` 与 `exit_force` 保留独立 phase：前者由策略风控触发，后者由外部控制触发，排障时需要区分来源。

## 5. 目标 step 命名

### 5.1 entry

| 目标 step | 含义 | 可重复 |
|---|---|---|
| `signal_received` | 收到交易信号。 | 否 |
| `buy_order_placed` | BUY 订单被 CLOB 接受。 | 否 |
| `buy_order_failed` | BUY 下单失败或未发送。 | 否 |
| `buy_order_skipped` | BUY 未发起，例如入场前资金不足。 | 否 |
| `buy_filled` | 一笔 BUY 成交。 | 是 |
| `fill_reconciled` | 通过 REST 或重连校准累计成交。 | 是 |
| `entry_complete` | 入场阶段完成，通常为全部买入。 | 否 |
| `entry_timeout` | 入场等待超时并处理剩余挂单。 | 否 |

### 5.2 monitor

| 目标 step | 含义 | 可重复 |
|---|---|---|
| `tick_verify_failed` | tick 验证失败。 | 是 |
| `tick_verified` | tick 变化被确认。 | 否 |
| `normal_exit_deferred` | 已发现可退出条件，但按规则延迟退出。 | 否 |

### 5.3 exit

| 目标 step | 含义 | 可重复 |
|---|---|---|
| `tick_refresh_failed` | 刷新 tick 失败。 | 是 |
| `tick_refreshed` | tick 刷新成功。 | 是 |
| `sell_order_placed` | SELL 订单被 CLOB 接受。 | 是 |
| `sell_order_failed` | SELL 下单失败或未发送。 | 是 |
| `sell_retry_started` | 开始一次卖出重试。 | 是 |
| `balance_settlement_retry` | 等待余额结算后重试。 | 是 |
| `sell_filled` | 一笔 SELL 成交。 | 是 |
| `fill_reconciled` | 校准 SELL 或 BUY 累计成交。 | 是 |
| `sell_retry_exhausted` | 卖出重试达到停止条件。 | 否 |
| `sell_complete` | 卖出阶段完成，仓位清零。 | 否 |
| `dust_position_detected` | 剩余仓位低于最小下单量。 | 否 |
| `strategy_paused` | 卖出失败触发策略级熔断。 | 否 |

### 5.4 exit_risk

| 目标 step | 含义 | 可重复 |
|---|---|---|
| `risk_triggered` | 风控条件触发。 | 否 |
| `buy_cancelled` | BUY 挂单撤单完成。 | 否 |
| `sell_cancelled` | SELL 挂单撤单完成。 | 否 |
| `sell_order_placed` | 风控清仓卖单被接受。 | 是 |
| `sell_order_failed` | 风控清仓卖单失败。 | 是 |
| `sell_retry_started` | 风控清仓重试开始。 | 是 |
| `sell_filled` | 风控清仓成交。 | 是 |
| `fill_reconciled` | 校准风控清仓成交。 | 是 |
| `sell_complete` | 风控清仓完成。 | 否 |
| `dust_position_detected` | 风控清仓后剩余仓位为 dust。 | 否 |
| `strategy_paused` | 风控清仓失败触发熔断。 | 否 |

### 5.5 exit_force

| 目标 step | 含义 | 可重复 |
|---|---|---|
| `force_exit_requested` | 收到强制退出请求。 | 否 |
| `buy_cancelled` | BUY 挂单撤单完成。 | 否 |
| `sell_cancelled` | SELL 挂单撤单完成。 | 否 |
| `fill_reconciled` | 撤单后校准成交。 | 是 |
| `dust_position_detected` | 强制退出后剩余仓位为 dust。 | 否 |

### 5.6 跨 phase 共享 step

以下 step 可以出现在多个 phase：

| step | 允许 phase | 差异表达 |
|---|---|---|
| `fill_reconciled` | entry / exit / exit_risk / exit_force | `detail.side`、`detail.source` |
| `buy_cancelled` | exit_risk / exit_force | `detail.trigger` |
| `sell_cancelled` | exit_risk / exit_force | `detail.trigger` |
| `dust_position_detected` | exit / exit_risk / exit_force | `detail.trigger` |
| `strategy_paused` | exit / exit_risk | `detail.reason` |
| `event_closed` | 所有 phase | `detail.outcome`、`detail.close_reason` |

`event_closed` 是唯一的 terminal step。它表示“这笔执行日志关闭”，不表示成功；结果由 `detail.outcome` 表达。

## 6. 历史 step 迁移映射

| 当前 / 历史 step | 目标 step | 说明 |
|---|---|---|
| `order_placed` | `buy_order_placed` | 明确 BUY。 |
| `order_failed` | `buy_order_failed` | 明确 BUY。 |
| `buy_order_failed` 且 `detail.status=no_cash` | `buy_order_skipped` | 资金不足未下单，不归类为失败。 |
| `tick_detected` | 废弃，不再记录 | 候选变化不落 event；三源成功时只记录三条 `tick_verified`。 |
| `fill_reconcile` | `fill_reconciled` | 表示校准结果。 |
| `sell_retry_start` | `sell_retry_started` | 使用完成态。 |
| `risk_sell_order_placed` | `sell_order_placed` | phase 已表达 risk，trigger 放 detail。 |
| `risk_cancel_buy` | `buy_cancelled` | phase 已表达 risk，trigger 放 detail。 |
| `risk_cancel_sell` | `sell_cancelled` | phase 已表达 risk，trigger 放 detail。 |
| `dust_position` | `dust_position_detected` | 明确这是检测结果。 |
| `force_exit` | `force_exit_requested` | 避免与 phase、close_reason 同名。 |
| `exit_failed` | `event_closed` | 不再用 step 表达结局。 |
| `sell_give_up` | `event_closed` | 终态统一，失败信息进 detail。 |

`cancel_failed` 和 `exit_reconcile_failed` 应按 `detail.side` 拆成：

```text
buy_cancel_failed / sell_cancel_failed
fill_reconcile_failed
```

## 7. trades 表目标字段

落地时保留旧 `status` 作为兼容字段，新增 `lifecycle_status` 和终态字段：

```sql
ALTER TABLE strategy_weather_sweep_trades
  ADD COLUMN lifecycle_status
    ENUM('entry_working', 'exit_working', 'closed') NOT NULL DEFAULT 'entry_working',
  ADD COLUMN trade_outcome ENUM('completed', 'failed', 'skipped') NULL,
  ADD COLUMN failure_reason VARCHAR(64) NULL,
  ADD COLUMN needs_attention TINYINT(1) NOT NULL DEFAULT 0;
```

字段语义：

| 字段 | 取值 | 说明 |
|---|---|---|
| `lifecycle_status` | `entry_working` | 仍在入场阶段。 |
|  | `exit_working` | 仍在退出阶段。 |
|  | `closed` | 生命周期已结束，不再执行动作。 |
| `trade_outcome` | `completed` | 按预期规则到达终态，无未解决仓位或订单。 |
|  | `skipped` | 策略未发起交易，例如资金不足。 |
|  | `failed` | 因动作失败或存在未解决状态到达终态。 |
| `close_reason` | 见第 8 节 | 关闭原因。 |
| `failure_reason` | 见第 9 节 | 失败细分原因；`trade_outcome=failed` 时必填。 |
| `needs_attention` | `0 / 1` | 是否需要人工确认订单、仓位或对账结果。 |

约束建议：

1. `lifecycle_status != 'closed'` 时，`trade_outcome`、`close_reason`、`failure_reason` 必须为空。
2. `lifecycle_status = 'closed'` 时，`trade_outcome`、`close_reason` 必填。
3. `trade_outcome = 'failed'` 时，`failure_reason` 必填。
4. `needs_attention = 1` 时，最终 `event_closed.detail` 必须说明原因。

MySQL 可以先在应用层保证这些约束；如果数据库版本支持，再补充 CHECK 约束。

## 8. close_reason 规范

`close_reason` 只表示“为什么关闭”，不表示是否成功：

| close_reason | 含义 | 常见 trade_outcome |
|---|---|---|
| `normal_exit` | tick 验证后按正常卖出流程关闭。 | `completed` |
| `stop_loss` | 风控触发后清仓关闭。 | 清仓完成为 `completed`，清仓失败为 `failed` |
| `buy_failed` | BUY 下单失败。 | `failed` |
| `no_cash` | 入场前可用现金不足，未发起 BUY。 | `skipped` |
| `timeout_no_fill` | 入场等待超时且没有成交。 | `completed` |
| `sell_failed` | SELL 或退出流程失败。 | `failed` |
| `force_exit` | 用户或配置禁用触发强制退出。 | 无残留为 `completed`，有残留为 `failed` |

历史值迁移：`tick_exit` 仅作为旧数据别名，当前代码统一写 `normal_exit`。

`timeout_no_fill` 是预期规则触发的关闭，不建议归为失败；是否有盈利机会损失由策略指标分析，不由 `trade_outcome` 表达。

## 9. failure_reason 规范

`failure_reason` 是失败细分原因，只用于 `trade_outcome='failed'`：

| failure_reason | 含义 |
|---|---|
| `buy_placement_failed` | BUY 请求未成功挂出。 |
| `sell_placement_failed` | SELL 请求未成功挂出，包括 CLOB 拒绝、网络失败、tick 刷新失败。 |
| `sell_fill_parse_error` | CLOB 返回 matched，但无法按 side 解析成交份额。 |
| `exit_order_unfilled` | 卖出流程结束时仍有仓位。 |
| `fill_reconcile_failed` | 撤单或断连后无法确认最终成交。 |
| `buy_cancel_failed` | BUY 撤单失败，订单可能仍 LIVE。 |
| `sell_cancel_failed` | SELL 撤单失败，订单可能仍 LIVE。 |
| `dust_position` | 剩余仓位低于最小下单量，无法继续卖出。 |
| `tick_refresh_failed` | 无法刷新 tick，导致不能安全下单。 |
| `invalid_tick_retry_exhausted` | tick 错误重试耗尽。 |
| `same_error_repeated` | 相同错误连续重复，触发熔断。 |
| `total_failures_exceeded` | 总失败次数超过限制。 |
| `deadline_exceeded` | 退出截止时间超时。 |
| `unknown_failure` | 无法归入已知原因，必须保留原始 error。 |

`failure_reason` 不用于 `completed`。例如 `timeout_no_fill` 不写 failure_reason。

## 10. event_closed 终态设计

所有终态统一写一条：

```text
step = event_closed
```

成功示例：

```json
{
  "outcome": "completed",
  "close_reason": "normal_exit",
  "duration_ms": 123456,
  "position_open": false,
  "manual_action_required": false
}
```

失败示例：

```json
{
  "outcome": "failed",
  "close_reason": "sell_failed",
  "failure_reason": "sell_cancel_failed",
  "duration_ms": 123456,
  "position_shares": "3.5000",
  "position_open": true,
  "manual_action_required": true
}
```

`event_closed.detail` 中的 `failure_reason` 应与 trades 表保持一致。trades 表是查询权威，event 终态 detail 是过程回放时的上下文快照。

## 11. 典型过程

### 11.1 BUY 直接全部成交

```text
entry / signal_received
entry / buy_order_placed
entry / buy_filled
entry / entry_complete
monitor / tick_verified
exit / sell_order_placed
exit / sell_filled
exit / sell_complete
exit / event_closed(outcome=completed, close_reason=normal_exit)
```

### 11.2 BUY 下单失败

```text
entry / signal_received
entry / buy_order_failed
entry / event_closed(outcome=failed, close_reason=buy_failed, failure_reason=buy_placement_failed)
```

### 11.3 BUY 等待超时，中间部分成交

```text
entry / signal_received
entry / buy_order_placed
entry / buy_filled
entry / buy_filled
entry / entry_timeout
monitor / normal_exit_deferred
exit / sell_order_placed
exit / sell_filled
exit / sell_complete
exit / event_closed(outcome=completed, close_reason=normal_exit)
```

### 11.4 BUY 等待超时，没有成交

```text
entry / signal_received
entry / buy_order_placed
entry / entry_timeout
entry / event_closed(outcome=completed, close_reason=timeout_no_fill)
```

### 11.5 SELL 失败且仓位未清

```text
exit / sell_order_failed
exit / sell_retry_started
exit / sell_order_failed
exit / sell_retry_exhausted
exit / strategy_paused
exit / event_closed(outcome=failed, close_reason=sell_failed, failure_reason=deadline_exceeded)
```

## 12. 数据迁移方案

项目仍在构建阶段，推荐做一次干净迁移，而不是长期兼容旧命名。

### 12.1 trades 表

```text
entry_working -> lifecycle_status=entry_working
exit_working  -> lifecycle_status=exit_working
closed        -> lifecycle_status=closed
exit_failed   -> lifecycle_status=closed
```

`trade_outcome` 迁移规则：

| 旧 status + close_reason | 新 trade_outcome |
|---|---|
| `closed + normal_exit` | `trade_outcome='completed'` |
| `closed + timeout_no_fill` | `trade_outcome='completed'` |
| `closed + no_cash` | `trade_outcome='skipped'` |
| `closed + stop_loss` | 默认 `trade_outcome='completed'`，若有仓位残留则 `failed` |
| `closed + buy_failed` | `trade_outcome='failed'`，`failure_reason=buy_placement_failed` |
| `closed + sell_failed` | `trade_outcome='failed'` |
| `exit_failed + sell_failed` | `trade_outcome='failed'` |
| `exit_failed + force_exit` | 默认 `trade_outcome='failed'`，`failure_reason=exit_order_unfilled` |

历史 `closed + sell_failed` 是旧数据，应迁移为 `trade_outcome='failed'`。

### 12.2 events 表

1. 按第 6 节映射 step 名称。
2. 将最终 `exit_failed` / `sell_give_up` 行改为 `event_closed`。
3. 为最终行补充 `detail.outcome`、`detail.close_reason`、`detail.failure_reason`。
4. 非最终行的 detail 不补终态字段，避免过程行伪装成摘要行。
5. 历史 `tick_exit` 统一归并为 `normal_exit`；当前代码不再写入 `tick_exit`。

如果历史 JSON 迁移成本过高，可以保留旧 event 行，只迁移 trades 摘要；查询层按第 6 节做旧名映射。构建阶段更推荐直接迁移，减少长期兼容分支。

## 13. 查询方式

回放完整过程：

```sql
SELECT phase, step, sequence_no, detail, occurred_at
FROM strategy_weather_sweep_events
WHERE event_id = ?
ORDER BY sequence_no ASC;
```

查询失败且需要人工处理的交易：

```sql
SELECT event_id, token_id, market_slug, close_reason, failure_reason, closed_at
FROM strategy_weather_sweep_trades
WHERE lifecycle_status = 'closed'
  AND trade_outcome = 'failed'
  AND needs_attention = 1
ORDER BY closed_at DESC;
```

统计成交量：

```sql
SELECT event_id, JSON_UNQUOTE(JSON_EXTRACT(detail, '$.filled_size')) AS filled_size
FROM strategy_weather_sweep_events
WHERE step IN ('buy_filled', 'sell_filled')
ORDER BY event_id, sequence_no;
```

## 14. 前端展示建议

| 数据字段 | 展示用途 |
|---|---|
| `phase` | 过程分组或时间线分组。 |
| `step` | 时间线节点。 |
| `lifecycle_status` | 列表运行状态。 |
| `trade_outcome` | 成功 / 失败标识。 |
| `close_reason` | 关闭原因标签。 |
| `failure_reason` | 失败详情标签。 |
| `needs_attention` | 人工处理入口。 |

前端不要把 `step=force_exit_requested` 展示为最终结果，也不要把 `phase=exit_force` 当作 close_reason。最终结果只读 trades 表的 `trade_outcome` 和 `close_reason`。

列表中的红色状态只用于需要关注的执行异常：接口/程序错误、订单或成交校准异常、未清仓风险，以及 `force_exit` 强制退出。资金不足、未下单、买入无成交、买入部分成交后正常结束、入场超时和止损退出不使用红色；这些状态分别使用灰色、琥珀色或绿色表达结果，不制造失败告警。部分成交本身按绿色处理，只有系统异常才升级为红色。

## 15. 实施步骤

1. 新增数据库迁移：trades 表字段重命名与新增。
2. 修改 EventLogger 与 SweepTrade 终态写入，统一 `event_closed`。
3. 修改所有 step 常量，按第 5 节目标名输出。
4. 修改 DAO、API 和前端字段读取。
5. 编写历史数据迁移脚本，并先在测试库验证。
6. 增加测试：
   - BUY 直接全部成交。
   - BUY 失败。
   - BUY 部分成交后超时。
   - BUY 无成交后超时。
   - SELL 成功。
   - SELL 失败。
   - 风控清仓成功与失败。
   - 强制退出成功与失败。
7. 更新 `doc/strategy_weather_sweep字段说明.md`，保持字段字典与本规范一致。

## 16. 维护规则

1. 新增 step 时必须先补充本文的 phase-step 矩阵。
2. 新增 close_reason 或 failure_reason 时必须说明与 trade_outcome 的关系。
3. 禁止新增 `stage` 字段或展示文案。
4. 禁止把最终分类写入普通过程 step。
5. 禁止在 events 每行重复写入 trades 终态字段。
6. 挂单、成交、阶段终态、event 终态必须保持独立记录。
