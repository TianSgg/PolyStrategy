# 订单 Status 大小写规约

订单 `status` 字段在不同数据源的大小写格式不同，项目内部统一使用**大写**存储和比较。

## 各数据源格式

| 来源 | status 格式 | 示例值 |
|------|-------------|--------|
| Polymarket REST API (`create_and_post_order`) | 小写 | `live`, `matched`, `delayed` |
| Polymarket WS 推送 (`handle_order_event`) | 大写 | `LIVE`, `MATCHED`, `CANCELED` |
| DB `copy_trading_orders.status` | 大写 | `LIVE`, `MATCHED`, `CANCELED`, `ERROR`, `SKIPPED`, `DELAYED` |

## 处理规则

1. **`_place_order`** 解析 REST API 响应时用小写匹配（因为 API 返回小写），然后立即转大写写入 `PlaceOrderResult.raw_status`
2. **WS 事件** 的 `type` 和 `status` 由 Polymarket 推送时已是大写，直接使用
3. **内部比较和 DB 写入** 一律使用大写常量
4. V5 迁移脚本已将历史数据统一为大写

## 注意

- 新增 status 比较逻辑时，内部用大写；只有 REST API 入口用小写匹配
- WS 的 `side` 字段在 `ws.py` 中已做 `.upper()` 转换
- WS 的 `type` / `status` 字段本身就是大写，无需转换
