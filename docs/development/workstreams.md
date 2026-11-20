# 平行開發約定（Workstreams & Contracts）

本文件規範平行開發時各 workstream 的檔案所有權與共用介面。設計依據為 `PLAN.md`（v1.2）與本文件 §4 的範圍決策；兩者衝突時以本文件為準。

## 1. 檔案所有權

只修改自己擁有的檔案。需要改別人的檔案或共用介面時，在最終回報中提出，由整合者處理。

| Workstream | 擁有的檔案 |
|---|---|
| **A 訂票與金流** | `backend/src/ticketing/booking/**`、`payments/**`、`api/routes/orders.py`、`api/routes/payments.py`、`paymock/**`、`backend/migrations/versions/0002_booking.py`、`backend/tests/booking/**`、`backend/tests/payments/**`、`loadtest/scenario_a*`、`loadtest/consistency.sql` |
| **B 活動、匯入、報表** | `backend/src/ticketing/events/**`、`imports/**`、`reports/**`、`api/routes/events.py`、`api/routes/imports.py`、`api/routes/reports.py`、`backend/migrations/versions/0003_events.py`、`backend/tests/events/**`、`backend/tests/imports/**`、`backend/tests/reports/**`、`samples/**`、`scripts/gen_seats.py`、`scripts/seed_orders_500k.sql` |
| **C 即時推送** | `backend/src/ticketing/realtime/**`（`outbox.py` 除外）、`api/routes/seats.py`、`backend/migrations/versions/0004_realtime.py`、`backend/tests/realtime/**`、`loadtest/scenario_b*`、`loadtest/sse_client.py` |
| **D 監控** | `backend/src/ticketing/telemetry/**`、`api/routes/monitoring.py`、`backend/migrations/versions/0005_monitoring.py`、`backend/tests/telemetry/**` |
| **整合者（Claude）** | 其餘全部：`api/app.py`、`api/security.py`、`core/**`、`auth/**`、`worker/main.py`、`realtime/outbox.py`、`compose*.yaml`、`infra/**`、根目錄文件、`frontend/**`（後續階段） |

## 2. 共用介面（已就位，直接使用）

- **路由**：`api/routes/<name>.py` 的 `router = SecureRouter(...)`，已由 `app.py` 註冊。每個路由都必須宣告 `policy=`（`Public()`、`Authenticated()`、`Roles("admin", "user")` 等），需要使用者時以 `principal: Annotated[Principal, Depends(current_principal)]` 注入。路徑一律以 `/api/v1` 開頭。
- **背景工作**：`booking/sweeper.py`、`payments/reconciler.py`、`imports/runner.py` 的 `async def tick(ctx)` 由 worker 反覆呼叫（各自獨立迴圈）；`realtime/relay.py` 的 `tick` 由 relay 服務呼叫；`telemetry/collector.py` 的 `tick` 由 collector 呼叫。`tick` 必須有界、不可無限阻塞，並在 `ctx.stop` 設定時盡快返回。
- **API 程序內安裝**：`realtime/api_setup.py`、`telemetry/api_setup.py` 的 `install(app)` 由 app factory 呼叫一次，可加 middleware 或以 lifespan 啟停背景 task。
- **Outbox**：座位狀態改變的交易內呼叫 `realtime.outbox.emit_seat_changes(conn, event_id, [(seat_idx, status, version), ...])`；匯入完成呼叫 `emit_layout_changed(conn, event_id, layout_ver)`。一筆交易一筆 outbox 列。
- **業務指標**：`telemetry.business.record(event_id, outcome)`，outcome 取值：`hold_ok`、`hold_conflict`、`hold_soldout`、`hold_limit`、`pay_ok`、`pay_fail`、`cancel`、`expiry`。不可拋例外。
- **Session 圍欄**：敏感寫入在交易內先呼叫 `auth.sessions.lock_live_session(conn, principal.session_id, principal.user_id)`，回傳 False 時回 401。
- **錯誤**：一律 `raise ProblemError(status, code, title, detail, retryable=...)`（`core/errors.py`），code 使用 PLAN §5.2、§11 列出的名稱。
- **時鐘**：所有期限判斷用資料庫的 `app_now()`，取鎖之後才取樣。
- **request_id**：寫入 outbox、job、callback 的 `request_id` 用 `core.context.current_request_id()`。
- **連線**：`async with acquire(state.db, settings.db_pool_acquire_timeout_s) as conn`（`core/db.py`），逾時自動回 503。
- **Migration 鏈**：`0001_baseline → 0002_booking → 0003_events → 0004_realtime → 0005_monitoring`，各 workstream 只編輯自己的檔案；`SCHEMA_HEAD = "0005_monitoring"`。

## 3. 鎖順序（PLAN §5.2）

- 訂票：冪等 advisory lock → `user_event_quota` → `orders` INSERT → `seats`（NOWAIT / SKIP LOCKED）。
- 釋放與回呼：`orders` → `seats`（依 id 排序）→ `user_event_quota`。
- 活動層級（刪除、發布、匯入 admission/finalize、區域編輯）：`users FOR SHARE` → `events FOR UPDATE` → `import_jobs FOR UPDATE`。

## 4. 範圍決策（相對 PLAN v1.2）

| 項目 | 決策 |
|---|---|
| 分頁 | 保留：第一頁物化快照（不做共用公開快照） |
| 日誌與指標 | 保留 nginx access log + 各服務 JSON log 檔的 tail + checkpoint 同交易 COPY；HTTP 指標以 nginx 時鐘為準，連線數以 `stub_status` 每秒取樣 |
| 告警 | 6 條：EDGE_5XX、EDGE_P99、PURCHASE_FUNNEL_COLLAPSE、PAYMENT_CALLBACK_LAG、HOLD_EXPIRY_BACKLOG、SERVICE_DOWN（relay/collector/worker 心跳或 outbox 延遲） |
| 即時推送 | 單一 relay（advisory lock）指派 per-event seq 並批次寫 seat_events；每個 API 程序對每個有人訂閱的活動輪詢 PG 已指派的 outbox，放進共享有界 ring；斷層從 PG 補、不足則版本化快照；慢客戶端斷線。**不使用** Redis Streams、generation、epoch fencing、5 秒 reconcile |
| 金流 | 保留：每訂單唯一可扣款 attempt、HMAC 簽章回呼與去重、15 秒對帳、冪等退款、期限後 capture 退款、已付款取消於退款確認後釋放。刪除：provider_revision 等額外狀態 |
| 匯入 | 刪除 attempts API；JSON 以 `raw_decode` + offset 對應實體行號 |
| Excel 50 萬@512MB、A/B 壓測、破壞驗證 | 完整保留 |

## 5. 測試與品質要求

- 測試放在自己的 `backend/tests/<area>/`，整合測試標記 `pytestmark = pytest.mark.integration`，fixture 用 `tests/conftest.py` 既有的（`db`、`client`、`make_client`、`test_clock`、`settings_overrides`）與 `tests/support.py`（`create_user`、`login`）。
- 本機執行（每個 workstream 使用自己的 template 與 Redis DB 編號，避免互相干擾）：
  ```
  TEST_ADMIN_DATABASE_URL=postgresql://test:test@127.0.0.1:55432/postgres \
  REDIS_URL=redis://127.0.0.1:56379/<A=3,B=4,C=5,D=6> TEST_TEMPLATE_DB=tpl_<a|b|c|d> \
  .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests
  ```
- 提交前必須通過：`.venv/bin/ruff check backend paymock`、`.venv/bin/ruff format --check backend paymock`、`.venv/bin/mypy backend/src paymock/src`、`.venv/bin/lint-imports`、上述 pytest 全綠。
- **API 文件是評估重點**：每個端點都要有 `summary`、`description`（說明行為、權限與冪等語意）、明確的 `response_model`、以 `responses={...}` 列出可能的錯誤狀態碼與 problem `code`，request/response schema 的欄位附 `description` 與 `examples`。
- 併發測試要用真實的平行連線（`asyncio.gather` + 獨立 client/連線），並以不變式斷言結果。
- Commit：在自己的分支上以小步 Conventional Commits 提交，訊息帶 task 編號，結尾加上：
  ```
  Co-Authored-By: OpenAI Codex <noreply@openai.com>
  ```
