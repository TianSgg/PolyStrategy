# strategy_follow_weather_sweeper 改造计划

> 目标服务：`backend/src/strategy_follow_weather_sweeper/`（端口 8005，Consul/Traefik/compose 骨架已就绪）
> 执行引擎来源：`backend/src/strategy_weather_sweep/`（PolyStrategy 现有扫单策略）
> 信号源参考：`WeatherTaker/backend/src/copy_trading/predexon.py`（Predexon WS 客户端）
> 状态：待实施

---

## 1. 背景与目标

`strategy_weather_sweep` 消费 `signal_weather_orderbook` 广播的订单簿扫单信号（检测 NO token ask 档被扫空），通过 `SweepTrade` 执行 BUY@0.99 → tick_size 变 0.001 → SELL@0.999 的完整交易生命周期，含风控（止损）、事件日志、trade 摘要落库。

本服务的目标是**复用 `strategy_weather_sweep` 的整套执行引擎**，仅将信号源替换为 **Predexon WS**（链上/mempool 监听指定 leader 钱包的 BUY NO），实现"跟随天气扫单者"策略。

核心原则：

1. **执行引擎原样复用**：`SweepTrade` / `SweepStrategy` / `OrderExecutor` / `TickSizeService` / `TickVerifier` / `RiskMonitor` / `EventLogger` / `OrderBookWS` —— 全部来自 `strategy_weather_sweep` 和 framework，不重写；
2. **只替换信号源**：Predexon WS 接收 leader 的 `order_filled` 事件 → 转为 `Signal` 对象 → 喂给 `SweepStrategy.on_signal()`；
3. **服务独立部署**：与 `strategy_weather_sweep` 完全独立，各有自己的配置表、事件表、trades 表，可对同一账户并存。

---

## 2. 架构对比

```
strategy_weather_sweep (现有):
  signal_weather_orderbook WS ──→ WeatherSweepAdapter ──→ Signal
      ↓
  InstancePool ──→ SweepStrategy.on_signal(signal)
      ↓
  SweepTrade.enter() → OrderExecutor → RiskMonitor → TickVerifier → SELL

strategy_follow_weather_sweeper (新):
  Predexon WS ──→ PredexonAdapter ──→ Signal
      ↓
  InstancePool ──→ FollowSweepStrategy.on_signal(signal)
      ↓
  SweepTrade.enter() → OrderExecutor → RiskMonitor → TickVerifier → SELL
```

从 `SweepTrade.enter()` 开始的执行路径完全相同。差异只在信号源和信号分发逻辑。

---

## 3. 信号源：Predexon WS

### 3.1 Predexon 客户端（移植自 WeatherTaker `copy_trading/predexon.py`）

`predexon.py` 连接 `wss://wss.predexon.com/`，订阅指定 leader 钱包地址的 pending mempool 订单事件：

```python
class PredexonClient:
    """Predexon Trades WS — 监听 leader 钱包链上活动"""
    
    async def start(self)              # 启动连接循环（指数退避重连）
    def stop(self)                     # 标记停止
    def add_leader(self, address)      # 动态添加 leader，update 已有订阅
    def remove_leader(self, address)   # 动态移除 leader
    
    # 收到 order_filled 事件后回调 on_signal(payload)
```

原始 Predexon 消息格式（`event_type == "order_filled"`）：

```python
{
    "user": "0x...",           # leader proxy wallet
    "side": "BUY",
    "outcome": "NO",
    "token_id": "12345...",
    "shares_normalized": 100,
    "price": 0.98,
    "tx_hash": "0x...",
    "role": "taker"
}
```

### 3.2 信号适配器 PredexonAdapter

将 Predexon payload 转为 framework 的 `Signal` 对象：

```python
class PredexonAdapter:
    def adapt(self, payload: dict) -> Optional[Signal]:
        # 过滤：只跟 BUY NO
        if payload["side"] != "BUY" or payload["outcome"] != "NO":
            return None
        return Signal(
            signal_id=f"predexon:{payload['tx_hash']}:{payload['token_id']}",
            signal_type="sweep",         # 复用 SweepStrategy 的类型判断
            token_id=payload["token_id"],
            market_slug="",              # 事后由 SweepTrade 的 event_logger 填充
            occurred_at_ms=int(time.time() * 1000),
            source="predexon",
            payload={
                "outcome": "no",
                "leader_wallet": payload["user"],
                "leader_price": payload["price"],
                "leader_size": payload["shares_normalized"],
                "tx_hash": payload["tx_hash"],
                "orderbook_snapshot": {},  # Predexon 无订单簿快照，risk monitor 从 WS 获取初始 BBO
            },
        )
```

### 3.3 SweepTrade 兼容性

`SweepTrade.enter()` 从 signal 中读取的字段：

| 字段 | 用途 | Predexon 信号中 |
|---|---|---|
| `signal.signal_id` | 去重 + event_logger | `predexon:{tx_hash}:{token_id}` |
| `signal.token_id` | 下单目标 | ✓ 直接映射 |
| `signal.market_slug` | event_logger 记录 | `""` 空（可接受，不影响执行） |
| `signal.payload["orderbook_snapshot"]` | risk.start() 初始 BBO | `{}` 空 → risk monitor 从 OrderBookWS 自行获取 |
| `signal.payload["event_slug"]` | event_logger 记录 | `None`（可接受） |
| `signal.payload["city"]` | event_logger 记录 | `None`（可接受） |
| `signal.payload["direction"]` | event_logger 记录 | `None`（可接受） |

结论：**`SweepTrade` 无需修改**，空字段不影响执行逻辑，仅影响日志中的元数据丰富度。

---

## 4. 策略类：FollowSweepStrategy

继承或变体 `SweepStrategy`，调整信号分发逻辑：

```python
class FollowSweepStrategy(SweepStrategy):
    """跟随策略实例：监听指定 leader 钱包，跟进 BUY NO"""
    
    EVENTS_TABLE = "strategy_follow_weather_sweeper_events"
    
    def __init__(self):
        super().__init__()
        self._leader_wallets: set[str] = set()
    
    @classmethod
    async def create(cls, config_data, orderbook_ws, ...):
        instance = await super().create(config_data, orderbook_ws, ...)
        instance._leader_wallets = set(config_data.get("leader_wallets", []))
        return instance
    
    def _should_accept_signal(self, signal: Signal) -> bool:
        """替换原有的天气信号过滤逻辑，改为 leader 钱包匹配"""
        payload = signal.payload
        leader = payload.get("leader_wallet", "").lower()
        if leader and leader not in self._leader_wallets:
            return False
        if payload.get("outcome", "") != "no":
            return False
        return True
```

关键差异：
- `_should_accept_signal()`：原版按 outcome/source/threshold/direction 过滤天气信号；新版按 leader_wallet 匹配
- `EVENTS_TABLE`：指向新服务的事件表
- `_trade_dao`：使用新服务的 trades 表

---

## 5. 配置模型

### 5.1 配置表

```sql
CREATE TABLE strategy_follow_weather_sweeper_configs (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_user_id       INT NOT NULL,
    account_id          INT NOT NULL,
    name                VARCHAR(64) NOT NULL,
    enabled             TINYINT(1) NOT NULL DEFAULT 0,
    -- 交易参数（与 strategy_weather_sweep 对齐）
    fixed_entry_shares  DECIMAL(18,2) NOT NULL DEFAULT 100.00,
    entry_wait_ms       INT NOT NULL DEFAULT 1200000,
    stop_loss_ratio     DECIMAL(5,2) NOT NULL DEFAULT 0.60,
    exit_wait_ms        INT NOT NULL DEFAULT 600000,
    -- leader 钱包列表（JSON 数组）
    leader_wallets      JSON NOT NULL DEFAULT '[]',
    -- 版本控制
    params_version      INT NOT NULL DEFAULT 1,
    created_at          DATETIME(3) NOT NULL,
    updated_at          DATETIME(3) NOT NULL,
    deleted_at          DATETIME(3) NULL,
    UNIQUE KEY uq_owner_name (owner_user_id, name),
    KEY idx_account (account_id)
);
```

### 5.2 事件表和 trades 表

结构与 `strategy_weather_sweep` 完全一致，仅表名替换：

```sql
-- 事件日志（EventLogger 写入）
CREATE TABLE strategy_follow_weather_sweeper_events (
    -- 与 strategy_weather_sweep_events 结构完全相同
    ...
);

-- 交易摘要（SweepTrade 写入）
CREATE TABLE strategy_follow_weather_sweeper_trades (
    -- 与 strategy_weather_sweep_trades 结构完全相同
    ...
);
```

### 5.3 与 strategy_weather_sweep 配置差异

| 字段 | strategy_weather_sweep | strategy_follow_weather_sweeper |
|---|---|---|
| `sweep_outcome_filter` | 有（no/yes/all） | 无（固定 BUY NO） |
| `signal_source_filter` | 有（main/next/all） | 无 |
| `signal_threshold_filter` | 有（0.99/0.98/all） | 无 |
| `direction_filter` | 有（highest/lowest/all） | 无 |
| `leader_wallets` | 无 | 有（JSON 数组） |

---

## 6. 服务组装（app.py）

与 `strategy_weather_sweep/app.py` 结构完全对齐，信号源从 SignalWSClient 换成 PredexonClient：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    set_client_provider(get_account_service())
    await orderbook_ws.start()
    await book_bbo_client.start()
    await pool.start()

    # 差异点：Predexon WS 替代 SignalWSClient
    predexon = PredexonClient()
    # 从 pool 中所有实例收集 leader_wallets，注册到 Predexon 订阅
    for strategy in pool.all_instances():
        for addr in strategy._leader_wallets:
            predexon.add_leader(addr)
    predexon_task = asyncio.create_task(
        predexon.start(on_signal=_dispatch_signal)
    )

    app.state.pool = pool
    app.state.predexon = predexon

    async with consul_lifespan(...):
        try:
            yield
        finally:
            predexon.stop()
            predexon_task.cancel()
            await pool.stop()
            await stop_all_user_ws()
            await orderbook_ws.stop()
            await book_bbo_client.close()
```

### 6.1 信号分发

```python
adapter = PredexonAdapter()

async def _dispatch_signal(payload: dict) -> None:
    signal = adapter.adapt(payload)
    if not signal:
        return
    for strategy in pool.all_instances():
        try:
            await strategy.on_signal(signal)
        except Exception:
            logger.exception("Strategy error on Predexon signal")
```

### 6.2 Leader 订阅动态更新

`pool.reload()` 后需要同步更新 Predexon 的 leader 订阅列表：

```python
@app.post("/internal/reload")
async def reload():
    result = await pool.reload()
    # 同步 leader 订阅
    predexon = request.app.state.predexon
    current_leaders = set()
    for strategy in pool.all_instances():
        current_leaders.update(strategy._leader_wallets)
    predexon.sync_leaders(current_leaders)
    return {"status": "ok", **result}
```

---

## 7. API 设计

前缀 `/api/follow-weather`，与 `strategy_weather_sweep` 的 `/api/strategy` 结构对齐：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/configs` | 配置列表 |
| POST | `/configs` | 创建（选账户 + leader 钱包列表 + buy_size） |
| GET | `/configs/{id}` | 详情 |
| PUT | `/configs/{id}` | 更新（enabled、buy_size、leader_wallets） |
| DELETE | `/configs/{id}` | 软删 |
| GET | `/events` | 事件摘要列表 |
| GET | `/events/{event_id}` | 单事件 step 详情 |
| GET | `/trades` | 交易摘要列表 |
| GET | `/trades/{event_id}` | 单条 trade 详情 |

鉴权：`framework/auth`（与 PolyStrategy 其他服务一致）。

---

## 8. 代码组织

微服务之间不互相 import（策略解耦），通用组件放 `framework/`。

### 8.1 framework 中已有的可复用组件

| 组件 | 路径 | 用途 |
|---|---|---|
| `OrderExecutor` | `framework/strategy_runtime/order_executor.py` | 下单、撤单、余额查询 |
| `TickSizeService` | `framework/strategy_runtime/tick_size_service.py` | tick_size 查询与共识 |
| `TickVerifier` | `framework/strategy_runtime/tick_verifier.py` | 三源 tick_size 校验 |
| `EventLogger` | `framework/strategy_runtime/event_logger.py` | 事件日志写入（table 参数化） |
| `StopLossMonitor` | `framework/strategy_runtime/stop_loss.py` | 基础止损风控 |
| `InstancePool` | `framework/instance_pool.py` | 配置驱动的实例管理 |
| `OrderBookWS` | `framework/orderbook_ws.py` | 订单簿 WS |
| `UserWS` | `framework/user_ws.py` | User Channel WS（成交/撤单事件） |
| `Signal` / `OrderResult` / `CancelResult` | `framework/strategy_runtime/interfaces.py` | 通用接口 |
| `LeaderBuyAdapter` | `framework/strategy_runtime/leader_adapter.py` | leader 信号适配器（已有） |

### 8.2 新服务的文件清单

| 文件 | 来源 | 说明 |
|---|---|---|
| `predexon.py` | WeatherTaker `copy_trading/predexon.py` 移植 | 去掉对 service 的直接引用，改为回调模式 |
| `service.py` | 从 `strategy_weather_sweep/service.py` **复制** | `SweepTrade`（2080 行）整体复制，交易执行逻辑不改；`SweepStrategy` 复制后改名 `FollowSweepStrategy`，修改 `_should_accept_signal()` 为 leader 钱包匹配，`EVENTS_TABLE` 改为新表名 |
| `internal/risk_monitor.py` | 从 `strategy_weather_sweep/internal/` **复制** | 原样复制，不改 |
| `internal/sell_failure.py` | 从 `strategy_weather_sweep/internal/` **复制** | 原样复制，不改 |
| `internal/clob_book_bbo.py` | 从 `strategy_weather_sweep/internal/` **复制** | 原样复制，不改 |
| `dao.py` | 从 `strategy_weather_sweep/dao.py` **复制** | 表名改为 `strategy_follow_weather_sweeper_*`，configs DAO 调整字段（去天气过滤，加 `leader_wallets`） |
| `api.py` | 从 `strategy_weather_sweep/api.py` **复制** | 路由前缀改为 `/api/follow-weather`，引用本服务 dao |
| `app.py` | 从 `strategy_weather_sweep/app.py` **复制** | 信号源从 `SignalWSClient` 换为 `PredexonClient`，其余结构不变 |
| `type.py` | 从 `strategy_weather_sweep/type.py` **复制** | 调整请求体字段（去天气过滤，加 `leader_wallets`） |
| `config.yml` | 已有 | — |

### 8.3 复制原则

- **交易执行逻辑原样复制**：`SweepTrade` 的入场 / 出场 / 风控退出 / force_exit 全套、`internal/` 下的 risk_monitor / sell_failure / clob_book_bbo 三个组件，直接从 `strategy_weather_sweep` 复制，不修改不简化
- **只改两处**：信号获取（Predexon WS 替代 SignalWSClient）和配置管理（`leader_wallets` 替代天气信号过滤字段）
- 两个策略的代码独立演进，后续如果执行逻辑分歧加大各自修改互不影响；如果趋同可再提取到 framework

---

## 9. 数据库迁移

迁移文件：`backend/migrations/incremental/007_follow_weather_sweeper.sql`

三张表：configs + events + trades，结构参照 `strategy_weather_sweep` 的同名表，仅：
- 表名前缀改为 `strategy_follow_weather_sweeper_`
- configs 表去掉天气信号特有的过滤字段（outcome_filter / source_filter / threshold_filter / direction_filter），新增 `leader_wallets JSON`

---

## 10. 前端计划

新增页面 `frontend/src/pages/FollowSweeper.tsx`：

**复用 SweepTrades 页面结构**：配置列表 + trades 列表 + 事件 timeline，数据走 `/api/follow-weather`。

**差异**：
- 配置编辑：新增 leader 钱包输入（多地址，逗号分隔或列表）
- 去掉天气信号过滤相关 UI（outcome/source/threshold/direction filter）
- 导航：策略分组下新增「跟随扫单者」，与 Weather Sweep 并列

---

## 11. 实施步骤

| 阶段 | 内容 | 验证 |
|---|---|---|
| P1 | DB 迁移 007（三张表） | SQL 执行成功 |
| P2 | `dao.py`（configs/events/trades DAO，改表名） | 冒烟测试 CRUD |
| P3 | `predexon.py`（Predexon WS 客户端移植） | 连接 + 消息解析日志 |
| P4 | `service.py`（FollowSweepStrategy，import SweepTrade，PredexonAdapter） | 单测：信号过滤、leader 匹配 |
| P5 | `api.py` + `app.py` lifespan 组装 | `./start.sh status` + curl `/health` + `/api/follow-weather/configs` |
| P6 | 前端 FollowSweeper 页面 | `npm run build` + 手工验证 |
| P7 | 联调：Predexon 信号→入场→tick 出场全链路 | 日志走查 + trades 表核对 |

---

## 12. 风险与注意事项

1. **Predexon API Key**：需配置 `PREDEXON_API_KEY` 环境变量，无 key 则跳过启动。
2. **orderbook_snapshot 为空**：`SweepTrade` 的 risk monitor 会从 OrderBookWS 获取初始 BBO，Predexon 信号没有快照不影响风控功能，但 risk_started 事件中不会有信号时刻的 BBO 参考线。
3. **market_slug 为空**：Predexon 信号只有 token_id，无 market 元数据。event_logger 记录时 market_slug / event_slug 为空。后续可异步查询 Gamma 补充（参考 WeatherTaker 的 `_fetch_and_cache_asset_question`）。
4. **与 strategy_weather_sweep 共用账户**：两服务互不知晓，可能同时入场同一 token；本期不做互斥。
5. **Predexon 信号延迟**：mempool 信号比链上确认快，但比内部订单簿信号慢（多一跳外部 WS）。入场时机可能略有差异。
6. **重启后 leader 订阅重建**：`pool.start()` 加载配置后需将所有 leader_wallets 注册到 Predexon；`pool.reload()` 需要 sync。
