# Taker 价格检测机制

## 背景

跟单系统需要判断 leader 的订单是 taker（吃单）还是 maker（挂单），以决定 follower 的跟单策略：
- **leader 是 taker** → follower 直接用 best_ask/best_bid 下单，追求立即成交
- **leader 是 maker** → follower 在 leader_price ± tick_size 处挂单

## 判断方法

核心逻辑：**如果 leader 的成交价格不是 tick_size 的整数倍，则为 taker。**

```python
@staticmethod
def _is_taker_price(price: float, tick_size: str) -> bool:
    return Decimal(str(price)) % Decimal(tick_size) != 0
```

### 原理

Polymarket 的 maker 挂单价格必须是 tick_size 的整数倍（如 tick_size=0.01 时，价格为 0.50, 0.51, 0.52...）。

taker 成交后会被收取手续费，导致链上实际支付/收到的金额偏离整数 tick 价格：
- BUY taker 手续费 = `(1 - price) × fee_rate`
- SELL taker 手续费 = `price × fee_rate`

因此 taker 的等效成交价（usdc/shares）带有手续费残留小数，不再是 tick_size 的整数倍。

## 信号来源差异

### RTDS 来源
RTDS WebSocket 推送的价格直接来自订单簿，精度由订单本身决定，不含手续费。判断可靠。

### Chain 来源
链上信号的价格通过 `price = usdc_amount / shares_amount` 反算，包含手续费影响。

关键处理：chain.py 中 price 保留 **5 位小数**（`round(price, 5)`）：
- 不能不 round：浮点除法会产生如 `0.9949999...` 的尾巴，导致本该对齐 tick 的 maker 价格被误判
- 不能 round 到 4 位：手续费差异可能出现在第 4-5 位，round(4) 会抹掉 taker 特征
- round 到 5 位：既消除浮点噪声，又保留手续费残留信号

### 示例

tick_size = 0.001:

| 场景 | 原始价格 | chain price (round 5) | % tick_size | 判定 |
|------|---------|----------------------|-------------|------|
| maker 成交 @ 0.995 | 0.995 | 0.995 | 0 | maker |
| taker 成交 @ 0.979 (含手续费) | 0.980027... | 0.98003 | ≠ 0 | taker |
| maker 成交 @ 0.98 | 0.98 | 0.98 | 0 | maker |

## 跟单价格策略

```python
def _buy_follow_price_role(leader_price, tick_size, best_ask, has_best_ask):
    # 1. taker → 直接用 best_ask 吃单
    if _is_taker_price(leader_price, tick_size) and has_best_ask:
        return best_ask, "taker"
    # 2. maker → leader_price + tick_size 挂单
    maker_price = min(leader_price + tick_size, 1 - tick_size)
    # 3. 如果 maker 价格已经 >= best_ask，不如直接吃
    if has_best_ask and maker_price >= best_ask:
        return best_ask, "taker"
    return maker_price, "maker"
```

SELL 侧对称：taker 用 best_bid，maker 用 leader_price - tick_size。

## 局限性

chain 来源存在极端边界：如果 taker 手续费恰好使 price round 到 5 位后成为 tick 整数倍（概率极低），会被误判为 maker。此时 follower 会以 maker 方式跟单（leader_price ± tick_size），不影响正确性，只是成交速度可能慢一拍。
