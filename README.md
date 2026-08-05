# WeatherTaker

Polymarket 量化跟单交易平台。

## 核心功能

### 跟单交易

复制 leader 的交易信号，自动跟随开仓/平仓。

- **信号来源**: Polymarket RTDS WebSocket `activity/*` 订阅
- **市场报价**: 跟单下单前通过 MarketService 维护的 market WS 订单簿获取最优 bid/ask
- **内存去重**: 按 `transactionHash` 去重，不查 DB
- **SELL 比例**: `(leader_sell / leader_position) * follower_position` 计算跟卖量
- **持久化**: leader / follower 仓位与跟单订单写入 MySQL
- **配置控制**: 默认禁用，`enabled=True` 时才生效

### 账户管理

管理 Polymarket 账户、Builder Code、余额和持仓信息。

- **账户密钥**: 私钥和 Builder 凭据加密存储
- **余额统计**: 汇总现金、持仓价值和账户总金额
- **持仓查看**: 拉取 Polymarket 持仓数据并展示盈亏

### 性能状态

状态栏展示后端关键链路延迟。

- **WS-mkt**: 跟单 MarketService 使用的 Polymarket market WS
- **WS-user**: follower user channel WS
- **Polygon / RTDS**: 链上监听和 Polymarket RTDS 延迟
- **HTTP APIs**: Polymarket Data API、CLOB API、Gamma API 与 Polygon HTTP

## 实现亮点

- **O(1) 配置查找**: `_config_id_to_config` dict，避免遍历
- **单例模式**: `AccountService`、`CopyTradingService`、`MarketService` 全局单例
- **DB 写后置**: 跟单订单记录在下单成功后才落 DB，避免失败也写
- **RTDS 多 config 复用**: 单 WebSocket 连接按 `proxyWallet` 分发到多个配置并行跟单
