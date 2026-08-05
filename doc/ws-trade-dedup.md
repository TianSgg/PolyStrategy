# WS Trade 去重机制

## 背景

下单入口 `_place_order` 返回 `matched` 时已计算 `position_delta` 并立即更新仓位和 pending，
但 WS 会随后推送一条相同成交的 trade CONFIRMED 消息。如果不去重，仓位会被重复计算。

旧方案使用 `Set[str]` 按 order_id 全量屏蔽，对**全部成交**有效，但**部分成交**场景下
同一个 order_id 后续可能有第二笔 trade（剩余挂单被吃），全量屏蔽会导致后续 trade 丢失。

## 设计

```python
self._order_post_filled: Dict[str, float] = {}
# key: order_id, value: 剩余需跳过的成交量
```

### 写入时机

`_register_post_order_result` 中，只要 `position_delta > 0` 就记录：

```python
if result.position_delta > 0:
    self._order_post_filled[order_id] = result.position_delta
```

覆盖两种场景：
- 全部成交：`position_delta = size`
- 部分成交：`position_delta = filled_size < size`（剩余挂单 status=LIVE）

### 消费时机

`handle_trade_confirmed` 收到 trade 时按量扣除：

```python
already_filled = self._order_post_filled.get(order_id, 0)
if already_filled > 0:
    skip_amount = min(already_filled, matched_amount)
    matched_amount -= skip_amount
    # 更新或移除
    remaining_skip = already_filled - skip_amount
    if remaining_skip <= 0.001:
        self._order_post_filled.pop(order_id, None)
    else:
        self._order_post_filled[order_id] = remaining_skip
    # matched_amount <= 0.001 则跳过 position 更新
    # 否则用剩余 matched_amount 正常更新
```

### 行为示例

| 场景 | _place_order 返回 | _order_post_filled | WS trade #1 | WS trade #2 |
|------|-------------------|-------------------|-------------|-------------|
| 全部成交 10 shares | position_delta=10 | {order: 10} | skip 10, 移除 key | N/A |
| 部分成交 3/10 | position_delta=3 | {order: 3} | skip 3, 移除 key | 正常更新 7 |
| 分两笔推送 (2+1) | position_delta=3 | {order: 3} | skip 2, 余 1 | skip 1, 移除 key; 后续 trade 正常 |

## PLACEMENT 去重

WS 新订单上线通知（PLACEMENT）也使用 `_order_post_filled` 判断是否为程序发起的单：

```python
if order_id in self._order_live_on_post_ids or order_id in self._order_delayed_on_post_ids or order_id in self._order_post_filled:
    # skip WS PLACEMENT
```
