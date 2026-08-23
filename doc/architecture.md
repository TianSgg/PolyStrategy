# PolyStrategy 项目架构说明

## 项目概述

PolyStrategy 是一个 Polymarket 预测市场的自动化交易系统，采用微服务架构。系统由**信号源**产生交易信号，**策略服务**消费信号并执行交易，**网关**提供管理界面和 API。

---

## 系统全景

```
┌─────────────────────────────────────────────────────────────────────┐
│                          用户 / 前端                                  │
│                    (React + Vite, port 5173)                         │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ HTTP / WebSocket
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Gateway (main.py)                             │
│                    FastAPI 网关, port 8000                            │
│  路由: /api/account, /api/auth, /api/market, /api/pnl, /api/perf    │
│  WS:   /ws/market, /ws/performance, /ws/pnl                        │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ 信号源服务     │     │   策略服务         │     │  基础设施服务      │
│              │     │                  │     │                 │
│ weather:8001 │────▶│ sweep:8003       │     │ account         │
│ leader:8002  │────▶│ leader:8004      │     │ market          │
│              │     │ sweep_leader:8005│     │ performance     │
└──────────────┘     └──────────────────┘     │ pnl             │
                                              └─────────────────┘
```

---

## 进程模型

| 进程 | 端口 | 职责 |
|------|------|------|
| Gateway (`backend/main.py`) | 8000 | 前端 API 网关、管理界面、WS 推送 |
| 天气信号源 (`signal_weather_orderbook/app.py`) | 8001 | 监控 Polymarket 天气市场盘口，检测 sweep 事件 |
| Leader 信号源 (`signal_leader_activity/app.py`) | 8002 | 监控目标 leader 的链上 BUY 活动 |
| Sweep 策略 (`strategy_sweep/app.py`) | 8003 | 消费 sweep 信号，执行 BUY@0.99 → tick exit |
| Leader 策略 (`strategy_leader/app.py`) | 8004 | 消费 leader_buy 信号，执行跟单交易 |
| 组合策略 (`strategy_sweep_leader/app.py`) | 8005 | sweep 试探 + leader 确认追加 |

每个策略是独立进程，停一个不影响其他。

---

## 后端目录结构 (`backend/src/`)

### 核心框架层

```
strategy_runtime/           ← 策略容器（极简，不含业务逻辑）
├── interfaces.py           Signal, BaseStrategy, StrategyContext, OrderExecutorProtocol
├── container.py            StrategyContainer: 生命周期 + 信号分发
├── state_store.py          策略状态持久化（MySQL）
└── app_factory.py          create_app(): 标准化 FastAPI 应用工厂

toolkit/                    ← 可选工具箱（策略按需引入，可继承重写）
├── signals/
│   ├── ws_client.py        通用 WS 信号接收器（连接/重连/心跳/去重）
│   ├── dedup.py            TTL 去重器
│   └── adapters/           各信号源格式 → 通用 Signal 转换器
│       ├── weather_adapter.py
│       └── leader_adapter.py
├── execution/
│   ├── order_executor.py   Polymarket CLOB 下单（实现 OrderExecutorProtocol）
│   ├── account_ledger.py   内存资金/份额保留管理
│   └── order_tracker.py    订单生命周期幂等投影
├── risk/
│   └── stop_loss.py        BBO 止损风控
└── market/
    ├── market_data.py      价格/tick_size/neg_risk 查询
    ├── orderbook_ws.py     Polymarket 订单簿 WS 连接
    └── tick_verifier.py    tick=0.001 HTTP 校验
```

### 策略层

```
strategy_sweep/             ← 扫单策略
├── app.py                  ~45行：配置信号源 + create_app()
└── strategy.py             实现 start/on_signal/stop

strategy_leader/            ← 跟单策略
├── app.py
└── strategy.py

strategy_sweep_leader/      ← 组合策略
├── app.py
└── strategy.py
```

### 信号源层

```
signal_weather_orderbook/   ← 天气信号源微服务
├── app.py                  FastAPI 入口 (port 8001)
├── coordinator.py          多市场协调器
├── monitor.py              单个市场盘口监控
├── discovery.py            市场发现（Gamma API）
├── orderbook.py            订单簿解析
├── ws_hub.py               向策略广播信号的 WS Hub
└── types.py                WeatherSweepSignal 数据类型

signal_leader_activity/     ← Leader 信号源微服务
├── app.py                  FastAPI 入口 (port 8002)
├── service.py              Leader 活动检测逻辑
├── ws_hub.py               向策略广播信号的 WS Hub
└── types.py                LeaderBuySignal 数据类型
```

### 基础设施层

```
account/                    ← 账户管理（钱包、密钥、CLOB Client 池）
auth/                       ← 认证（JWT、用户管理）
shared/                     ← 公共工具（DB 连接池、日志、加密、余额查询、WS 管理）
market/                     ← Polymarket 市场数据服务（WS 订单簿、性能监控用）
performance/                ← 系统性能监控（延迟、内存状态快照）
pnl/                        ← PnL 定时采集与推送
```

---

## 信号流

```
Polymarket WS ──→ signal_weather_orderbook ──(检测 sweep)──→ WS Hub
                                                              │
                                                              │ WebSocket
                                                              ▼
                                               toolkit/signals/ws_client.py
                                                              │
                                                              │ adapt()
                                                              ▼
                                                     通用 Signal 对象
                                                              │
                                                              │ dispatch
                                                              ▼
                                              strategy_runtime/container.py
                                                              │
                                                              │ on_signal()
                                                              ▼
                                                    strategy.on_signal()
                                                              │
                                                              ▼
                                               toolkit/execution/order_executor
                                                              │
                                                              ▼
                                                     Polymarket CLOB API
```

---

## 核心接口

### Signal（通用信号格式）

```python
@dataclass(frozen=True)
class Signal:
    signal_id: str          # 全局唯一
    signal_type: str        # "sweep", "leader_buy", ...
    token_id: str           # Polymarket token
    market_slug: str        # 所属市场
    occurred_at_ms: int     # 信号产生时间
    source: str             # 信号源标识
    payload: dict           # 信号源特有数据
```

### BaseStrategy（策略接口）

```python
class BaseStrategy(ABC):
    SUBSCRIBED_SIGNALS: ClassVar[set[str]] = set()

    async def start(self, ctx: StrategyContext) -> None: ...
    async def on_signal(self, signal: Signal) -> None: ...
    async def stop(self) -> None: ...
```

### StrategyContext（工具入口）

```python
@dataclass
class StrategyContext:
    executor: OrderExecutorProtocol   # 下单/撤单
    state_store: StateStoreProtocol   # 状态持久化
    config: dict[str, Any]            # 策略参数
    proxy_wallet: str                 # 执行钱包
    run_id: str                       # 运行 ID
```

---

## 前端

| 技术栈 | React + TypeScript + Vite |
|--------|--------------------------|
| 状态管理 | Context API |
| 通信 | REST API + WebSocket（实时推送） |

### 主要页面

| 页面 | 功能 |
|------|------|
| WeatherMonitor | 天气信号源状态、订单簿实时监控 |
| StrategyDashboard | 策略运行状态、活跃订单 |
| CopyTrading | （遗留，待移除） |
| PnL | 损益曲线、持仓跟踪 |
| PerformanceMonitor | 系统延迟、内存状态快照 |
| Account | 账户/钱包管理 |
| UserManagement | 用户权限管理 |

---

## 部署

```
deploy/
├── bootstrap_prod.sh.example    # 首次部署初始化
├── deploy_prod.sh               # 生产部署脚本
├── deploy_test.sh               # 测试环境部署
├── nginx_prod.conf.example      # Nginx 反向代理配置
└── nginx_test.conf.example
```

生产环境每个服务独立 systemd unit，通过 Nginx 统一入口。

---

## 新增策略步骤

```bash
# 1. 创建策略包
mkdir backend/src/strategy_xxx/

# 2. 写两个文件
strategy_xxx/
├── app.py          # 配置信号源 + create_app()
└── strategy.py     # 实现 start/on_signal/stop

# 3. 如果是新信号源，写一个 adapter
toolkit/signals/adapters/xxx_adapter.py

# 4. 启动
python backend/src/strategy_xxx/app.py
```

不需要改 runtime、不需要改其他策略、不需要改信号源。

---

## 依赖关系

```
strategy_sweep ─────┐
strategy_leader ────┼──→ strategy_runtime (interfaces + container)
strategy_sweep_leader┘         │
                               ↓
                          toolkit/ (按需引入)
                          ├── execution/  ← 依赖 account/
                          ├── risk/
                          ├── market/
                          └── signals/    ← 连接 signal_* 微服务

signal_weather_orderbook ──(WS)──→ toolkit/signals/ws_client
signal_leader_activity ────(WS)──→ toolkit/signals/ws_client

Gateway (main.py) ──→ account/, auth/, market/, performance/, pnl/
```

---

## 外部依赖

| 服务 | 用途 |
|------|------|
| Polymarket CLOB API | 下单/撤单/查询 |
| Polymarket Market WS | 订单簿实时数据、tick_size 变更 |
| Polymarket Gamma API | 市场元数据查询 |
| MySQL | 持久化（账户、信号记录、策略状态） |

---

## 设计原则

1. **策略独立** — 每个策略是独立进程，互不影响
2. **信号与策略解耦** — 信号源只负责产出信号，不知道谁消费
3. **工具箱可选** — 策略按需使用 toolkit 组件，可继承重写
4. **容器极简** — strategy_runtime 只管生命周期和信号分发，不含业务逻辑
5. **扩展无侵入** — 新增策略/信号源不需要修改现有代码
