# Weather Sweep 策略详细设计

## 1. 策略目标

在 Polymarket 天气类市场中，当检测到某个 outcome 的订单簿出现"扫单"信号（大额买入导致价格快速上升）时，立即跟单买入。持仓后监控 tick size 变化（0.01 → 0.001），一旦确认 tick 变化即以最高价卖出获利。

**盈利逻辑**：扫单发生时价格约 0.95-0.99，我们以 0.99 买入；tick 从 0.01 缩小到 0.001 意味着市场认为该 outcome 极大概率发生（接近 1.0），此时以 0.999 卖出，赚取 ~0.009/份。

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────┐
│ 信号服务 (signal_weather_orderbook, port 8001)           │
│   监控订单簿 → 检测扫单 → 广播信号                        │
└──────────────────────┬──────────────────────────────────┘
                       │ WebSocket /ws/signals
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 策略服务 (strategy_weather_sweep, port 8003)             │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │ StrategyContainer                                │    │
│  │   ├── WS Client (连接信号服务)                    │    │
│  │   ├── InstanceManager (从 DB 加载配置)            │    │
│  │   │                                              │    │
│  │   ├── Instance 1 (config_id=5)                   │    │
│  │   │   ├── SweepStrategy                          │    │
│  │   │   ├── OrderExecutor → BalancePoller          │    │
│  │   │   ├── EventLogger                            │    │
│  │   │   └── StopLossMonitor + TickVerifier         │    │
│  │   │                                              │    │
│  │   └── Instance 2 (config_id=7)                   │    │
│  │       ├── SweepStrategy                          │    │
│  │       ├── OrderExecutor → BalancePoller (共享)    │    │
│  │       └── ...                                    │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  /health              — 健康检查                         │
│  /api/status          — 运行状态                         │
│  /internal/reload     — 热更新（主服务调用）              │
└─────────────────────────────────────────────────────────┘
                       │
                       │ place_order / cancel_order
                       ▼
┌─────────────────────────────────────────────────────────┐
│ Polymarket CLOB (clob.polymarket.com)                    │
└─────────────────────────────────────────────────────────┘
```

### 2.1 进程模型

- **一个进程，N 个策略实例**
- 所有实例共享同一个 WS 连接（减少信号延迟）
- 每个信号广播给所有实例，由实例自行决定是否处理
- 同一 proxy_wallet 的多个实例共享同一个 BalancePoller

### 2.2 微服务入口

```python
# strategy_weather_sweep/app.py
app = create_app(
    strategy_class=SweepStrategy,
    signal_sources=[SignalSourceConfig(url=WS_URL, adapter=WeatherSweepAdapter())],
    service_name="strategy_weather_sweep",
    strategy_type="weather_sweep",
    config_table="weather_sweep_configs",
    events_table="weather_sweep_events",
)
```

---

## 3. 策略状态机

```
                  sweep 信号
    ┌──────┐   ──────────────>   ┌───────────────┐
    │ idle │                     │ entry_working │
    └──────┘                     └───────┬───────┘
        ▲                               │
        │ (无持仓)                       │ 超时/成交
        │                               ▼
        │                        ┌──────────────┐
        │                        │ exit_working │
        │                        └──────┬───────┘
        │                               │
        │         tick verified         │
        │         或 stop_loss          │
        │                               ▼
        │                        ┌────────────────┐
        └────────────────────────│    closed      │
                                 └────────────────┘

    任意状态 ──── force_exit() ──→ closed
```

### 状态定义

| 状态 | 含义 | 接受的信号 |
|------|------|-----------|
| `idle` | 空闲，等待新信号 | sweep |
| `entry_working` | 已下买单，等待成交 | bbo_update |
| `exit_working` | 持仓中，等待 tick 变化卖出 | bbo_update |
| `risk_exiting` | 止损触发，正在卖出 | 无 |
| `closed` | 事件结束 | 无 |

---

## 4. 主要交易流程

### 4.1 开仓（Entry）

```
收到 sweep 信号 (signal_type == "sweep")
│
├── 检查状态 == idle 且未 draining
├── 从 BalancePoller 读取 available_cash（instant）
├── 计算 actual_shares = min(配置份额, 可用余额/0.99)
│
├── actual_shares <= 0 → buy_failed(no_cash) → closed
│
├── place_order(BUY, price=0.99, size=actual_shares)
│   ├── 结果 == filled → 记录持仓，启动风控
│   ├── 结果 == live → 挂单中，等待成交
│   └── 结果 == failed → buy_failed → closed
│
└── 启动 entry_timer（entry_wait_ms 后超时撤单）
```

### 4.2 等待成交超时

```
entry_timer 到期（默认 30 秒）
│
├── 撤销未成交的买单
├── 检查已成交份额
│   ├── 有持仓 → state = exit_working（继续等 tick）
│   └── 无持仓 → event_closed(timeout_no_fill)
```

### 4.3 监控 Tick 变化

```
收到 bbo_update 信号
│
├── 检查 tick_size 字段
├── tick_size == 0.001?
│   ├── YES → 调用 TickVerifier.verify(token_id)
│   │           ├── confirmed → _tick_exit()
│   │           └── not confirmed → 继续等待
│   └── NO → 继续等待
│
└── 同时 StopLossMonitor.check() 监控价格
    └── 价格跌到 entry_price * stop_loss_ratio → _risk_exit()
```

### 4.4 自然退出（Tick Exit）

```
tick 条件验证通过
│
├── sell_price = 1 - tick_size
│   (tick_size=0.001 → sell_price=0.999)
│   (tick_size=0.01  → sell_price=0.99)
│
├── place_order(SELL, price=sell_price, size=position_shares)
│   ├── filled → 记录卖出
│   └── live → 挂单中（event_closed 仍然触发）
│
└── event_closed(reason=tick_exit)
```

### 4.5 风控退出（Risk Exit）

```
StopLossMonitor 触发（当前价格 < 入场价 * 0.6）
│
├── place_order(SELL, price=0.01, size=position_shares)
│   └── 以极低价格市价卖出，确保成交
│
└── event_closed(reason=stop_loss)
```

### 4.6 强制退出（Force Exit）

```
容器调用 force_exit("config_disabled" / "config_changed")
│
├── 取消 entry_timer
├── 撤销未成交买单（如果有）
├── 已持份额 > 0?
│   ├── YES → place_order(SELL, price=1-tick_size, size=position)
│   └── NO → 直接关闭
│
└── event_closed(reason=force_exit)
```

---

## 5. 余额管理

### 5.1 BalancePoller

每个 proxy_wallet 一个全局单例，后台每 1 秒轮询：

```
Polymarket CLOB API
├── get_balance_allowance(COLLATERAL) → 总 USDC 余额
└── get_open_orders() → 所有挂单

计算:
  locked_usdc = sum(price * remaining for BUY orders)
  available_cash = balance - locked_usdc - safety_buffer(2 USDC)
```

### 5.2 下单流程中的余额检查

```python
# OrderExecutor.place_order()
if check_balance and side == "BUY":
    notional = price * size
    if poller.available_cash < notional:
        return OrderResult(status="insufficient_balance")  # 不发请求

# 实际下单...
result = clob.create_and_post_order(...)

if result.status == "failed":
    poller.refresh()  # 立即刷新缓存
```

### 5.3 启动时机

| 事件 | BalancePoller 状态 |
|------|-------------------|
| 配置创建（enabled=false） | 不启动 |
| 配置启用 | 启动轮询 |
| 配置禁用 | force_exit 后，如无其他实例用同一钱包则停止 |
| 策略进程启动 | 加载所有 enabled 配置，各自启动 poller |

---

## 6. 配置管理与热更新

### 6.1 配置表结构 (weather_sweep_configs)

```sql
CREATE TABLE weather_sweep_configs (
  id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  owner_user_id         INT NOT NULL,
  account_id            INT NOT NULL,           -- 关联钱包
  name                  VARCHAR(128) NOT NULL,
  enabled               TINYINT(1) DEFAULT 0,   -- 启用开关

  -- 策略参数
  fixed_entry_shares    DECIMAL(20,4) DEFAULT 100,   -- 固定买入份额
  entry_wait_ms         INT DEFAULT 30000,           -- 等待成交超时
  sweep_outcome_filter  VARCHAR(8) DEFAULT 'no',     -- 信号过滤
  stop_loss_ratio       DECIMAL(5,4) DEFAULT 0.6000, -- 止损比率
  exit_wait_ms          INT DEFAULT 5000,            -- 退出等待
  tick_verify_retries   SMALLINT DEFAULT 3,          -- tick 验证重试
  tick_verify_backoff_ms INT DEFAULT 1000,           -- tick 验证退避

  -- 版本控制
  params_version        SMALLINT DEFAULT 1,     -- 每次修改 +1
  deleted_at            DATETIME(3) NULL,
  created_at            DATETIME(3),
  updated_at            DATETIME(3),

  UNIQUE KEY (owner_user_id, name)
);
```

### 6.2 热更新机制

```
前端操作 → PUT /api/strategy/configs/:id
                     │
                     ▼
后端更新 DB（params_version + 1）
                     │
                     ▼
POST 策略服务 /internal/reload
                     │
                     ▼
StrategyContainer.reload()
  ├── 从 DB 加载所有 enabled 配置
  ├── 对比当前运行中的实例
  │
  ├── config 被禁用 → force_exit("config_disabled") → stop
  ├── params_version 变化 → force_exit("config_changed") → stop → start(new_cfg)
  └── 新 config → start(cfg)
```

### 6.3 强制退出时序

```
reload() 检测到变更
    │
    ▼
force_exit_and_stop(config_id, reason)
    ├── strategy.force_exit(reason)
    │     ├── 记录 force_exit step
    │     ├── cancel 买单
    │     ├── sell 持仓（如有）
    │     └── event_closed
    ├── strategy.stop()
    │     └── 释放定时器等资源
    └── log: "Instance X force-exited and stopped"
```

---

## 7. 事件记录系统

### 7.1 事件表结构 (weather_sweep_events)

```sql
CREATE TABLE weather_sweep_events (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  owner_user_id   INT NOT NULL,
  proxy_wallet    VARCHAR(128) NOT NULL,
  config_id       BIGINT UNSIGNED NOT NULL,
  config_snapshot JSON NULL,          -- 创建事件时的配置快照

  event_id        CHAR(36) NOT NULL,  -- UUID，一个事件多个 step
  signal_id       VARCHAR(512) NULL,
  token_id        VARCHAR(128) NULL,
  market_slug     VARCHAR(255) NULL,
  event_slug      VARCHAR(255) NULL,

  phase           ENUM('entry','monitor','exit','exit_risk','exit_force') NOT NULL,
  step            VARCHAR(64) NOT NULL,
  sequence_no     INT UNSIGNED NOT NULL,
  detail          JSON NOT NULL,
  occurred_at     DATETIME(3) NOT NULL,

  UNIQUE KEY (event_id, sequence_no),
  KEY idx_phase (phase),
  KEY idx_config_event (config_id, event_id),
  KEY idx_occurred (occurred_at DESC)
);
```

### 7.2 Phase 与 Step 完整映射

#### `entry` — 开仓阶段

| step | 触发时机 | detail 关键字段 |
|------|---------|----------------|
| signal_received | 收到合格 sweep 信号 | signal_id, token_id, city, direction |
| buy_placed | 买单提交成功 | order_id, price, size |
| buy_filled | 买单成交 | filled_size, total_position |
| buy_failed | 买单失败 | reason (no_cash/insufficient_balance/api_error) |
| entry_timeout | 超时撤单 | wait_ms, final_position |

#### `monitor` — 持仓监控阶段

| step | 触发时机 | detail 关键字段 |
|------|---------|----------------|
| risk_started | 风控监控启动 | entry_price, stop_loss_ratio |
| tick_detected | BBO 中检测到 tick=0.001 | tick_size, source |
| tick_verified | HTTP 验证 tick 确认 | token_id, confirmed |

#### `exit` — 自然退出

| step | 触发时机 | detail 关键字段 |
|------|---------|----------------|
| sell_placed | tick exit 卖单提交 | order_id, price, size, reason |
| sell_filled | 卖单成交 | filled_size, fill_price |
| event_closed | 事件结束 | reason=tick_exit, duration_ms |

#### `exit_risk` — 风控退出

| step | 触发时机 | detail 关键字段 |
|------|---------|----------------|
| stop_loss_triggered | 止损条件触发 | threshold, sell_price, sell_size |
| event_closed | 事件结束 | reason=stop_loss, duration_ms |

#### `exit_force` — 强制退出

| step | 触发时机 | detail 关键字段 |
|------|---------|----------------|
| force_exit | 容器调用强制退出 | reason (config_disabled/config_changed), state_at_exit |
| buy_cancelled | 撤销未成交买单 | order_id, success |
| force_sell_placed | 平仓卖单提交 | order_id, price, size, tick_size |
| force_sell_filled | 平仓卖单成交 | filled_size, fill_price |
| event_closed | 事件结束 | reason=force_exit, duration_ms |

### 7.3 EventLogger 用法

```python
# 策略内部使用
el = self.ctx.event_logger

el.start_event(signal_id=..., token_id=..., market_slug=..., event_slug=...)
el.log_step("signal_received", {...}, phase="entry")
el.log_step("buy_placed", {...}, phase="entry")
el.log_step("risk_started", {...}, phase="monitor")
el.log_step("sell_placed", {...}, phase="exit")
el.log_step("event_closed", {...}, phase="exit")
el.end_event()  # 重置，准备下一个事件
```

### 7.4 查询 API

```
GET /api/strategy/events              → 事件摘要列表（含 final_phase）
GET /api/strategy/events/:event_id    → 单个事件的所有 steps
```

---

## 8. 完整事件示例

### 8.1 正常退出

```json
[
  {"phase": "entry",   "step": "signal_received",  "detail": {"signal_id": "...", "city": "New York"}},
  {"phase": "entry",   "step": "buy_placed",       "detail": {"order_id": "abc", "price": "0.99", "size": "100"}},
  {"phase": "entry",   "step": "buy_filled",       "detail": {"filled_size": "100", "total_position": "100"}},
  {"phase": "monitor", "step": "risk_started",     "detail": {"entry_price": "0.99", "stop_loss_ratio": "0.60"}},
  {"phase": "monitor", "step": "tick_detected",    "detail": {"tick_size": "0.001"}},
  {"phase": "monitor", "step": "tick_verified",    "detail": {"confirmed": true}},
  {"phase": "exit",    "step": "sell_placed",      "detail": {"price": "0.999", "size": "100"}},
  {"phase": "exit",    "step": "sell_filled",      "detail": {"filled_size": "100", "fill_price": "0.999"}},
  {"phase": "exit",    "step": "event_closed",     "detail": {"reason": "tick_exit", "duration_ms": 45230}}
]
```

### 8.2 配置变更导致的强制退出

```json
[
  {"phase": "entry",      "step": "signal_received",   "detail": {"signal_id": "..."}},
  {"phase": "entry",      "step": "buy_placed",        "detail": {"order_id": "abc", "price": "0.99", "size": "100"}},
  {"phase": "entry",      "step": "buy_filled",        "detail": {"filled_size": "60"}},
  {"phase": "monitor",    "step": "risk_started",      "detail": {"entry_price": "0.99"}},
  {"phase": "exit_force", "step": "force_exit",        "detail": {"reason": "config_changed", "state_at_exit": "entry_working", "position_shares": "60"}},
  {"phase": "exit_force", "step": "buy_cancelled",     "detail": {"order_id": "abc", "success": true}},
  {"phase": "exit_force", "step": "force_sell_placed", "detail": {"order_id": "def", "price": "0.99", "size": "60", "tick_size": "0.01"}},
  {"phase": "exit_force", "step": "force_sell_filled", "detail": {"filled_size": "60", "fill_price": "0.99"}},
  {"phase": "exit_force", "step": "event_closed",      "detail": {"reason": "force_exit", "duration_ms": 12500}}
]
```

### 8.3 止损退出

```json
[
  {"phase": "entry",     "step": "signal_received",     "detail": {"signal_id": "..."}},
  {"phase": "entry",     "step": "buy_placed",          "detail": {"price": "0.99", "size": "100"}},
  {"phase": "entry",     "step": "buy_filled",          "detail": {"filled_size": "100"}},
  {"phase": "monitor",   "step": "risk_started",        "detail": {"entry_price": "0.99", "stop_loss_ratio": "0.60"}},
  {"phase": "exit_risk", "step": "stop_loss_triggered", "detail": {"threshold": "0.60", "sell_price": "0.01"}},
  {"phase": "exit_risk", "step": "event_closed",        "detail": {"reason": "stop_loss", "duration_ms": 180000}}
]
```

---

## 9. 配置参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| fixed_entry_shares | decimal | 100 | 每次买入的固定份额 |
| entry_wait_ms | int | 30000 | 买单等待成交的超时时间（ms） |
| sweep_outcome_filter | string | "no" | 只响应特定 outcome 的信号（"yes"/"no"/空） |
| stop_loss_ratio | decimal | 0.60 | 止损比率（当前价 < 入场价 * ratio 触发） |
| exit_wait_ms | int | 5000 | 卖单等待 |
| tick_verify_retries | int | 3 | tick 验证 HTTP 重试次数 |
| tick_verify_backoff_ms | int | 1000 | tick 验证重试间隔 |

---

## 10. 部署与运维

### 10.1 启动

```bash
# 策略服务
uvicorn strategy_weather_sweep.app:app --host 0.0.0.0 --port 8003
```

### 10.2 健康检查

```
GET /health → {"status": "ok", "strategy": "SweepStrategy", "instances": 3, ...}
```

### 10.3 热更新触发

```
POST /internal/reload → {"status": "ok", "stopped": [5], "started": [8], "reloaded": [7]}
```

### 10.4 日志关键字

| 日志 | 含义 |
|------|------|
| `Instance X started` | 新实例启动 |
| `Instance X force-exited and stopped` | 强制退出完成 |
| `Reload complete: {...}` | 热更新结果 |
| `Insufficient balance` | 余额不足未下单 |
| `Order failed` | CLOB 下单失败 |
