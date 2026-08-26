# 微服务目录重构计划

## 目标

将当前扁平散落的模块结构，重构为每个微服务自包含、公共代码统一收入 `framework/` 的标准结构。

## 当前问题

```
backend/src/
  account/              ← 业务逻辑，被 account_service 引用
  account_service/      ← 微服务入口
  auth/                 ← 业务逻辑，被 auth_service 引用
  auth_service/         ← 微服务入口
  market/               ← 业务逻辑，被 account_service 引用
  performance/          ← 业务逻辑，被 strategy_config_service 引用
  pnl/                  ← 业务逻辑，被 strategy_config_service 引用
  strategy/             ← 业务逻辑，被 strategy_config_service 引用
  strategy_config_service/  ← 微服务入口
  signal_leader_activity/   ← 微服务（自包含）
  signal_weather_orderbook/ ← 微服务（自包含）
  strategy_leader/          ← 策略执行服务
  strategy_weather_sweep/   ← 策略执行服务
  strategy_sweep_leader/    ← 策略执行服务
  base_strategy/        ← 策略运行时框架
  shared/               ← 公共工具
```

问题：
1. 内部模块（account, auth, market, pnl, performance, strategy）和微服务平级，职责边界不清
2. `shared/` 混杂了不同功能：DB连接、Consul注册、加密工具、WebSocket工具等
3. `base_strategy/` 是框架代码，不是微服务，却放在同一层级
4. 服务内部缺少统一的命名规范（有的用 dao，有的用 repository，有的用 models）
5. 没有 per-service 配置文件，路由规则等写死在代码中

## 重构后目标结构

```
backend/src/
  framework/                         ← 公共框架代码（所有服务共享）
    db/
      __init__.py
      pool.py                        ← get_db_pool, get_db 连接池管理
      migrations.py                  ← 通用迁移工具
    consul/
      __init__.py
      registration.py                ← ConsulRegistration（注册/注销/心跳）
      discovery.py                   ← ConsulDiscovery（服务发现 + 缓存）
      lifespan.py                    ← consul_lifespan（FastAPI 集成）
    auth/
      __init__.py
      dependencies.py                ← AuthUser dataclass + get_current_user（从请求头读取 X-User-Id/X-User-Role/X-Username）
    logging/
      __init__.py
      config.py                      ← setup_logging, 统一日志格式配置
    time_utils.py                    ← 时间处理工具（多服务共用）
    balance.py                       ← CLOB 余额查询（account, signal_leader, pnl 共用）

  # 以下模块不放 framework，留在各自服务内部：
  # - crypto_utils → 只有 account_service 用 → account_service/crypto.py
  # - frontend_ws → 只有 strategy_config_service 用 → strategy_config_service/ws_frontend.py
  # - orderbook.py → 无人引用，死代码，删除
  # - jwt 签发/验证 → 只有 auth_service 用 → auth_service/jwt.py
    strategy_runtime/                ← base_strategy 整体移入（策略执行框架）
      __init__.py
      app_factory.py
      container.py
      event_logger.py
      instance_manager.py
      interfaces.py
      state_store.py
      toolkit/
        ...（保持原结构）

  auth_service/
      __init__.py
      app.py                         ← FastAPI 入口
      config.yml                     ← 服务配置（端口、路由规则、依赖等）
      api.py                         ← HTTP 路由
      service.py                     ← 业务逻辑
      dao.py                         ← 数据库操作
      migrations.py                  ← auth 表迁移
      forward_auth.py                ← ForwardAuth 端点
      types.py                       ← 数据结构

  account_service/
      __init__.py
      app.py
      config.yml
      api.py                         ← 账户 + 市场路由合并
      service.py                     ← 账户业务逻辑
      dao.py                         ← 账户 DB 操作
      market_api.py                  ← 市场相关路由
      market_service.py              ← 市场业务逻辑
      types.py

  strategy_config_service/
      __init__.py
      app.py
      config.yml
      strategy_api.py                ← 策略 CRUD 路由
      strategy_dao.py                ← 策略 DB 操作
      pnl_api.py                     ← PnL 路由
      pnl_service.py                 ← PnL 计算逻辑
      pnl_types.py                   ← PnL 数据结构
      performance_api.py             ← 绩效路由
      performance_service.py         ← 绩效计算逻辑
      types.py

  signal_leader/                     ← 重命名自 signal_leader_activity
      __init__.py
      app.py
      config.yml
      api.py
      service.py
      dao.py                         ← 重命名自 signal_repository.py
      types.py                       ← 统一存放 dataclass
      ws_hub.py
      protocol.py                    ← 只保留 WS 协议编解码逻辑（不含数据结构）

  signal_weather/                    ← 重命名自 signal_weather_orderbook
      __init__.py
      app.py
      config.yml
      ...（同上规范）

  strategy_leader/
      __init__.py
      app.py
      config.yml
      strategy.py

  strategy_weather_sweep/
      __init__.py
      app.py
      config.yml
      strategy.py

  strategy_sweep_leader/
      __init__.py
      app.py
      config.yml
      strategy.py
```

## config.yml 格式规范

每个服务目录下放一个 `config.yml`，`app.py` 启动时加载它，不再用 os.getenv 硬编码。

### 核心服务示例（auth_service/config.yml）

```yaml
service:
  name: auth-service
  port: 8010

consul:
  tags:
    - "traefik.enable=true"
    - "traefik.http.routers.auth.rule=PathPrefix(`/api/auth`)"
    - "traefik.http.routers.auth.entrypoints=web"
  health_path: /health

dependencies: []
```

### 信号服务示例（signal_leader/config.yml）

```yaml
service:
  name: signal-leader
  port: 8002

consul:
  tags:
    - "traefik.enable=true"
    - "traefik.http.routers.signal-leader.rule=PathPrefix(`/api/signals`)"
    - "traefik.http.routers.signal-leader.entrypoints=web"
    - "traefik.http.routers.signal-leader.middlewares=forward-auth@file"
  health_path: /health

dependencies: []
```

### 策略执行服务示例（strategy_leader/config.yml）

```yaml
service:
  name: strategy-leader
  port: 8004

consul:
  tags:
    - "traefik.enable=true"
    - "traefik.http.routers.strategy-leader.rule=PathPrefix(`/api/strategy-leader`)"
    - "traefik.http.routers.strategy-leader.entrypoints=web"
    - "traefik.http.routers.strategy-leader.middlewares=forward-auth@file"
  health_path: /health

dependencies:
  - signal-leader        # 通过 Consul 发现，不写死 ws://localhost:8002

strategy:
  initial_cash: 1000
  fixed_entry_shares: 100
  entry_size_mode: fixed
  entry_wait_ms: 30000
  stop_loss_ratio: 0.60
  proxy_wallet: ""       # 从环境变量覆盖（敏感信息不放 config.yml）
```

### 加载方式

`framework/` 提供一个 `config_loader.py`：

```python
import yaml
from pathlib import Path

def load_service_config(service_dir: str = None) -> dict:
    """加载当前服务的 config.yml"""
    if service_dir is None:
        service_dir = Path(__file__).parent
    config_path = Path(service_dir) / "config.yml"
    with open(config_path) as f:
        return yaml.safe_load(f)
```

各 `app.py` 启动时：
```python
from framework.config_loader import load_service_config
config = load_service_config(Path(__file__).parent)
```

### 覆盖优先级

`config.yml`（默认值）→ 环境变量覆盖（敏感信息如密钥、钱包地址）

敏感信息（私钥、DB密码、API Key）仍走 `.env.dev`，不放 config.yml 进 git。

## 文件命名规范

| 文件名 | 职责 |
|--------|------|
| `app.py` | FastAPI 入口、lifespan、中间件 |
| `config.yml` | 服务配置（端口、路由、依赖） |
| `api.py` | HTTP 路由定义 |
| `service.py` | 业务逻辑 |
| `dao.py` | 数据库读写操作 |
| `types.py` | dataclass / Pydantic model / 数据结构 |
| `ws_hub.py` | WebSocket 连接管理（如有） |
| `protocol.py` | 协议编解码（如有），不含数据结构 |
| `migrations.py` | 本服务的数据库迁移（如有） |

## 执行步骤

### Step 1 — 创建 framework 骨架

创建 `framework/` 目录，将 `shared/` 按功能拆分到子目录中：
- `shared/db.py` → `framework/db/__init__.py`
- `shared/consul.py` → `framework/consul/__init__.py`
- `shared/auth_headers.py` → `framework/auth/__init__.py`
- `shared/logging_config.py` → `framework/logging/__init__.py`
- `shared/crypto_utils.py` → `framework/crypto/__init__.py`
- `shared/frontend_ws.py` → `framework/websocket/__init__.py`
- `shared/orderbook.py` + `shared/time_utils.py` → `framework/market/__init__.py`
- `shared/balance.py` → `framework/market/balance.py`
- `base_strategy/` 整体移入 `framework/strategy_runtime/`

### Step 2 — 重组 auth_service

将 `auth/` 内的文件合并到 `services/auth_service/` 内：
- `auth/api.py` → `services/auth_service/api.py`
- `auth/service.py` → `services/auth_service/service.py`
- `auth/dependencies.py` → `services/auth_service/dependencies.py`（或合并到 service）
- `auth/migrations.py` → `services/auth_service/migrations.py`
- `auth_service/forward_auth.py` → `services/auth_service/forward_auth.py`
- 新增 `services/auth_service/config.yml`

### Step 3 — 重组 account_service

将 `account/` 和 `market/` 合并到 `services/account_service/`：
- `account/api.py` → `services/account_service/api.py`
- `account/service.py` → `services/account_service/service.py`
- `account/dao.py` → `services/account_service/dao.py`
- `market/api.py` → `services/account_service/market_api.py`
- `market/service.py` → `services/account_service/market_service.py`
- 新增 `services/account_service/config.yml`

### Step 4 — 重组 strategy_config_service

将 `strategy/`、`pnl/`、`performance/` 合并到 `services/strategy_config_service/`：
- `strategy/api.py` → `services/strategy_config_service/strategy_api.py`
- `strategy/dao.py` → `services/strategy_config_service/strategy_dao.py`
- `pnl/router.py` → `services/strategy_config_service/pnl_api.py`
- `pnl/service.py` → `services/strategy_config_service/pnl_service.py`
- `pnl/models.py` → `services/strategy_config_service/pnl_types.py`
- `performance/router.py` → `services/strategy_config_service/performance_api.py`
- `performance/service.py` → `services/strategy_config_service/performance_service.py`
- 新增 `services/strategy_config_service/config.yml`

### Step 5 — 重组 signal 服务

**signal_leader_activity → services/signal_leader/**
- `signal_repository.py` → `dao.py`
- `protocol.py` 中的 dataclass 移到 `types.py`，protocol.py 只保留编解码函数
- 新增 `config.yml`

**signal_weather_orderbook → services/signal_weather/**
- 同样规范化命名
- 新增 `config.yml`

### Step 6 — 重组策略执行服务

- `strategy_leader/` → `services/strategy_leader/`
- `strategy_weather_sweep/` → `services/strategy_weather_sweep/`
- `strategy_sweep_leader/` → `services/strategy_sweep_leader/`
- 每个加 `config.yml`
- import 路径从 `base_strategy` 改为 `framework.strategy_runtime`

### Step 7 — 全局 import 路径修复

所有服务中：
- `from shared.xxx` → `from framework.xxx`
- `from base_strategy.xxx` → `from framework.strategy_runtime.xxx`
- `from auth.xxx` → 直接使用本地文件（同包内 import）
- `from account.xxx` → 直接使用本地文件

### Step 8 — 更新启动脚本和配置

- `start.sh` 中模块路径更新为 `services.auth_service.app` 格式
- `docker-compose.services.yml` 对应更新
- `Dockerfile` 中 PYTHONPATH 调整

### Step 9 — 删除旧目录

确认所有 import 修复完成后，删除：
- `backend/src/shared/`
- `backend/src/auth/`
- `backend/src/account/`
- `backend/src/market/`
- `backend/src/performance/`
- `backend/src/pnl/`
- `backend/src/strategy/`
- `backend/src/base_strategy/`
- `backend/src/auth_service/`（已移入 services/）
- `backend/src/account_service/`（已移入 services/）
- 等等

### Step 10 — 验证

- 逐个服务 `python -c "import services.xxx.app"` 验证导入
- 启动 infra + 全部服务，验证 Consul 注册
- 通过 Traefik 访问各 API 端点

## 风险点

1. **import 路径变更面广**：每个文件几乎都要改 import，需要逐服务验证
2. **base_strategy 被多个策略服务引用**：移入 framework 后要确保 PYTHONPATH 能找到
3. **数据库迁移**：`auth/migrations.py` 移入 auth_service 后运行路径要正确
4. **前端代理无影响**：前端只经过 Traefik，路由规则不变

## 预计每步提交一次 Git
