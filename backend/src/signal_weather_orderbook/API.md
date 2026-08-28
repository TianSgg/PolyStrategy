# Signal Weather Orderbook API

服务端口: `8001`  
路由前缀: `/api/weather`

---

## REST API

### GET /health

健康检查。

**Response:**
```json
{"status": "ok", "service": "polystrategy-signal-weather-orderbook"}
```

---

### GET /api/weather/cities

获取天气监控城市列表 (Dashboard 用)。

**Response:**
```json
{
  "cities": [
    {
      "city_name": "Singapore",
      "city_slug": "singapore",
      "directions": [
        {
          "direction": "highest",
          "event_slug": "highest-temperature-in-singapore-on-august-28-2026",
          "signal_count": 42,
          ...
        }
      ]
    }
  ]
}
```

---

### GET /api/weather/cities/{city_slug}/{direction}

获取单个城市某方向的详细信息 (市场列表、监控状态等)。

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| city_slug | path | string | 城市标识 (如 `singapore`) |
| direction | path | string | `highest` 或 `lowest` |

**Response:** 城市方向详情 (含 markets 列表、orderbook 状态)

**Error:** `404` — 未找到配置的城市方向

---

### GET /api/weather/events/{event_slug}/signals

分页查询某个天气事件的信号列表。

| 参数 | 位置 | 类型 | 默认 | 说明 |
|------|------|------|------|------|
| event_slug | path | string | — | 事件标识 (如 `highest-temperature-in-singapore-on-august-28-2026`) |
| limit | query | int | 100 | 每页数量 (1-500) |
| before_id | query | int? | null | 游标分页: 获取此 ID 之前的记录 |

**Response:**
```json
{
  "event_slug": "highest-temperature-in-singapore-on-august-28-2026",
  "signals": [
    {
      "id": 1234,
      "signal_id": "sweep:highest-...:asset_id:1724800000000",
      "occurred_at": "2026-08-28T04:16:00Z",
      "signal_type": "sweep",
      "event_slug": "highest-temperature-in-singapore-on-august-28-2026",
      "market_slug": "highest-temperature-in-singapore-on-august-28-2026",
      "main_market_slug": null,
      "main_temperature_label": null,
      "main_outcome": null,
      "city": "Singapore",
      "city_slug": "singapore",
      "direction": "highest",
      "local_date": "2026-08-28",
      "temperature_label": "32°C",
      "outcome": "no",
      "token_id": "0x1234abcd...",
      "status": null,
      "reason": "asks_cleared",
      "payload": {},
      "created_at": "2026-08-28T04:16:01Z"
    }
  ],
  "next_before_id": 1200
}
```

---

### GET /api/weather/signals/recent

分页查询最近的信号 (跨所有事件)。

| 参数 | 位置 | 类型 | 默认 | 说明 |
|------|------|------|------|------|
| limit | query | int | 100 | 每页数量 (1-500) |
| before_id | query | int? | null | 游标分页 |

**Response:**
```json
{
  "signals": [...],
  "next_before_id": 1100
}
```

---

### GET /api/weather/orderbook/{token_id}

实时获取某个 token 的 CLOB 订单簿 (从 Polymarket REST API 拉取)。

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| token_id | path | string | Polymarket asset token ID |

**Response:**
```json
{
  "token_id": "0x1234abcd...",
  "observed_at": "2026-08-28 04:16:00.123 UTC",
  "tick_size": "0.01",
  "best_bid": {"price": "0.68", "size": "500"},
  "best_ask": {"price": "0.79", "size": "300"},
  "bid_levels": 12,
  "ask_levels": 8,
  "bids": [{"price": "0.68", "size": "500"}, ...],
  "asks": [{"price": "0.79", "size": "300"}, ...]
}
```

**Error:** `502` — CLOB 请求失败

---

## SSE (Server-Sent Events)

### GET /api/weather/live

实时订单簿推送 (当前监控中市场的 L2 book 变化)。

**Content-Type:** `text/event-stream`

**Events:**

| 事件名 | 时机 | 数据 |
|--------|------|------|
| `snapshot` | 连接后立即发送 | 全量当前所有监控市场的 orderbook |
| `orderbook` | 价格变化时 | 单个 token 的更新 |
| `: keepalive` | 每 15s 无数据时 | 心跳 (注释帧) |

---

### GET /api/weather/signal-counts/live

实时信号计数推送 (各事件的信号总数)。

**Content-Type:** `text/event-stream`

**Events:**

| 事件名 | 时机 | 数据 |
|--------|------|------|
| `snapshot` | 连接后立即发送 | `{"event_slug": count, ...}` |
| `notification-count` | 新信号产生时 | `{"type": "update", "event_slug": "...", "count": 43}` |
| `: keepalive` | 每 15s 无数据时 | 心跳 |

---

## WebSocket

### WS /ws/signal

策略执行服务订阅天气扫单信号的端点。

**连接流程:**

1. 客户端连接后发送 hello 帧:
```json
{"client_id": "strategy-sweep-1", "main_only": false, "subscribe": ["weather_sweep"]}
```

2. 服务端回复 welcome:
```json
{"type": "welcome", "server_id": "weather_signal", "subscriptions": ["weather_sweep"]}
```

3. 服务端推送信号:
```json
{
  "type": "weather_sweep",
  "signal": {
    "event_id": "highest-...:asset_id:1724800000000",
    "event_type": "sweep",
    "token_id": "0x1234abcd...",
    "outcome": "no",
    "city": "Singapore",
    "event_slug": "highest-temperature-in-singapore-on-august-28-2026",
    "market_slug": "...",
    "temperature_label": "32°C",
    "direction": "highest",
    "reason": "asks_cleared",
    "is_from_main": true,
    "occurred_at_ms": 1724800000000,
    "received_at_ns": 1724800000000000000,
    "orderbook_snapshot": {
      "best_bid": "0.68",
      "best_ask": null,
      "observed_at_unix_ms": 1724800000000
    }
  }
}
```

4. 客户端可发送 ping 保活:
```json
{"type": "ping"}
```
服务端回复:
```json
{"type": "pong"}
```

**参数说明:**

| 字段 | 说明 |
|------|------|
| `main_only` | `true` 时只接收主力市场的信号，过滤 next 候选市场 |

**鉴权:** HTTP 403 — 需要有效认证 (通过 Traefik 网关路由时附带 JWT)

---

## Admin API (需要 Root 权限)

### GET /api/weather/cities/admin

获取所有城市配置 (含未启用的)。

**Response:**
```json
{
  "cities": [
    {
      "id": 1,
      "city_name": "Singapore",
      "city_slug": "singapore",
      "timezone": "Asia/Singapore",
      "has_highest_market": true,
      "has_lowest_market": false,
      "monitor_highest": true,
      "monitor_lowest": false,
      "enabled": true
    }
  ]
}
```

---

### POST /api/weather/cities/admin

新增城市配置。

**Request Body:**
```json
{
  "city_name": "Chengdu",
  "city_slug": "chengdu",
  "timezone": "Asia/Shanghai",
  "has_highest_market": true,
  "has_lowest_market": false,
  "monitor_highest": true,
  "monitor_lowest": false,
  "enabled": true
}
```

**Response:** `201` — `{"city": {...}}`

**Error:**
- `400` — 无效时区 / monitor 配置冲突
- `409` — city_slug 已存在

---

### PUT /api/weather/cities/admin/{city_id}

更新城市配置。

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| city_id | path | int | 城市 ID |

**Request Body:** 同 POST

**Error:**
- `400` — 校验失败
- `404` — 城市不存在
- `409` — city_slug 冲突

---

### DELETE /api/weather/cities/admin/{city_id}

删除城市配置。

**Response:** `{"deleted": true}`

**Error:** `404` — 城市不存在

---

### POST /api/weather/cities/reload

热重载城市配置 (从数据库重新读取，重启所有监控)。

**Response:** `{"status": "reloaded"}`
