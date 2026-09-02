# 交易与 Event 存储重构方案

> 目标：把现在混乱的层级收敛掉。  
> 核心原则：**只保留两层语义**
> 1. 交易现在处于哪个流程阶段。
> 2. 交易最终因为什么结束。

---

## 1. 设计目标

1. `event` 表负责记录过程，不负责做结果分类。
2. `trade` 表负责记录最终摘要，只保留 `phase + close_reason` 两个状态字段。
3. 前端展示只依赖 `phase + close_reason`，不再拼 `status`、`trade_outcome`、`failure_reason`、`needs_attention` 这些中间概念。
4. `partial fill`、`no cash`、`dust position` 这类“流程本身的自然结果”，不要和接口/程序错误混在一起。

---

## 2. 表结构总览

这个方案只保留两张核心表：

| 表 | 作用 | 一行代表什么 |
|---|---|---|
| `strategy_weather_sweep_trades` | 摘要表 | 一笔交易的最终摘要 |
| `strategy_weather_sweep_events` | 过程表 | 这笔交易中的一条过程日志 |

两张表通过 `event_id` 关联，但不重复承担相同职责：

- `trades` 负责“最后是什么结果”
- `events` 负责“中间发生了什么”
- `signal_id`、`market_slug`、`event_slug`、`city`、`direction` 等身份字段只放在 `trades`
- 查询完整过程时，通过 `event_id` 关联 `trades` 和 `events`

### 2.1 策略配置表

策略参数保存在独立的配置表：

```text
strategy_weather_sweep_configs
```

一条配置记录代表一个策略实例，`id` 是该配置的稳定身份。参数修改时更新原记录，不创建新的 `config_id`。

配置表中的版本字段表示当前配置版本：

| 字段 | 作用 |
|---|---|
| `id` | 配置的稳定身份，即 `config_id` |
| `params_version` | 当前配置的参数版本，修改参数后递增 |
| 参数列 | 当前正在使用的策略参数 |
| `deleted_at` | 软删除时间；删除后不再加载为运行中的策略实例 |

配置表不负责保存每一笔交易历史上使用过的参数内容。历史参数由 trade 的 `config_snapshot` 保存。

---

## 3. trade 表设计

### 3.1 作用

`strategy_weather_sweep_trades` 是摘要表。  
一笔交易只对应一行，内容尽量稳定、扁平、便于列表查询。

### 3.2 建议字段

#### A. 身份字段

这些字段用于定位一笔交易，建议在 `trade` 表中保留：

| 字段 | 说明 |
|---|---|
| `event_id` | 一笔交易的生命周期 ID，主关联键 |
| `config_id` | 策略配置 ID |
| `params_version` | 创建这笔交易时使用的参数版本 |
| `config_snapshot` | 创建这笔交易时实际生效的完整配置快照 |
| `owner_user_id` | 所属用户 |
| `proxy_wallet` | 执行钱包 |
| `signal_id` | 触发交易的信号 ID |
| `token_id` | 交易标的 token |
| `market_slug` | 市场 slug |
| `event_slug` | 事件 slug |
| `city` | 城市 |
| `direction` | 方向 |

#### B. 阶段与终态字段

| 字段 | 说明 |
|---|---|
| `phase` | 当前或最终流程阶段，见下表 |
| `close_reason` | 最终结束原因，平铺枚举 |
| `started_at` | 交易开始时间 |
| `closed_at` | 交易结束时间 |

`trade.phase` 与 `event.phase` 使用同一套词汇：

| phase | trade 表含义 | event 表含义 |
|---|---|---|
| `entry` | 从收到信号到第一次 SELL 下单尝试前 | 这条日志发生在信号接收、BUY、成交确认或 tick 确认阶段 |
| `exit` | 第一次 SELL 下单尝试后，到交易结束 | 这条日志发生在正常卖出、止损卖出或强制退出阶段 |
| `closed` | 整笔交易已结束 | 不使用；`event_closed` 保持关闭前的 `entry` 或 `exit` |

这样两张表不再使用两套相似但不同的状态词。区别只在于：

- `trade.phase` 是这笔交易**当前的阶段**
- `event.phase` 是这条过程记录**发生时的阶段**

#### 配置关联规则

- `config_id` 是策略配置的稳定身份，不是某次交易的 ID。
- 修改策略参数时更新原配置记录，`config_id` 保持不变。
- 配置表中的 `params_version` 表示当前配置版本；每次参数修改都递增。
- trade 表复制一份创建交易时的 `params_version`，表示这笔交易实际使用的参数版本。
- `config_snapshot` 保存交易创建时实际使用的完整参数，不能在查询历史交易时重新读取当前配置来替代。
- 配置删除采用软删除。删除后，历史交易仍通过 `config_id`、`params_version` 和 `config_snapshot` 保留完整关联，不依赖配置表当前是否还能查询到。

例如：

```text
config 表：
  id = 7
  params_version = 3
  stop_loss_ratio = 0.50

trade 表：
  config_id = 7
  params_version = 2
  config_snapshot.stop_loss_ratio = 0.60
```

这表示该交易由配置 `7` 产生，但使用的是配置修改前的第 `2` 版参数。查询历史交易时，以 `config_snapshot` 为准，不能用配置表当前参数覆盖它。

#### C. 入场摘要字段

| 字段 | 说明 |
|---|---|
| `entry_started_at` | 开始买入的时间 |
| `entry_order_id` | 买单 ID |
| `entry_order_size` | 买单委托量 |
| `entry_price` | 买入均价或主成交价 |
| `entry_shares` | 累计买入份额 |
| `entry_cost` | 累计买入成本 |
| `entered_at` | 入场完成时间 |

#### D. 出场摘要字段

| 字段 | 说明 |
|---|---|
| `exit_started_at` | 开始卖出的时间 |
| `exit_order_id` | 卖单 ID |
| `exit_order_size` | 卖单委托量 |
| `exit_price` | 卖出均价或主成交价 |
| `exit_shares` | 累计卖出份额 |
| `exit_revenue` | 累计卖出收入 |
| `exited_at` | 出场完成时间 |

#### E. 盈亏字段

| 字段 | 说明 |
|---|---|
| `pnl` | 盈亏绝对值 |
| `pnl_pct` | 盈亏百分比 |
| `duration_ms` | 生命周期持续时长 |

#### F. 摘要计算规则

- `entry_shares` 是所有 BUY 成交数量的累计值。
- `entry_cost` 是所有 BUY 成交金额的累计值。
- `entry_price` 是 BUY 成交的加权平均价格，不是最后一笔成交价。
- `exit_shares` 是所有 SELL 成交数量的累计值。
- `exit_revenue` 是所有 SELL 成交金额的累计值。
- `exit_price` 是 SELL 成交的加权平均价格，不是最后一笔成交价。
- 多次 SELL 重试产生的订单和成交，统一累计到出场摘要；每个订单的详细信息保存在 event 流水中。
- `entry_order_id` 和 `exit_order_id` 用于保存主订单或当前关联订单；如果存在多次重试，完整订单列表以 event 记录为准。

#### G. 时间字段定义

| 字段 | 定义 |
|---|---|
| `started_at` | `signal_received` 发生时间，表示本次交易生命周期开始 |
| `entry_started_at` | 第一次 `buy_order_placed` 发生时间 |
| `entered_at` | BUY 阶段结束时间，包括全部成交或超时撤单并完成最终回查 |
| `exit_started_at` | 第一次 SELL 下单尝试时间，不要求下单成功 |
| `exited_at` | 程序确认仓位已经清零的时间 |
| `closed_at` | `event_closed` 发生时间 |

如果 `started_at` 和 `entry_started_at` 在实现中始终相同，应合并字段，避免保存重复时间。

### 3.3 不建议作为核心字段

这些字段不要再作为主存储语义：

- `trade_outcome`
- `failure_reason`
- `needs_attention`
- `status`
- `lifecycle_status`

如果兼容旧接口需要这些字段，应当由 `phase + close_reason + detail` 派生，而不是作为核心设计。

### 3.4 `close_reason` 平铺枚举

只保留一层，不再做父子分类。

#### 自然结束
- `normal_exit`
- `no_cash`
- `timeout_no_fill`
- `dust_position`
- `exit_order_unfilled`

#### 控制类结束
- `stop_loss`
- `force_exit`

#### 市场状态结束
- `market_settled`

#### 真错误
- `buy_placement_failed`
- `sell_placement_failed`
- `sell_fill_parse_error`
- `fill_reconcile_failed`
- `buy_cancel_failed`
- `sell_cancel_failed`
- `tick_refresh_failed`
- `unknown_failure`

结束原因与交易过程的对应关系：

| 场景 | `close_reason` |
|---|---|
| BUY 全部或部分成交，最终 SELL 清空仓位 | `normal_exit` |
| BUY 超时且完全没有成交 | `timeout_no_fill` |
| BUY 部分成交，但最终剩余仓位低于最小 SELL 数量 | `dust_position` |
| SELL 未完成，剩余仓位仍高于最小下单量且重试停止 | `exit_order_unfilled` |
| 接口、程序、解析、撤单或对账异常 | 对应的 `*_failed` |
| 策略风控主动退出 | `stop_loss` |
| 系统、配置或人工主动要求退出 | `force_exit` |
| 市场已经结算，停止后续交易动作 | `market_settled` |

---

## 4. event 表设计

### 4.1 作用

`strategy_weather_sweep_events` 只存过程流水。  
一笔交易对应一个 `event_id`，每条日志按 `sequence_no` 排序回放。

### 4.2 建议字段

#### A. 关联字段

事件表只保留关联摘要表所需的字段，不重复存储交易身份：

| 字段 | 说明 |
|---|---|
| `event_id` | 一笔交易的生命周期 ID |

#### B. 过程字段

| 字段 | 说明 |
|---|---|
| `phase` | `entry` / `exit` |
| `step` | 具体动作名 |
| `sequence_no` | 同一 `event_id` 内的顺序号 |
| `detail` | JSON 详情 |
| `occurred_at` | UTC 时间 |

#### C. 推荐保留的过程补充字段

如果后端需要更细的排障能力，可以继续放在 `detail` 里，不建议再提升成表级主字段：

- `utc`
- `offset_ms`
- `order`
- `pre_bbo`
- `aft_bbo`
- `filled_size`
- `fill_price`
- `source`
- `error`
- `error_signature`
- `attempt`
- `stop_reason`
- `position_shares`
- `min_order_size`

### 4.3 phase 定义

- `entry`：从收到信号开始，到第一次 SELL 下单尝试之前。这里包含 BUY 下单、买入成交、撤单回查、ticksize 监听和 ticksize 确认。
- `exit`：第一次 SELL 下单尝试之后，到交易结束；即使第一次 SELL 下单失败，也属于 `exit` 阶段。
- `closed`：只用于 `trade.phase`，表示整笔交易已经结束。

`entry` 用来容纳买入成交和 ticksize 变化的先后不确定性：

- `buy_filled`、`fill_reconciled`、`entry_complete`、`entry_timeout`、`tick_verified`、`tick_verify_failed`、`exit_trigger_deferred` 都属于 `entry`。
- ticksize 的发现、三源确认、确认失败，以及买入完成后的等待，都属于 `entry`。
- 不根据 `buy_filled` 和 `tick_verified` 谁先发生来重新划分阶段。
- 第一次 SELL 下单尝试开始时，后续过程进入 `exit`。

止损和强制退出不再创建新的 phase：

- `risk_triggered` 和 `force_exit_requested` 根据发生时机使用 `phase=entry` 或 `phase=exit`。
- 具体来源写入 `detail.trigger`，例如 `stop_loss` 或 `force_exit`。
- 最终业务含义写入 `close_reason`。

其他规则：

- `step` 只回答“发生了什么动作”。
- `detail` 只放上下文，不做分类。
- 不要再写 `exit_failed` 这种既像 step 又像结果的词。
- 终态统一用 `event_closed`。

### 4.4 推荐 step

| phase | step | 含义 |
|---|---|---|
| `entry` | `signal_received` | 收到一个符合条件的交易信号，创建本次 event。 |
| `entry` | `buy_order_skipped` | 策略决定不发起 BUY，例如账户可用资金不足；这不是下单接口错误。 |
| `entry` | `buy_order_placed` | BUY 订单被 CLOB 接受，记录订单信息和下单结果。 |
| `entry` | `buy_order_failed` | BUY 订单没有被接受，或下单请求执行失败。 |
| `entry` | `buy_filled` | BUY 订单发生一笔成交，记录本次成交量、成交价和累计持仓。 |
| `entry` | `fill_reconciled` | 通过撤单回查、REST 或重连校准发现了内存中遗漏的成交。 |
| `entry` | `entry_complete` | BUY 订单已经全部成交，或入场持仓已经确定。 |
| `entry` | `entry_timeout` | BUY 等待时间结束，完成撤单和最终成交回查。 |
| `entry` | `tick_verified` | 检测到 ticksize 变化，并通过规定的数据源确认。 |
| `entry` | `tick_verify_failed` | ticksize 变化尚未得到确认，记录各来源结果和失败原因。 |
| `entry` | `exit_trigger_deferred` | 已满足卖出触发条件，但因为买单仍未结束或暂时没有可卖持仓，暂不发 SELL。 |
| `entry` / `exit` | `risk_triggered` | 风控条件触发；第一次 SELL 下单尝试前属于 `entry`，之后属于 `exit`，最终可通过 `detail.trigger` 和 `close_reason` 表达止损来源。 |
| `entry` / `exit` | `force_exit_requested` | 收到配置禁用、策略停止或人工发起的强制退出请求；按第一次 SELL 下单尝试前后记录阶段。 |
| `entry` | `buy_cancelled` | BUY 挂单撤销完成，并记录撤单后的最终成交回查结果。 |
| `entry` | `buy_cancel_failed` | BUY 撤单请求或撤单后的最终成交回查失败。 |
| `entry` / `exit` | `fill_reconcile_failed` | 订单成交结果无法通过查询或回查确认。 |
| `entry` / `exit` | `tick_refresh_failed` | ticksize 刷新或多源确认失败，无法得到可用结果。 |
| `exit` | `sell_cancelled` | SELL 挂单撤销完成，并记录撤单后的最终成交回查结果。 |
| `exit` | `sell_cancel_failed` | SELL 撤单请求或撤单后的最终成交回查失败。 |
| `exit` | `sell_order_placed` | SELL 订单被 CLOB 接受，记录订单、价格、份额和触发来源。 |
| `exit` | `sell_order_failed` | SELL 订单没有被接受，或卖出请求执行失败。 |
| `exit` | `sell_filled` | SELL 订单发生一笔成交，记录本次卖出量、成交价和剩余持仓。 |
| `exit` | `sell_complete` | SELL 流程完成，策略确认仓位已经清零。 |
| `exit` | `sell_retry_started` | 上一次卖出未成功，开始按重试策略等待并再次尝试。 |
| `exit` | `sell_circuit_breaker_triggered` | 同一卖出错误连续达到熔断停止条件，停止继续重复请求；不表示总次数或时间预算耗尽。 |
| `exit` | `dust_position_detected` | 剩余持仓低于市场最小下单量，无法继续正常挂 SELL。 |
| `entry` / `exit` | `market_settled` | 检测到市场已经结算，停止后续交易动作并进入关闭流程。 |
| `entry` / `exit` | `event_closed` | 本次 event 结束，具体结束原因写入 `detail.close_reason` 和 trade 摘要。 |

### 4.5 事件顺序与幂等

- 同一个 `event_id` 内的 `sequence_no` 必须严格递增，事件按 `sequence_no` 和 `occurred_at` 回放。
- `sequence_no` 的分配必须由同一个交易执行上下文串行完成，避免并发任务写入重复序号。
- 每一笔真实成交只记录一条 `buy_filled` 或 `sell_filled`；WebSocket 重复推送不能重复累计成交数量。
- `fill_reconciled` 可以补充此前漏掉的成交，但必须记录校准前数量、校准后数量和新增数量。
- 每一次 SELL 重试都要记录新的 `sell_order_placed`；重试等待和熔断停止分别记录 `sell_retry_started`、`sell_circuit_breaker_triggered`。
- `event_closed` 只能写入一次。写入后不得再追加新的交易过程事件。
- 数据库写入重试必须具备幂等键，不能因为网络重试重复创建成交或关闭记录。

---

## 5. 哪些是摘要字段，哪些是事件字段

### 5.1 摘要字段

摘要字段属于 `trade` 表，目标是让列表页可以直接读取。

| 字段 | 类型 | 作用 |
|---|---|---|
| `event_id` | 标识 | 关联过程日志 |
| `config_id` / `owner_user_id` / `proxy_wallet` | 标识 | 查询和权限过滤 |
| `params_version` | 配置版本 | 还原交易使用的参数版本 |
| `config_snapshot` | 配置快照 | 还原交易创建时实际生效的完整参数 |
| `signal_id` / `token_id` / `market_slug` / `event_slug` / `city` / `direction` | 标识 | 列表展示、搜索、回放定位 |
| `phase` | 阶段 | 交易现在处于哪个流程阶段 |
| `close_reason` | 终态原因 | 为什么结束 |
| `entry_*` | 结果 | 入场汇总 |
| `exit_*` | 结果 | 出场汇总 |
| `pnl` / `pnl_pct` | 结果 | 盈亏 |
| `started_at` / `closed_at` / `duration_ms` | 时间 | 统计 |
| `entry_started_at` / `entered_at` | 时间 | 入场开始与完成 |
| `exit_started_at` / `exited_at` | 时间 | 出场开始与完成 |

### 5.2 事件字段

事件字段属于 `event` 表，目标是回放过程。

| 字段 | 类型 | 作用 |
|---|---|---|
| `event_id` | 标识 | 归属哪笔交易 |
| `sequence_no` | 顺序 | 还原先后关系 |
| `phase` | 阶段 | 这条日志发生在哪一段流程 |
| `step` | 动作 | 发生了什么 |
| `detail` | JSON | 具体上下文 |
| `occurred_at` | 时间 | 发生时间 |

### 5.3 字段归属原则

身份字段只放在 `trade` 表：

- `signal_id`
- `token_id`
- `market_slug`
- `event_slug`
- `city`
- `direction`

`event` 表只通过 `event_id` 关联 `trade`。这样事件表保持纯过程记录，不重复存储摘要信息。

---

## 6. 前端展示规则

### 6.1 第一个徽标

只表示 `trade.phase`：

- `entry` -> `入场中`
- `exit` -> `出场中`
- `closed` -> `已结束`

颜色固定，不跟结果混用。

### 6.2 第二个徽标

只根据 `close_reason` 显示。

| close_reason | 文案 | 颜色 |
|---|---|---|
| `normal_exit` | 正常退出 | 绿色 |
| `no_cash` | 资金不足 · 未下单 | 灰色 |
| `timeout_no_fill` | 入场超时 · 无成交 | 灰色 |
| `market_settled` | 市场已结算 | 灰色 |
| `dust_position` | 低于最小下单量 | 琥珀色 |
| `exit_order_unfilled` | 卖出未完成 | 琥珀色 |
| `stop_loss` | 止损退出 | 琥珀色 |
| `force_exit` | 强制退出 | 红色 |
| `*_failed` | 执行异常 · 对应原因 | 红色 |

### 6.3 部分成交

部分成交**不要**单独升成一个终态层。

展示方式：

- 成交数量放在详情里，例如 `买入 15/20`
- 如果最终正常退出，第二个徽标还是 `正常退出`
- 如果只买到一部分但没有异常，结果仍按最终终态原因显示，不新增一层“部分成交完成”

---

## 7. 推荐判定逻辑

前端和 API 只做两步：

1. 看 `phase`，决定第一徽标。
2. 看 `close_reason`，决定第二徽标和颜色。

其他字段只做补充说明，不做主分类。

---

## 8. 典型场景

### 8.1 下单买入，直接全部成交

- event：`signal_received` -> `buy_order_placed` -> `buy_filled` -> `entry_complete` -> `tick_verified` -> `sell_order_placed` -> `sell_filled` -> `sell_complete` -> `event_closed`
- trade：`phase = closed`，`close_reason = normal_exit`

### 8.2 下单买入，但挂单失败

- event：`signal_received` -> `buy_order_failed` -> `event_closed`
- trade：`phase = closed`，`close_reason = buy_placement_failed`

### 8.3 下单买入，等待到超时，中间有部分成交或没有成交

- 没有成交：
  - event：`buy_order_placed` -> `entry_timeout` -> `event_closed`
  - trade：`close_reason = timeout_no_fill`
- 有部分成交：
  - event：`buy_order_placed` -> 若干 `buy_filled` -> `entry_timeout` -> `sell_order_placed` -> 若干 `sell_filled` -> `sell_complete` -> `event_closed`
- trade：
  - 有部分成交并最终清空：`close_reason = normal_exit`
  - 有部分成交但剩余仓位低于最小 SELL 数量：`close_reason = dust_position`
  - 有部分成交但卖出未完成：`close_reason = exit_order_unfilled`

### 8.4 下单买入，等待过程中提前全部成交

- event：`buy_order_placed` -> `buy_filled` -> `entry_complete` -> `tick_verified` -> `sell_order_placed` -> `sell_filled` -> `sell_complete` -> `event_closed`
- trade：`close_reason = normal_exit`

### 8.5 卖出后剩余仓位小于最小下单量

- event：`...` -> `sell_order_placed` -> `sell_filled` -> `dust_position_detected` -> `event_closed`
- trade：`close_reason = dust_position`

这类情况是流程限制，不是接口错误。

买入阶段如果已经只有低于最小 SELL 数量的持仓，也可以直接记录
`dust_position_detected`，不必伪造一次 SELL 下单。

### 8.6 账户资金不足，不执行买入

- event：`signal_received` -> `buy_order_skipped` -> `event_closed`
- trade：`close_reason = no_cash`

`no_cash` 表示策略判断当前无法执行买入，不是 BUY 接口或程序错误。

### 8.7 风控止损

- event：`...` -> `entry_complete` -> `risk_triggered` -> `sell_order_placed` -> `sell_filled` -> `sell_complete` -> `event_closed`
- trade：`close_reason = stop_loss`

如果风控在第一次 SELL 下单尝试之后才触发，则 `risk_triggered` 的
`phase` 为 `exit`；最终原因仍然是 `stop_loss`。

### 8.8 强制退出

如果仍有未完成的 BUY 挂单：

- event：`...` -> `force_exit_requested` -> `buy_cancelled` -> `sell_order_placed` -> `sell_filled` -> `sell_complete` -> `event_closed`
- trade：`close_reason = force_exit`

如果没有待撤销的 BUY 挂单，不记录 `buy_cancelled`：

- event：`force_exit_requested` -> `sell_order_placed` -> `sell_filled` -> `sell_complete` -> `event_closed`
- trade：`close_reason = force_exit`

### 8.9 市场已经结算

- event：`...` -> `market_settled` -> `event_closed`
- trade：`close_reason = market_settled`

`market_settled` 是市场状态导致的控制类结束，不是接口或程序错误。
如果结算时仍有仓位，必须将结算得到的金额和最终持仓写入
`event_closed.detail`，并纳入 P&L 计算。

---

## 9. 结论

最终建议是：

1. `event` 表只保留过程，不做结局分层。
2. `trade` 表只保留 `phase + close_reason` 这条主线。
3. 前端颜色和文案全部由 `close_reason` 映射。
4. 所有“中间分类字段”都尽量改成派生值，不再作为核心存储字段。
5. 配置身份使用 `config_id`，参数版本使用 `params_version`，历史参数内容使用不可变的 `config_snapshot`。
