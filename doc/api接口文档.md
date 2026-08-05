# weathertaker 接口文档

> 根据当前代码整理：`backend/main.py`、`backend/src/*/api.py`、`backend/src/performance/router.py`、`frontend/src/api.ts`。最后更新：2026-05-16。

## 1. 基础约定

### 1.1 Base URL

- 本地后端默认：`http://localhost:8000`
- 前端调用 REST 时使用 `frontend/src/api.ts` 中的 `API_BASE`：
  - `VITE_API_BASE` 有值时使用该值；
  - 否则使用同源相对路径，例如 `/api/auth/me`。
- 前端调用 WebSocket 时使用 `WS_BASE`：
  - `VITE_WS_BASE` 有值时使用该值；
  - 否则从当前页面 origin 推导 `ws://host` 或 `wss://host`。

### 1.2 鉴权

REST 和 WebSocket 都使用登录后写入的 HttpOnly Cookie。

- 登录接口：`POST /api/auth/login`
- 登出接口：`POST /api/auth/logout`
- 前端 `apiFetch()` 固定带 `credentials: "include"`。
- 未登录或会话失效时，受保护 REST 接口通常返回 `401`。
- 普通用户只能访问自己名下的数据；管理员可以访问全量数据。
- 管理员接口需要 `role = "admin"`。

### 1.3 通用错误格式

FastAPI 默认错误形状：

```json
{
  "detail": "错误信息"
}
```

常见状态码：

- `400`：参数错误、业务校验失败
- `401`：未登录或登录失败
- `404`：资源不存在，或当前用户无权访问该资源
- `409`：重复创建等冲突
- `500`：外部服务或数据库异常

### 1.4 时间格式

业务接口返回的 `created_at`、`updated_at`、`generated_at` 等时间字段当前整理为 UTC+8 可读字符串。调用方不要按毫秒时间戳解析。

## 2. Auth 用户与会话

### POST `/api/auth/login`

登录并设置认证 Cookie。

权限：公开

请求：

```json
{
  "username": "admin",
  "password": "password"
}
```

成功响应：

```json
{
  "user": {
    "id": 1,
    "username": "admin",
    "role": "admin",
    "enabled": true
  }
}
```

错误：

- `401 Invalid username or password`

### POST `/api/auth/logout`

清除认证 Cookie。

权限：公开

成功响应：

```json
{
  "status": "ok"
}
```

### GET `/api/auth/me`

获取当前登录用户。

权限：已登录

成功响应：

```json
{
  "user": {
    "id": 1,
    "username": "admin",
    "role": "admin",
    "enabled": true
  }
}
```

### GET `/api/auth/users`

列出系统用户。

权限：管理员

成功响应：

```json
{
  "users": [
    {
      "id": 1,
      "username": "admin",
      "role": "admin",
      "enabled": true,
      "created_at": "2026-05-16 12:00:00.000",
      "updated_at": "2026-05-16 12:00:00.000"
    }
  ]
}
```

### POST `/api/auth/users`

创建用户。

权限：管理员

请求：

```json
{
  "username": "trader",
  "password": "123456",
  "role": "user"
}
```

字段说明：

- `role`：`admin` 或 `user`；默认 `user`
- `password`：至少 6 位

成功响应：

```json
{
  "user_id": 2
}
```

### PUT `/api/auth/users/{user_id}`

更新用户状态或密码。

权限：管理员

请求：

```json
{
  "enabled": true,
  "password": "new-password"
}
```

字段说明：

- `enabled`、`password` 至少传一个
- 当前管理员不能禁用自己
- `password` 至少 6 位

成功响应：

```json
{
  "status": "ok"
}
```

## 3. Account 跟单账户

### POST `/api/account/add`

添加 follower 账户。后端会通过私钥派生地址、获取 Polymarket API 凭证并保存加密信息。

权限：已登录

请求：

```json
{
  "private_key": "0x...",
  "builder_code": "optional-builder-code"
}
```

字段说明：

- `private_key`：必填
- `builder_code`：可选，保存后下单时会放入 BuilderConfig

成功响应：

```json
{
  "id": 1,
  "name": "账户名",
  "wallet_address": "0x...",
  "proxy_wallet": "0x..."
}
```

### GET `/api/account/list`

获取账户列表。

权限：已登录

成功响应：

```json
[
  {
    "id": 1,
    "name": "账户名",
    "wallet_address": "0x...",
    "proxy_wallet": "0x...",
    "builder_api_key": "...",
    "builder_code": "optional-builder-code",
    "owner_user_id": 1,
    "created_at": "2026-05-16 12:00:00.000"
  }
]
```

备注：响应是数组本身，不包 `{ "accounts": ... }`。

### PUT `/api/account/{account_id}/builder-code`

为已有账户设置 `builder_code`。

权限：已登录，且账户属于当前用户；管理员可访问所有账户

请求：

```json
{
  "builder_code": "builder-code"
}
```

成功响应：

```json
{
  "status": "ok"
}
```

### DELETE `/api/account/{proxy_wallet}`

删除账户。

权限：已登录，且账户属于当前用户；管理员可访问所有账户

成功响应：

```json
{
  "status": "ok"
}
```

### GET `/api/account/{proxy_wallet}/balance`

获取账户余额、持仓和总资产估值。

权限：已登录，且账户属于当前用户；管理员可访问所有账户

成功响应：

```json
{
  "balance": 12.34,
  "positions": [
    {
      "asset": "token_id",
      "currentValue": 10.5
    }
  ],
  "total_position_value": 10.5,
  "total_value": 22.84
}
```

备注：`positions` 原样来自 Polymarket Data API `positions?user=...`，字段可能随上游变化而增加。

### GET `/api/account/total-balance`

获取当前可见账户的总资产估值。

权限：已登录

成功响应：

```json
{
  "total_balance": 123.45
}
```

## 4. Leader 管理

### GET `/api/leaders`

列出 Leader。

权限：已登录

成功响应：

```json
{
  "leaders": [
    {
      "id": 1,
      "proxy_wallet": "0x...",
      "name": "leader name",
      "profile_image": "https://...",
      "bio": "",
      "pseudonym": "",
      "x_username": "",
      "verified_badge": false,
      "display_username_public": false,
      "poly_created_at": "2026-05-16 12:00:00.000",
      "owner_user_id": 1,
      "created_at": "2026-05-16 12:00:00.000",
      "updated_at": "2026-05-16 12:00:00.000"
    }
  ]
}
```

### POST `/api/leaders`

添加 Leader。后端会从 Polymarket profile 接口补全名字和 profile 信息。

权限：已登录

请求：

```json
{
  "proxy_wallet": "0x..."
}
```

校验：

- 地址必须以 `0x` 开头
- 长度必须为 42
- 当前用户可见范围内不能重复

成功响应：

```json
{
  "leader_id": 1,
  "name": "leader name"
}
```

### PUT `/api/leaders/{leader_id}`

更新 Leader 名字。

权限：已登录，且 Leader 属于当前用户；管理员可访问所有 Leader

请求：

```json
{
  "name": "new name"
}
```

成功响应：

```json
{
  "status": "ok"
}
```

### DELETE `/api/leaders/{leader_id}`

删除 Leader。

权限：已登录，且 Leader 属于当前用户；管理员可访问所有 Leader

成功响应：

```json
{
  "status": "ok"
}
```

### POST `/api/leaders/{leader_id}/refresh`

从 Polymarket 刷新 Leader profile。

权限：已登录，且 Leader 属于当前用户；管理员可访问所有 Leader

成功响应：

```json
{
  "status": "ok"
}
```

### GET `/api/leaders/balances`

获取 Leader 余额信息。

权限：已登录

查询参数：

- `leader_address`：可选。传入时返回单个 Leader 的余额；不传时只返回当前用户可见 Leader 地址列表。

不传 `leader_address` 的响应：

```json
{
  "addresses": ["0x..."]
}
```

传 `leader_address` 的响应：

```json
{
  "balances": {
    "0x...": {
      "position_value": 100.12,
      "available_balance": 23.45,
      "total_balance": 123.57
    }
  }
}
```

备注：

- `position_value` 来自 Polymarket Data API `value?user=...`
- `available_balance` 来自 Polygon USDC `balanceOf`

## 5. Copy Trading 跟单

### POST `/api/copy-trading/configs`

创建跟单配置。

权限：已登录

请求：

```json
{
  "leader_proxy_wallet": "0x...",
  "follower_proxy_wallet": "0x...",
  "share_ratio": 0.1,
  "threshold": 0
}
```

字段说明：

- `leader_proxy_wallet`：Leader proxy wallet
- `follower_proxy_wallet`：Follower proxy wallet，必须是当前用户可见账户
- `share_ratio`：跟单比例，必须大于 0
- `threshold`：总额度；`0` 表示无限额度，后端以大数 `INF = 1e10` 存储

成功响应：

```json
{
  "config_id": 1
}
```

### GET `/api/copy-trading/configs`

列出跟单配置。

权限：已登录

成功响应：

```json
{
  "configs": [
    {
      "id": 1,
      "leader_proxy_wallet": "0x...",
      "leader_name": "leader name",
      "follower_proxy_wallet": "0x...",
      "follower_name": "account name",
      "share_ratio": 0.1,
      "enabled": true,
      "threshold": 10000000000.0,
      "allowance": 10000000000.0,
      "owner_user_id": 1
    }
  ]
}
```

### GET `/api/copy-trading/configs/{config_id}`

获取单个跟单配置。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

成功响应：

```json
{
  "id": 1,
  "leader_proxy_wallet": "0x...",
  "follower_proxy_wallet": "0x...",
  "share_ratio": 0.1,
  "enabled": true,
  "threshold": 10000000000.0,
  "allowance": 10000000000.0,
  "owner_user_id": 1
}
```

### PUT `/api/copy-trading/configs/{config_id}`

更新跟单配置。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

请求：

```json
{
  "share_ratio": 0.2,
  "enabled": true,
  "threshold": 100
}
```

字段说明：

- `share_ratio`：可选，范围 `(0, 1]`
- `enabled`：可选
- `threshold`：可选，必须大于等于 0；`0` 表示无限额度
- 至少传一个字段

成功响应：

```json
{
  "status": "ok"
}
```

### DELETE `/api/copy-trading/configs/{config_id}`

删除跟单配置。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

成功响应：

```json
{
  "status": "ok"
}
```

### GET `/api/copy-trading/configs/{config_id}/positions`

获取当前内存中的 Leader/Follower 仓位。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

成功响应：

```json
{
  "leader_positions": {
    "asset_id": 12.3
  },
  "follower_positions": {
    "asset_id": 1.23
  }
}
```

### GET `/api/copy-trading/configs/{config_id}/position-assets`

获取该配置可用于仓位历史查询的 asset 列表。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

成功响应：

```json
{
  "assets": [
    {
      "asset_id": "token_id",
      "question": "Market question",
      "outcome": "Yes",
      "last_seen_at": "2026-05-16 12:00:00.000"
    }
  ]
}
```

### GET `/api/copy-trading/configs/{config_id}/orders`

获取跟单订单历史。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

查询参数：

- `limit`：可选，默认 `100`

成功响应：

```json
{
  "orders": [
    {
      "id": "order_id",
      "config_id": 1,
      "leader": "0x...",
      "follower": "0x...",
      "leader_tx_hash": "0x...",
      "asset_id": "token_id",
      "side": "BUY",
      "leader_size": 10.0,
      "leader_price": 0.5,
      "follow_size": 1.0,
      "follow_price": 0.51,
      "size_matched": 0.0,
      "status": "live",
      "created_at": "2026-05-16 12:00:00.000",
      "updated_at": "2026-05-16 12:00:00.000"
    }
  ]
}
```

### GET `/api/copy-trading/configs/{config_id}/position-history`

获取指定 `config_id + asset_id` 的 Leader/Follower 仓位历史曲线。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

查询参数：

- `asset_id`：必填
- `start`：可选，可被后端 `to_utc8_dt()` 解析的时间
- `end`：可选，可被后端 `to_utc8_dt()` 解析的时间
- `normalized`：可选，默认 `false`
- `limit`：可选，默认 `2000`，范围 `1..5000`

成功响应：

```json
{
  "points": [
    {
      "id": 1,
      "config_id": 1,
      "asset_id": "token_id",
      "leader_proxy_wallet": "0x...",
      "follower_proxy_wallet": "0x...",
      "share_ratio": 0.1,
      "leader_position": 10.0,
      "follower_position": 1.0,
      "follower_pending_buy": 0.0,
      "follower_pending_sell": 0.0,
      "source": "leader_signal",
      "side": "BUY",
      "event_size": 10.0,
      "event_price": 0.5,
      "order_id": "order_id",
      "leader_tx_hash": "0x...",
      "raw_context": "{}",
      "created_at": "2026-05-16 12:00:00.000",
      "leader_value": 1.0,
      "follower_value": 1.0
    }
  ]
}
```

字段说明：

- `normalized=false` 时，`leader_value = leader_position`
- `normalized=true` 时，`leader_value = leader_position * share_ratio`
- `follower_value` 始终为 `follower_position`

### POST `/api/copy-trading/configs/{config_id}/sync`

立即同步该配置的 Leader 仓位、Follower 仓位和 Follower pending 订单。

权限：已登录，且配置属于当前用户；管理员可访问所有配置

成功响应：

```json
{
  "leader_synced": true,
  "follower_synced": true,
  "pending_synced": "ok"
}
```

备注：如果某一项同步抛异常，对应字段会返回异常字符串。

## 6. Performance 性能与运行时缓存

### POST `/api/performance/latency`

手动触发全量延迟检测。

权限：已登录

成功响应：

```json
{
  "success": true,
  "data": {
    "ws": {
      "ws_market": "12",
      "ws_user": "--",
      "polygon_ws": "34",
      "poly_rtds": "56"
    },
    "http": {
      "data_api": "100",
      "clob_api": "120",
      "gamma_api": "80",
      "polygon_http": "90"
    }
  }
}
```

备注：延迟值是字符串形式的毫秒数；`"--"` 表示不可用。

### GET `/api/performance/cache/summary`

获取运行时内存缓存概览。

权限：管理员

成功响应：

```json
{
  "success": true,
  "data": {
    "generated_at": "2026-05-16 12:00:00.000",
    "services": [
      {
        "service": "copy_trading",
        "label": "Copy Trading",
        "status": "ok",
        "total_items": 10,
        "caches": [
          {
            "service": "copy_trading",
            "cache": "configs",
            "label": "Configs",
            "total": 1,
            "summary": {
              "enabled": 1,
              "disabled": 0
            },
            "sample": []
          }
        ]
      }
    ]
  }
}
```

### GET `/api/performance/cache/{service_name}/{cache_name}`

获取指定运行时缓存的分页详情。

权限：管理员

路径参数：

- `service_name`
- `cache_name`

查询参数：

- `limit`：默认 `100`，范围 `1..500`
- `offset`：默认 `0`
- `asset_id`：可选。当前主要用于 `market/order_books` 详情过滤。

成功响应：

```json
{
  "success": true,
  "data": {
    "service": "copy_trading",
    "cache": "configs",
    "total": 1,
    "limit": 100,
    "offset": 0,
    "truncated": false,
    "generated_at": "2026-05-16 12:00:00.000",
    "items": []
  }
}
```

当前支持的缓存路径：

| service_name | cache_name |
| --- | --- |
| `copy_trading` | `configs` |
| `copy_trading` | `leader_positions` |
| `copy_trading` | `follower_positions` |
| `copy_trading` | `pending_buy_orders` |
| `copy_trading` | `pending_sell_orders` |
| `copy_trading` | `buy_debt` |
| `copy_trading` | `sell_debt` |
| `copy_trading` | `processed_txs` |
| `copy_trading` | `processed_orders` |
| `copy_trading` | `runtime` |
| `market` | `subscriptions` |
| `market` | `order_books` |
| `market` | `tick_sizes` |
| `market` | `neg_risks` |
| `market` | `runtime` |
| `account` | `clob_clients` |
| `account` | `names` |
| `leader` | `names` |
| `chain_monitor` | `leader_subscriptions` |
| `chain_monitor` | `follower_subscriptions` |
| `chain_monitor` | `pending_subscriptions` |
| `chain_monitor` | `processed_txs` |
| `chain_monitor` | `runtime` |
| `copy_trading_ws` | `instances` |
| `frontend_ws` | `connections` |
| `performance` | `latency` |
| `performance` | `runtime` |

错误：

- `404 Cache not found`

## 7. WebSocket

### WS `/ws/market`

前端状态 WebSocket。

权限：已登录

连接方式：

```text
ws://localhost:8000/ws/market
```

客户端消息：当前后端只保持连接并消费文本消息，不定义业务入站消息。

服务端消息：

```json
{
  "event_type": "latency",
  "ws": {
    "ws_market": "12",
    "ws_user": "--",
    "polygon_ws": "34",
    "poly_rtds": "56"
  },
  "http": {
    "data_api": "100",
    "clob_api": "120",
    "gamma_api": "80",
    "polygon_http": "90"
  }
}
```

未授权时关闭连接，code 为 `1008`。

### WS `/ws/performance`

管理员性能监控 WebSocket。

权限：管理员

连接方式：

```text
ws://localhost:8000/ws/performance
```

服务端每 5 秒发送一次缓存概览：

```json
{
  "event_type": "cache_summary",
  "data": {
    "generated_at": "2026-05-16 12:00:00.000",
    "services": []
  }
}
```

未授权或非管理员时关闭连接，code 为 `1008`。

## 8. 前端当前使用的接口索引

| 页面/模块 | 接口 |
| --- | --- |
| `App.tsx` | `GET /api/auth/me`、`POST /api/auth/logout`、`POST /api/performance/latency`、`WS /ws/market` |
| `Login.tsx` | `POST /api/auth/login`、`GET /api/auth/me` |
| `UserManagement.tsx` | `GET /api/auth/users`、`POST /api/auth/users`、`PUT /api/auth/users/{user_id}` |
| `Account.tsx` | `GET /api/account/list`、`POST /api/account/add`、`DELETE /api/account/{proxy_wallet}`、`PUT /api/account/{account_id}/builder-code`、`GET /api/account/{proxy_wallet}/balance` |
| `CopyTrading.tsx` | Leader、Account、Copy Trading 相关接口 |
| `PerformanceMonitor.tsx` | `GET /api/performance/cache/summary`、`GET /api/performance/cache/{service}/{cache}`、`WS /ws/performance` |

## 9. 代码入口索引

| 模块 | 文件 |
| --- | --- |
| FastAPI app 和 WebSocket | `backend/main.py` |
| Auth 路由 | `backend/src/auth/api.py` |
| Account 路由 | `backend/src/account/api.py` |
| Leader 路由 | `backend/src/leader/api.py` |
| Copy Trading 路由 | `backend/src/copy_trading/api.py` |
| Performance 路由 | `backend/src/performance/router.py` |
| 前端 REST/WS base | `frontend/src/api.ts` |

