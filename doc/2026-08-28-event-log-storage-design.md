# Event Log 存储设计

> 本文档定义 `strategy_weather_sweep_events` 表中每个 step 的 `detail` JSON 结构，
> 以及 `strategy_weather_sweep_trades` 摘要表的写入规则。
> 涵盖买入、卖出、风控、强制退出全部场景。

---

## 1. 设计原则：三层分离

每笔交易（trade）从信号到达到关闭，产生若干 step。step 分为三层，每层语义固定、职责单一：

| 层 | 买入 step | 卖出 step | 职责 |
|----|-----------|-----------|------|
| **挂单结果** | `order_placed` / `order_failed` | `sell_order_placed` / `sell_order_failed` | CLOB 是否接受了订单 |
| **成交通知** | `buy_filled` | `sell_filled` | 发生了一次成交（不管来源） |
| **阶段终态** | `entry_complete` / `entry_timeout` | `sell_complete` / `sell_timeout` | 入场/退出阶段结束 |

关键规则：

- 挂单和成交是独立的 step。即使 `place_order` 返回 `matched` 全部成交，也写成"挂单 + 成交"两条，不合并。
- 每个阶段一定以终态 step 收尾。入场阶段以 `entry_complete`（满仓）或 `entry_timeout`（超时撤单）结束。
- 所有成交量都在 `buy_filled` / `sell_filled` 里，不散落在挂单 step 中。统计成交只查一个 step name。

---

## 2. 存储模型

### 2.1 events 表（流水）

```
strategy_weather_sweep_events
  event_id        CHAR(36)      -- 一笔交易的唯一 ID（UUID）
  sequence_no     INT           -- step 递增序号
  phase           ENUM          -- entry / monitor / exit / exit_risk / exit_force
  step            VARCHAR(64)   -- step 名称
  detail          JSON          -- 本文档定义的 JSON 结构
  occurred_at     DATETIME(3)   -- UTC 毫秒
```

同一个 `event_id` 的所有 step 按 `sequence_no` 递增，可通过 `WHERE event_id = ? ORDER BY sequence_no` 回放完整生命周期。

### 2.2 trades 表（摘要）

每笔交易一行，随 events 写入实时更新，供前端列表展示和盈亏统计：

```
strategy_weather_sweep_trades
  event_id, config_id, owner_user_id, proxy_wallet
  status: entry_working / exit_working / closed
  entry_price, entry_shares, entry_cost, entry_order_id, entered_at
  exit_price,  exit_shares,  exit_revenue,  exit_order_id,  exited_at
  pnl, pnl_pct, duration_ms, close_reason, started_at, closed_at
```

写入时机：`signal_received` 时 INSERT；后续每个成交 step 更新对应字段；终态 step 更新 status；`event_closed` 时最终更新 pnl 和 closed_at。

---

## 3. 公共结构

### 3.1 时间字段

| 字段 | 格式 | 说明 |
|------|------|------|
| `utc` | `2026-08-28T04:16:16.356` | 事件发生时刻，毫秒精度 UTC |
| `offset_ms` | `120` | 相对信号源时间戳 `signal.occurred_at_ms` 的毫秒偏移 |

`offset_ms` 的基准是信号源产生信号的时间，而非本地代码进入 `enter()` 的时间。
这样从信号产生到每一步的完整延迟链路都可以观测。

### 3.2 BBO 快照

出现在 `pre_bbo`（下单前盘口）和 `aft_bbo`（下单后盘口）中：

```json
{
  "best_bid": 0.70,
  "best_bid_size": 18,
  "best_ask": 0.85,
  "best_ask_size": 10,
  "utc": "2026-08-28T04:16:16.356",
  "offset_ms": 120
}
```

| 字段 | 说明 |
|------|------|
| best_bid / best_ask | 最优买/卖价，null 表示该侧为空 |
| best_bid_size / best_ask_size | 最优价位的挂单量 |
| utc | 快照时刻 |
| offset_ms | 相对信号源时间戳的偏移 |

### 3.3 order 对象

出现在 `order_placed`、`order_failed`、`sell_order_placed` 等挂单 step 中：

```json
{
  "order_id": "abc123-...",
  "side": "BUY",
  "price": "0.99",
  "size": "20.0000",
  "utc": "2026-08-28T04:16:16.430",
  "offset_ms": 186
}
```

`order.size` 是原始委托量。CLOB API 返回的 `takingAmount` / `makingAmount` 原样记录在挂单 step 的 `clob_status` / `clob_taking` / `clob_making` 字段中，供调试对比。

### 3.4 fill 成交记录

出现在 `buy_filled`、`sell_filled` 中，每次成交一条：

```json
{
  "order_id": "abc123-...",
  "filled_size": "5.0000",
  "fill_price": "0.99",
  "total_position": "5.0000",
  "source": "clob_response",
  "trade_id": "trade-uuid-..."
}
```

| 字段 | 说明 |
|------|------|
| filled_size | 本次成交量 |
| fill_price | 成交价格 |
| total_position | 成交后累计持仓（买入）或剩余持仓（卖出） |
| source | `clob_response`（下单 API 返回的成交）/ `ws_user`（User WS 推送的后续成交） |
| trade_id | Polymarket trade 事件 ID，仅 `ws_user` 来源有值，用于去重 |

---

## 4. 买入阶段（entry）

### 4.1 signal_received

收到信号，开始入场流程。detail 记录信号元数据和风控参数。

```json
{
  "signal_id": "sweep:highest-...:asset_id:1724800000000",
  "token_id": "0x1234abcd...",
  "market_slug": "highest-temperature-in-singapore-on-august-28-2026",
  "event_slug": "highest-temperature-in-singapore-on-august-28-2026",
  "city": "Singapore",
  "direction": "highest",
  "utc": "2026-08-28T04:16:16.300",
  "risk_ref_mid": "0.75",
  "risk_threshold": "0.375"
}
```

trades 表：INSERT 一行，`status = entry_working`。

### 4.2 order_placed

CLOB 接受了订单。不管 API 返回 matched（全部成交）、部分成交还是 live（完全挂单），只要订单被接受就写这一条。

```json
{
  "order": {
    "order_id": "abc123-...",
    "side": "BUY",
    "price": "0.99",
    "size": "20.0000",
    "utc": "2026-08-28T04:16:16.430",
    "offset_ms": 186
  },
  "clob_status": "matched",
  "clob_taking": "20.0000",
  "clob_making": "0",
  "pre_bbo": { "...": "下单前盘口" },
  "aft_bbo": { "...": "下单后盘口" }
}
```

| 字段 | 说明 |
|------|------|
| order.order_id | CLOB 分配的订单 ID |
| clob_status | CLOB 原始返回状态：`matched` / `live` / `delayed` |
| clob_taking | CLOB 返回的 takingAmount（taker 成交量） |
| clob_making | CLOB 返回的 makingAmount（maker 成交量） |
| pre_bbo | 下单前盘口快照 |
| aft_bbo | 下单后盘口快照 |

`clob_status` 的含义：

| clob_status | 含义 | 后续成交 step | 是否启动超时 |
|-------------|------|---------------|-------------|
| `matched` + taking = size | 全部成交 | 1 条 `buy_filled`（source=clob_response） | 否 |
| `matched` + taking < size | 部分成交，剩余挂单 | 1 条 `buy_filled`（source=clob_response）+ 后续可能多条 ws_user | 是 |
| `live` | 完全未成交，全部挂单 | 后续 0~N 条 `buy_filled`（source=ws_user） | 是 |

不管哪种情况，成交量都不在这个 step 里更新 position——那是 `buy_filled` 的职责。`clob_taking` 仅作为调试对照。

### 4.3 order_failed

下单被 CLOB 拒绝或网络异常。订单没有被接受，没有 order_id。

**本地余额不足（no_cash / insufficient_balance）** — 不会发送请求，无 BBO 快照：

```json
{
  "status": "no_cash",
  "requested_size": "100.0000",
  "available_cash": "0.00"
}
```

**CLOB 拒绝 / 网络异常** — 已发送请求，有 BBO 快照：

```json
{
  "status": "failed",
  "error": "unexpected CLOB status: error | {\"error_msg\": \"...\"}",
  "order": {
    "side": "BUY",
    "price": "0.99",
    "size": "20.0000",
    "utc": "2026-08-28T04:16:16.430",
    "offset_ms": 186
  },
  "pre_bbo": { "...": "下单前盘口" },
  "aft_bbo": { "...": "下单后盘口" }
}
```

trades 表：UPDATE `status = closed, close_reason = buy_failed, closed_at = ...`。
紧接着写 `event_closed`，生命周期结束。

### 4.4 buy_filled

每次成交一条。来源有两种：

- `clob_response` — `place_order` 返回 matched（全部或部分），在 API 响应里就知道成交量
- `ws_user` — 订单 live/partial 后，User WS 推送的后续成交

```json
{
  "order_id": "abc123-...",
  "filled_size": "5.0000",
  "fill_price": "0.99",
  "total_position": "5.0000",
  "source": "ws_user",
  "trade_id": "trade-uuid-..."
}
```

trades 表：每次 `buy_filled` UPDATE `entry_shares`（累计）和 `entry_cost`（累计）。
首次成交时额外 UPDATE `entry_price` 和 `entered_at`。

### 4.5 entry_complete

入场阶段终态：超时前全部成交。取消超时定时器，进入 monitor 阶段。

```json
{
  "order_id": "abc123-...",
  "total_filled": "20.0000",
  "total_position": "20.0000",
  "fill_count": 3,
  "elapsed_ms": 4500
}
```

| 字段 | 说明 |
|------|------|
| total_filled | 累计成交量（= 委托量） |
| total_position | 成交后总持仓 |
| fill_count | 共经过几次成交才填满（1 = 一次全吃，>1 = 多次部分成交） |
| elapsed_ms | 从下单到全部成交的耗时 |

trades 表：UPDATE `status = exit_working`。

### 4.6 entry_timeout

入场阶段终态：超时触发，撤销未成交的买单。

```json
{
  "wait_ms": 1200000,
  "cancelled_order_id": "abc123-...",
  "final_position": "15.0000",
  "unfilled_size": "5.0000"
}
```

| 字段 | 说明 |
|------|------|
| wait_ms | 等待时长 |
| cancelled_order_id | 被撤销的挂单 ID（null 表示已无挂单） |
| final_position | 超时时的累计持仓 |
| unfilled_size | 未成交的剩余量（= 委托量 - 成交量） |

走向：
- `final_position = 0` → 写 `event_closed`（reason=timeout_no_fill），生命周期结束
- `final_position > 0` → trades 表 UPDATE `status = exit_working`，进入 monitor/exit 阶段

如果 tick size 在入场等待期内已变为 `0.001` 并通过 HTTP 校验，但买单仍没有任何成交，
只写 `normal_exit_deferred`（trigger=tick_size_change, reason=no_position），不提前关闭 event。
订单仍等待 `entry_wait_ms` 到期；到期撤单并校准后若仍无仓位，最终关闭原因仍是 `timeout_no_fill`。

---

## 5. 买入场景完整示例

### 场景 1：下单全部成交

`place_order` 返回 `matched`，`takingAmount = 委托量`。

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, token_id, ..., utc, risk_ref_mid}` |
| 2 | order_placed | `{order: {order_id, price, size: "20"}, clob_status: "matched", clob_taking: "20", pre_bbo, aft_bbo}` |
| 3 | buy_filled | `{order_id, filled_size: "20", fill_price: "0.99", total_position: "20", source: "clob_response"}` |
| 4 | entry_complete | `{order_id, total_filled: "20", fill_count: 1, elapsed_ms: 0}` |

trades 表：INSERT → UPDATE entry_* → UPDATE status=exit_working

之后进入 monitor/exit 阶段。不启动超时定时器。

### 场景 1b：下单部分成交，剩余挂单等待

`place_order` 返回 `matched` 但 `takingAmount < 委托量`。一部分立即成交，剩余挂在盘口上。

下面展示"部分成交 + 后续 WS 成交 + 超时"的序列（剩余部分也可能在超时前全部成交，变为 `entry_complete`）：

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, ...}` |
| 2 | order_placed | `{order: {order_id, price, size: "20"}, clob_status: "matched", clob_taking: "12", pre_bbo, aft_bbo}` |
| 3 | buy_filled | `{order_id, filled_size: "12", fill_price: "0.99", total_position: "12", source: "clob_response"}` |
| 4 | buy_filled | `{order_id, filled_size: "5", fill_price: "0.99", total_position: "17", source: "ws_user", trade_id: "t1"}` |
| 5 | entry_timeout | `{wait_ms: 1200000, cancelled_order_id: "abc", final_position: "17", unfilled_size: "3"}` |

trades 表：INSERT → UPDATE entry_shares=12 → UPDATE entry_shares=17 → UPDATE status=exit_working

与场景 3b 的区别：第一条 `buy_filled` 的 source 是 `clob_response`（API 返回时已成交），而非 `ws_user`。

### 场景 2：下单失败

`place_order` 返回 `failed` 或余额不足。

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, ...}` |
| 2 | order_failed | `{status: "failed", error: "...", order: {...}, pre_bbo, aft_bbo}` |
| 3 | event_closed | `{reason: "buy_failed", total_position: "0", duration_ms: 186}` |

trades 表：INSERT → UPDATE status=closed, close_reason=buy_failed

生命周期结束。

### 场景 3a：live + 零成交 + 超时

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, ...}` |
| 2 | order_placed | `{order: {order_id, price, size: "20"}, clob_status: "live", clob_taking: "0", pre_bbo, aft_bbo}` |
| 3 | entry_timeout | `{wait_ms: 1200000, cancelled_order_id: "abc", final_position: "0", unfilled_size: "20"}` |
| 4 | event_closed | `{reason: "timeout_no_fill", total_position: "0", duration_ms: 1200186}` |

trades 表：INSERT → UPDATE status=closed, close_reason=timeout_no_fill

### 场景 3b：live + 部分成交 + 超时

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, ...}` |
| 2 | order_placed | `{order: {order_id, price, size: "20"}, clob_status: "live", clob_taking: "0", pre_bbo, aft_bbo}` |
| 3 | buy_filled | `{order_id, filled_size: "8", total_position: "8", source: "ws_user", trade_id: "t1"}` |
| 4 | buy_filled | `{order_id, filled_size: "7", total_position: "15", source: "ws_user", trade_id: "t2"}` |
| 5 | entry_timeout | `{wait_ms: 1200000, cancelled_order_id: "abc", final_position: "15", unfilled_size: "5"}` |

trades 表：INSERT → UPDATE entry_shares=8 → UPDATE entry_shares=15 → UPDATE status=exit_working

### 场景 4：live + 超时前全部成交

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, ...}` |
| 2 | order_placed | `{order: {order_id, price, size: "20"}, clob_status: "live", clob_taking: "0", pre_bbo, aft_bbo}` |
| 3 | buy_filled | `{order_id, filled_size: "12", total_position: "12", source: "ws_user", trade_id: "t1"}` |
| 4 | buy_filled | `{order_id, filled_size: "8", total_position: "20", source: "ws_user", trade_id: "t2"}` |
| 5 | entry_complete | `{order_id, total_filled: "20", fill_count: 2, elapsed_ms: 4500}` |

trades 表：INSERT → UPDATE entry_shares=12 → UPDATE entry_shares=20, entered_at → UPDATE status=exit_working

### 五场景区分要点

| 区分维度 | 场景 1 | 场景 1b | 场景 2 | 场景 3a | 场景 3b | 场景 4 |
|----------|--------|---------|--------|---------|---------|---------|
| 挂单 step | order_placed | order_placed | order_failed | order_placed | order_placed | order_placed |
| clob_status | matched | matched | (无) | live | live | live |
| buy_filled.source | clob_response | clob_response+ws_user | (无) | (无) | ws_user | ws_user |
| buy_filled 条数 | 1 | 1+ | 0 | 0 | 2 | 2 |
| 阶段终态 | entry_complete | entry_timeout | (直接 event_closed) | entry_timeout | entry_timeout | entry_complete |
| 走向 | monitor | exit | 结束 | 结束 | exit | monitor |

---

## 6. Monitor 阶段

### 6.1 tick_detected

WS 检测到 tick size 变化（价格精度变为 0.001）。

```json
{
  "tick_size": "0.001",
  "source": "risk_ws"
}
```

### 6.2 tick_verified

通过 REST API 确认 tick size 变化属实。

```json
{
  "token_id": "0x1234abcd...",
  "confirmed": true
}
```

---

## 7. 卖出阶段（exit）

### 7.1 sell_order_placed

CLOB 接受了卖单。结构同 `order_placed`，加 `trigger` 和 `attempt`。

```json
{
  "order": {
    "order_id": "def456-...",
    "side": "SELL",
    "price": "0.999",
    "size": "20.0000",
    "utc": "2026-08-28T04:18:00.000",
    "offset_ms": 85000
  },
  "clob_status": "matched",
  "clob_taking": "20.0000",
  "clob_making": "0",
  "trigger": "tick_size_change",
  "attempt": 1,
  "pre_bbo": { "...": "下单前盘口" },
  "aft_bbo": { "...": "下单后盘口" }
}
```

| clob_status | 含义 | 后续成交 step | 是否启动卖出超时 |
|-------------|------|---------------|-----------------|
| `matched` + taking = size | 全部成交 | 1 条 `sell_filled`（source=clob_response） | 否 |
| `matched` + taking < size | 部分成交，剩余挂单 | 1 条 `sell_filled`（source=clob_response）+ 后续可能多条 ws_user | 是 |
| `live` | 完全未成交，全部挂单 | 后续 0~N 条 `sell_filled`（source=ws_user） | 是 |

### 7.2 sell_order_failed

卖单被 CLOB 拒绝。写完后进入重试流程（`sell_retry_start`）。

```json
{
  "status": "failed",
  "error": "L1_INSUFFICIENT",
  "order": {
    "side": "SELL",
    "price": "0.999",
    "size": "20.0000",
    "utc": "...",
    "offset_ms": 85000
  },
  "attempt": 1
}
```

### 7.3 sell_filled

每次成交一条。来源同买入：`clob_response`（API 返回时已成交）和 `ws_user`（后续 WS 推送）。

```json
{
  "order_id": "def456-...",
  "filled_size": "5.0000",
  "fill_price": "0.999",
  "remaining_position": "15.0000",
  "source": "ws_user",
  "trade_id": "trade-uuid-..."
}
```

trades 表：每次 `sell_filled` UPDATE `exit_shares`（累计）和 `exit_revenue`（累计）。
首次成交时额外 UPDATE `exit_price` 和 `exited_at`。

### 7.4 sell_complete

卖出阶段终态：全部成交，持仓清零。

```json
{
  "order_id": "def456-...",
  "total_filled": "20.0000",
  "remaining_position": "0",
  "fill_count": 2,
  "elapsed_ms": 3000
}
```

之后写 `event_closed`（reason=normal_exit）。

### 7.5 sell_timeout

卖出阶段终态：live/partial 卖单等待成交超时，撤销挂单。

```json
{
  "order_id": "def456-...",
  "waited_ms": 30000,
  "filled_during_wait": "5.0000",
  "remaining_position": "15.0000"
}
```

| 字段 | 说明 |
|------|------|
| order_id | 被撤销的卖单 ID |
| waited_ms | 等待时长 |
| filled_during_wait | 等待期间已成交量（可能部分成交） |
| remaining_position | 撤单后剩余持仓 |

撤单后重试：写 `sell_order_placed`（attempt + 1）。

### 7.6 sell_retry_start

`place_order` 返回 `failed`，开始 backoff 重试。与 `sell_timeout` 的区别：
- `sell_retry_start` — 订单没上去（CLOB 拒绝）
- `sell_timeout` — 订单成功挂上（live）但成交超时，需要撤单再重试

```json
{
  "attempt": 1,
  "error": "L1_INSUFFICIENT"
}
```

### 7.7 sell_give_up

卖出总超时，放弃。

```json
{
  "attempts": 5,
  "timeout_sec": 600,
  "remaining_position": "20.0000",
  "last_error": "timeout"
}
```

### 7.8 卖出流程状态机

```
sell_order_placed
  ├─ matched (全部) → sell_filled (source=clob_response) → sell_complete → event_closed
  │
  ├─ matched (部分) → sell_filled (source=clob_response)
  │    └─ 剩余挂单等待 WS 成交
  │         ├─ 全部成交 → sell_filled ... → sell_complete → event_closed
  │         └─ 超时 → sell_timeout → 撤单 → 重试 sell_order_placed
  │
  ├─ live → 等待 User WS 成交推送（带超时）
  │    ├─ 收到 trade → sell_filled (source=ws_user)
  │    │    ├─ remaining=0 → sell_complete → event_closed
  │    │    └─ remaining>0 → 继续等待
  │    └─ 超时未全部成交 → sell_timeout → 撤单 → 重试 sell_order_placed
  │
  └─ failed → sell_retry_start → backoff → 重试 sell_order_placed

总超时到达 → sell_give_up → event_closed (reason=sell_failed)
```

---

## 8. 风控退出阶段（exit_risk）

### 8.1 risk_triggered

风控条件触发（mid 价格跌破阈值）。

```json
{
  "reference_mid": "0.75",
  "threshold": "0.375",
  "stop_loss_ratio": "0.50",
  "state_at_trigger": "entry_working",
  "pending_buy": "abc123-...",
  "pending_sell": null,
  "position_shares": "20.0000"
}
```

### 8.2 risk_cancel_buy / risk_cancel_sell

风控撤销挂单。

```json
{
  "order_id": "abc123-...",
  "success": true
}
```

### 8.3 risk_sell_order_placed

风控以最低价强制卖出。结构同 `sell_order_placed`，price 固定 0.01，reason 固定 `stop_loss`。

```json
{
  "order": {
    "order_id": "ghi789-...",
    "side": "SELL",
    "price": "0.01",
    "size": "20.0000",
    "utc": "...",
    "offset_ms": 200000
  },
  "clob_status": "matched",
  "clob_taking": "20.0000",
  "reason": "stop_loss",
  "attempt": 1
}
```

风控卖出也可能出现 live/partial，step 流程同正常卖出（`sell_filled` / `sell_timeout` / `sell_complete`），
只是 phase 标为 `exit_risk`，close reason 为 `stop_loss`。

---

## 9. 强制退出阶段（exit_force）

配置被禁用或参数变更时触发，立即撤销所有挂单。

### 9.1 force_exit

```json
{
  "reason": "config_disabled",
  "trigger": "user",
  "state_at_exit": "entry_working",
  "position_shares": "15.0000"
}
```

| reason 值 | 含义 |
|-----------|------|
| config_disabled | 配置被禁用 |
| config_changed | 配置参数变更 |
| container_shutdown | 容器关闭 |

### 9.2 buy_cancelled / sell_cancelled

```json
{
  "order_id": "abc123-...",
  "success": true
}
```

强制退出不主动平仓——只撤挂单，已有持仓保持。如果持仓 > 0，需要后续手动处理或等市场结算。

---

## 10. 关闭

### 10.1 event_closed

事件最终关闭。一定是最后一个 step。

```json
{
  "reason": "normal_exit",
  "total_position": "0",
  "duration_ms": 85000
}
```

| reason 值 | 含义 |
|-----------|------|
| normal_exit | 买入成功后按策略预期完成卖出 |
| stop_loss | 风控止损完成 |
| buy_failed | 入场失败 |
| timeout_no_fill | 入场超时无成交 |
| sell_failed | 卖出超时失败 |
| force_exit | 强制退出 |

历史数据中可能存在 `tick_exit`，表示旧版“tick 变化且正常卖出完成”的合并语义；
新数据不再写入该值，前端仅作只读兼容展示。

trades 表：最终 UPDATE `status = closed, close_reason = reason, pnl = ..., pnl_pct = ..., duration_ms = ..., closed_at = ...`。

`event_closed` 写完后调用 `el.end_event()`，该 event_id 不再写入新 step。

---

## 11. User WS 成交检测

### 11.1 连接

```
wss://ws-subscriptions-clob.polymarket.com/ws/user
```

连接后立即发送认证订阅帧：

```json
{
  "auth": {
    "apiKey": "<clob_api_key>",
    "secret": "<clob_api_secret>",
    "passphrase": "<clob_api_passphrase>"
  },
  "type": "user"
}
```

心跳：每 10 秒发文本帧 `PING`，服务端回 `PONG`。

### 11.2 事件类型

**Order 事件**（`event_type: "order"`）— 订单状态变更：

| type | 含义 |
|------|------|
| PLACEMENT | 新订单被接受，挂上盘口 |
| UPDATE | 部分或全部成交，size_matched 变了 |
| CANCELLATION | 剩余未成交部分被撤销 |

关键字段：`id`（order_id）、`original_size`、`size_matched`、`status`（LIVE / MATCHED / CANCELED）。

**Trade 事件**（`event_type: "trade"`）— 具体成交流水：

关键字段：`taker_order_id`、`size`（本次成交量）、`price`（成交价）、`side`、`trader_side`（TAKER / MAKER）、`status`（MATCHED -> MINED -> CONFIRMED 终态）。

### 11.3 与 event log 的关系

- Trade 事件 -> 每次成交推一条，写入 `buy_filled` / `sell_filled` step，source = `ws_user`
- `place_order` 返回 matched 时的成交 -> 写 `buy_filled` / `sell_filled`，source = `clob_response`，不依赖 WS
- Order UPDATE 事件 -> `size_matched` 是累计值，用于判断满仓（`size_matched == original_size` -> `entry_complete` / `sell_complete`）

### 11.4 去重

`place_order` 返回 matched 时已记录成交量，随后 WS 会推送同一条 trade。
用 `_order_post_filled: Dict[str, Decimal]` 按 order_id 记录已通过 API 响应计入的成交量，
WS trade 到达时按量扣除，避免重复计算 position（参见 ws-trade-dedup.md）。

### 11.5 重连恢复

WS 断连期间可能错过成交事件。重连后需要：
1. 调用 `get_open_orders()` 获取所有挂单的当前 `size_matched`
2. 与本地记录的已成交量对比，补录差额作为 `buy_filled` / `sell_filled`
3. 如果 `size_matched == original_size`，触发 `entry_complete` / `sell_complete`

---

## 12. trades 表写入时机汇总

| step | trades 表操作 |
|------|---------------|
| signal_received | INSERT: status=entry_working, started_at=now |
| buy_filled (首次) | UPDATE: entry_price, entry_shares, entry_cost, entry_order_id, entered_at |
| buy_filled (后续) | UPDATE: entry_shares (累计), entry_cost (累计) |
| entry_complete | UPDATE: status=exit_working |
| entry_timeout (有仓位) | UPDATE: status=exit_working |
| entry_timeout (无仓位) | UPDATE: status=closed, close_reason=timeout_no_fill, closed_at |
| order_failed | UPDATE: status=closed, close_reason=buy_failed, closed_at |
| sell_filled (首次) | UPDATE: exit_price, exit_shares, exit_revenue, exit_order_id, exited_at |
| sell_filled (后续) | UPDATE: exit_shares (累计), exit_revenue (累计) |
| sell_complete | (无额外操作，等待 event_closed) |
| event_closed | UPDATE: status=closed, close_reason, pnl, pnl_pct, duration_ms, closed_at |

pnl 计算：`pnl = exit_revenue - entry_cost`，`pnl_pct = pnl / entry_cost * 100`。
仅在 `entry_cost > 0 且 exit_revenue > 0` 时计算，否则为 null。

---

## 13. 完整生命周期示例

### 13.1 典型成功交易（live 买入 + tick 卖出）

```
seq  phase       step                detail 关键字段
1    entry       signal_received     {signal_id, token_id, utc, risk_ref_mid}
2    entry       order_placed        {order: {order_id, price: "0.99", size: "20"}, clob_status: "live", clob_taking: "0", pre_bbo, aft_bbo}
3    entry       buy_filled          {filled_size: "12", total_position: "12", source: "ws_user", trade_id: "t1"}
4    entry       buy_filled          {filled_size: "8", total_position: "20", source: "ws_user", trade_id: "t2"}
5    entry       entry_complete      {total_filled: "20", fill_count: 2, elapsed_ms: 4500}
6    monitor     tick_detected       {tick_size: "0.001"}
7    monitor     tick_verified       {confirmed: true}
8    exit        sell_order_placed   {order: {order_id, price: "0.999", size: "20"}, clob_status: "matched", clob_taking: "20", trigger: "tick_size_change", attempt: 1}
9    exit        sell_filled         {filled_size: "20", remaining_position: "0", source: "clob_response"}
10   exit        sell_complete       {total_filled: "20", fill_count: 1, elapsed_ms: 0}
11   exit        event_closed        {reason: "normal_exit", total_position: "0", duration_ms: 85000}
```

trades 表最终状态：
```
status=closed, close_reason=normal_exit
entry_price=0.99, entry_shares=20, entry_cost=19.80
exit_price=0.999, exit_shares=20, exit_revenue=19.98
pnl=0.18, pnl_pct=0.91, duration_ms=85000
```

### 13.2 风控止损

```
seq  phase       step                detail 关键字段
1    entry       signal_received     {signal_id, ...}
2    entry       order_placed        {order: {order_id, size: "20"}, clob_status: "matched", clob_taking: "20", pre_bbo, aft_bbo}
3    entry       buy_filled          {filled_size: "20", source: "clob_response"}
4    entry       entry_complete      {total_filled: "20", fill_count: 1, elapsed_ms: 0}
5    exit_risk   risk_triggered      {reference_mid: "0.75", threshold: "0.375", position_shares: "20"}
6    exit_risk   risk_sell_order_placed {order: {price: "0.01", size: "20"}, clob_status: "matched", clob_taking: "20", reason: "stop_loss", attempt: 1}
7    exit_risk   sell_filled         {filled_size: "20", remaining_position: "0", source: "clob_response"}
8    exit_risk   sell_complete       {total_filled: "20", fill_count: 1, elapsed_ms: 0}
9    exit_risk   event_closed        {reason: "stop_loss", total_position: "0", duration_ms: 120000}
```

trades 表：
```
status=closed, close_reason=stop_loss
entry_price=0.99, entry_shares=20, entry_cost=19.80
exit_price=0.01, exit_shares=20, exit_revenue=0.20
pnl=-19.60, pnl_pct=-98.99
```

### 13.3 强制退出（配置禁用，有持仓）

```
seq  phase       step                detail 关键字段
1    entry       signal_received     {signal_id, ...}
2    entry       order_placed        {order: {order_id, size: "20"}, clob_status: "live", clob_taking: "0", pre_bbo, aft_bbo}
3    entry       buy_filled          {filled_size: "15", total_position: "15", source: "ws_user", trade_id: "t1"}
4    exit_force  force_exit          {reason: "config_disabled", state_at_exit: "entry_working", position_shares: "15"}
5    exit_force  buy_cancelled       {order_id: "abc", success: true}
6    exit_force  event_closed        {reason: "force_exit", total_position: "15", duration_ms: 300000}
```

trades 表：
```
status=closed, close_reason=force_exit
entry_price=0.99, entry_shares=15, entry_cost=14.85
exit_price=null, exit_shares=null, exit_revenue=null
pnl=null, pnl_pct=null
```

持仓 15 shares 保留，未平仓。后续需手动处理或等市场结算。
