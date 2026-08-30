# Weather Sweep Tick Size 缓存与 SELL 熔断修复方案

> 日期: 2026-08-30  
> 状态: 已实施（2026-08-31）
> 主题: Warsaw tick size 事故分析、tick size 权威来源设计、SELL 失败熔断与终态语义

实施记录：

1. 本地与策略服务统一使用 vendored `py_clob_client_v2`。
2. `TickSizeService` 已接入 BUY/SELL、tick 验证与风控轮询。
3. SELL 已接入错误分类、事件级熔断与 `invalid_tick_size` refresh 后重试一次。
4. `strategy_weather_sweep_trades.status` 已增加 `exit_failed`，本地 MySQL enum 已同步扩展。
5. `force_exit` 已区分清仓成功与仍有仓位；策略级同一错误跨 event 熔断已接入。

---

## 一、事故背景

### 1.1 涉及交易

| 字段 | 值 |
|---|---|
| event_slug | `highest-temperature-in-warsaw-on-august-30-2026` |
| market_slug | `highest-temperature-in-warsaw-on-august-30-2026-26c` |
| event_id | `cd447222-2477-4498-bcee-12294855dec3` |
| trade_id | `112` |
| token_id | `20022357734336316059735666401349281946071178402343390319568614682835369695917` |
| outcome | `yes` |

本记录的 `occurred_at` 与 detail 中的 `utc` 对齐为 UTC。进程日志时间是北京时间 UTC+8。

### 1.2 时间线

| UTC 时间 | step | 关键信息 |
|---|---|---|
| 15:17:19.910 | `signal_received` | 收到 Warsaw sweep 信号 |
| 15:17:20.917 | `order_placed` | BUY 10 shares @ `0.99` |
| 15:17:28.411 | `buy_filled` | 成交 7 shares |
| 15:17:52.564 | `buy_filled` | 成交 3 shares，累计持仓 10 |
| 15:17:52.570 | `entry_complete` | 买入完成 |
| 15:19:14.788 | `tick_detected` | WS 推送 tick size 为 `0.001` |
| 15:19:15.203 | `tick_verified` | HTTP 验证确认 `0.001` |
| 15:19:15.208 | `sell_order_failed` | SELL 10 @ `0.999`，报 `invalid tick size (0.001), minimum for the market is 0.01` |
| 15:19:15.208 至 15:25:17.231 | 多次 `sell_order_failed` | 同一错误连续出现 11 次 |
| 15:25:32.499 | `force_exit` | 配置被禁用，触发强制退出 |
| 15:25:32.501 | `event_closed` | event 记录关闭，但最终持仓仍是 10 |

当前直接查询 CLOB 也确认该 token 的 tick size 为 `0.001`：

```text
GET /tick-size?token_id=... -> {"minimum_tick_size":0.001}
GET /book?token_id=...     -> tick_size: "0.001"
```

因此，SELL 报错中的 `minimum for the market is 0.01` 不是服务端当时的权威值，而是本地客户端状态。

---

## 二、已确认事实与根因

### 2.1 已确认事实

1. WS 与 HTTP 都确认过 tick size 为 `0.001`。
2. SELL 连续 11 次失败，错误完全相同。
3. 失败 SELL 记录里的 order id 是本地生成的 UUID，不是 CLOB 返回的 `0x...` order hash，说明订单没有真正提交到服务端。
4. 当前本地策略进程实际 import 的 `py_clob_client_v2` 来自：

   ```text
   /Users/liutianshi/anaconda3/envs/RL_class/lib/python3.9/site-packages/py_clob_client_v2
   ```

   而不是仓库中的：

   ```text
   backend/vendor/py-clob-client-v2
   ```

5. `start.sh` 只设置了 `backend/src` 到 `PYTHONPATH`，没有设置 vendor 路径。
6. Docker 构建文件会安装 vendored 包，但本地 `start.sh` 直接使用当前 conda Python，因此本地和容器运行时依赖并不一致。
7. 当前策略最终把 event 记录为 closed，但真实 position 仍为 10，说明 `force_exit` 的终态语义掩盖了未清仓事实。

### 2.2 旧版客户端的本地校验

运行时使用的旧版 `py_clob_client_v2` 中存在以下行为：

```python
def get_tick_size(self, token_id):
    if token_id in self.__tick_sizes:
        return self.__tick_sizes[token_id]

    result = self._get(...)
    self.__tick_sizes[token_id] = str(result["minimum_tick_size"])
    return self.__tick_sizes[token_id]
```

```python
def __resolve_tick_size(self, token_id, tick_size=None):
    min_tick_size = self.get_tick_size(token_id)
    if tick_size and is_tick_size_smaller(tick_size, min_tick_size):
        raise PolyException(
            f"invalid tick size ({tick_size}), minimum for the market is {min_tick_size}"
        )
    return tick_size or min_tick_size
```

这会带来两个问题：

1. `get_tick_size()` 的缓存没有 TTL，也没有失效接口。
2. `create_order()` 会先拿传入 tick size 与本地缓存 minimum 比较，缓存过期时会在本地直接拒绝订单。

### 2.3 根因链

以下是高置信推断，依据是代码路径、错误文本与运行时 import 路径。

```text
BUY 时市场 tick = 0.01
    ↓
交易侧 ClobClient 在 create_order 时缓存 token tick = 0.01
    ↓
两分钟后 WS 推送 tick = 0.001
    ↓
TickVerifier 使用另一个 MarketData / ClobClient 做 HTTP 验证
    ↓
验证侧拿到 0.001，记录 tick_verified
    ↓
SELL 仍通过交易侧 ClobClient 下单，并传入 tick_size=0.001
    ↓
交易侧本地缓存仍是 0.01
    ↓
旧版 __resolve_tick_size 判断 0.001 < 0.01
    ↓
本地抛出 invalid tick size，请求未发出
    ↓
重试复用同一个交易 client，缓存不刷新
    ↓
同一错误重复 11 次
```

### 2.4 设计层面的四个问题

#### 问题 1: 运行时依赖不一致

仓库 vendor 代码与实际运行代码不同。只修改仓库 vendor 不一定能影响本地进程。

#### 问题 2: tick size 有多套状态

当前至少存在以下互相独立的状态：

| 位置 | 状态 |
|---|---|
| `SweepTrade._tick_size` | 策略内部 tick |
| `TickVerifier -> MarketData._tick_size_cache` | 验证侧缓存 |
| `SweepRiskMonitor -> MarketData._tick_size_cache` | 风控侧缓存 |
| 交易侧 `ClobClient.__tick_sizes` | 下单客户端缓存 |
| account service 的 client 池 | 每个 wallet 一个 ClobClient |

HTTP 验证成功无法让交易侧缓存同步失效。

#### 问题 3: SELL 重试没有错误分类和熔断

`invalid tick size` 是确定性参数错误。当前逻辑将它当作普通失败进行指数退避重试，导致同一个错误持续 6 分钟。

#### 问题 4: event 终态掩盖真实仓位

`force_exit` 最终调用 `_close()`，即使 position 没有清掉，也会把 event 记录为 closed。数据库状态与真实仓位不一致。

---

## 三、修复目标

1. 本地与容器必须运行同一份 `py_clob_client_v2`。
2. tick size 只允许有一个进程内权威来源。
3. WS 只负责触发变更，HTTP refresh 后的值才是权威值。
4. SELL 前必须使用最新 tick size 计算价格。
5. 确定性错误不允许无限或长时间重复。
6. event 停止与仓位清零必须分开表达。
7. 失败时必须留下足够的信息用于排查和人工处理。

---

## 四、修复方案

## 4.1 P0: 统一运行时依赖

### 改动点

1. `start.sh` 将 vendored client 路径放在 `backend/src` 之前：

   ```text
   PYTHONPATH=$BACKEND_DIR/vendor/py-clob-client-v2:$BACKEND_DIR/src
   ```

2. 策略服务启动时记录并校验：

   ```python
   import py_clob_client_v2
   logger.info("py_clob_client_v2: %s", py_clob_client_v2.__file__)
   ```

3. 如果 import 路径不在 `backend/vendor/py-clob-client-v2` 下，服务直接启动失败。

4. Docker 镜像重建后确认同样使用 vendored 包。

### 验收

启动日志中的 `py_clob_client_v2.__file__` 必须指向仓库 vendor 目录。错误路径直接阻止服务启动。

---

## 4.2 P0: 新增统一 TickSizeService

新增进程级服务：

```text
backend/src/framework/strategy_runtime/tick_size_service.py
```

接口建议：

```python
class TickSizeService:
    async def get(
        self,
        token_id: str,
        *,
        max_age_ms: int | None = None,
    ) -> Decimal:
        ...

    async def refresh(self, token_id: str) -> Decimal:
        ...

    def invalidate(self, token_id: str) -> None:
        ...
```

### 设计规则

1. HTTP 直接调用 CLOB `/tick-size`，不经过 `ClobClient.get_tick_size()`。
2. cache 只作为短 TTL 缓存，建议 5 到 10 秒。
3. `refresh()` 强制绕过缓存。
4. WS 收到 `tick_size_change` 后调用 `invalidate(token_id)`。
5. HTTP refresh 成功后的值是权威值。
6. HTTP refresh 失败时，SELL 不应使用旧 tick 继续下单，应进入等待或失败处理。
7. 服务内加锁，避免同一 token 并发重复请求。
8. HTTP 请求设置超时与有限重试。

### 状态流

```text
WS tick_size_change
    ↓
TickSizeService.invalidate(token)
    ↓
HTTP refresh(token)
    ↓
权威 tick = 0.001
    ↓
SELL price = 1 - 0.001 = 0.999
```

---

## 4.3 P0: 所有使用方接入同一个 TickSizeService

| 模块 | 改动 |
|---|---|
| `TickVerifier` | 不再 new `MarketData`，改为注入 `TickSizeService` |
| `SweepRiskMonitor` | tick 轮询和 WS 检测统一使用 `TickSizeService` |
| `OrderExecutor` | 下单前通过 `TickSizeService` 解析 tick，不默认 `0.01` |
| `SweepTrade` | `self._tick_size` 更新来源改为 service 权威值 |
| app startup | 创建进程级 singleton 并注入相关组件 |

### 下单规则

1. BUY 与 SELL 都使用同一个 service。
2. SELL 前强制 `refresh(token_id)`。
3. sell price 必须由 refresh 后的 tick 计算。
4. 如果调用方传入 tick，必须与 service 权威值一致；不一致时以 service 为准或直接拒绝。

---

## 4.4 P0: 修复 invalid tick size 重试

### 错误处理

遇到 `invalid tick size` 时：

```text
停止当前参数重试
    ↓
TickSizeService.refresh(token)
    ↓
重新计算 sell price
    ↓
最多重试一次
    ↓
仍失败则进入 exit_failed
```

不允许将同一 tick 参数继续指数退避重试。

### 原因

这类错误通常是本地市场参数状态不一致。继续重试同一参数无法自愈，只会延长风险暴露时间。

---

## 4.5 P1: 抽取公共 SELL 退出控制器

当前 normal exit 与 risk exit 各自维护 SELL 重试循环，容易产生行为差异。

新增公共控制器，例如：

```text
backend/src/strategy_weather_sweep/internal/sell_exit_controller.py
```

统一负责：

1. tick refresh；
2. sell price 计算；
3. 下单；
4. User WS fill 追踪；
5. 撤单；
6. 失败分类；
7. 熔断；
8. position reconcile；
9. event 记录。

normal exit 与 risk exit 只传入不同的 trigger 和风控参数。

---

## 4.6 P1: event 级熔断

### 阈值建议

| 条件 | 阈值 | 动作 |
|---|---|---|
| 连续失败次数 | 5 次 | 停止继续下单 |
| 同一 error signature 连续出现 | 3 次 | 停止继续下单 |
| SELL 阶段总时长 | 180 秒 | 停止继续下单 |
| position 无减少且持续失败 | 满足以上任一条件 | 停止继续下单 |
| `invalid tick size` | refresh 后重试 1 次仍失败 | 立即停止 |

error signature 建议先使用归一化错误类型，而不是完整错误字符串：

```text
invalid_tick_size
network_timeout
insufficient_balance
authentication_error
invalid_order_params
unknown_api_error
```

### 熔断前清理

1. 如果存在 live SELL order，先撤单。
2. 查询订单最终成交量。
3. 查询或 reconcile 真实 position。
4. 记录最终 position。
5. 只有 position 为 0 才能标记为清仓成功。

---

## 4.7 P1: 显式失败终态

当前 `strategy_weather_sweep_trades.status` 只有：

```text
entry_working
exit_working
closed
```

建议增加：

```text
exit_failed
```

### 状态含义

| status | 含义 |
|---|---|
| `entry_working` | 买入流程进行中 |
| `exit_working` | 卖出流程进行中 |
| `closed` | 流程结束且真实仓位为 0，或没有仓位需要处理 |
| `exit_failed` | 卖出流程失败终止，可能仍有真实仓位 |

`exit_failed` 是终态，但不是清仓成功。

### force_exit 语义

`force_exit` 不应无条件调用 `_close()`。建议：

```text
force_exit
    ↓
撤未完成订单
    ↓
尝试清仓
    ↓
position = 0 -> closed
position > 0 -> exit_failed
```

detail 中记录：

```json
{
  "reason": "config_disabled",
  "position_open": true,
  "position_shares": "10",
  "manual_action_required": true
}
```

---

## 4.8 P1: 全局熔断

如果多个 event 在短时间内出现同一系统性错误，应暂停整个策略配置，而不是让每个 event 独立失败。

建议规则：

```text
5 分钟内 >= 3 个 event 出现同一 error signature
    ↓
暂停当前 strategy config
    ↓
记录 strategy_paused
    ↓
通知用户或标记人工处理
```

该规则用于防止以下系统级问题扩散：

1. 运行时依赖错误；
2. tick cache 污染；
3. 网络长时间异常；
4. API 凭证异常；
5. 客户端版本不兼容。

---

## 4.9 P2: event 记录增强

### tick_verified

```json
{
  "ws_tick_size": "0.001",
  "http_tick_size": "0.001",
  "confirmed": true,
  "source": "http",
  "utc": "2026-08-30T15:19:15.203"
}
```

### tick_refreshed

```json
{
  "reason": "invalid_tick_size",
  "old_tick_size": "0.01",
  "new_tick_size": "0.001",
  "source": "http",
  "utc": "..."
}
```

### sell_retry_exhausted

```json
{
  "attempt_count": 3,
  "consecutive_same_error": 3,
  "error_signature": "invalid_tick_size",
  "last_error": "invalid tick size (0.001), minimum for the market is 0.01",
  "elapsed_ms": 45000,
  "position_shares": "10",
  "action": "stop_event",
  "utc": "..."
}
```

### exit_failed

```json
{
  "reason": "sell_retry_exhausted",
  "position_shares": "10",
  "position_open": true,
  "manual_action_required": true,
  "utc": "..."
}
```

### position_reconcile

```json
{
  "source": "clob_order_or_data_api",
  "order_id": "...",
  "matched_size": "0",
  "position_shares": "10",
  "utc": "..."
}
```

---

## 五、实施清单

### 阶段 1: P0 止血

1. 修改 `start.sh` 的 `PYTHONPATH`。
2. 增加启动时 vendor 包路径校验。
3. 新增 `TickSizeService`。
4. 接入 `TickVerifier`。
5. 接入 `SweepRiskMonitor`。
6. 接入 `OrderExecutor`。
7. SELL 前强制 refresh tick。
8. `invalid tick size` 改为 refresh 后最多重试一次。

### 阶段 2: P1 行为收敛

1. 抽取公共 SELL 退出控制器。
2. 增加 event 级熔断。
3. 增加撤单与 position reconcile。
4. 增加 `exit_failed` 状态。
5. 修改 `force_exit` 终态语义。
6. 增加全局熔断。

### 阶段 3: P2 观测与验证

1. 补充 event detail 字段。
2. 补充单元测试与集成测试。
3. 前端展示 `exit_failed` 和 `position_open`。
4. 手动 reconcile Warsaw 遗留仓位与数据库记录。

---

## 六、测试计划

### 6.1 运行时依赖测试

1. 启动服务时检查 `py_clob_client_v2.__file__`。
2. 如果 import 自 site-packages，服务必须启动失败。
3. Docker 与本地启动日志中的包路径一致。

### 6.2 TickSizeService 测试

1. 首次请求会调用 HTTP。
2. TTL 内命中缓存。
3. TTL 过期后重新请求。
4. `refresh()` 绕过缓存。
5. `invalidate()` 后下次请求强制 HTTP。
6. 并发请求同一 token 时只发出一次 HTTP。
7. HTTP 失败时不返回旧值作为权威值。

### 6.3 SELL 测试

模拟以下场景：

```text
BUY 时 tick = 0.01
SELL 前 HTTP tick = 0.001
交易 client 内部缓存仍是 0.01
```

预期：

1. SELL 前强制 refresh；
2. SELL 使用 tick `0.001`；
3. SELL 价格为 `0.999`；
4. 不因旧缓存本地拒绝；
5. 如果仍报 `invalid tick size`，refresh 后只重试一次；
6. 第二次失败后进入 `exit_failed`。

### 6.4 熔断测试

1. 同一 error signature 连续 3 次触发停止。
2. 连续 5 次失败触发停止。
3. 180 秒 deadline 触发停止。
4. 停止前撤销 live sell order。
5. 停止前记录最终 position。
6. position 不为 0 时不标记为 closed。

### 6.5 force_exit 测试

1. 清仓成功时进入 `closed`。
2. 清仓失败且 position 不为 0 时进入 `exit_failed`。
3. detail 记录 `position_open` 与 `manual_action_required`。
4. 配置禁用不会把未清仓 event 伪装成正常 closed。

---

## 七、非目标

本方案不在第一版中处理：

1. 自动替用户卖出遗留仓位；
2. 修改 Polymarket 服务端行为；
3. 重写整个 account service；
4. 将所有市场参数缓存全部迁移到数据库。

Warsaw 的 10 shares 遗留仓位需要单独人工 reconcile，不应由本修复自动处理。

---

## 八、验收标准

1. 本地与 Docker 均使用 vendored `py_clob_client_v2`。
2. 进程内只有一个权威 tick size 来源。
3. WS tick change 后，HTTP refresh 与 SELL 使用同一个新值。
4. 不再出现 HTTP 已确认 `0.001` 但 SELL 本地认为 minimum 是 `0.01` 的分叉。
5. `invalid tick size` 最多 refresh 后重试一次。
6. 同一错误不会连续重试 11 次。
7. 熔断前完成撤单与 position reconcile。
8. 未清仓的 event 不会被记录为普通 closed。
9. event 表能明确回答：为什么停止、停止时仓位多少、是否需要人工处理。

---

## 九、核心结论

这次事故不是 Polymarket 服务端 tick size 没变，而是本地系统没有统一的市场参数状态。

修复必须同时覆盖四件事：

1. 运行时依赖一致；
2. tick size 单一权威来源；
3. SELL 错误分类与熔断；
4. event 终态真实反映仓位。

只修其中一项都不足以避免同类问题再次发生。
