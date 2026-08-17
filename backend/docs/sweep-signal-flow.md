# Sweep Signal Flow

WeatherOrderBookMonitor 检测 orderbook sweep 并触发 copy_trading 入场的完整链路。

---

## 架构概览

```
SharedMarketWebSocket
    │
    ▼ deliver(raw)
WeatherOrderBookMonitor (per candidate market)
    │  mode="full" only
    ▼ _publish("sweep", ...)
Coordinator._handle_event()
    │
    ▼ EventBus.publish("weather.sweep", payload)
CopyTradingService._consume_weather_sweep()
    │  outcome=="no" only
    ▼ _execute_weather_sweep(asset_id)
Place BUY order → SWEEP_PENDING → timeout/confirm
```

---

## 1. 数据入口：`monitor.deliver(raw)`

SharedMarketWebSocket 将原始 WS 消息路由到 monitor，支持三种格式：

| 格式 | 触发条件 | 处理方式 |
|------|----------|----------|
| JSON array | 连接/重连后的初始快照 | `apply_snapshot` 每个 asset |
| `event_type="book"` | 整本 L2 替换 | `apply_snapshot` 单个 asset |
| `event_type="price_change"` | 增量更新 | `apply_change` 逐条价位 |

每次 book 变更后调用 `_evaluate(asset_id, previous_asks, high_probability_asset, previous_tick)`。

---

## 2. Sweep 检测前置条件

`_evaluate` 中 4 个条件全部满足才进入 sweep 检查：

```python
if (
    previous_asks is not None              # 1. 非首次快照（有前值可对比）
    and asset_id == high_probability_asset  # 2. 变动的是高概率方
    and previous_tick == SWEEP_TICK_SIZE    # 3. 变更前 tick_size == 0.01
    and self._ticks[asset_id] == SWEEP_TICK_SIZE  # 4. 变更后 tick_size 仍为 0.01
):
```

### 条件说明

- **high_probability_asset**：取变更前 YES/NO 两侧的 `(best_ask + best_bid) / 2`，取较高者。两侧相等返回 None，不触发检测。
- **tick_size == 0.01**：tick_size=0.001 表示市场已进入终局（接近 resolve），此时 ask 消失是正常收敛而非 sweep。
- **previous_asks is not None**：首次快照没有前值，无法判断变化方向。

---

## 3. Sweep 判定逻辑

```python
SWEEP_ASK_THRESHOLDS = (0.99,)

for threshold in SWEEP_ASK_THRESHOLDS:
    before_exists = any(price <= threshold and size > 0 for price, size in previous_asks.items())
    after_exists  = any(price <= threshold and size > 0 for price, size in book.asks.items())
    if before_exists and not after_exists:
        # SWEEP!
```

含义：**之前存在 <=0.99 的 ask 挂单，更新后全部消失**（被吃光）。

阈值从高到低遍历，命中后 break（只触发最高阈值的信号）。

---

## 4. 信号发出

```python
# 日志（full 和 sweep_only 模式都会打印）
logger.info("[Monitor] SWEEP detected: %s %s %s outcome=%s threshold=%.2f tick=%s mode=%s", ...)

# 只有 full mode 才发布事件
if self.mode == "full":
    self._publish("sweep", asset_id, previous, current, "ask_levels_through_0.99_cleared")
```

`_publish` 构造 `WeatherEvent` 并通过 `asyncio.create_task` 回调 coordinator。

### WeatherEvent 结构

```python
@dataclass(frozen=True)
class WeatherEvent:
    event_type: "sweep" | "no_longer_possible" | "market_resolved"
    asset: WeatherAsset  # {asset_id, city, event_slug, market_slug, temperature_label, outcome}
    previous_orderbook: dict | None  # {best_bid, best_ask, observed_at, ...}
    current_orderbook: dict          # 同上
    reason: str                      # "ask_levels_through_0.99_cleared"
```

---

## 5. Coordinator 中转

```python
async def _handle_event(self, event: WeatherEvent):
    # 1. 日志
    logger.info("[Coordinator] event=%s city=%s asset=%s outcome=%s reason=%s", ...)

    # 2. 通知（Telegram 等）
    asyncio.create_task(self._on_event(event, main_ctx))

    # 3. 发布到 EventBus
    payload = event.payload()
    payload["main_monitor"] = main_ctx  # 当前 main monitor 的市场上下文
    self._event_bus.publish("weather.sweep", payload)
    logger.info("[Coordinator] EventBus published: weather.sweep")
```

### EventBus payload 示例

```json
{
    "event_type": "sweep",
    "asset": {
        "asset_id": "26769266...",
        "city": "Tokyo",
        "event_slug": "tokyo-temperature-2026-08-18",
        "market_slug": "tokyo-temperature-highest-27",
        "temperature_label": "27C",
        "outcome": "no"
    },
    "previous_orderbook": {"best_bid": {"price": "0.94", "size": "50"}, "best_ask": {"price": "0.99", "size": "100"}},
    "current_orderbook": {"best_bid": {"price": "0.95", "size": "30"}, "best_ask": null},
    "reason": "ask_levels_through_0.99_cleared",
    "main_monitor": {"market_slug": "...", "temperature_label": "...", "favored_outcome": "no"}
}
```

---

## 6. CopyTrade 消费

```python
# 订阅
self._weather_sweep_queue = event_bus.subscribe("weather.sweep")

# 消费循环
payload = await self._weather_sweep_queue.get()
outcome = payload["asset"]["outcome"]
if outcome == "no" and asset_id:
    await self._execute_weather_sweep(asset_id)
else:
    logger.debug("[WeatherSweep] Skipped: outcome=%s ...")
```

**只跟 outcome="no" 的信号。** YES 侧 sweep 丢弃。

策略逻辑：NO token 代表"不会达到该温度"，NO ask 被扫空说明有人大量买入 NO（看跌温度），系统跟随。

---

## 7. 入场执行 (`_execute_weather_sweep`)

对每个 enabled 且 `sweep_confirm_window_ms > 0` 的 config：

1. **跳过条件**：
   - 该 (config_id, asset_id) 已在 SWEEP_PENDING/ACTIVE 状态
   - follower 已有该 asset 持仓

2. **下单**：BUY `buy_size @ 0.99`，tick_size=0.01，neg_risk=True

3. **状态转移**：IDLE → SWEEP_PENDING

4. **启动确认窗口定时器**：`sweep_confirm_window_ms`（当前 1000ms）

---

## 8. 确认窗口机制

### 正常流程：leader 确认

如果在窗口内收到 leader 跟单信号（相同 asset）：
- 状态 SWEEP_PENDING → ACTIVE
- 取消定时器
- 保留持仓，正常跟单逻辑接管

### 超时流程：无确认

定时器到期，仍为 SWEEP_PENDING：
1. 记录 `_sweep_exited_orders[order_id] = (config_id, asset_id)`
2. 状态 → IDLE
3. Cancel 未成交的 buy order
4. Sell 已成交部分 @ 0.99

### 延迟成交补偿

WS trade confirmation 比实际成交慢 8-9s。如果 cancel 后 WS 才报告 fill：
- 在 Trade CONFIRMED handler 中检查 `order_id in self._sweep_exited_orders`
- 检测到延迟成交 → 自动 sell @ 0.99 清仓

---

## 9. Monitor Mode 说明

| Mode | 创建场景 | Sweep 检测 | Sweep 发布 | High-certainty |
|------|----------|-----------|-----------|----------------|
| `full` | 当前活跃候选（main monitor） | Yes | Yes | Yes (60s timer) |
| `sweep_only` | 下一温度候选（next monitor） | Yes (仅日志) | No | No |

---

## 10. 关键常量

| 常量 | 值 | 含义 |
|------|----|------|
| `SWEEP_ASK_THRESHOLDS` | (0.99,) | sweep 判定价格阈值 |
| `SWEEP_TICK_SIZE` | 0.01 | 允许 sweep 检测的 tick_size |
| `CONFIRM_SECONDS` | 60 | high-certainty 确认等待秒数 |
| `MAX_HIGH_CERTAINTY_TICK` | 0.001 | high-certainty 要求的 tick_size |
| `MIN_HIGH_CERTAINTY_ASK` | 0.999 | high-certainty 最低 best_ask |
| `MIN_HIGH_CERTAINTY_BID_WITH_ASK` | 0.995 | high-certainty 有 ask 时最低 bid |
| `sweep_confirm_window_ms` | 1000 (config) | 入场后等待 leader 确认的窗口 |

---

## 11. 时序图

```
Time ─────────────────────────────────────────────────────────►

WS:     [book update: asks<=0.99 cleared]
             │
Monitor:     ├─ _evaluate() → sweep detected
             ├─ log "[Monitor] SWEEP detected ..."
             ├─ _publish("sweep") ──────────┐
             │                              │
Coordinator: │                    _handle_event()
             │                              ├─ log "[Coordinator] event=sweep ..."
             │                              ├─ asyncio.task → Telegram notify
             │                              ├─ EventBus.publish("weather.sweep")
             │                              │
CopyTrade:   │                              ├─ _consume_weather_sweep()
             │                              │   └─ outcome=="no" ✓
             │                              │
             │                    _execute_weather_sweep()
             │                              ├─ BUY 8.00@0.99
             │                              ├─ state → SWEEP_PENDING
             │                              ├─ start timer (1000ms)
             │                              │
             │              ┌───────────────┤
             │              │  1000ms later  │
             │              ▼               │
             │     [No leader confirm]      │
             │              │               │
             │     _sweep_timeout()         │
             │              ├─ state → IDLE
             │              ├─ cancel buy order
             │              └─ sell filled @ 0.99
```

---

## 12. 日志 grep 参考

```bash
# sweep 检测
grep "\[Monitor\] SWEEP detected" app.log

# coordinator 转发
grep "\[Coordinator\] EventBus published: weather.sweep" app.log

# copy_trading 收到信号
grep "\[WeatherSweep\] Received" app.log

# 入场下单
grep "\[WeatherSweep\] BUY" app.log

# 状态变化
grep "\[WeatherState\]" app.log

# 超时退出
grep "\[SweepExit\]" app.log

# 延迟成交补偿
grep "\[SweepExit\] Delayed fill" app.log
```
