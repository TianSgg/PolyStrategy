# PolyStrategy

Polymarket 天气市场量化策略平台。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  Frontend (Vite :5173 / Nginx :9097)                        │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP
┌───────────────────────────▼─────────────────────────────────┐
│  Traefik (反向代理)         :8000                            │
│    /api/auth           → auth-service                       │
│    /api/account        → account-service                     │
│    /api/weather        → signal-weather-orderbook            │
│    /api/strategy       → strategy-weather-sweep              │
│    /api/follow-weather → strategy-follow-weather-sweeper     │
└───┬───────────┬──────────────┬──────────┬─────────┬─────────┘
    │           │              │          │         │
    ▼           ▼              ▼          ▼         ▼
auth:8010  account:8011  signal:8001  strategy:8003  follow:8005
                               │ WebSocket        │
                               └───────┐          │
                                       ▼          │
                               Polymarket API ◄───┘
```

| 服务 | 模块 | 端口 | 说明 |
|------|------|------|------|
| auth-service | `auth_service.app` | 8010 | JWT 登录/鉴权 |
| account-service | `account_service.app` | 8011 | 账户管理、余额、持仓 |
| signal-weather-orderbook | `signal_weather_orderbook.app` | 8001 | 天气市场订单簿信号采集 |
| strategy-weather-sweep | `strategy_weather_sweep.app` | 8003 | Weather Sweep 自动交易策略 |
| strategy-follow-weather-sweeper | `strategy_follow_weather_sweeper.app` | 8005 | 跟随天气扫单者策略（骨架） |
| frontend | Vite + React / Nginx | 5173 (dev) / 9097 (docker) | 前端 SPA |

## 快速开始

### 环境要求

- Python 3.9+
- Node.js 18+
- MySQL 8.0+
- Docker & Docker Compose (生产部署)

### 本地开发

```bash
# 1. 初始化数据库
mysql -u root -p < backend/migrations/schema.sql

# 2. 配置环境变量
cp backend/.env.example backend/.env
# 编辑 backend/.env 填入实际值

# 3. 安装后端依赖
cd backend
pip install -r requirements.txt
pip install -e vendor/py-clob-client-v2
cd ..

# 4. 安装前端依赖
cd frontend
npm install
cd ..

# 5. 启动所有服务
./start.sh

# 查看状态
./start.sh status

# 停止
./start.sh stop
```

启动后访问 http://localhost:5173（本地开发）或 http://localhost:9097（Docker 部署）

### start.sh 启动顺序

1. Core services: auth, account
2. Signal services: signal_weather_orderbook
3. Strategy services: strategy_weather_sweep
4. Frontend: Vite dev server

日志在 `logs/<service_name>/` 目录下。

## Docker 部署

项目使用两个 compose 文件，分属不同仓库：

| 文件 | 仓库 | 作用 |
|------|------|------|
| `docker-compose.infra.yml` | PolyInfra | Consul + Traefik 基础设施 |
| `docker-compose.services.yml` | PolyStrategy | 5 个后端服务 + 前端 |

### 服务名称一览

| compose 服务名 | 容器名 | 端口 | 说明 |
|---------------|--------|------|------|
| `auth-service` | `polystrategy-auth` | 8010 | JWT 鉴权 |
| `account-service` | `polystrategy-account` | 8011 | 账户管理 |
| `signal-weather-orderbook` | `polystrategy-signal-weather-orderbook` | 8001 | 信号采集 |
| `strategy-weather-sweep` | `polystrategy-strategy-weather-sweep` | 8003 | 交易策略 |
| `strategy-follow-weather-sweeper` | `polystrategy-strategy-follow-weather-sweeper` | 8005 | 跟随扫单策略 |
| `frontend` | `polystrategy-frontend` | 9097 | Nginx 静态服务 + API 反代 |

### 前置: 基础设施 (PolyInfra)

```bash
cd /path/to/PolyInfra

# 启动 Consul + Traefik
docker compose -f docker-compose.infra.yml up -d

# 停止基础设施
docker compose -f docker-compose.infra.yml down
```

- Consul UI: http://localhost:8500
- Traefik Dashboard: http://localhost:8080

### 首次配置

```bash
cd /path/to/PolyStrategy

# 1. 配置环境变量
cp backend/.env.example backend/.env
# 编辑填入 MYSQL_PASSWORD, AUTH_JWT_SECRET, ENCRYPTION_KEY, CONSUL_HTTP_TOKEN 等

# 2. 创建外部网络（只需执行一次）
docker network create polystrategy

# 3. 初始化数据库
mysql -u root -p < backend/migrations/schema.sql
```

### 启动

```bash
# 启动全部服务（后台运行，首次会自动构建镜像）
docker compose -f docker-compose.services.yml up -d --build

# 启动全部服务（不重新构建）
docker compose -f docker-compose.services.yml up -d

# 启动单个服务
docker compose -f docker-compose.services.yml up -d auth-service
docker compose -f docker-compose.services.yml up -d account-service
docker compose -f docker-compose.services.yml up -d signal-weather-orderbook
docker compose -f docker-compose.services.yml up -d strategy-weather-sweep
docker compose -f docker-compose.services.yml up -d strategy-follow-weather-sweeper
docker compose -f docker-compose.services.yml up -d frontend
```

跟随天气扫单服务的单独启动命令如下。首次启动或代码更新后使用 `--build`：

```bash
docker compose -f docker-compose.services.yml up -d --build \
  auth-service account-service strategy-follow-weather-sweeper
```

如果鉴权和账户服务已经在运行，只启动策略服务：

```bash
docker compose -f docker-compose.services.yml up -d --build \
  strategy-follow-weather-sweeper
```

服务默认监听 `8005`，健康检查和日志命令：

```bash
curl http://127.0.0.1:8005/health
docker compose -f docker-compose.services.yml logs -f \
  strategy-follow-weather-sweeper
```

### 停止

```bash
# 停止全部服务
docker compose -f docker-compose.services.yml down

# 停止单个服务
docker compose -f docker-compose.services.yml stop frontend
docker compose -f docker-compose.services.yml stop strategy-weather-sweep
docker compose -f docker-compose.services.yml stop strategy-follow-weather-sweeper
docker compose -f docker-compose.services.yml stop signal-weather-orderbook
docker compose -f docker-compose.services.yml stop account-service
docker compose -f docker-compose.services.yml stop auth-service
```

### 重启

```bash
# 重启全部
docker compose -f docker-compose.services.yml restart

# 重启单个服务
docker compose -f docker-compose.services.yml restart strategy-weather-sweep
docker compose -f docker-compose.services.yml restart strategy-follow-weather-sweeper
docker compose -f docker-compose.services.yml restart signal-weather-orderbook
```

### 重新构建并启动

```bash
# 重新构建全部（代码更新后）
docker compose -f docker-compose.services.yml up -d --build

# 只重新构建某个服务
docker compose -f docker-compose.services.yml up -d --build strategy-weather-sweep
docker compose -f docker-compose.services.yml up -d --build signal-weather-orderbook
docker compose -f docker-compose.services.yml up -d --build strategy-follow-weather-sweeper
docker compose -f docker-compose.services.yml up -d --build frontend
```

### 查看状态与日志

```bash
# 查看所有服务状态
docker compose -f docker-compose.services.yml ps

# 查看全部日志（实时跟踪）
docker compose -f docker-compose.services.yml logs -f

# 查看单个服务日志
docker compose -f docker-compose.services.yml logs -f strategy-weather-sweep
docker compose -f docker-compose.services.yml logs -f strategy-follow-weather-sweeper
docker compose -f docker-compose.services.yml logs -f signal-weather-orderbook

# 查看最近 100 行日志
docker compose -f docker-compose.services.yml logs --tail=100 strategy-weather-sweep
```

### 进入容器调试

```bash
docker exec -it polystrategy-strategy-weather-sweep bash
docker exec -it polystrategy-strategy-follow-weather-sweeper bash
docker exec -it polystrategy-signal-weather-orderbook bash
docker exec -it polystrategy-auth bash
docker exec -it polystrategy-account bash
docker exec -it polystrategy-frontend sh    # alpine 镜像无 bash
```

### 清理

```bash
# 停止并删除容器、网络
docker compose -f docker-compose.services.yml down

# 停止并删除容器、网络、镜像（完全清理）
docker compose -f docker-compose.services.yml down --rmi all

# 停止并删除容器、网络、数据卷
docker compose -f docker-compose.services.yml down -v
```

### 环境变量

服务从 `backend/.env` 加载环境变量，compose 中的 `environment` 字段可覆盖：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MYSQL_HOST` | `host.docker.internal` | MySQL 地址 |
| `MYSQL_PORT` | `3306` | MySQL 端口 |
| `MYSQL_USER` | `root` | MySQL 用户 |
| `MYSQL_PASSWORD` | (必填) | MySQL 密码 |
| `MYSQL_DATABASE` | `polystrategy` | 数据库名 |
| `AUTH_JWT_SECRET` | (必填) | JWT 签名密钥 |
| `ENCRYPTION_KEY` | (必填) | 账户私钥加密密钥 |
| `CONSUL_HTTP_ADDR` | `http://host.docker.internal:8500` | Consul 地址 |
| `CONSUL_HTTP_TOKEN` | (可选) | Consul ACL Token |
| `FRONTEND_PORT` | `9097` | 前端 Nginx 监听端口 |
| `TRAEFIK_ENTRY_PORT` | `8000` | Traefik 入口端口（前端 API 反代目标） |

### Docker 网络

所有服务使用 `network_mode: host` 直接共享宿主机网络。PolyInfra 的 Consul/Traefik 同样使用 host 网络模式，服务容器通过 `host.docker.internal` 访问宿主机上的 Consul 和 MySQL。后端服务启动后自动注册到 Consul，由 Traefik 发现并路由。前端 Nginx 容器将 `/api/`、`/ws/`、`/auth/` 请求反代到 Traefik 网关，其余请求返回 SPA 静态文件。

## 项目结构

```
PolyStrategy/
├── backend/
│   ├── src/
│   │   ├── auth_service/          # 登录鉴权
│   │   ├── account_service/       # 账户管理
│   │   ├── signal_weather_orderbook/  # 天气信号采集
│   │   ├── strategy_weather_sweep/    # Sweep 交易策略
│   │   ├── framework/             # 共享框架
│   │   │   ├── auth/              # JWT 鉴权中间件
│   │   │   ├── consul/            # 服务注册
│   │   │   ├── db/                # MySQL 连接池
│   │   │   ├── logging/           # 统一日志
│   │   │   └── strategy_runtime/  # 策略运行时框架
│   │   └── shared/                # 共享工具
│   ├── migrations/schema.sql      # 全量建表 DDL
│   ├── vendor/                    # 本地依赖 (py-clob-client-v2)
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/                 # 页面组件
│   │   └── App.tsx                # 路由入口
│   ├── Dockerfile                 # 两阶段构建: node build → nginx
│   ├── nginx.conf                 # Nginx 反代 + SPA fallback
│   └── package.json
├── docker-compose.services.yml    # 后端服务编排
├── start.sh                       # 本地开发启动脚本
└── logs/                          # 运行时日志
```

## Consul 服务注册

每个服务启动时自动注册到 Consul，名称格式 `polystrategy-{service-name}`：

| Consul 服务名 | Traefik 路由 |
|---------------|-------------|
| `polystrategy-auth` | `/api/auth` |
| `polystrategy-account` | `/api/account` |
| `polystrategy-signal-weather-orderbook` | `/api/weather` |
| `polystrategy-strategy-weather-sweep` | `/api/strategy` |
| `polystrategy-strategy-follow-weather-sweeper` | `/api/follow-weather` |

## 数据库

MySQL 表结构在 `backend/migrations/schema.sql`（全量 DDL，含种子数据）。

主要表：

| 表 | 所属服务 | 说明 |
|----|---------|------|
| `users` | auth | 登录用户 |
| `accounts` | account | Polymarket 账户 |
| `weather_cities` | signal | 天气城市监听配置 |
| `weather_orderbook_signals` | signal | 信号记录 |
| `strategy_weather_sweep_configs` | strategy | 策略实例配置 |
| `strategy_weather_sweep_events` | strategy | 交易执行事件日志 |
| `strategy_account_ledger` | strategy | 资金账本 |
