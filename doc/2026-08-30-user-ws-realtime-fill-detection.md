# User WebSocket 实时成交追踪 — 设计方案

> 日期: 2026-08-30
> 状态: 设计中
> 参考: `doc/2026-08-28-event-log-storage-design.md`（event log 字段定义）

---

## 一、问题

### 现状

当前策略下单后，**只能从 `place_order()` 的同步返回中获取即时成交量**。
如果订单进入 `live` 状态（未立即全部成交），后续的部分成交**完全不可见**。

这导致三个问题：

**1. Event 记录不完整 — 丢失成交细节**

`2026-08-28-event-log-storage-design.md` 设计了 `buy_filled(source: "ws_user")` 来记录 WS 推送的逐步成交，
但代码中没有 WS 监听，live 订单的中途成交不会产生任何 `buy_filled` / `sell_filled` 记录。

```
设计预期:  buy_placed → buy_filled(8) → buy_filled(7) → entry_timeout(position=15)
实际产出:  buy_placed → entry_timeout(position=0)   ← 中间的成交全部丢失
```

**2. 撤单后 position_shares 不准**

`_entry_timeout()` 和 `_risk_exit()` 用 `cancel_order()` 撤单，不查询实际成交量。
撤单时订单可能已经部分成交，但 `position_shares` 还停留在 `place_order()` 返回时的值。

| 代码位置 | 行为 | 风险 |
|---|---|---|
| `service.py:327` `_entry_timeout` | `cancel_order()` 无返回 fill 信息 | position_shares 偏小，遗漏持仓 |
| `service.py:544` `_risk_exit` 撤买单 | 同上 | 后续清仓量不对 |
| `service.py:553` `_risk_exit` 撤卖单 | 同上 | 不知道已卖出多少，可能 over-sell |

**3. 风控无法正确清仓**

风控触发后需要卖出所有持仓。由于 position_shares 不准：
- 偏小 → 少卖，留下裸仓
- 偏大 → 多卖，CLOB 报 insufficient_balance

清仓循环中卖单 live 时直接 `break`（`service.py:629`），放弃等待成交，可能实际已卖出部分但策略不知道。

### 代码中的 TODO

- `service.py:466` — `# live → wait for WS fills (TODO: implement User WS fill detection)`
- `service.py:629` — `# live → wait (TODO: User WS)`

---

## 二、User WebSocket 设计

### 2.1 Polymarket User WS 协议

**连接**: `wss://ws-subscriptions-clob.polymarket.com/ws/user`

**认证**: 连接后发送：
```json
{
  "auth": {
    "apiKey": "<clob_api_key>",
    "secret": "<clob_api_secret>",
    "passphrase": "<clob_api_passphrase>"
  },
  "type": "user"
}
```

**心跳**: 每 10 秒发 `PING`，服务端回 `PONG`。

**推送事件**:

| 事件类型 | 触发 | 关键字段 |
|---|---|---|
| order PLACEMENT | 新订单被接受 | `id`, `original_size`, `size_matched`, `status` |
| order UPDATE | 有新成交 | `id`, `size_matched`（累计值） |
| order CANCELLATION | 被撤销 | `id`, `size_matched` |
| trade MATCHED | 单笔成交明细 | `taker_order_id`, `size`（本次增量）, `price`, `trade_id` |

**增量计算**: order UPDATE 推的是累计 `size_matched`，增量 = `size_matched - last_known_matched`。
trade 事件直接给增量 `size`。同一笔成交两种事件都会推，需去重。

### 2.2 Framework 层: `UserWS` 组件

在 `framework/user_ws.py` 新建，与 `orderbook_ws.py` 平级：

```
framework/
├── orderbook_ws.py     # 已有: Market Channel (公开, 订单簿)
├── user_ws.py          # 新增: User Channel  (认证, 订单/成交)
```

**核心设计**:

```python
@dataclass(frozen=True)
class FillEvent:
    order_id: str
    token_id: str
    side: str               # "BUY" | "SELL"
    fill_size: Decimal       # 本次增量
    fill_price: Decimal
    total_matched: Decimal   # 累计成交量
    trade_id: str | None
    source: str              # "ws_order_update" | "reconnect_reconcile"
    timestamp_ms: int

@dataclass(frozen=True)
class CancelEvent:
    order_id: str
    token_id: str
    side: str
    size_matched: Decimal
    timestamp_ms: int

class UserWS:
    """一个 proxy_wallet 一个实例，全局单例注册表管理。"""

    def __init__(self, proxy_wallet: str): ...

    # 生命周期
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # 策略调用接口
    def watch_order(self, order_id, token_id, side, price,
                    initial_matched=0, on_fill=None, on_cancel=None): ...
    def unwatch_order(self, order_id): ...

    # 内部
    async def _run(self): ...          # WS 主循环 + 自动重连（指数退避 1→2→4→...→30s）
    async def _authenticate(self): ... # 发认证帧
    async def _heartbeat(self): ...    # 10s PING
    def _dispatch(self, raw): ...      # 消息路由
    def _handle_order_event(self): ... # 累计值 → 算增量 → 调 on_fill
    def _handle_trade_event(self): ... # 增量值 → 去重 → 调 on_fill
    async def _on_reconnect(self): ... # 重连后 REST 查询补偿

@dataclass
class OrderWatch:
    """每个被监听订单的状态。"""
    order_id: str
    token_id: str
    side: str
    price: Decimal               # 委托价，order UPDATE 的 fallback fill_price
    last_matched: Decimal        # 累计成交量（只由 order UPDATE 推进，单调递增）
    seen_trade_ids: set          # 已处理的 trade_id 集合
    pending_trade_price: Decimal | None  # trade 事件暂存的精确成交价
    pending_trade_id: str | None         # trade 事件暂存的 trade_id
    on_fill: Callable | None
    on_cancel: Callable | None

# 全局注册表
async def get_or_create_user_ws(proxy_wallet: str) -> UserWS: ...
async def stop_all_user_ws() -> None: ...
```

**去重 — order UPDATE 为主、trade 事件补充 price**:

order UPDATE 和 trade 事件可能推同一笔成交。核心难点：order UPDATE 给累计 `size_matched`，
trade 事件给增量 `size`，两者无法直接对比。如果简单地 `last_matched += trade.size`，
当 order UPDATE 先到时，后到的 trade 事件会重复记账：

```
① order UPDATE: size_matched=10 → delta=10, 记录 fill, last_matched=10  ✓
② trade event:  size=10, t1     → last_matched += 10 = 20               ✗ 重复！
```

因此采用 **order UPDATE 驱动记账，trade 事件只补充精确价格** 的策略：

- `OrderWatch.last_matched`（累计值）— **只由 order UPDATE 推进**，天然单调递增
- `OrderWatch.seen_trade_ids: set[str]` — 防止同一 trade 事件重复处理
- `OrderWatch.pending_trade_price / pending_trade_id` — 暂存 trade 的精确价格

```python
def _handle_order_event(self, order_id: str, size_matched: Decimal):
    watch = self._watches.get(order_id)
    if not watch:
        return
    delta = size_matched - watch.last_matched
    if delta <= 0:
        return
    watch.last_matched = size_matched
    # 如果有 trade 事件先到，用其精确价格；否则用委托价
    price = watch.pending_trade_price or watch.price
    trade_id = watch.pending_trade_id
    watch.pending_trade_price = None
    watch.pending_trade_id = None
    self._emit_fill(watch, delta, price, total_matched=size_matched,
                    trade_id=trade_id, source="ws_order_update")

def _handle_trade_event(self, order_id: str, trade_id: str,
                        size: Decimal, price: Decimal):
    watch = self._watches.get(order_id)
    if not watch or trade_id in watch.seen_trade_ids:
        return
    watch.seen_trade_ids.add(trade_id)
    # 暂存精确价格，等 order UPDATE 来时消费
    watch.pending_trade_price = price
    watch.pending_trade_id = trade_id
    # trade 事件不推进 last_matched，不触发 _emit_fill
    # 记账统一由 order UPDATE 驱动
```

**到达顺序分析**:

| 到达顺序 | 行为 | fill_price |
|---|---|---|
| trade 先到 → UPDATE 后到 | trade 暂存 price；UPDATE 到达时消费 pending_trade_price，记录 fill | 精确成交价 ✓ |
| UPDATE 先到 → trade 后到 | UPDATE 记录 fill（用委托价）；trade 到达时只写入 pending，不记账 | 委托价近似 |
| 只收到 UPDATE（trade 丢失） | 正常记录 fill | 委托价近似 |
| 只收到 trade（UPDATE 丢失） | 暂存但不记账 → **依赖重连补偿兜底** | — |

> **权衡**: "只收到 trade 无 UPDATE" 的场景极少发生（WS 正常时两者都推），
> 万一发生，重连补偿或撤单 REST 校准会兜底。
> 如果需要 100% 精确价格，可用 `GET /data/trades` 批量查询，但增加复杂度和延迟，暂不采用。

**回调串行**: `_dispatch` 中 **不用 `asyncio.create_task`**，改为 `await watch.on_fill(...)` 顺序执行。
原因：如果两个 fill event 连续到达，create_task 会导致两次回调并发修改 `position_shares`，产生竞态。
串行执行保证 position_shares 的每次更新都基于上一次的正确值。代价是回调中的 DB 写会阻塞下一条消息处理，
但 event log 写入已经是 ThreadPoolExecutor 异步的，实际延迟可忽略。

**重连补偿**: 断连期间可能漏推。重连后对所有 watched orders 调 `GET /data/order/{orderID}`，
用 `size_matched` 校准 `last_matched`，差额作为 `FillEvent(source="reconnect_reconcile")` 补发。

**凭证获取**: 从已有的 `ClobClient` 实例提取 `client.creds`（ApiCreds），无需改 DB。

### 2.3 Strategy 层: SweepTrade 如何使用

策略通过 `OrderExecutor` 间接使用 UserWS：

```python
# OrderExecutor 新增
async def ensure_user_ws(self) -> UserWS:
    """懒加载，首次下单时创建 WS 连接。"""

async def cancel_order_with_fill_check(self, order_id) -> CancelResult:
    """撤单 + REST 查询最终成交量（双保险）。"""
```

SweepTrade 的使用模式：

```
下单 → place_order()
  ├─ filled → 记录成交，不需要 WS
  ├─ partial → 记录即时成交 + watch_order(on_fill=回调)
  └─ live → watch_order(on_fill=回调)

WS 推送成交 → on_fill 回调 → _record_buy_fill(source="ws_order_update")
                            → fill_price: order UPDATE 用委托价，trade 用真实成交价
                            → position_shares 实时更新
                            → 全部成交时取消超时计时器

超时/风控 → unwatch_order + cancel_order_with_fill_check
         → REST 校准 position_shares（防 WS 漏推）
```

**place_order 即时成交与 WS 推送的去重**:

partial 场景下，place_order 返回时已记录一次 `buy_filled(source="clob_response")`。
随后 WS 会推送同一笔成交的 order UPDATE。如果不处理，同一笔 fill 会被记两次。

解决方式：`watch_order(initial_matched=Decimal(result.filled_size))`。
`initial_matched` 设为 place_order 已返回的成交量，UserWS 的 `last_matched` 从这个值开始计算，
后续 WS 推送的 `size_matched` 如果 ≤ `initial_matched`，增量为 0，自然跳过。

---

## 三、Event 记录

### 3.1 设计方向

沿用 `2026-08-28-event-log-storage-design.md` 的设计：
- **统一用 `buy_filled` / `sell_filled` step 名**，通过 `source` 字段区分来源
- 不引入新 step 名（如 `ws_buy_fill`），保持统计查询简单

`source` 字段值：

| source | 含义 | 何时产生 |
|---|---|---|
| `clob_response` | place_order 同步返回的即时成交 | **已实现** |
| `ws_order_update` | WS order UPDATE 驱动的记账（fill_price 可能来自 trade 事件的精确价格或委托价） | 待实现 |
| `cancel_reconcile` | 撤单后 REST 查询发现的漏记成交 | 待实现 |
| `reconnect_reconcile` | WS 重连后 REST 查询补偿 | 待实现 |

### 3.2 新增 step 类型

在现有 step 之外，新增两个 step：

**fill_reconcile** — 撤单后发现 WS 漏推的成交差额

```json
{
  "side": "BUY",
  "order_id": "abc123-...",
  "clob_matched": "30.0000",
  "memory_before": "20.0000",
  "reconciled": "10.0000"
}
```

**reconnect_reconcile** — WS 重连后 REST 查询发现的断连期间成交

```json
{
  "order_id": "abc123-...",
  "side": "BUY",
  "delta": "5.0000",
  "new_matched": "25.0000"
}
```

### 3.3 已实现 vs 待实现

**已实现 ✅**:
- `_record_buy_fill(source="clob_response")` — service.py:265，place_order 即时成交
- `_record_sell_fill(source="clob_response")` — service.py:480, 634，卖单即时成交
- 两个方法已支持 `source` 和 `trade_id` 参数，JSON schema 已到位

**待实现 ❌**:
- WS 回调中调用 `_record_buy_fill(source="ws_order_update")` — 复用现有方法，无需改签名
- WS 回调中调用 `_record_sell_fill(source="ws_order_update")` — 同上
- `fill_reconcile` step — 在撤单校准逻辑中手动调 `log_step("fill_reconcile", ...)`
- `reconnect_reconcile` step — 在 WS 重连补偿回调中调 `log_step("reconnect_reconcile", ...)`

**关键点**: `_record_buy_fill` / `_record_sell_fill` 方法不需要改动。
WS 回调只是以不同 `source` 值调用它们，event log 自然就有了逐步成交记录。

### 3.4 完整场景示例

**场景: 买单 live → WS 多次部分成交 → 超时撤单 + 校准**

| seq | step | phase | detail 关键字段 |
|-----|------|-------|----------------|
| 1 | signal_received | entry | `{signal_id, token_id, ...}` |
| 2 | order_placed | entry | `{order: {order_id, size: "50"}, clob_status: "live"}` |
| 3 | buy_filled | entry | `{filled_size: "20", total_position: "20", source: "ws_order_update"}` |
| 4 | buy_filled | entry | `{filled_size: "10", total_position: "30", source: "ws_order_update", trade_id: "t1"}` |
| 5 | fill_reconcile | entry | `{side: "BUY", clob_matched: "35", memory_before: "30", reconciled: "5"}` |
| 6 | entry_timeout | entry | `{cancelled_order_id: "...", final_position: "35", unfilled_size: "15"}` |

> seq 3: order UPDATE 先到，fill_price 用委托价
> seq 4: trade 事件先到暂存了精确 price，随后 order UPDATE 消费了它，trade_id 一并记录
> seq 5: 撤单后 REST 查到实际成交 35，补偿 5 shares
> seq 6: 超时，有 35 shares 仓位，进入 exit_working

**场景: 卖单 live → WS 逐步成交 → 全部成交**

| seq | step | phase | detail 关键字段 |
|-----|------|-------|----------------|
| N | sell_order_placed | exit | `{order: {order_id, size: "35"}, clob_status: "live"}` |
| N+1 | sell_filled | exit | `{filled_size: "15", remaining_position: "20", source: "ws_order_update"}` |
| N+2 | sell_filled | exit | `{filled_size: "20", remaining_position: "0", source: "ws_order_update"}` |
| N+3 | sell_complete | exit | `{total_filled: "35", remaining_position: "0", fill_count: 2}` |
| N+4 | event_closed | exit | `{reason: "tick_exit"}` |

---

## 四、风控如何利用实时份额

### 4.1 当前风控问题

`_risk_exit()` 的流程：撤买单 → 撤卖单 → 用 `position_shares` 挂卖单清仓。

三处 `position_shares` 可能不准：

| 缺口 | 位置 | 问题 | 后果 |
|---|---|---|---|
| ① 撤买单 | service.py:544 | 买单可能已部分成交 | position_shares 偏小 → 少卖 → 裸仓 |
| ② 撤卖单 | service.py:553 | 卖单可能已部分成交 | position_shares 偏大 → 多卖 → insufficient_balance |
| ③ 清仓 live | service.py:629 | 直接 break 放弃 | 不知道卖出了多少 → 可能有剩余 |

### 4.2 UserWS 实现后的风控流程

有了 UserWS 之后，`position_shares` 在 WS 推送每次成交时实时更新。风控触发时：

**缺口 ① 解决**: 买单已通过 WS 实时追踪，风控触发时 position_shares 已经是准确的。
撤买单时用 `cancel_order_with_fill_check` 做 REST 兜底（防 WS 漏推）。

**缺口 ② 解决**: 卖单同理，WS 实时追踪已卖出量。撤卖单时 REST 兜底。

**缺口 ③ 解决**: 清仓卖单 live 时不再 `break`，改为 `watch_order` + `asyncio.Event` 等待：
- WS 通知全部成交 → 退出循环
- 超时 → `cancel_order_with_fill_check` 撤单 + REST 校准 → 有剩余则继续循环

**改造后完整流程**:

```
风控触发 (_risk_exit)
  │
  ├─ ① 撤买单 (如有)
  │    ├─ unwatch_order (取消 WS 监听)
  │    ├─ cancel_order_with_fill_check (撤单 + REST 查成交)
  │    └─ 校准 position_shares (WS 已追踪大部分，REST 补漏)
  │
  ├─ ② 撤卖单 (如有)
  │    ├─ unwatch_order
  │    ├─ cancel_order_with_fill_check
  │    └─ 校准 position_shares
  │
  ├─ ③ 清仓循环 (while position_shares > 0)
  │    ├─ place_order(SELL, price=0.01, size=position_shares)
  │    ├─ filled → position_shares 减少 → 继续或退出
  │    ├─ partial → 记录即时成交 + watch_order
  │    └─ live → watch_order → 等待 WS 或超时
  │         ├─ WS 全部成交 → position=0 → 退出循环
  │         └─ 超时 → 撤单 + REST 校准 → 有剩余则继续循环
  │
  └─ _close("stop_loss")
```

**风控场景 event 记录示例**:

| seq | step | phase | detail 关键字段 |
|-----|------|-------|----------------|
| N | risk_triggered | exit_risk | `{position_shares: "20", pending_buy: "buy-1"}` |
| N+1 | risk_cancel_buy | exit_risk | `{order_id: "buy-1", success: true, final_matched: "30"}` |
| N+2 | fill_reconcile | exit_risk | `{side: "BUY", clob_matched: "30", memory_before: "20", reconciled: "10"}` |
| N+3 | risk_sell_order_placed | exit_risk | `{order: {size: "30", price: "0.01"}, clob_status: "live"}` |
| N+4 | sell_filled | exit_risk | `{filled_size: "15", remaining_position: "15", source: "ws_order_update"}` |
| N+5 | sell_filled | exit_risk | `{filled_size: "15", remaining_position: "0", source: "ws_order_update"}` |
| N+6 | sell_complete | exit_risk | `{remaining_position: "0"}` |
| N+7 | event_closed | exit_risk | `{reason: "stop_loss"}` |

---

## 五、实施步骤

### Step 1: 撤单 REST 校准 + UserWS 框架组件

> **目标**: 建造两块基础能力 — 撤单后能查到真实成交量 + UserWS 框架可用。
> **耗时**: 4-5h｜**可独立上线**: cancel_order_with_fill_check 部分可以先上

#### 1.1 OrderExecutor 新增 `_get_order()` + `cancel_order_with_fill_check()`

**文件**: `framework/strategy_runtime/order_executor.py`

```python
async def _get_order(self, order_id: str) -> dict:
    """GET /data/order/{orderID}"""
    client = get_client(self._proxy_wallet)
    return await asyncio.to_thread(client.get_order, order_id)

async def cancel_order_with_fill_check(self, order_id: str) -> CancelResult:
    """撤单 + 查询最终成交量。等待订单进入终态后再返回。"""
    cancelled = await self.cancel_order(order_id)
    try:
        info = await self._get_order(order_id)
        # 撤单请求可能还没处理完，订单仍为 LIVE → 等 0.5s 重试一次
        if info.get("status") == "LIVE":
            await asyncio.sleep(0.5)
            info = await self._get_order(order_id)
        return CancelResult(
            order_id=order_id,
            cancelled=cancelled,
            final_matched=Decimal(info.get("size_matched", "0")),
            status=info.get("status", "unknown"),
        )
    except Exception as e:
        logger.warning("get_order after cancel failed: %s", e)
        return CancelResult(order_id=order_id, cancelled=cancelled,
                            final_matched=Decimal("-1"), status="query_failed")
```

**注意**: `size_matched` 格式需实测确认（可能是定点数字符串如 `"60000000"` 表示 60 shares）。

**文件**: `framework/strategy_runtime/interfaces.py`

新增 `CancelResult` 数据类。

#### 1.2 凭证获取

**文件**: `framework/trading/provider.py`

```python
@dataclass(frozen=True)
class ClobCredentials:
    api_key: str
    api_secret: str
    api_passphrase: str

def get_clob_credentials(proxy_wallet: str) -> ClobCredentials:
    client = get_client(proxy_wallet)
    creds = client.creds  # py_clob_client_v2 ApiCreds
    return ClobCredentials(api_key=creds.api_key,
                           api_secret=creds.api_secret,
                           api_passphrase=creds.api_passphrase)
```

如果 `client.creds` 不可访问，回退为从 DB accounts 表读取加密凭证并解密。

#### 1.3 新建 `framework/user_ws.py`

完整实现 UserWS 类（2.2 节设计），包括：
- 连接 + 认证 + 心跳
- watch_order / unwatch_order
- order/trade 事件分发 + 增量计算 + 去重
- 重连补偿（_on_reconnect）
- 全局注册表 get_or_create_user_ws / stop_all_user_ws

#### 1.4 验证

- `cancel_order_with_fill_check`: 手动下单 → 撤单 → 确认返回 size_matched
- UserWS: 连接 → 认证通过 → 下小额测试单 → 确认收到 order/trade 事件
- 重连: 断网 → 重连 → _on_reconnect 触发

**Step 1 完成标志**: `cancel_order_with_fill_check` 能返回准确的 final_matched；UserWS 能稳定接收事件。

---

### Step 2: 策略集成 — 买/卖/风控全路径接入 UserWS

> **目标**: SweepTrade 的 live 订单全部有实时成交追踪，消除所有 TODO。
> **耗时**: 3-4h｜**前置**: Step 1

#### 2.1 OrderExecutor 添加 `ensure_user_ws()`

**文件**: `framework/strategy_runtime/order_executor.py`

```python
async def ensure_user_ws(self) -> UserWS:
    if self._user_ws is None:
        self._user_ws = await get_or_create_user_ws(self._proxy_wallet)
    return self._user_ws
```

#### 2.2 买单入场 — live/partial 注册 WS 监听

**文件**: `strategy_weather_sweep/service.py`，`enter()` 方法末尾（当前 line 273-277）

改造：在启动超时计时器之前注册 WS 监听。
注意：当前 `buy_price` 是 `enter()` 的局部变量，需要先存到实例属性 `self.buy_price = buy_price`。

```python
# partial or live → WS 监听 + 超时计时器
self.buy_price = buy_price  # 保存到实例，供 WS 回调和 reconcile 使用
user_ws = await self._executor.ensure_user_ws()
user_ws.watch_order(
    order_id=result.order_id, token_id=self.token_id, side="BUY",
    price=self.buy_price,
    initial_matched=Decimal(result.filled_size),
    on_fill=self._on_buy_fill,
)
self._entry_timer = asyncio.create_task(self._entry_timeout(entry_wait_ms / 1000.0))
```

新增回调（复用现有 `_record_buy_fill`）：
```python
async def _on_buy_fill(self, event: FillEvent) -> None:
    if self.state == "closed":
        return
    self._record_buy_fill(event.order_id, event.fill_size, event.fill_price,
                          source=event.source, trade_id=event.trade_id)
    if event.total_matched >= self._order_size:
        if self._entry_timer and not self._entry_timer.done():
            self._entry_timer.cancel()
        self.entry_order_id = None
        (await self._executor.ensure_user_ws()).unwatch_order(event.order_id)
        self._record_entry_complete(event.order_id)
```

#### 2.3 买单超时 — unwatch + cancel_with_fill_check 校准

**文件**: `strategy_weather_sweep/service.py`，`_entry_timeout()`（当前 line 319-345）

改造：撤单前先 unwatch，撤单后用 REST 校准兜底。

```python
if self.entry_order_id:
    user_ws = await self._executor.ensure_user_ws()
    user_ws.unwatch_order(self.entry_order_id)
    cancel_result = await self._executor.cancel_order_with_fill_check(self.entry_order_id)
    self.entry_order_id = None
    # REST 校准兜底
    if cancel_result.final_matched > 0 and cancel_result.final_matched > self.position_shares:
        missed = cancel_result.final_matched - self.position_shares
        # fill_price 用委托价 self.buy_price（限价单成交价 ≤ 挂单价）。
        # REST get_order 只返回 size_matched 不返回加权均价，无法拿到精确成交价。
        # 限价单实际成交价可能略优于挂单价，但差异极小，reconcile 场景下可接受。
        self._record_buy_fill(self.entry_order_id, missed, self.buy_price,
                              source="cancel_reconcile")
        if self._el:
            self._el.log_step("fill_reconcile", {
                "side": "BUY", "order_id": self.entry_order_id,
                "clob_matched": str(cancel_result.final_matched),
                "memory_before": str(self.position_shares - missed),
                "reconciled": str(missed),
            }, phase="entry")
```

#### 2.4 卖单退出 — live 注册 WS 监听 + Event 等待

**文件**: `strategy_weather_sweep/service.py`，`_tick_exit()`（当前 line 466-468，替换 TODO）

注意：与 `buy_price` 同理，当前 `sell_price` 是 `_tick_exit()` 的局部变量，需要先存到实例属性 `self.sell_price = sell_price`。

```python
# live → WS 监听 + 等待全部成交或超时
self.sell_price = sell_price  # 保存到实例，供 WS 回调和 reconcile 使用
sell_start_shares = self.position_shares  # 保存本轮卖出前的持仓，用于撤单后校准
user_ws = await self._executor.ensure_user_ws()
sell_done = asyncio.Event()
self._sell_done_event = sell_done

user_ws.watch_order(
    order_id=result.order_id, token_id=self.token_id, side="SELL",
    price=self.sell_price,
    initial_matched=Decimal(result.filled_size),
    on_fill=self._on_sell_fill,
)

try:
    remaining = deadline - time.monotonic()
    await asyncio.wait_for(sell_done.wait(), timeout=max(remaining, 0))
except asyncio.TimeoutError:
    user_ws.unwatch_order(result.order_id)
    cancel_result = await self._executor.cancel_order_with_fill_check(result.order_id)
    # REST 校准卖出量（与买单 2.3 同模式，fill_price 用 self.sell_price）
    sold_by_clob = cancel_result.final_matched
    sold_by_memory = sell_start_shares - self.position_shares  # 已记录的卖出量
    if sold_by_clob > sold_by_memory:
        missed = sold_by_clob - sold_by_memory
        self._record_sell_fill(result.order_id, missed, self.sell_price,
                               source="cancel_reconcile")
    continue

# 全部成交
user_ws.unwatch_order(result.order_id)
self._record_sell_complete(result.order_id, sell_fill_count, sell_placed_ms)
break
```

回调（复用现有 `_record_sell_fill`）：
```python
async def _on_sell_fill(self, event: FillEvent) -> None:
    if self.state == "closed":
        return
    self._record_sell_fill(event.order_id, event.fill_size, event.fill_price,
                           source=event.source, trade_id=event.trade_id)
    if self.position_shares <= 0 and hasattr(self, '_sell_done_event'):
        self._sell_done_event.set()
```

#### 2.5 风控退出 — 三处缺口全部修复

**文件**: `strategy_weather_sweep/service.py`，`_risk_exit()`

**缺口 ①②**: 撤买单/卖单改用 `unwatch + cancel_order_with_fill_check + REST 校准`（与 2.3 同模式）。

**缺口 ③**: 清仓 live 改用 `watch_order + asyncio.Event + 超时` 等待（与 2.4 同模式），
替换当前的 `break`。超时后撤单 + REST 校准，有剩余则 `continue` 继续循环。

#### 2.6 force_exit — 同步改造

**文件**: `strategy_weather_sweep/service.py`，`_force_exit()`（当前 line 674-690）

当前 force_exit（配置禁用时触发）也用 `cancel_order()` 撤单，同样需要改造：
- 撤单前 `unwatch_order`
- 撤单改用 `cancel_order_with_fill_check`
- 校准 `position_shares`（force_exit 不主动清仓，但准确的 position_shares 需要记录到 event_closed）

#### 2.7 _close() 兜底 unwatch

**文件**: `strategy_weather_sweep/service.py`，`_close()` 方法

各退出路径（超时、风控、force_exit）都手动调用了 `unwatch_order`，但异常路径可能遗漏。
在 `_close()` 中加一个兜底，确保该 trade 的所有 watched orders 都被清理：

```python
def _close(self, reason: str):
    # 兜底：清理所有可能遗留的 WS 监听
    user_ws = getattr(self._executor, '_user_ws', None)
    if user_ws:
        for oid in (self.entry_order_id, self.exit_order_id):
            if oid:
                user_ws.unwatch_order(oid)
    # ... 现有 _close 逻辑
```

即使 order 已被 unwatch，重复调用 `unwatch_order` 是幂等的（order_id 不在 `_watches` 中则静默忽略）。

#### 2.8 app.py 生命周期

**文件**: `strategy_weather_sweep/app.py`

```python
from framework.user_ws import stop_all_user_ws

# lifespan finally:
await pool.stop()
await stop_all_user_ws()
await orderbook_ws.stop()
```

#### 2.9 清理 TODO + 验证

删除 service.py 中两处 TODO 注释。

验证场景：
1. 买单 live → WS 推送成交 → position_shares 实时更新 → 全部成交自动进入 exit
2. 买单 live → 超时 → unwatch + 撤单 + REST 校准
3. 卖单 live → WS 推送成交 → 全部成交 → 正常关闭
4. 卖单 live → 超时 → 撤单 + 重试
5. 风控触发 → 撤买单（校准）→ 清仓卖单 live → WS 成交 → 关闭
6. WS 断连 → 重连 → REST 补偿 → 仓位一致

**Step 2 完成标志**: 两处 TODO 清除，所有 live 订单路径都有实时成交追踪和校准。

---

### Step 3: 可观测性 & 收尾

> **目标**: 健康检查、日志规范、文档更新。
> **耗时**: 1-1.5h｜**前置**: Step 2

#### 3.1 UserWS 健康状态

**文件**: `framework/user_ws.py`

```python
def snapshot(self) -> dict:
    return {
        "proxy_wallet": self._proxy_wallet[:8],
        "connected": self._ws is not None,
        "watched_orders": len(self._watches),
        "reconnect_count": self._reconnect_count,
    }
```

#### 3.2 /health 端点

**文件**: `strategy_weather_sweep/app.py`

```python
@app.get("/health")
async def health():
    base = {"strategy": "SweepStrategy", **pool.health()}
    from framework.user_ws import _instances
    user_ws = {addr[:8]: ws.snapshot() for addr, ws in _instances.items()}
    if user_ws:
        base["user_ws"] = user_ws
    return base
```

#### 3.3 日志规范

统一前缀 `[UserWS:<wallet8>]`：
```
[UserWS:0xabcd12] Connected
[UserWS:0xabcd12] Order UPDATE: order=xxx matched=60/100
[UserWS:0xabcd12] Disconnected, reconnect in 2s
[UserWS:0xabcd12] Reconnect reconcile: order=xxx delta=10
```

#### 3.4 文档更新

- 更新 `doc/2026-08-28-event-log-storage-design.md` — 补充 `fill_reconcile`、`reconnect_reconcile` step 定义
- 更新 `doc/2026-08-27-天气Sweep策略详细实现说明.md` — 成交追踪章节

**Step 3 完成标志**: /health 展示 UserWS 状态，event log 中成交来源可追溯。

---

### 实施总览

```
Step 1 (4-5h)
  ├─ cancel_order_with_fill_check     ← 可独立先上线
  ├─ 凭证获取
  └─ UserWS 框架组件

Step 2 (3-4h)
  ├─ 买单 WS 监听 + 超时校准
  ├─ 卖单 WS 监听 + 超时校准
  └─ 风控三处缺口修复

Step 3 (1-1.5h)
  ├─ 健康检查
  ├─ 日志规范
  └─ 文档更新

总耗时: 8-10.5h
```

### 文件变更清单

| 文件 | 操作 | Step |
|------|------|------|
| `framework/user_ws.py` | 新建 | 1 |
| `framework/trading/provider.py` | 修改: 添加 `get_clob_credentials()` | 1 |
| `framework/strategy_runtime/order_executor.py` | 修改: `_get_order` + `cancel_order_with_fill_check` + `ensure_user_ws` | 1, 2 |
| `framework/strategy_runtime/interfaces.py` | 修改: 添加 `CancelResult` | 1 |
| `strategy_weather_sweep/service.py` | 修改: 买/卖/风控/force_exit WS 集成 | 2 |
| `strategy_weather_sweep/app.py` | 修改: lifespan 关闭 UserWS + /health | 2, 3 |
