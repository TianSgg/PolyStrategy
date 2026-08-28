# Strategy Weather Sweep — Event Log JSON 设计

## 概述

一个策略实例（SweepInstance）从收到信号到关闭，会产生一系列 event log step。
每个 step 有 `step_name` 和 `detail`（JSON），记录该阶段发生了什么。

---

## 时间线

```
信号到达
  │
  ├─ signal_received          (entry)
  ├─ buy_placed / buy_failed  (entry)
  ├─ buy_filled               (entry, 仅立即成交时)
  │
  │  ... 等待成交 / 监控中 ...
  │
  ├─ entry_timeout            (entry, 超时未成交)
  ├─ tick_detected            (monitor)
  ├─ tick_verified            (monitor)
  │
  │  ... 退出阶段 ...
  │
  ├─ sell_placed              (exit)
  ├─ sell_filled              (exit)
  ├─ sell_retry_start         (exit, 卖单失败重试)
  ├─ sell_give_up             (exit, 超时放弃)
  │
  │  ... 或风控触发 ...
  │
  ├─ risk_triggered           (exit_risk)
  ├─ risk_cancel_buy          (exit_risk)
  ├─ risk_cancel_sell         (exit_risk)
  ├─ risk_force_sell          (exit_risk)
  │
  └─ event_closed             (最终)
```

---

## 公共结构

### 时间基准

所有 `offset_ms` 字段的基准为 **信号到达时刻**（`signal.occurred_at_ms`），即信号源产生该信号的时间戳。通过 offset 可以观测从信号产生到每一步的完整延迟链路。

### bbo 快照

出现在 `pre_bbo` 和 `aft_bbo` 字段中：

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
| utc | 快照时刻 (UTC) |
| offset_ms | 相对信号到达时刻的毫秒偏移 |

**pre_bbo** — risk.start() 返回后（WS 已收到 orderbook）立即拍摄，代表"下单前盘口"

**aft_bbo** — place_order() 返回后立即拍摄，代表"下单后盘口"

---

## Entry 阶段

### signal_received

收到信号，开始入场流程。

```json
{
  "signal_id": "sweep:highest-...:asset_id:1724800000000",
  "token_id": "0x1234abcd...",
  "market_slug": "highest-temperature-in-singapore-on-august-28-2026",
  "event_slug": "highest-temperature-in-singapore-on-august-28-2026",
  "city": "Singapore",
  "direction": "highest",
  "risk_ref_mid": "0.75",
  "risk_threshold": "0.375"
}
```

---

### buy_placed

下单成功提交到 CLOB（状态为 `filled` 或 `live`）。

```json
{
  "status": "filled",
  "order": {
    "order_id": "abc123-...",
    "side": "BUY",
    "price": "0.99",
    "size": "20.0000",
    "utc": "2026-08-28T04:16:16.430",
    "offset_ms": 186
  },
  "pre_bbo": {
    "best_bid": 0.70,
    "best_bid_size": 18,
    "best_ask": 0.85,
    "best_ask_size": 10,
    "utc": "2026-08-28T04:16:16.356",
    "offset_ms": 120
  },
  "aft_bbo": {
    "best_bid": 0.70,
    "best_bid_size": 18,
    "best_ask": null,
    "best_ask_size": null,
    "utc": "2026-08-28T04:16:16.430",
    "offset_ms": 186
  }
}
```

| 字段 | 说明 |
|------|------|
| status | CLOB 返回状态: `filled`(立即全部成交) / `live`(挂单等待) |
| order.order_id | CLOB 分配的订单 ID |
| order.side | 方向，固定 "BUY" |
| order.price | 委托价格 |
| order.size | 委托数量 |
| order.utc | 下单返回时刻 (UTC) |
| order.offset_ms | 相对信号到达时刻的毫秒偏移 |

---

### buy_failed

下单失败（CLOB 拒绝 / 余额不足 / 网络异常）。

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
  "pre_bbo": {
    "best_bid": 0.70,
    "best_bid_size": 18,
    "best_ask": 0.85,
    "best_ask_size": 10,
    "utc": "2026-08-28T04:16:16.356",
    "offset_ms": 120
  },
  "aft_bbo": {
    "best_bid": 0.70,
    "best_bid_size": 18,
    "best_ask": 0.85,
    "best_ask_size": 10,
    "utc": "2026-08-28T04:16:16.430",
    "offset_ms": 186
  }
}
```

| 字段 | 说明 |
|------|------|
| status | `failed` / `insufficient_balance` / `no_cash` |
| error | CLOB 返回的具体错误信息（仅 failed 时有） |

`no_cash` 是本地余额检查就不够，不会发送请求，此时无 order/pre_bbo/aft_bbo。

---

### buy_filled

订单成交通知。每次收到 User WS 推送的 trade event 时写入一条。
一个 live 订单可能被多次部分吃单，因此可能产生多条 `buy_filled`。

```json
{
  "order_id": "abc123-...",
  "filled_size": "5.0000",
  "fill_price": "0.99",
  "total_position": "5.0000",
  "source": "ws_user"
}
```

| 字段 | 说明 |
|------|------|
| filled_size | 本次成交量 |
| fill_price | 成交价格 |
| total_position | 成交后累计持仓 |
| source | `clob_response`(下单时立即成交) / `ws_user`(User WS 推送的后续成交) |

---

### entry_timeout

入场等待超时，撤销未成交的买单。

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
| cancelled_order_id | 被撤销的挂单 ID（null 表示已无挂单） |
| final_position | 超时时的累计持仓 |
| unfilled_size | 未成交的剩余量 |

如果 final_position = 0，事件关闭（reason: timeout_no_fill）。
如果 final_position > 0，进入退出阶段。

---

### entry_complete

入场在超时前全部成交（User WS 推送累计 filled = 委托量时触发）。
取消超时定时器，直接进入 monitor/exit 阶段。

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
| fill_count | 共经过几次成交才填满（1 = 一次全吃，>1 = 多次部分成交） |
| elapsed_ms | 从下单到全部成交的耗时 |

---

## 买入场景完整示例

### 场景 1：下单直接全部成交

place_order 返回 `matched`，不经过 live 阶段。

**events 表：**

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, token_id, ...}` |
| 2 | buy_placed | `{status: "filled", order: {order_id, price: "0.99", size: "20"}, pre_bbo, aft_bbo}` |
| 3 | buy_filled | `{order_id, filled_size: "20", total_position: "20", source: "clob_response"}` |

**trades 表：**
```
INSERT: status=entry_working
UPDATE: entry_price=0.99, entry_shares=20, entry_cost=19.80, entered_at=..., status=exit_working
```

之后直接进入 monitor/exit 阶段。

---

### 场景 2：下单失败

place_order 返回 `failed` 或异常。

**events 表：**

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, token_id, ...}` |
| 2 | buy_failed | `{status: "failed", error: "L1_INSUFFICIENT", order: {...}, pre_bbo, aft_bbo}` |
| 3 | event_closed | `{reason: "buy_failed", total_position: "0", duration_ms: 186}` |

**trades 表：**
```
INSERT: status=entry_working
UPDATE: status=closed, close_reason=buy_failed, closed_at=...
```

生命周期结束。

---

### 场景 3：下单 live，超时（部分成交 / 完全未成交）

place_order 返回 `live`，挂单等待，User WS 推送成交通知。

**events 表（部分成交后超时）：**

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, token_id, ...}` |
| 2 | buy_placed | `{status: "live", order: {order_id, price: "0.99", size: "20"}, pre_bbo, aft_bbo}` |
| 3 | buy_filled | `{order_id, filled_size: "8", total_position: "8", source: "ws_user"}` |
| 4 | buy_filled | `{order_id, filled_size: "7", total_position: "15", source: "ws_user"}` |
| 5 | entry_timeout | `{wait_ms: 1200000, cancelled_order_id: "abc123", final_position: "15", unfilled_size: "5"}` |

**events 表（完全未成交后超时）：**

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, token_id, ...}` |
| 2 | buy_placed | `{status: "live", order: {order_id, price: "0.99", size: "20"}, pre_bbo, aft_bbo}` |
| 3 | entry_timeout | `{wait_ms: 1200000, cancelled_order_id: "abc123", final_position: "0", unfilled_size: "20"}` |
| 4 | event_closed | `{reason: "timeout_no_fill", total_position: "0", duration_ms: 1200186}` |

**trades 表（部分成交）：**
```
INSERT: status=entry_working
UPDATE: entry_price=0.99, entry_shares=8 (第一次 fill)
UPDATE: entry_shares=15, entry_cost=14.85 (第二次 fill)
UPDATE: status=exit_working (超时后有仓位，进入退出)
```

**trades 表（完全未成交）：**
```
INSERT: status=entry_working
UPDATE: status=closed, close_reason=timeout_no_fill, closed_at=...
```

---

### 场景 4：下单 live，超时前全部成交

place_order 返回 `live`，User WS 多次推送直到累计 filled = 委托量。

**events 表：**

| seq | step | detail 关键字段 |
|-----|------|----------------|
| 1 | signal_received | `{signal_id, token_id, ...}` |
| 2 | buy_placed | `{status: "live", order: {order_id, price: "0.99", size: "20"}, pre_bbo, aft_bbo}` |
| 3 | buy_filled | `{order_id, filled_size: "12", total_position: "12", source: "ws_user"}` |
| 4 | buy_filled | `{order_id, filled_size: "8", total_position: "20", source: "ws_user"}` |
| 5 | entry_complete | `{order_id, total_filled: "20", total_position: "20", fill_count: 2, elapsed_ms: 4500}` |

**trades 表：**
```
INSERT: status=entry_working
UPDATE: entry_shares=12, entry_cost=11.88 (第一次 fill)
UPDATE: entry_shares=20, entry_cost=19.80, entered_at=... (第二次 fill)
UPDATE: status=exit_working (entry_complete 后进入退出)
```

之后直接进入 monitor/exit 阶段，取消超时定时器。

---

## Monitor 阶段

### tick_detected

WS 检测到 tick size 变化（价格精度变为 0.001）。

```json
{
  "tick_size": "0.001",
  "source": "risk_ws"
}
```

### tick_verified

通过 REST API 确认 tick size 变化属实。

```json
{
  "token_id": "0x1234abcd...",
  "confirmed": true
}
```

---

## Exit 阶段

### sell_placed

提交卖单。

```json
{
  "order_id": "def456-...",
  "price": "0.999",
  "size": "20.0000",
  "reason": "tick_exit",
  "attempt": 1
}
```

### sell_filled

卖单成交。

```json
{
  "order_id": "def456-...",
  "filled_size": "20.0000",
  "fill_price": "0.999",
  "remaining_position": "0"
}
```

### sell_retry_start

首次卖单失败，开始重试。

```json
{
  "attempt": 1,
  "status": "failed",
  "error": "L1_INSUFFICIENT"
}
```

### sell_give_up

卖单重试超时，放弃。

```json
{
  "attempts": 5,
  "timeout_sec": 600,
  "remaining_position": "20.0000",
  "last_error": "timeout"
}
```

---

## Exit Risk 阶段（风控强制退出）

### risk_triggered

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

### risk_cancel_buy / risk_cancel_sell

风控撤销挂单。

```json
{
  "order_id": "abc123-...",
  "success": true
}
```

### risk_force_sell

风控以最低价强制卖出。

```json
{
  "order_id": "ghi789-...",
  "price": "0.01",
  "size": "20.0000",
  "status": "filled",
  "attempt": 1
}
```

---

## 关闭

### event_closed

事件最终关闭。

```json
{
  "reason": "tick_exit",
  "total_position": "0",
  "duration_ms": 85000
}
```

| reason 值 | 含义 |
|-----------|------|
| tick_exit | tick 变化触发正常卖出 |
| stop_loss | 风控止损 |
| buy_failed | 入场失败 |
| timeout_no_fill | 入场超时无成交 |
| sell_failed | 卖出超时失败 |
| config_disabled | 配置被禁用/强制退出 |
