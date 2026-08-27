# Framework 模块改造方案

日期：2026-08-26

## 1. 改造目标

`framework` 只保留被多个微服务稳定复用的基础能力，不承载某一个微服务的业务实现，也不反向依赖具体微服务。

目标依赖方向：

```text
微服务 / 策略服务
        ↓
framework 公共能力
        ↓
第三方库、基础设施
```

禁止出现：

```text
framework → account_service / signal_weather / strategy_weather_sweep
```

## 2. 当前调用关系结论

### 2.1 已确认的公共模块

以下模块被多个微服务或多个策略服务直接使用，可以继续放在 `framework`：

| 当前路径 | 使用方 | 归属建议 |
| --- | --- | --- |
| `framework/auth/` | `account_service`、`signal_leader`、`strategy_weather_sweep` | 公共鉴权 |
| `framework/db/` | 账户、认证、信号、策略服务 | 公共数据库连接 |
| `framework/logging/` | 多个微服务 | 公共日志 |
| `framework/consul/` | 多个微服务、策略运行时 | 公共服务注册 |
| `framework/time_utils.py` | `account_service`、`auth_service`、`signal_leader` | 公共时间处理 |
| `framework/strategy_runtime/stop_loss.py`（迁移后） | 3 个策略服务 | 策略风控组件 |
| `framework/strategy_runtime/tick_verifier.py`（迁移后） | 3 个策略服务 | 策略市场校验组件 |
| `framework/strategy_runtime/*_adapter.py`（迁移后） | 多个策略服务 | 信号适配组件 |

### 2.2 当前只有单个服务使用的模块

这些模块不应继续放在 `framework`，但迁移前必须保留兼容路径或先完成调用方改造：

| 当前路径 | 当前使用方 | 后续归属 |
| --- | --- | --- |
| `framework/config_loader.py` | 只有 `signal_weather` | `signal_weather/internal/config.py` |
| `framework/market_service.py` | 目前只有 `account_service/api.py` | 暂时原地保留，待市场接口重新归属 |

`market_service.py` 之所以被 `account_service` 引用，是因为账户服务当前额外挂载了：

```text
GET /api/account/market/price
```

这不是账户领域逻辑。当前阶段不移动、不删除，先保留调用关系，后续在确定市场服务边界后再处理。

### 2.3 存在反向依赖的模块

以下公共模块直接导入了 `account_service`：

```text
framework/balance.py
framework/trading/order.py
framework/strategy_runtime/balance_poller.py（迁移后）
    └── from account_service.service import get_account_service
```

这违反公共模块的依赖方向。后续应改成依赖注入或协议接口，由调用方提供 `ClobClient` 获取能力，公共模块不再知道 `account_service` 的存在。

### 2.4 需要确认的遗留模块

以下文件没有找到当前业务代码的直接调用者，暂不删除：

```text
framework/strategy_runtime/orderbook_ws.py（迁移后）
framework/strategy_runtime/order_tracker.py（迁移后）
framework/consul/discovery.py
```

它们需要通过运行配置、动态导入和历史启动脚本再次确认后，才能决定删除或归档。

## 3. 目标目录结构

```text
framework/
├── auth/                        # HTTP 鉴权依赖
├── db/                          # MySQL 连接池
├── logging/                     # 统一日志
├── consul/                      # 服务注册和发现
├── time_utils.py                # 时间格式化
├── balance.py                   # 暂保留，待处理反向依赖
├── config_loader.py             # 待迁移到 signal_weather
├── market_service.py            # 暂保留，待确定市场接口归属
├── trading/                     # 公共下单/撤单适配
│   ├── __init__.py
│   └── order.py
└── strategy_runtime/            # 多个策略服务共享的运行时
    ├── __init__.py
    ├── app_factory.py
    ├── container.py
    ├── interfaces.py
    ├── instance_manager.py
    ├── state_store.py
    ├── event_logger.py
    ├── order_executor.py
    ├── balance_poller.py
    ├── account_ledger.py
    ├── order_tracker.py
    ├── market_data.py
    ├── orderbook_ws.py
    ├── tick_verifier.py
    ├── stop_loss.py
    ├── signal_client.py
    ├── signal_dedup.py
    ├── leader_adapter.py
    └── weather_adapter.py
```

说明：保留已有的 `auth/`、`db/`、`logging/`、`consul/` 分类，不额外增加 `platform/`；保留 `trading/` 作为公共交易能力，不增加 `integrations/polymarket/`。`strategy_runtime` 作为一个有明确业务边界的一级目录保留，但取消其下 `toolkit/execution/`、`toolkit/market/`、`toolkit/risk/`、`toolkit/signals/` 的重复嵌套。

## 4. 策略运行时的边界调整

当前 `framework/strategy_runtime/` 同时包含通用运行时和天气扫单专用实现，尤其是：

```text
instance_manager.py
event_logger.py
```

其中存在以下天气策略专用内容：

- `weather_sweep_configs`
- `weather_sweep_events`
- `fixed_entry_shares`
- `entry_wait_ms`
- `sweep_outcome_filter`
- `stop_loss_ratio`

改造后：

1. `app_factory.py`、`interfaces.py`、信号 WS 客户端、通用生命周期管理保留在 `strategy_runtime`。
2. 配置表字段解析、天气事件表写入、天气策略多实例配置加载移回 `strategy_weather_sweep`。
3. `InstanceManager` 改成通用配置加载器，或者由具体策略服务实现自己的 `ConfigRepository`。
4. `EventLogger` 改成抽象事件记录接口；具体表名和业务字段由策略服务提供。
5. `StrategyContext` 只暴露协议和依赖，不暴露天气策略专用数据结构。

## 5. 公共模块分类规则

新增代码必须满足以下规则：

### 放入现有的基础设施目录

- 数据库连接池 → `framework/db/`
- 统一日志 → `framework/logging/`
- Consul 注册和生命周期 → `framework/consul/`
- HTTP 鉴权依赖 → `framework/auth/`
- 无业务含义的时间格式化 → `framework/time_utils.py`

### 放入 `framework/trading/`

- 多个策略服务共用的下单、撤单适配
- Polymarket 交易参数转换
- 第三方交易 API 的重试、协议转换和错误处理

余额、市场查询是否放入 `framework/trading/`，要根据实际调用方和对 `account_service` 的依赖方向决定，不能仅按功能名称归类。

### 放入 `framework/strategy_runtime/`

- 多策略共享的生命周期
- 信号标准结构和 WS 订阅
- 下单执行协议
- 订单跟踪、资金账本、止损、tick 校验
- 文件直接放在 `strategy_runtime/` 下；文件数量明显增长前不再新增二级分类目录

### 放入具体微服务

- 只被一个微服务使用的配置加载器
- 具体市场发现算法
- 具体策略配置表的 SQL
- 具体信号源的业务规则
- 具体服务的 API、DAO、Service

## 6. 分阶段迁移计划

### 阶段一：建立调用边界

- 保持现有文件位置不变。
- 为每个 framework 模块记录直接调用者。
- 补充模块级文档字符串，说明公共范围和禁止依赖。
- 暂不处理 `market_service.py`，避免误删账户接口依赖。

### 阶段二：迁移单服务模块

- 将 `config_loader.py` 复制到 `signal_weather/internal/config.py`。
- 修改 `signal_weather/app.py` 的导入。
- 静态检查无其他服务引用后，再删除 `framework/config_loader.py`。

### 阶段三：压平策略运行时

- 将 `toolkit/execution/*`、`toolkit/market/*`、`toolkit/risk/*`、`toolkit/signals/*` 平移到 `strategy_runtime/` 根目录。
- 按职责保留独立文件，不把所有逻辑合并成一个大文件。
- 更新所有导入路径，例如：

  ```python
  from framework.strategy_runtime.order_executor import OrderExecutor
  from framework.strategy_runtime.tick_verifier import TickVerifier
  from framework.strategy_runtime.stop_loss import StopLossMonitor
  ```

- 旧路径迁移完成并通过编译检查后，再删除空的 `toolkit/` 目录。
- 把天气策略的配置表 SQL、事件表字段和配置快照移回 `strategy_weather_sweep`。
- 保持 `strategy_leader` 和 `strategy_sweep_leader` 的单实例模式兼容。
- 每次迁移后执行全量导入检查和策略模块编译检查。

### 阶段四：消除反向依赖

- 定义 `AccountClientProvider` 或等价协议。
- `balance_poller.py`、`trading/order.py` 只依赖协议。
- 由 `account_service` 或策略应用层注入具体实现。
- 禁止 framework 文件直接导入 `account_service`。

### 阶段五：重新处理市场服务

- 决定 `/api/account/market/price` 的最终归属。
- 如果成为独立服务，迁移 `market_service.py` 和对应 API。
- 如果只是公共 Polymarket 市场能力，保留在现有公共交易/市场模块中，不新增 `integrations/polymarket/` 层。
- 迁移完成前保留现有 `framework/market_service.py`，不做破坏性删除。

### 阶段六：清理遗留代码

- 确认 `orderbook_ws.py`、`order_tracker.py`、`consul/discovery.py` 没有动态调用后再删除。
- 删除前保留一次提交记录，必要时可恢复。

## 7. 验收标准

- `framework` 中不存在对具体微服务的直接导入。
- 单服务模块不再被新代码放入 `framework`。
- 现有 `auth/`、`db/`、`logging/`、`consul/`、`trading/` 目录职责明确，不额外增加无意义的包装层。
- `strategy_runtime` 内部不再存在 `toolkit/execution/`、`toolkit/market/`、`toolkit/risk/`、`toolkit/signals/` 的重复嵌套。
- `strategy_runtime` 不再写死某个策略的数据库表和业务字段。
- 所有现有微服务和策略服务的导入路径可编译。
- `market_service.py` 在最终归属确定前保持可用，不因目录整理导致 `/api/account/market/price` 失效。
- 每一阶段独立提交，避免大范围重构无法回退。

## 8. 当前不做的事情

- 不启动服务，不连接数据库或 Consul。
- 不删除未确认的遗留模块。
- 不移动 `market_service.py`。
- 不改变现有 API 路径和策略行为。
- 不在调用关系未确认前进行批量重命名。
