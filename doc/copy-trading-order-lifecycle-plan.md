# 跟单订单生命周期检查计划

## 背景

Polymarket CLOB 里所有订单本质都是 limit order。当前代码使用
`create_and_post_order(..., OrderType.GTC)`，也就是 GTC 限价单：指定
`token_id`、`side`、`size`、`price` 后，只会按该价格或更优价格成交。

如果限价单 crossing：

- BUY `price >= best ask`，或 SELL `price <= best bid`，就可能立刻撮合。
- 入口返回可能是 `matched`，也可能在 sports market 进入 `delayed`。
- taker 享受价格改善：BUY 出价 0.55 但 best ask 是 0.52，实际成交价按 0.52。

如果限价单不 crossing：

- 入口返回 `live`，订单挂在 book 上。
- 后续可能被部分成交、多次成交，直到全成、取消、过期。
- open order 的未成交量是 `original_size - size_matched`。

跟单服务因此需要保持四本账一致：

- follower 持仓
- pending buy/sell 订单
- config allowance
- buy/sell share debt

本文档用于按路线逐条检查这些流程。

## 共同假设

- 当前代码使用 `create_and_post_order`，默认 `OrderType.GTC`。
- 文档里 market order 的 BUY `amount` 是 USDC，SELL `amount` 是 shares；但当前代码使用的是 GTC limit order 的 `OrderArgs.size`，BUY 和 SELL 都按 shares 理解。
- `live` 表示订单挂在订单簿上，应锁 pending。
- `matched` 表示订单立即成交，应直接更新 position，不新增 pending；后续 `trade CONFIRMED` 由 `_processed_matched_order_ids` 去重。
- `delayed` 表示订单进入延迟撮合，行为上先按 pending 处理，等待 user-channel 的 order/trade 事件回补。
- position 变化以 `trade CONFIRMED` 为最终确认源；当前代码对入口 `matched` 做乐观本地入账，并跳过对应 confirmed trade，避免重复入账。
- `order PLACEMENT/LIVE` 表示新挂单出现。程序自己刚下的 `live` 单已本地加 pending，所以 WS `PLACEMENT` 必须去重。
- `order CANCELLATION` 只释放未成交部分：`original_size - size_matched`。
- `order UPDATE` 只更新订单 `size_matched` / `status`，不直接改 position 或 pending。
- `trade CONFIRMED` 是最终确认后的成交增量：更新 position，同时释放 pending。

## 检查路线

### 1. BUY 返回 live

- 确认 allowance 只扣一次。
- 确认 `pending_buy` 按订单 size 增加。
- 确认本程序订单的 WS `PLACEMENT` 会被去重。
- 确认后续 `trade CONFIRMED` 会增加 position 并释放 pending。
- 确认 `CANCELLATION` 会释放 pending 并返还未使用 allowance。

### 2. BUY 返回 matched

- 确认 allowance 只扣一次。
- 确认 follower position 立即增加。
- 确认不创建 pending。
- 确认后续 `trade CONFIRMED` 会被去重，不会重复加仓。

### 3. BUY 返回 delayed

- 确认 allowance 只扣一次。
- 确认 delayed 期间 `pending_buy` 增加。
- 确认后续 `PLACEMENT`、`UPDATE`、`CONFIRMED` 事件能推进状态。
- 确认成交前取消会返还未使用 allowance。

### 4. BUY 返回 error

- 确认 allowance 不扣减。
- 确认不创建 pending 或 position。
- 确认 buy debt 与预期重试行为一致。
- 确认 error 订单记录和通知准确。

### 5. SELL 返回 live

- 确认可卖 shares 按 position 减 pending sell 计算。
- 确认挂单时 allowance 不变化。
- 确认 `pending_sell` 按订单 size 增加。
- 确认本程序订单的 WS `PLACEMENT` 会被去重。
- 确认后续 `trade CONFIRMED` 会减少 position、释放 pending，并返还 allowance。

### 6. SELL 返回 matched

- 确认 follower position 立即减少。
- 确认 allowance 只返还一次。
- 确认不创建 pending。
- 确认后续 `trade CONFIRMED` 会被去重，不会重复减仓或重复返还 allowance。

### 7. SELL 返回 delayed

- 确认 delayed 期间 `pending_sell` 增加。
- 确认后续 order/trade 事件能正确推进状态。
- 确认成交确认后会减少 position 并返还 allowance。
- 确认 retry-delayed 订单使用 retry size 计算 pending。

### 8. SELL error 和余额不足 retry

- 确认原始失败尝试不改变 pending 或 position。
- 确认 retry size 基于实际余额。
- 确认 retry 的 `live`、`matched`、`delayed` 路线都一致使用 retry size。
- 确认 sell debt 只按实际下单 size 抵扣。

### 9. SELL size 为 0

- 确认仍然记录 leader SELL 信号日志。
- 确认不提交 0 size 订单。
- 确认 sell debt 按当前 debt 算法持久化。
- 确认不产生误导性的订单记录或通知。

### 10. WS order CANCELLATION

- BUY：释放 pending，并返还未成交部分的 allowance。
- SELL：只释放 pending。
- 确认重复 cancellation 事件会被去重。
- 确认优先使用运行时订单缓存，再兜底查 DB。
- 确认服务重启或缓存丢失后 DB 兜底可用。

### 11. WS trade CONFIRMED

- BUY：增加 position，并释放 pending。
- SELL：减少 position，释放 pending，并返还 allowance。
- 确认立即 `matched` 的订单会被去重。
- 确认 delayed 订单会更新 DB `size_matched` 和最终 status。

### 12. Poller 全量同步

- 确认 follower position sync 只在 API 响应和解析成功后覆盖内存。
- 确认 pending sync 不会与本地下单或事件更新互相覆盖。
- 确认 leader position sync 不会与 signal 增量更新冲突。
- 确认 DB 全量替换 helper 能正确表达“成功同步到空结果”。

### 13. Allowance 总账

- BUY 的 `live`、`delayed`、`matched` 都会扣 allowance。
- BUY cancellation 会返还未使用 allowance。
- SELL confirmed/matched 成交会返还 allowance。
- 重复 WS 事件不能造成 allowance 重复变化。
- threshold 更新应保持预期的 allowance 语义。

### 14. Share debt 总账

- BUY debt：小额意图买入会累计，实际下单 size 会抵扣 debt。
- SELL debt：小额意图卖出会累计，min-size 预支可能形成负 debt。
- 失败订单应让 debt 保持在预期可重试状态。
- 零余额 SELL 应持久化 debt，但不能提交订单。

### 15. 订单历史 DB

- 程序订单和手动订单应能区分。
- `follow_size`、`follow_price`、`size_matched`、`status` 应匹配各自订单路线。
- `UPDATE`、`CANCELLATION`、`CONFIRMED` 应推进记录状态，且不破坏原始订单元数据。

## 建议检查顺序

1. BUY `live`
2. SELL `live`
3. BUY `matched`
4. SELL `matched`
5. BUY/SELL `delayed`
6. error 和 retry 路线
7. poller 和账务总账
