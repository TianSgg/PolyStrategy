# 改造 Asset Question/Outcome 解析链路

## Summary
把当前 `asset_id -> question` 的获取从“依赖用户 positions”升级为“优先按 CLOB token id 查市场元数据”。新增 `outcome` 字段缓存该 asset/token 对应的 outcome，例如 `Yes`、`No` 或具体选项文案。Data API `positions?user=` 仍保留为最后兜底，因为它也能返回 `outcome/outcomeIndex/oppositeOutcome/oppositeAsset`。

## Confirmed API Facts
- Gamma `GET https://gamma-api.polymarket.com/markets?clob_token_ids={asset_id}` 可以用 CLOB token id 直接查 market。
- Gamma market 返回里的 `outcomes`、`clobTokenIds`、`outcomePrices` 在文档和实测中都是 **字符串化 JSON 数组**，例如：
  - `outcomes = "[\"Yes\", \"No\"]"`
  - `clobTokenIds = "[\"9802...\", \"5383...\"]"`
- Gamma token/outcome 映射按 index 对应：
  - `clobTokenIds[i] -> outcomes[i]`
- CLOB `GET https://clob.polymarket.com/markets-by-token/{asset_id}` 返回：
  - `condition_id`
  - `primary_token_id`
  - `secondary_token_id`
- Data API `GET https://data-api.polymarket.com/positions?user={addr}` 返回 position item 中包含：
  - `asset`
  - `conditionId`
  - `title`
  - `outcome`
  - `outcomeIndex`
  - `oppositeOutcome`
  - `oppositeAsset`

## Key Changes
- 修改 `copy_trading_asset_questions`：
  - `schema.sql` 增加 `outcome VARCHAR(128) DEFAULT '' COMMENT 'Asset 对应 outcome'`。
  - 新增手动迁移脚本，例如 `backend/migrations/20260511_asset_question_outcome.sql`：
    ```sql
    USE weathertaker;
    ALTER TABLE copy_trading_asset_questions
      ADD COLUMN outcome VARCHAR(128) DEFAULT '' COMMENT 'Asset 对应 outcome' AFTER question;
    ```
  - 不接入 `run_auth_migrations()`，人工执行。
- 修改 DB helper：
  - `get_asset_question(asset_id)` 保持只返回 `question`，避免影响现有通知/pending 调用。
  - `upsert_asset_question(asset_id, question, outcome='')` 支持 outcome。
  - `batch_upsert_asset_questions(assets)` 支持元素 `{asset_id, question, outcome}`。
  - upsert 规则：
    - `question = VALUES(question)` 保持现有覆盖。
    - `outcome = COALESCE(NULLIF(VALUES(outcome), ''), outcome)`，避免空 outcome 覆盖已有值。
- 新增解析 helper：
  - `_parse_json_list(value)`：兼容 Gamma 字符串化 JSON 数组、真实 list、空值、异常。
  - `_extract_market_assets(market)`：
    - 解析 `market["question"]`
    - 解析 `market["clobTokenIds"]`
    - 解析 `market["outcomes"]`
    - 返回 `[{asset_id, question, outcome}, ...]`
  - 只缓存 `asset_id` 非空且 `question` 非空的记录。
- 改造 `_fetch_and_cache_asset_question(asset_id, addr)` 主流程：
  1. 调 Gamma：
     ```text
     https://gamma-api.polymarket.com/markets?clob_token_ids={asset_id}
     ```
     如果返回 market，解析并批量缓存该 market 下所有 token，找到当前 `asset_id` 后返回 question。
  2. 如果 Gamma 直接查不到，调 CLOB：
     ```text
     https://clob.polymarket.com/markets-by-token/{asset_id}
     ```
     取 `condition_id` 后调：
     ```text
     https://gamma-api.polymarket.com/markets?condition_ids={condition_id}
     ```
     再按 Gamma market 解析/缓存。
  3. 如果还查不到，保留当前 Data API fallback：
     ```text
     https://data-api.polymarket.com/positions?user={addr}
     ```
     遍历 positions：
     - `pos_asset = pos.get("asset")`
     - `question = pos.get("title", "") or pos.get("question", "")`
     - `outcome = pos.get("outcome", "")`
     - 如果有 `oppositeAsset/oppositeOutcome`，可顺手缓存 opposite asset。
  4. 全部失败时返回 `asset_id[:20]`。
- 并发去重保持不变：
  - `_get_asset_question()` 继续用 `_asset_fetch_events[asset_id]` 避免同 asset 并发重复外部请求。
  - cache 命中仍然直接返回，不发外部请求。
- 使用点保持不变：
  - 通知仍只展示 question。
  - pending buy/sell 表仍只写 question。
  - 本次不改通知文案，不把 outcome 拼进消息里。

## Test Plan
- 静态和编译：
  - `python3 -m compileall -q backend/main.py backend/src`
  - `git diff --check`
  - `rg -n "20260511_asset_question_outcome|copy_trading_asset_questions|outcome" backend`
  - 确认新迁移脚本没有被 `run_auth_migrations()` 引用。
- 手工 API 验证：
  - 找一个真实 Gamma market token，调用 `_fetch_and_cache_asset_question(asset_id, addr)`，确认走 Gamma 直接路径。
  - DB 检查：
    - 同一 market 的多个 `clobTokenIds` 都被缓存。
    - `outcome` 和 Gamma `outcomes` 同 index 对齐。
  - 模拟/选取 Gamma 直接查不到但 CLOB `markets-by-token` 能返回 `condition_id` 的 token，确认 CLOB + Gamma fallback 可用。
  - 用一个当前有 positions 的 user fallback，确认 Data API 路径能缓存 `title/outcome`，并能顺手缓存 `oppositeAsset/oppositeOutcome`。
- 回归：
  - BUY/SELL 通知仍正常拿到 question。
  - `_save_pending_buy_with_question()` / `_save_pending_sell_with_question()` 仍正常写 pending 表。
  - 缓存已有 question 时不触发外部请求。

## Assumptions
- “表里加一个字段 asset 代表的 outcome”指的是 `copy_trading_asset_questions` 表新增 `outcome`。
- outcome 暂时只缓存，不改前端、不改通知展示。
- Gamma 是主路径；Data API positions 只作为最终兜底。
- Gamma 的 `clobTokenIds/outcomes/outcomePrices` 按字符串化 JSON 数组处理，即使未来返回 list 也兼容。
