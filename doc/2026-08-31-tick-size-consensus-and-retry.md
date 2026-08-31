# Tick size 三源校验与 invalid tick 重试设计

## 背景

天气 sweep 策略依赖 tick size 从 `0.01` 变为 `0.001` 后，以 `0.999` 卖出持仓。此前只依赖单一 HTTP 来源或 WS 通知，容易在服务端多个接口的数据短暂不一致时，使用已被服务端拒绝的旧 tick size 反复下单。

本设计将“确认 tick 已变化”和“invalid tick 后重试”拆开处理：

- 退出前必须用三个独立来源确认 tick size。
- 只有服务端明确返回 `invalid_tick_size` 时，才使用长间隔重试。
- 其他错误保持原有短退避和失败判定。

## 三源校验

正常退出触发后，以下三个来源必须同时可用且完全一致：

| 来源 | 字段 | 获取方式 |
| --- | --- | --- |
| Market WS | 内存中的最新 tick size | `OrderBookWS.get_tick_size(token_id)` |
| Tick API | `minimum_tick_size` | `GET /tick-size?token_id=...` |
| Book API | `tick_size` | `GET /book?token_id=...` |

对天气 sweep 的正常退出而言，三个值都必须是 `0.001`，随后才允许：

- 卖价：`1 - tick_size = 0.999`
- 下单参数：`tick_size = 0.001`

三个来源缺少任何一个，或任意两个值不一致，都视为校验失败。这里不做“少数服从多数”或时间戳判断，因为下单是否被接受最终由服务端决定；客户端只有在所有可见来源一致时才继续。

## 校验流程

### 1. WS 首次发现 tick 变化

WS 收到 `tick_size_change` 且值为 `0.001` 时，只作为候选触发。`TickVerifier` 会持续请求 `/tick-size` 和 `/book`，并要求三个来源一致且都为 `0.001`。

校验成功记录 `tick_verified`：

| 字段 | 含义 |
| --- | --- |
| `confirmed` | 是否三源一致且等于目标值 |
| `ws_tick_size` | Market WS 内存中的 tick size |
| `http_tick_size` | `/tick-size` 返回的 `minimum_tick_size` |
| `book_tick_size` | `/book` 返回的 `tick_size` |
| `source` | 固定为 `ws+tick-size+book` |
| `utc` | 事件记录的 UTC 时间 |

校验超时或始终不一致记录 `tick_verify_failed`，字段含义同上，另外用 `error` 说明失败原因。

### 2. SELL 前刷新

即使此前已经 `tick_verified`，真正进入正常卖出前仍会强制刷新三源。这样可以避免“验证时一致、下单时服务端已切换”的窗口。

如果刷新时三源不一致，记录 `tick_refresh_failed`：

| 字段 | 含义 |
| --- | --- |
| `error` | `tick_source_mismatch` 或具体请求错误 |
| `ws_tick_size` | Market WS 内存中的 tick size |
| `http_tick_size` | `/tick-size` 返回值 |
| `book_tick_size` | `/book` 返回值 |
| `action` | `stop_event` |
| `reason` | 风控退出场景下为 `stop_loss` |

三源不一致会停止该 event，标记为 `exit_failed`，不会继续盲试卖出。

### 3. 风控强制卖出

风控退出同样使用三源校验。其差异只在卖价和目的：风控以尽可能卖出为优先，但 tick size 仍必须先取得三源共识，避免使用服务端已拒绝的参数。

## invalid tick 专属重试

如果三源校验通过后，下单仍被服务端拒绝，且错误被归类为 `invalid_tick_size`，则按以下顺序处理：

1. 第一次 invalid tick 后等待 `120s`，重新三源校验，再重试 SELL。
2. 第二次 invalid tick 后等待 `300s`，重新三源校验，再重试 SELL。
3. 第三次 invalid tick 后等待 `600s`，重新三源校验，再重试 SELL。
4. 第四次仍是 invalid tick 时，停止重试。

每次等待后都会重新读取三个来源。若三者一致，则使用共识 tick 重新计算卖价；若三者不一致，则记录 `tick_refresh_failed` 并停止 event。

invalid tick 的长等待会延长该轮卖出的 deadline，保证第三次 `600s` 重试有机会真正执行。重试等待发生在两次下单尝试之间。

重试耗尽后：

- event 状态：`exit_failed`
- `failure_reason`：`sell_placement_failed`
- `stop_reason`：`invalid_tick_retry_exhausted`
- `manual_action_required`：`true`

## 不受影响的错误

长间隔重试只针对 `invalid_tick_size`。以下情况保持原有策略：

- `insufficient_balance`：首次等待 `3s`，作为结算等待后重试一次。
- 普通网络或 API 错误：继续使用原有短退避和 `SellFailureTracker` 的超时判定。
- 粉尘仓位、撤单失败、成交解析失败等：走各自既有失败路径，不进入 tick 长重试。

## 实现位置

- `backend/src/framework/strategy_runtime/tick_size_service.py`
  - 请求 `/tick-size` 与 `/book`
  - 执行三源共识，抛出 `TickSizeConsensusError`
- `backend/src/framework/strategy_runtime/tick_verifier.py`
  - 在 WS 值为 `0.001` 时持续验证三源
- `backend/src/strategy_weather_sweep/service.py`
  - 记录 `tick_verified`、`tick_verify_failed`、`tick_refresh_failed`
  - 实现 invalid tick 的 `120s / 300s / 600s` 重试
