# Order 记录逻辑（copy_trading_orders 表）

三种入场场景会产生不同的 order 记录：

## 情况1：Sweep 先到 → Leader 后确认

产生 2 条记录：

### Sweep 订单（sweep 信号到达，下单入场）

| 字段 | 值 |
|------|-----|
| leader_tx_hash | `"WEATHER_SWEEP"` |
| leader_size | 0 |
| leader_price | 0 |
| follow_size | buy_size |
| follow_price | 0.99 |
| size_matched | 实际成交量 |
| status | LIVE/MATCHED/... |
| sweep_to_leader_ms | leader 确认后回填 |

### Leader 确认记录（leader 信号到达，确认不退场，不下新单）

| 字段 | 值 |
|------|-----|
| leader_tx_hash | 真实 tx hash |
| leader_size | signal.size |
| leader_price | signal.price |
| follow_size | 0 |
| follow_price | 0.99 |
| size_matched | 0 |
| status | `LEADER_CONFIRM` |
| order_id 前缀 | `LEADER_CONFIRM_0x` |

## 情况2：Leader 先到 → 走跟单逻辑

产生 1 条记录：

| 字段 | 值 |
|------|-----|
| leader_tx_hash | 真实 tx hash |
| leader_size | signal.size |
| leader_price | signal.price |
| follow_size | buy_size |
| follow_price | 0.99 |
| size_matched | 实际成交量 |
| status | LIVE/MATCHED/... |
| signal_latency_ms | 信号到达→下单结果耗时 |

后续 sweep 到来不做动作，状态已是 ACTIVE。

## 情况3：Sweep 到达 → 超时未等到 Leader → 退场

同情况1的 sweep 订单，超时后产生 SELL 订单退场。`sweep_to_leader_ms` 不会被填充。

## 延迟字段区分

| 字段 | 含义 | 适用场景 |
|------|------|---------|
| `signal_latency_ms` | leader 信号路径"收到信号→下单接口返回"耗时 | 仅情况2 |
| `sweep_to_leader_ms` | sweep 路径"sweep 入场→leader 信号到达确认"延迟 | 仅情况1的 sweep 订单 |

## 买卖点图渲染规则

| 记录类型 | Leader 折线（上图） | Follower 折线（下图） | 仓位累计 |
|----------|--------------------|--------------------|---------|
| Sweep 订单 | 不画（leader_price=0→null） | 正常画 @ 0.99 | follower +size_matched |
| LEADER_CONFIRM | 正常画 @ leader_price | 不画（status 判断→null） | leader +leader_size，follower 不变 |
| 跟单订单（情况2） | 正常画 @ leader_price | 正常画 @ 0.99 | 两方同时累加 |
