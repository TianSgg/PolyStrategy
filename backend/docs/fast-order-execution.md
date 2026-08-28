# 快速下单优化总结

## 已有优化

### 1. HTTP 连接复用 + HTTP/2 多路复用

py_clob_client 底层使用 `httpx.Client(http2=True)` 全局单例，所有请求共享同一连接池。

- 首次请求完成 TCP 三次握手 + TLS 握手后，后续请求复用已建立的连接
- HTTP/2 支持同一连接上多个请求并行（多路复用）
- Header 里设置了 `Connection: keep-alive`

**效果**：除第一笔订单外，每笔省去 ~100-150ms 的握手开销。

### 2. 余额缓存读取（零网络延迟判断）

`BalancePoller` 后台每 1 秒轮询 Polymarket API，将余额缓存在内存中。

- 下单前 `available_cash` 是纯内存读取，不发网络请求
- 余额不足时直接拒绝，省去一次无效的下单 API 调用
- 下单失败后自动触发 `refresh()` 刷新缓存

**效果**：余额判断 0ms（vs 轮询式 ~200-500ms）。

### 3. 下单与风控并行启动

`enter()` 中 `place_order` 和 `risk.start()` 通过 `asyncio.gather` 并行执行：

```python
order_task = asyncio.create_task(self._executor.place_order(...))
risk_task = asyncio.create_task(self.risk.start(...))
result, _ = await asyncio.gather(order_task, risk_task)
```

**效果**：风控订阅 orderbook 的延迟不阻塞下单，两者同时进行。

### 4. ClobClient 实例复用

`ClientProvider` 缓存已创建的 ClobClient 实例（含签名密钥、API 凭证），不会每次下单重新初始化。

```python
def get_or_create_clob_client(self, proxy_wallet: str):
    if addr in self._clients:
        return self._clients[addr]  # 复用
```

**效果**：省去密钥解密、Client 初始化开销。

### 5. tick_size / neg_risk 传参避免远程查询

py_clob_client 的 `create_order` 内部逻辑：

```python
tick_size = options.tick_size if options.tick_size is not None else self.get_tick_size(token_id)  # HTTP!
neg_risk = options.neg_risk if options.neg_risk is not None else self.get_neg_risk(token_id)      # HTTP!
```

当前下单时显式传入了这两个参数（BUY 阶段 tick_size=0.01, neg_risk=False），py_clob 不会额外发 HTTP 请求查询。

**效果**：省去 2 次 HTTP 往返（~200-400ms）。

### 6. 协议版本硬编码（跳过 /version 查询）

py_clob_client 中 `get_version()` 和 `__resolve_version()` 均直接 `return 2`，`create_order` 中也硬编码 `version=2`。

- 原始行为：首次下单时调 `GET /version` 获取协议版本号
- 当前行为：直接使用 v2，省去一次 HTTP 往返

**效果**：省去首次下单时的版本查询请求。

### 7. DB 写入异步化（fire-and-forget）

`EventLogger.log_step()` 通过 `loop.run_in_executor()` 将 INSERT 提交到专用线程池，不阻塞事件循环。

- 下单流程中的所有 `log_step` 调用立即返回
- 实际写入在后台线程完成，失败仅打日志不影响交易

**效果**：下单热路径不再被 MySQL 写入阻塞（省去 1-5ms）。

### 8. BBO 快照从本地 OrderBook 读取

挂单前后的 best_bid/best_ask 从内存中的 `LocalOrderBook` 读取，不走网络。

**效果**：记录 BBO 不增加任何延迟。

---

## 未优化 / 潜在瓶颈

### 1. 未开启 retry_on_error

创建 ClobClient 时未传 `retry_on_error=True`。网络抖动导致请求失败后不会自动重试，直接返回 failed。

py_clob 内置的重试逻辑：5xx 或网络错误时等 30ms 重试一次。但当前未启用。

### 2. 无连接保活机制

没有定期心跳/ping 来检测连接是否存活。如果连接因空闲被中间设备断开，下一笔订单会在死连接上失败后才重建。

### 3. 无预签名

ECDSA 签名在 `create_and_post_order()` 调用时实时计算（~1-5ms）。由于 size 取决于余额，无法在信号到达前提前签好。

对总延迟影响很小（1-5ms），优化优先级低。

---

## 下单延迟拆解（典型场景）

| 阶段 | 耗时 | 说明 |
|------|------|------|
| 信号到达 → enter() | ~0ms | 内存回调 |
| 余额检查 | ~0ms | 内存读取 |
| 取 pre_bbo | ~0ms | 本地 orderbook |
| ECDSA 签名 | 1-5ms | CPU 计算 |
| HTTP POST 下单 | 100-400ms | 网络往返（连接已复用） |
| 取 post_bbo | ~0ms | 本地 orderbook |
| **总计** | **~100-410ms** | 瓶颈在网络往返 |

首次下单（冷连接）额外增加 ~100-150ms TCP+TLS 握手。

---

## 对比 WeatherTaker 的额外优化

WeatherTaker 项目在同一套 py_clob_client 基础上做了更多优化，以下是 PolyStrategy **尚未具备**的：

### ~~1. DB 写入 fire-and-forget~~ ✓ 已完成

`EventLogger.log_step()` 已改为通过 `run_in_executor` 提交到专用线程池（2 线程），fire-and-forget，不阻塞事件循环。

### 2. WebSocket 保活 ping

WeatherTaker 所有 WS 连接显式设置 `ping_interval=10~20`，防止中间设备因空闲超时断开连接。

PolyStrategy 未对 orderbook WS 设置 ping_interval。

### 3. 余额从订单结果推算（非轮询）

WeatherTaker 不轮询余额 API，而是从下单结果的 `pending_delta` / `position_delta` 直接增减内存余额，热路径完全无 API 调用。

PolyStrategy 使用 BalancePoller 每 1s 轮询（下单时是内存读取，但存在 1s 内的陈旧窗口）。

### 4. 业务级智能重试

WeatherTaker 解析具体错误类型自动修正：
- "lower than the minimum" → 自动以 min_size=5 重试
- "not enough balance" on SELL → 从错误消息解析实际余额，修正 size 后重试

PolyStrategy 当前无此逻辑。

---

## 可改进项（按优先级）

1. **开启 retry_on_error=True** — 让网络抖动时自动重试一次，成本 30ms
2. **WebSocket ping_interval** — orderbook WS 连接加 ping_interval=20
3. **连接保活 ping** — 每 30s 发一次 `/time` 请求保持 HTTP 连接热
4. **显式预热连接** — 策略启动时主动发一次请求建立连接，确保首笔订单不吃冷启动延迟
5. **业务级智能重试** — 解析 "not enough balance" / "lower than minimum" 错误自动修正重试
