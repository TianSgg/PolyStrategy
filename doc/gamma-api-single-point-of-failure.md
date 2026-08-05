# Gamma API 单点故障

## 问题描述

`_fetch_event_slug_async` 通过 HTTP 请求 Gamma API (`https://gamma-api.polymarket.com/markets?clobTokenIds=...`) 获取 `event_slug`。

当前实现将所有异常都静默返回空字符串 `""`，不区分"查不到"和"网络不可达"。

## 影响

- Gamma API 不可用时（如 `Cannot connect to host gamma-api.polymarket.com:443 ssl:default [nodename nor servname provided, or not known]`），所有 Transfer 信号都带 `event_slug=""`
- `_handle_buy` 已移除 event_slug 过滤，所有 BUY 信号都会执行，不再有重复跟买问题

## 根因

- 无备用数据源
- 无重试机制
- 无熔断/降级策略

## 触发条件

- Gamma API 服务端故障
- DNS 解析失败
- 网络分区
