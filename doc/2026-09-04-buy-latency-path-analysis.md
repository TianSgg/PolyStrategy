# BUY 下单延迟路径分析

> 目的: 解释从收到信号到 BUY 下单成功之间，当前代码里还有哪些延迟点，以及 `wait_for_first_bbo()` 为什么会把“成功时间”拉晚。

## 1. 先区分两个时间

### 1.1 真实发单时间
指 BUY 请求真正进入 `place_order()`，最终发到 CLOB 的时间。

### 1.2 前端看到的成功时间
指 event 里 `buy_order_placed` 这条 step 被写入的时间。

这两个时间不一定相同。当前代码里，`buy_order_placed` 不是“请求刚发出”就记，而是等了更多前置流程后才记录。

## 2. 收到信号到创建 event 的前置流程

信号进入策略后，先经过这些步骤：

1. WS 收到原始消息
2. `WeatherSweepAdapter` 转成统一 `Signal`
3. `TTLDeduplicator` 去重
4. `SweepStrategy.on_signal()` 做过滤
5. 创建 `EventLogger`
6. `trade.enter(signal)` 才开始真正的 event 生命周期

这段主要是内存逻辑，但因为是串行 `await`，仍然会有少量协程调度开销。

## 3. BUY 下单前还会发生什么

在 `enter()` 里，当前仍可能有这些前置开销：

1. `signal_received` 写 event
2. `_insert_trade_summary(signal)` 写 trade 摘要
3. 计算可买份额
4. 创建 `order_task`
5. 启动 `risk.start()`
6. 等 `wait_for_first_bbo()`

其中最值得注意的是：

### 3.1 `_insert_trade_summary(signal)` 是同步 DB 写入
这一步会直接做数据库 insert，不像 `log_step()` 那样丢线程池。

这意味着它会卡在 BUY 入口路径上，属于一个真实的前置延迟点。

### 3.2 `wait_for_first_bbo()` 会拖晚成功事件的记录
它本身不是下单请求的一部分，但当前代码会在：

```python
await risk_task
await self.risk.wait_for_first_bbo()
result = await order_task
```

这个位置等待风控拿到首个 BBO 快照。

## 4. `wait_for_first_bbo()` 到底在等什么

风控启动时会：

1. 订阅订单簿 WS
2. 尝试抓首个 BBO
3. 如果还没有，就等后续 BBO 更新

内部是一个 `asyncio.Event`：

```python
await asyncio.wait_for(self._first_bbo_event.wait(), timeout=2.0)
```

所以它的作用不是“等买单成交”，而是“等风控接到第一份可用的订单簿快照”。

### 4.1 它为什么会让成功时间变晚
因为 `buy_order_placed` 的记录放在这之后。

结果就是：

- 买单请求可能已经在后台发出
- 但 event 里的成功 step 还没写
- 前端看到的时间就会被拉晚

## 5. BUY 阶段当前仍可能有的延迟点

按影响大小排：

1. `_insert_trade_summary(signal)` 的同步 DB 写
2. `wait_for_first_bbo()` 带来的展示延迟
3. `asyncio.to_thread(place_limit_order, ...)` 的线程调度开销
4. `create_and_post_order()` 的本地签名 + payload 生成 + HTTP POST
5. CLOB 网络往返和服务端处理
6. `retry_on_error=True` 时的内部重试
7. 信号分发链路的串行 `await`

## 6. 推荐改法

### 6.1 把“发单时间”和“展示时间”拆开
建议至少拆成两种 step：

- `buy_order_sent`
- `buy_order_placed`

这样能区分：

- 是真正发单慢
- 还是只是后面的 BBO 等待把展示时间拖晚了

### 6.2 把 trade 摘要改成后台写，或挪到下单之后
如果目标是压 BUY 首单延迟，`_insert_trade_summary(signal)` 不该卡在下单前。

### 6.3 `wait_for_first_bbo()` 与事件记录
更合理的做法是：

- `order_response_at_ms` 记录真实 BUY 响应时间，不受后续等待影响
- `pre_bbo` / `aft_bbo` 按约定嵌入 `buy_order_placed`
- `risk_started` 合并记录风控启动和首个 BBO 的 `ready` / `timeout` 状态

## 7. 结论

当前 BUY 的主要延迟，不在 tick 校验了，重点是：

- 同步 trade 摘要写库
- `wait_for_first_bbo()` 把成功事件记录往后拖
- CLOB 下单本身的签名和 HTTP 往返

如果要继续压延迟，优先级建议是：

1. 拆分成功时间和展示时间
2. 异步化 trade 摘要写入
3. 保留必要的订单簿快照，但不要阻塞 BUY 成功事件
