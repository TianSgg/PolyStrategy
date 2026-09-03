# Weather Sweep 退出债务与 CLOB 同步延迟设计

## 1. 问题背景

Weather Sweep 的核心流程是：收到 sweep 信号后 BUY，随后等待 tick size 变化或其他退出条件，再 SELL 退出。

实际运行中出现过一种时序问题：User WebSocket 已经推送 BUY 全部成交，但 CLOB 下单校验侧尚未把全部成交份额变成可卖余额。此时如果策略立即按内存中的完整成交份额 SELL，会触发：

```text
not enough balance / allowance
```

典型例子：

```text
User WS: BUY filled total = 10
CLOB SELL check: balance = 4.39
Strategy: SELL 10
Result: not enough balance / allowance
```

这个问题的本质不是“仓位不存在”，而是 Polymarket 后端不同数据通道之间存在短暂同步延迟：

- User WS 更早告诉策略成交已经发生。
- CLOB 的可卖余额/allowance 校验稍后才完全追上。

因此，不能把一次 SELL 报错中的 `balance` 当成最终仓位，也不能把它覆盖成策略主账本。

---

## 2. 核心原则

### 2.1 主账本维护退出债务

策略主状态不应叫 `sellable_position`，而应维护一笔“退出债务”：

```text
exit_debt = buy_filled_total - sell_filled_total - sell_pending_total
```

含义：当前还有多少已买入份额需要被退出。

只有真实成交和已接受的 SELL 挂单能改变它：

| 来源 | 影响 |
|---|---|
| BUY fill | 增加 `buy_filled_total` |
| SELL order accepted/live | 增加 `sell_pending_total` |
| SELL fill | 增加 `sell_filled_total`，减少 pending |
| SELL cancel | 减少 `sell_pending_total` |
| SELL placement failed | 不改变主账本 |
| `not enough balance / allowance` | 不改变主账本，只作为本次可卖额度观察 |

### 2.2 CLOB 可卖余额只是临时门控

CLOB 返回的 balance 或 allowance 只表示“这一次 CLOB 当前允许卖多少”，不能表示策略最终只需要卖多少。

正确关系是：

```text
target_to_exit = exit_debt
attempt_size = min(target_to_exit, observed_clob_sellable_if_available)
```

如果 CLOB 当前只允许卖一部分，就先卖这一部分；剩余部分仍保留在 `exit_debt` 中，由后续 exit worker 继续维护。

---

## 3. 状态字段设计

### 3.1 内存状态

每个 `SweepTrade` 应维护以下退出相关状态：

| 字段 | 含义 |
|---|---|
| `buy_filled_total` | BUY 累计成交份额；当前可直接复用或替代 `position_shares` 的买入侧含义 |
| `sell_filled_total` | SELL 累计成交份额 |
| `sell_pending_total` | 已被 CLOB 接受但还没完全成交/取消的 SELL 份额 |
| `exit_requested` | 是否已经满足退出条件，例如 tick verified、stop loss、force exit |
| `exit_debt` | 派生值：`buy_filled_total - sell_filled_total - sell_pending_total` |
| `last_buy_fill_at` | 最近一次 BUY fill 的 UTC/monotonic 时间，用于第一次 SELL 宽限期 |
| `first_sell_attempted` | 是否已经尝试过第一次 SELL |
| `next_balance_lag_retry_at` | balance lag 后下一次允许重试的时间 |
| `balance_lag_retry_index` | balance lag 重试档位：2min、5min、10min |

`position_shares` 可以继续作为兼容字段，但语义要明确：它不能表示 CLOB 当前可卖数量，只表示策略账本中尚未退出的持仓。

### 3.2 trade 表摘要字段

不建议为了这个问题新增大量摘要字段。现有 `trade` 表仍保留：

| 字段 | 用法 |
|---|---|
| `entry_shares` | 对应 `buy_filled_total` |
| `exit_shares` | 对应 `sell_filled_total` |
| `exit_order_size` | 最近一次或主 SELL 委托量；完整多次重试看 event |
| `phase` | 第一次 SELL 尝试前为 `entry`，之后为 `exit`，结束为 `closed` |
| `close_reason` | 最终关闭原因 |

如果后续排障需要，可以考虑新增只读摘要字段：

| 字段 | 是否必须 | 含义 |
|---|---|---|
| `exit_debt` | 可选 | 当前还需要退出的份额，便于前端直接展示 |
| `sell_pending_shares` | 可选 | 当前 SELL 挂单锁定份额 |

但第一版可以先不加表字段，把这些值写在 event detail 中，并由后端接口派生。

---

## 4. 第一次 SELL 同步宽限期

### 4.1 设计目的

当 BUY fill 和退出触发几乎同时发生时，第一次 SELL 最容易撞上 CLOB 同步延迟。

因此，在第一次 SELL 尝试前增加一个很短的宽限期：

```text
first_sell_not_before = last_buy_fill_at + clob_sync_grace_ms
delay = max(0, first_sell_not_before - now)
```

只有第一次 SELL 需要这个等待；后续重试由 retry schedule 控制。

### 4.2 推荐参数

| 参数 | 推荐值 | 含义 |
|---|---|---|
| `clob_sync_grace_ms` | `2000` | 最近 BUY fill 后，第一次 SELL 至少等待 2 秒 |
| `clob_sync_jitter_ms` | `0-300` | 可选随机抖动，避免多个事件同一毫秒集中下单 |

该等待不能改变账本，只是降低第一次 SELL 直接撞 `not enough balance / allowance` 的概率。

### 4.3 事件记录

如果发生等待，记录：

```text
phase=entry
step=exit_trigger_deferred
```

detail 示例：

```json
{
  "reason": "clob_sync_grace",
  "last_buy_fill_at": "2026-09-02T08:53:12.000Z",
  "clob_sync_grace_ms": 2000,
  "wait_ms": 1430,
  "exit_debt": "10.0000",
  "utc": "2026-09-02T08:53:12.570Z"
}
```

第一次 SELL 尝试真正开始时，才把 `trade.phase` 更新为 `exit`，并写入 `exit_started_at`。

---

## 5. Exit Worker 设计

### 5.1 触发条件

以下任一条件发生，都应唤醒同一个 exit worker：

| 条件 | 说明 |
|---|---|
| tick size 三源确认通过 | 正常退出触发 |
| stop loss | 风控退出触发 |
| force exit | 人工、配置禁用或服务停止触发 |
| BUY fill 到达且 `exit_requested=true` | 退出条件已满足后又出现新增成交 |
| SELL fill 到达 | 重新计算剩余退出债务 |
| SELL cancel 完成 | 释放 pending，重新计算可卖债务 |
| balance lag retry 到期 | CLOB 同步延迟后的重试 |

所有触发都只唤醒同一个 worker，不能并发启动多个 SELL 循环。

### 5.2 执行循环

推荐伪代码：

```python
async def exit_worker(trigger):
    if exit_worker_running:
        return
    exit_worker_running = True
    try:
        exit_requested = True

        await cancel_open_buy_if_needed()

        while True:
            exit_debt = buy_filled_total - sell_filled_total - sell_pending_total

            if exit_debt <= 0:
                close(normal_exit or trigger_reason)
                return

            min_order_size = await load_min_order_size()
            if exit_debt < min_order_size:
                close(dust_position)
                return

            if not first_sell_attempted:
                await wait_clob_sync_grace_if_needed()

            mark_exit_started_before_first_sell_attempt()

            result = await place_sell(exit_debt)

            if result.accepted:
                sell_pending_total += result.pending_size
                sell_filled_total += result.filled_size
                continue

            if result.error_signature == "insufficient_balance":
                observed_sellable = parse_observed_sellable(result.error)
                if observed_sellable >= min_order_size:
                    partial = await place_sell(min(exit_debt, observed_sellable))
                    handle_partial_result(partial)
                    continue
                schedule_balance_lag_retry()
                return

            handle_real_sell_error(result)
            return
    finally:
        exit_worker_running = False
```

### 5.3 关键约束

1. `exit_debt` 每一轮都重新计算，不能缓存旧值长期使用。
2. SELL 下单成功后，必须把未成交部分计入 `sell_pending_total`，避免重复卖同一份额。
3. SELL 下单失败不能增加 pending，也不能减少 `exit_debt`。
4. balance lag 只影响重试时间和本次尝试数量，不改变目标退出份额。
5. `event_closed` 只有在 `exit_debt <= 0`、`dust_position`、`market_settled` 或真正熔断/错误停止时才能写入。

---

## 6. Balance Lag 处理

### 6.1 错误分类

`not enough balance / allowance` 在 SELL 阶段应归为：

```text
error_signature = insufficient_balance
sub_reason = balance_lag_possible
```

它默认不是最终失败，而是一次“CLOB 可卖余额滞后”的信号。

### 6.2 处理规则

当 SELL 返回 `insufficient_balance`：

1. 从错误文本解析 `balance` 和 `sum of matched orders`。
2. 计算本次观测到的可卖份额：

```text
observed_sellable = max(0, balance - matched_orders) / 1e6
```

3. 如果 `observed_sellable >= min_order_size`，可以立即缩量下单：

```text
sell_size = min(exit_debt, observed_sellable)
```

4. 如果 `observed_sellable < min_order_size`，不关闭交易，安排 balance lag 重试。
5. 每次重试前重新计算 `exit_debt`，因为期间可能有新的 BUY fill、SELL fill 或 cancel 事件。

### 6.3 重试间隔

只针对 tick size 错误以外的 `insufficient_balance` / balance lag，推荐：

```text
2min -> 5min -> 10min -> 之后维持 10min
```

这和 invalid tick size 的重试可以共用档位，但事件 detail 要写清楚不同原因。

### 6.4 事件记录

SELL 因 balance lag 失败时记录：

```text
phase=exit
step=sell_order_failed
```

detail 示例：

```json
{
  "error_signature": "insufficient_balance",
  "sub_reason": "balance_lag_possible",
  "attempt_size": "10.0000",
  "exit_debt": "10.0000",
  "observed_sellable": "4.3900",
  "raw_balance": "4390000",
  "raw_matched_orders": "0",
  "will_retry": true,
  "utc": "2026-09-02T08:53:13.000Z"
}
```

如果进入等待，记录：

```text
phase=exit
step=sell_retry_started
```

detail 示例：

```json
{
  "reason": "balance_lag",
  "retry_after_ms": 120000,
  "retry_index": 1,
  "exit_debt": "5.6100",
  "observed_sellable": "0.0000",
  "utc": "2026-09-02T08:53:13.100Z"
}
```

---

## 7. 成交事件维护

### 7.1 BUY fill

BUY fill 到达时：

```text
buy_filled_total += fill_size
entry_shares += fill_size
entry_cost += fill_size * fill_price
last_buy_fill_at = now
```

如果此时 `exit_requested=true`，说明退出条件已经满足，但新的 BUY 成交刚到，应唤醒 exit worker。worker 会先走第一次 SELL 宽限期或后续重试流程。

### 7.2 SELL order accepted

SELL 被 CLOB 接受时：

```text
sell_pending_total += accepted_size - immediate_filled_size
sell_filled_total += immediate_filled_size
```

如果返回中有即时成交，直接记录 `sell_filled`。

### 7.3 SELL fill

SELL fill 到达时：

```text
sell_pending_total -= fill_size
sell_filled_total += fill_size
exit_shares += fill_size
exit_revenue += fill_size * fill_price
```

然后重新计算：

```text
exit_debt = buy_filled_total - sell_filled_total - sell_pending_total
```

如果 `exit_debt > 0` 且没有 pending SELL 覆盖剩余份额，继续唤醒 exit worker。

### 7.4 SELL cancel

SELL cancel 后，需要释放未成交部分：

```text
sell_pending_total -= cancelled_unfilled_size
```

然后重新计算 `exit_debt` 并继续维护。

---

## 8. 关闭条件

### 8.1 正常关闭

满足以下条件时关闭为：

```text
close_reason = normal_exit
```

条件：

```text
buy_filled_total > 0
exit_debt <= 0
```

即所有买到的份额都已经通过 SELL 成交或被 pending 覆盖并最终确认完成。严格实现中，最好等 pending SELL 全部成交或取消后再关闭；如果 pending SELL 已经被 CLOB 接受但尚未成交，不应提前写 `normal_exit`。

### 8.2 Dust 关闭

如果：

```text
0 < exit_debt < min_order_size
```

且没有可继续合并卖出的 pending 或新增成交预期，则关闭为：

```text
close_reason = dust_position
```

这是流程本身的限制，不是红色错误。

### 8.3 熔断或错误关闭

以下情况才属于真正异常：

- CLOB API 持续网络错误。
- 签名、认证、参数错误。
- 同一真实错误连续达到熔断条件。
- 撤单或成交回查失败，导致无法确认账本。

此时使用现有平铺 close reason：

```text
sell_placement_failed
sell_fill_parse_error
fill_reconcile_failed
buy_cancel_failed
sell_cancel_failed
tick_refresh_failed
unknown_failure
```

### 8.4 Balance lag 不应直接关闭

单次或短期 `not enough balance / allowance` 不应直接关闭为 `sell_placement_failed`。

只有在多轮重试后确认不是同步滞后，而是持续无法卖出且达到停止条件时，才关闭为：

```text
close_reason = exit_order_unfilled
```

或者如果错误本身明确是程序/API异常，再关闭为对应 `*_failed`。

---

## 9. 推荐事件流

### 9.1 BUY 全部成交，tick 已确认，但 CLOB 可卖余额滞后

```text
signal_received
buy_order_placed
buy_filled              detail: buy_filled_total=10
entry_complete
tick_verified
exit_trigger_deferred   detail: reason=clob_sync_grace, wait_ms=...
sell_order_failed       detail: insufficient_balance, observed_sellable=4.39, exit_debt=10
sell_order_placed       detail: order_size=4.39
sell_filled             detail: sell_filled_total=4.39, exit_debt=5.61
sell_retry_started      detail: reason=balance_lag, retry_after_ms=120000
sell_order_placed       detail: order_size=5.61
sell_filled             detail: sell_filled_total=10, exit_debt=0
sell_complete
event_closed            detail: close_reason=normal_exit
```

### 9.2 tick 先确认，BUY 后续才部分成交

```text
signal_received
buy_order_placed
tick_verified
exit_trigger_deferred   detail: reason=waiting_for_entry_position
buy_filled              detail: buy_filled_total=...
exit_trigger_deferred   detail: reason=clob_sync_grace
sell_order_placed
sell_filled
sell_complete
event_closed
```

### 9.3 BUY 超时但部分成交，退出时可卖余额滞后

```text
signal_received
buy_order_placed
buy_filled
entry_timeout
buy_cancelled
exit_trigger_deferred   detail: reason=clob_sync_grace
sell_order_failed       detail: insufficient_balance, balance_lag_possible
sell_retry_started
...
event_closed
```

---

## 10. 与前端展示的关系

前端仍只用 `phase + close_reason` 做主展示：

- `phase=entry`：入场中。
- `phase=exit`：出场中。
- `phase=closed`：已结束。
- `close_reason=normal_exit`：绿色。
- `close_reason=dust_position` / `exit_order_unfilled` / `stop_loss`：琥珀色。
- 真实接口、程序、撤单、回查错误：红色。

`balance_lag` 不作为 `close_reason`，只作为 event detail 和时间线提示。例如前端时间线可以显示：

```text
CLOB 可卖余额同步中，2 分钟后重试
```

但列表摘要不要把它显示成失败。

---

## 11. 实现步骤建议

1. 在 `SweepTrade` 中明确买入、卖出、pending 三个账本值，统一由成交和订单状态事件更新。
2. 把退出逻辑改成单一 exit worker，所有 tick、风控、强制退出、fill、cancel、retry 都只唤醒它。
3. 第一次 SELL 前加入 `clob_sync_grace_ms`，基于 `last_buy_fill_at` 计算实际等待时间。
4. 将 `not enough balance / allowance` 改为 balance lag 分支：不关闭、不覆盖主账本、必要时缩量卖出并保留剩余 `exit_debt`。
5. 给 balance lag 增加 `2min -> 5min -> 10min` 重试调度。
6. event detail 增加 `exit_debt`、`observed_sellable`、`retry_after_ms`、`clob_sync_grace_ms` 等排障字段。
7. 最后再更新前端时间线展示，让 balance lag 表现为“等待同步/重试中”，不是红色失败。

---

## 12. 结论

最终设计应把“已成交仓位”和“CLOB 当前可卖余额”彻底分开：

- 已成交仓位决定最终必须退出多少。
- CLOB 可卖余额只决定本次最多能卖多少。
- 第一次 SELL 宽限期减少同步延迟导致的误报。
- balance lag 重试保证暂时不可卖的份额不会丢失。

核心公式保持不变：

```text
exit_debt = buy_filled_total - sell_filled_total - sell_pending_total
```

只要 `exit_debt` 没有归零，策略就不能把这笔交易当成已经正常退出。
