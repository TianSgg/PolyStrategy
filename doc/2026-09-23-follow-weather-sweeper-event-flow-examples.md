# Follow Weather Sweeper Event 流程示例

本文按照当前 `strategy_follow_weather_sweeper` 的实现，说明一笔交易从收到信号、提交买单、成交、风控监控到退出时，event 表中可能出现的记录。

## 时间和顺序

一条 event step 的结构如下：

```json
{
  "event_id": "同一笔交易生命周期 ID",
  "phase": "entry",
  "step": "buy_order_placed",
  "sequence_no": 2,
  "detail": {},
  "occurred_at": "2026-09-23T05:00:00.146Z"
}
```

- `event_id`：一次信号交易生命周期的 ID。
- `phase`：`entry` 或 `exit`。
- `sequence_no`：后端调用 `EventLogger.log_step()` 时分配的序号。BBO 和风控是异步任务，所以它表示事件写入顺序，不代表交易所撮合顺序。
- `occurred_at`：后端调用 `log_step()` 的 UTC 时间，数据库字段是 `DATETIME(3)`，保留毫秒。它不是数据库落库完成时间，也不一定是交易所撮合时间。
- 事件详情中的时间点统一使用 UTC ISO 字符串，例如 `2026-09-23T05:00:00.145Z`。
- `offset_ms`、`latency_ms`、`duration_ms`、`wait_ms` 是时长或偏移量，仍然使用毫秒数值。

## 买单和 BBO 的实际流程

收到信号后并行启动：

1. 查询下单前的 `pre_bbo`。
2. 提交买单。
3. 启动风控，风控独立订阅并同步订单簿。

CLOB 返回买单被接受后，立即注册 User WebSocket 的成交监听，然后等待 BBO 查询结果。BBO 查询不会阻塞下单、成交监听或入场超时计时器。

因此 `pre_bbo` 和 `aft_bbo` 都属于同一个 `buy_order_placed` 事件：

- 初次写入时，如果 BBO 还没有返回，`pre_bbo` 或 `aft_bbo` 可以是 `null`。
- BBO 查询完成后，后台更新同一条 `buy_order_placed` 数据库记录。
- 不会新增单独的 `buy_bbo_observed` 业务事件。
- `buy_order_placed.detail.order_response_at` 是 CLOB 下单响应时间。
- `pre_bbo.captured_at` 是下单前实际观察到 BBO 的时间。
- `aft_bbo.captured_at` 是 CLOB 接受订单后实际观察到 BBO 的时间。
- `latency_ms` 和 `offset_ms` 保留用于诊断查询耗时和相对信号时间。

`risk_started` 也是异步任务，可能在 `buy_filled`、`entry_complete` 或 BBO 字段更新前后出现。下面的序号只是示例，不是固定协议。

## 1. 买入直接全部成交

假设请求买入 20 份，价格为 `0.99`，CLOB 返回 `filled`，实际成交价为 `0.9700`。

### 事件链

```text
1  entry  signal_received
2  entry  buy_order_placed       CLOB 已接受，状态 filled；同一条记录随后补齐 pre/aft BBO
3  entry  buy_filled             20 份，source=clob_response
4  entry  entry_complete
5  entry  risk_started
6  entry  tick_verified           market_ws = 0.001
7  entry  tick_verified           tick_size_api = 0.001
8  entry  tick_verified           book_api = 0.001
9  exit   sell_order_placed       正常退场
10 exit   sell_filled             全部卖出
11 exit   sell_complete
12 exit   event_closed             close_reason=normal_exit
```

### `signal_received`

```json
{
  "event_id": "evt-direct-filled-001",
  "phase": "entry",
  "step": "signal_received",
  "sequence_no": 1,
  "detail": {
    "signal_id": "predexon:0xabc:token-001",
    "token_id": "token-001",
    "market_slug": "highest-temperature-in-beijing-on-september-23-2026-29c",
    "event_slug": "highest-temperature-in-beijing-on-september-23-2026",
    "risk_reference_source": "post_signal_orderbook_sync",
    "signal_orderbook_snapshot_present": false
  },
  "occurred_at": "2026-09-23T05:00:00.002Z"
}
```

### `buy_order_placed` 初次写入

这条记录代表买单请求已经得到 CLOB 接受。它不是成交记录；实际成交仍由后面的 `buy_filled` 表示。

```json
{
  "event_id": "evt-direct-filled-001",
  "phase": "entry",
  "step": "buy_order_placed",
  "sequence_no": 2,
  "detail": {
    "order": {
      "order_id": "0xbuy-direct-001",
      "side": "BUY",
      "price": "0.99",
      "size": "20",
      "offset_ms": 145
    },
    "clob_status": "filled",
    "order_response_at": "2026-09-23T05:00:00.145Z",
    "pre_bbo": null,
    "aft_bbo": null,
    "bbo_observation_pending": true
  },
  "occurred_at": "2026-09-23T05:00:00.146Z"
}
```

### BBO 查询完成后更新同一条记录

下面是同一个 `event_id`、同一个 `sequence_no=2` 的最终内容。不是新事件。

```json
{
  "event_id": "evt-direct-filled-001",
  "phase": "entry",
  "step": "buy_order_placed",
  "sequence_no": 2,
  "detail": {
    "order": {
      "order_id": "0xbuy-direct-001",
      "side": "BUY",
      "price": "0.99",
      "size": "20",
      "offset_ms": 145
    },
    "clob_status": "filled",
    "order_response_at": "2026-09-23T05:00:00.145Z",
    "pre_bbo": {
      "status": "ok",
      "source": "clob_book_api",
      "captured_at": "2026-09-23T05:00:00.052Z",
      "latency_ms": 51.2,
      "best_bid": 0.96,
      "best_bid_size": 10,
      "best_ask": 0.97,
      "best_ask_size": 20,
      "offset_ms": 52,
      "tick_size": "0.01"
    },
    "aft_bbo": {
      "status": "ok",
      "source": "clob_book_api",
      "captured_at": "2026-09-23T05:00:00.170Z",
      "latency_ms": 24.1,
      "best_bid": 0.96,
      "best_bid_size": 10,
      "best_ask": 0.97,
      "best_ask_size": 20,
      "offset_ms": 170,
      "tick_size": "0.01"
    },
    "bbo_observation_pending": false
  },
  "occurred_at": "2026-09-23T05:00:00.146Z"
}
```

注意：`occurred_at` 仍然是 `buy_order_placed` 的发生时间；BBO 自己的观察时间看 `pre_bbo.captured_at` 和 `aft_bbo.captured_at`，查询耗时看 `latency_ms`。

### 成交和正常退出

```json
{
  "event_id": "evt-direct-filled-001",
  "phase": "entry",
  "step": "buy_filled",
  "sequence_no": 3,
  "detail": {
    "order_id": "0xbuy-direct-001",
    "filled_size": "20",
    "fill_price": "0.9700",
    "total_position": "20",
    "source": "clob_response"
  },
  "occurred_at": "2026-09-23T05:00:00.148Z"
}
```

```text
entry_complete -> tick_verified x3 -> sell_order_placed
               -> sell_filled -> sell_complete
               -> event_closed(close_reason=normal_exit)
```

正常卖单示例：

```json
{
  "event_id": "evt-direct-filled-001",
  "phase": "exit",
  "step": "sell_order_placed",
  "sequence_no": 9,
  "detail": {
    "order": {
      "order_id": "0xsell-normal-001",
      "side": "SELL",
      "price": "0.999",
      "size": "20",
      "offset_ms": 600010
    },
    "clob_status": "live",
    "clob_taking": false,
    "clob_making": true,
    "trigger": "tick_size_change",
    "attempt": 1
  },
  "occurred_at": "2026-09-23T05:10:00.011Z"
}
```

## 2. 买入慢慢成交

订单接受后先注册 User WebSocket 监听，后续每次成交都会产生一条 `buy_filled`。

### 2A. 慢慢成交，最终全部成交

假设买单 20 份分三次成交：5、7、8。

```text
1  entry  signal_received
2  entry  buy_order_placed       clob_status=live，记录并最终补齐 pre/aft BBO
3  entry  risk_started
4  entry  buy_filled             5 份，source=ws_order_update
5  entry  buy_filled             7 份，累计 12 份
6  entry  buy_filled             8 份，累计 20 份
7  entry  entry_complete
8  entry  tick_verified           三个来源均为 0.001
9  exit   sell_order_placed
10 exit   sell_filled
11 exit   sell_complete
12 exit   event_closed             close_reason=normal_exit
```

```json
{
  "event_id": "evt-slow-full-001",
  "phase": "entry",
  "step": "buy_filled",
  "sequence_no": 4,
  "detail": {
    "order_id": "0xbuy-slow-001",
    "filled_size": "5",
    "fill_price": "0.9600",
    "total_position": "5",
    "source": "ws_order_update"
  },
  "occurred_at": "2026-09-23T05:20:00.800Z"
}
```

### 2B. 慢慢成交，入场超时时只有部分成交

假设买单 20 份最终只成交 8 份。超时后撤销未成交的 12 份，已成交的 8 份保留为持仓。

```text
1  entry  signal_received
2  entry  buy_order_placed       clob_status=live，记录并最终补齐 pre/aft BBO
3  entry  risk_started
4  entry  buy_filled             8 份
5  entry  entry_timeout           撤销未成交部分，final_position=8
6  entry  tick_verified           三个来源确认 tick_size=0.001
7  exit   sell_order_placed       对已成交的 8 份退场
8  exit   sell_filled             8 份
9  exit   sell_complete
10 exit   event_closed             close_reason=normal_exit
```

```json
{
  "event_id": "evt-slow-partial-001",
  "phase": "entry",
  "step": "entry_timeout",
  "sequence_no": 5,
  "detail": {
    "wait_ms": 1200000,
    "cancelled_order_id": "0xbuy-slow-002",
    "final_position": "8",
    "unfilled_size": "12"
  },
  "occurred_at": "2026-09-23T05:40:00.000Z"
}
```

如果 tick 尚未确认到 `0.001`，不会立即 SELL；交易会保留在 `entry`，等三个 tick 来源确认后再开始正常退场。

## 3. 买入无成交

假设买单被 CLOB 接受为 `live`，但直到 `entry_wait_ms` 到期都没有成交。

```text
1  entry  signal_received
2  entry  buy_order_placed       clob_status=live，记录并最终补齐 pre/aft BBO
3  entry  risk_started
4  entry  entry_timeout           撤销买单，final_position=0
5  entry  event_closed             close_reason=timeout_no_fill
```

```json
{
  "event_id": "evt-no-fill-001",
  "phase": "entry",
  "step": "entry_timeout",
  "sequence_no": 4,
  "detail": {
    "wait_ms": 1200000,
    "cancelled_order_id": "0xbuy-no-fill-001",
    "final_position": "0",
    "unfilled_size": "20"
  },
  "occurred_at": "2026-09-23T06:00:00.000Z"
}
```

这个场景不会出现 `buy_filled`、`entry_complete`、`sell_order_placed` 或 `sell_filled`。BBO 查询和成交监听不会阻塞超时处理。

## 4. 风控退出

假设买单已经成交 10 份，风控参考 mid 为 `0.965`，止损比例为 `0.60`，阈值为 `0.579`。随后当前 mid 跌到 `0.570`，触发保护性退出。

风控退出会先停止风险监控，再撤销仍然挂着的 BUY，最后提交一次快速 SELL。若 BUY 已经全部成交，则没有 `buy_cancelled`。

```text
1  entry  signal_received
2  entry  buy_order_placed
3  entry  risk_started
4  entry  buy_filled             10 份
5  entry  risk_triggered          mid <= reference_mid * stop_loss_ratio
6  entry  buy_cancelled           如果 BUY 仍然挂单
7  exit   sell_order_placed       fast_path=true，通常价格为 0.01
8  exit   sell_filled             风控 SELL 成交
9  exit   sell_complete
10 exit   event_closed             close_reason=stop_loss
```

### `risk_triggered`

```json
{
  "event_id": "evt-risk-exit-001",
  "phase": "entry",
  "step": "risk_triggered",
  "sequence_no": 5,
  "detail": {
    "reference_mid": "0.965",
    "threshold": "0.5790",
    "stop_loss_ratio": "0.60",
    "state_at_trigger": "entry",
    "pending_buy": "0xbuy-risk-001",
    "pending_sell": null,
    "position_shares": "10",
    "trigger_bbo": {
      "best_bid": "0.56",
      "best_ask": "0.58",
      "mid": "0.57"
    }
  },
  "occurred_at": "2026-09-23T07:00:20.001Z"
}
```

### 快速 SELL

```json
{
  "event_id": "evt-risk-exit-001",
  "phase": "exit",
  "step": "sell_order_placed",
  "sequence_no": 7,
  "detail": {
    "order": {
      "order_id": "0xsell-risk-001",
      "side": "SELL",
      "price": "0.01",
      "size": "10",
      "offset_ms": 20020
    },
    "clob_status": "live",
    "clob_taking": true,
    "clob_making": false,
    "reason": "stop_loss",
    "trigger": "stop_loss",
    "attempt": 1,
    "fast_path": true
  },
  "occurred_at": "2026-09-23T07:00:20.021Z"
}
```

```text
sell_order_placed -> sell_filled -> sell_complete
                  -> event_closed(close_reason=stop_loss)
```

## 事件和 trade 的对应关系

| 场景 | 关键 event step | `trade.close_reason` |
|---|---|---|
| 买入直接全部成交，正常卖出 | `buy_filled`、`entry_complete`、`sell_complete` | `normal_exit` |
| 买入慢慢成交并最终全部成交 | 多条 `buy_filled`、`entry_complete`、`sell_complete` | `normal_exit` |
| 买入慢慢成交但只成交一部分 | `entry_timeout`、`sell_complete` | `normal_exit` |
| 买入无成交 | `entry_timeout`、`event_closed` | `timeout_no_fill` |
| 风控退出 | `risk_triggered`、快速 `sell_order_placed` | `stop_loss` |

如果风控启动失败，事件会增加：

```text
risk_start_failed -> protective_exit -> event_closed(close_reason=unknown_failure)
```

这类失败不会被伪装成 `stop_loss`，也不会在正常退场已经开始后重复提交第二条 SELL 路径。
