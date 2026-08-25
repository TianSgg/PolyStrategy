# PolyStrategy 微服务架构迁移计划

## 目标架构

```
客户端 → Traefik (8000) → ForwardAuth (auth-service) → 各微服务
                ↕                                          ↕
             Consul (8500) ←←←←←←←←←←←← 各服务启动时注册
```

## 服务拆分

| 服务 | 端口 | 来源模块 | 路由 |
|------|------|----------|------|
| auth-service | 8010 | `auth/` | `/api/auth/*`, `/auth/verify` (ForwardAuth) |
| account-service | 8011 | `account/`, `shared/balance.py` | `/api/account/*`, `/api/market/*` |
| strategy-config-service | 8012 | `strategy/`, `pnl/`, `performance/` | `/api/strategy/*`, `/api/pnl/*`, `/ws/*` |
| signal-weather | 8001 | `signal_weather_orderbook/` | 不变 |
| signal-leader | 8002 | `signal_leader_activity/` | 不变 |
| strategy-sweep | 8003 | `strategy_weather_sweep/` | 不变 |
| strategy-leader | 8004 | `strategy_leader/` | 不变 |
| strategy-sweep-leader | 8005 | `strategy_sweep_leader/` | 不变 |

## 部署方式

- **docker-compose.infra.yml** — Consul + Traefik（独立启动，开发/服务器通用）
- **docker-compose.services.yml** — 所有业务微服务容器化
- **MySQL** — 永远独立运行，不纳入 compose

---

## 实施步骤

### Step 1: 基础设施配置文件

创建 infra 目录结构：

```
infra/
├── docker-compose.infra.yml
├── traefik/
│   ├── traefik.yml          (静态配置)
│   └── dynamic/
│       └── middlewares.yml   (ForwardAuth 中间件)
└── consul/
    └── config.json           (Consul agent 配置)
```

- Consul: 单节点 server 模式，暴露 8500 (HTTP API + UI)
- Traefik: 监听 8000 (入口)，8080 (dashboard)，从 Consul Catalog 动态发现服务
- ForwardAuth 中间件指向 `http://auth-service:8010/auth/verify`

### Step 2: 共享库 — Consul 注册与发现

创建 `backend/src/shared/consul.py`：

- `ConsulRegistration` 类：服务启动注册、关闭注销、TTL 心跳
- `ConsulDiscovery` 类：从 Consul 查询健康服务实例
- `consul_lifespan()` 工厂函数：FastAPI lifespan 集成
- 环境变量：`CONSUL_HTTP_ADDR`, `SERVICE_NAME`, `SERVICE_PORT`, `SERVICE_ID`

### Step 3: 共享库 — Header 鉴权依赖

创建 `backend/src/shared/auth_headers.py`：

- `get_current_user_from_headers(request)` — 从 `X-User-Id`, `X-User-Role`, `X-Username` 读取用户身份
- 兼容模式：如果 header 不存在则 fallback 到 JWT 直接验证（过渡期）
- 替代原来各服务里的 `auth.dependencies.get_current_user`

### Step 4: 提取 auth-service

创建 `backend/src/auth_service/`：

```
auth_service/
├── __init__.py
├── app.py       (FastAPI app + Consul 注册)
└── forward_auth.py  (ForwardAuth 端点)
```

- 挂载现有 `auth/api.py` router（`/api/auth/*`）
- 新增 `GET /auth/verify` 端点：验证 JWT → 返回 200 + X-User-Id/X-User-Role/X-Username headers
- 启动时运行 `run_auth_migrations()`
- 注册到 Consul，Traefik tags: `PathPrefix('/api/auth')`

### Step 5: 提取 account-service

创建 `backend/src/account_service/`：

```
account_service/
├── __init__.py
└── app.py
```

- 挂载 `account/api.py` router
- 挂载 `market/api.py` router（如果有）
- 使用 `get_current_user_from_headers()` 替代直接 JWT 验证
- 注册到 Consul，tags 路由: `PathPrefix('/api/account')`, `PathPrefix('/api/market')`
- ForwardAuth 中间件保护

### Step 6: 提取 strategy-config-service

创建 `backend/src/strategy_config_service/`：

```
strategy_config_service/
├── __init__.py
└── app.py
```

- 挂载 `strategy/api.py`
- 挂载 `pnl/router.py`
- 挂载 `performance/router.py`
- WebSocket 端点：`/ws/market`, `/ws/performance`, `/ws/pnl`
- 使用 Consul 发现 strategy-sweep 等服务的地址（替代硬编码 URL）
- 注册到 Consul

### Step 7: 现有 signal/strategy 服务加 Consul 注册

修改以下服务的 `app.py`，添加 Consul 注册/注销：

- `signal_weather_orderbook/app.py`
- `signal_leader_activity/app.py`
- `strategy_weather_sweep/app.py`
- `strategy_leader/app.py`
- `strategy_sweep_leader/app.py`

每个服务添加 `/health` 端点（如果没有）+ lifespan 中 Consul 注册。

### Step 8: Docker 化

创建：

- `backend/Dockerfile` — 通用 Dockerfile，通过 `SERVICE_MODULE` ARG 决定启动哪个服务
- `backend/requirements.txt` — 统一依赖（如果还没有）
- `docker-compose.services.yml` — 编排所有业务服务
- `.dockerignore`

### Step 9: 前端适配

- 前端请求地址不变（仍然是 `/api/*`，Traefik 在 8000 端口）
- 如果开发时端口变化，更新 Vite 的 proxy 配置
- WebSocket 连接通过 Traefik 代理

### Step 10: 废弃旧 gateway + 清理

- 删除 `backend/main.py`（旧网关入口）
- 更新 `start.sh`：移除 gateway 条目，改为启动新的三个服务（或指向 docker-compose）
- 清理不再需要的代码

---

## 鉴权流程详解

```
1. 请求到达 Traefik
2. Traefik 路由匹配后，调用 ForwardAuth 中间件
3. ForwardAuth → GET http://auth-service:8010/auth/verify
   - 携带原始请求的所有 headers（含 Cookie / Authorization）
4. auth-service 验证 JWT:
   - 有效 → 200 + 响应 headers: X-User-Id, X-User-Role, X-Username
   - 无效 → 401
5. Traefik 收到 200 后，将 auth 响应 headers 注入到发往下游的请求中
6. 下游服务通过 X-User-Id 等 header 获取用户身份

不经过 ForwardAuth 的路由：
- POST /api/auth/login
- POST /api/auth/register
- 所有 /health 端点
```

## 服务间通信

- **同步 HTTP**: account-service ↔ strategy-config-service 通过 Consul 发现
- **WebSocket**: strategy 服务连接 signal 服务的 `/ws/signal`（内部通信，不经过 Traefik）
- **共享数据库**: 所有服务共享同一个 MySQL（当前规模下合理）

## 环境变量

```env
# Consul
CONSUL_HTTP_ADDR=http://localhost:8500
SERVICE_NAME=auth-service
SERVICE_PORT=8010
SERVICE_ID=auth-service-1

# MySQL (各服务共用)
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=xxx
MYSQL_DATABASE=polystrategy

# Auth
AUTH_JWT_SECRET=xxx
```
