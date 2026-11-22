# 驗收條件與驗證方式

本文件把題目需求逐條拆成**可檢查的驗收條件**，並寫出驗證方式（操作介面、指令或自動化測試）與目前狀態。

- 預設帳號：Admin `admin@example.com`／`Admin#Dev2026!`、User `user@example.com`／`User#Dev2026!`、Viewer `viewer@example.com`／`Viewer#Dev2026!`。
- 介面：`http://localhost:8080`；API 文件：`http://localhost:8080/api/docs`。
- 測試：`make test`（預設測試套件）；單一測試可用 `uv run pytest -k <名稱>`（需測試用 PostgreSQL，見 README）。
- 狀態：✅ 已完成並有驗證；🔧 開發中。

---

## 一、交付項目

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 1.1 | 程式碼在 GitHub，保留完整 commit 歷史（非單一 squash） | `git log --oneline \| wc -l`（百餘筆，含 merge） | ✅ |
| 1.2 | README 含專案介紹、技術棧、本地運行、Docker 一鍵指令、API 文件連結、三種角色測試帳號、已知問題 | 閱讀 [README](../README.md) 各節 | ✅ |
| 1.3 | `docker compose up -d --build` 一行啟動全部服務，無需事先設定 | 全新 clone 執行後 `docker compose ps -a`：`migrate`、`seed` 以 0 結束，其餘服務 healthy | ✅ |
| 1.4 | 測試帳號由系統自動建立 | 啟動後直接用三組預設帳號登入成功 | ✅ |
| 1.5 | `.env.example` 列出所有可調參數並附說明 | 閱讀 [.env.example](../.env.example) | ✅ |
| 1.6 | 提供 CSV 與 JSON 座位匯入範例 | [samples/](../samples/)；README 列出各範例在兩種匯入模式下的預期結果 | ✅ |
| 1.7 | 系統架構圖呈現元件與資料流 | [docs/architecture.md](architecture.md)、[architecture.html](architecture.html) | ✅ |
| 1.8 | LOADTEST 含機器規格、情境、RPS、p50/p95/p99、錯誤率、瓶頸分析、壓測後一致性 | [LOADTEST.md](../LOADTEST.md) 並列 6 副本建議配置與 3 副本基準，兩輪同桌機、同 commit `346fe0f`。[6 副本環境](evidence/loadtest-final-6replicas-20261122/environment.txt#L1-L9) · [3 副本環境](evidence/loadtest-final-20261122/environment.txt#L1-L9) | ✅ |
| 1.9 | AI_USAGE 說明使用的 AI 工具與用途，並列出至少 3 處 AI 程式碼錯誤、如何發現與修正 | [AI_USAGE.md](../AI_USAGE.md)（6 例） | ✅ |
| 1.10 | DESIGN 回答 Q1–Q6，並列出假設、替代方案、取捨、驗證方式 | [DESIGN.md](../DESIGN.md) | ✅ |

## 二、技術要求

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 2.1 | 後端使用 FastAPI | `backend/pyproject.toml`、`backend/src/ticketing/api/app.py` | ✅ |
| 2.2 | 訂單與座位狀態的唯一真實來源是資料庫（PostgreSQL 16）；Redis 不決定座位歸屬 | DESIGN Q1；訂票交易只在 PostgreSQL 內決定（`booking/create.py`） | ✅ |
| 2.3 | Docker Compose 一行指令啟動全部服務 | 同 1.3 | ✅ |

## 三、角色與權限

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 3.1 | **Admin** 可管理所有活動：編輯、發布、刪除任何人的活動 | Admin 登入 → Organizer 可看到所有活動並操作；測試 `test_event_access`、`test_lifecycle_and_concurrent_patch` | ✅ |
| 3.2 | **Admin** 可查看與管理所有使用者的訂單（列出全站訂單、篩選、查看明細、取消） | Admin 登入 → **All orders** 頁（[截圖](screenshots/12-admin-all-orders.png)）；API `GET /api/v1/admin/orders`；`backend/tests/booking/test_admin_orders.py` | ✅ |
| 3.3 | **Admin** 可使用系統管理功能（使用者管理、監控、日誌查詢） | 導覽列 Monitoring、Users 只對 Admin 顯示；`test_monitoring_routes_require_admin`、`test_non_admin_cannot_list_users` | ✅ |
| 3.4 | **User** 可建立活動、匯入座位、鎖位與付款 | User 登入 → Organizer 建活動並匯入 → Events 下單付款；`scripts/e2e_flow.py` | ✅ |
| 3.5 | **User** 只能修改／刪除自己的活動：修改他人已發布活動回 403；他人的草稿對 User 不可見，回 404（不透露存在） | `test_event_access` | ✅ |
| 3.6 | **User** 只能查看自己的訂單；查看他人訂單回 404（不透露存在） | `test_admin_order_access_and_cancellation_replay` | ✅ |
| 3.7 | **User** 只能下載自己活動的報表 | `test_report_authorization` | ✅ |
| 3.8 | **Viewer** 只能瀏覽活動、座位與所有活動的報表；建立／修改活動、匯入、下單、付款等業務寫入一律回 403（登出等自身帳號操作除外） | Viewer 登入：無 Organizer／Monitoring；`test_report_authorization`、角色矩陣測試 | ✅ |
| 3.9 | 每個 API 端點都宣告權限政策，未宣告者無法註冊 | `test_every_api_route_declares_a_policy`、`test_route_without_policy_is_rejected_at_registration` | ✅ |

## 四、API 設計

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 4.1 | 票務 API 的 39 個 operations（含健康檢查）皆收錄於 `docs/openapi.json`（摘要、說明、參數、回應、錯誤碼、權限） | `/api/docs`；`test_openapi_covers_each_operation` | ✅ |
| 4.2 | 票務 API 錯誤回傳 RFC 9457 problem+json，含 `code`、`retryable`、`request_id`（例外：`/healthz/ready` 的 503 回傳供容器健康檢查判讀的診斷 JSON） | `test_problem_error_is_rfc9457`、`test_not_found_is_problem_json` | ✅ |
| 4.3 | 會產生副作用、客戶端可能重送的寫入（下單、付款、取消訂單）必須帶 Idempotency-Key，重送取得原結果；活動、使用者的更新與發布以 ETag／If-Match 防止互相覆蓋（建立活動不使用冪等鍵，重送會建立另一份草稿） | `test_idempotency_contention_and_canonical_replay`、`test_if_match_is_required_and_checked` | ✅ |
| 4.4 | 版控的 `docs/openapi.json` 與程式產生的一致 | `test_openapi_export_matches_the_application` | ✅ |

## 五、功能需求

### 5.1 使用者管理

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 5.1.1 | 可註冊（不可自選角色、不可重複 email）、登入、登出 | 介面 Register／Sign in／Sign out；`test_register_then_login_and_me`、`test_register_cannot_choose_role_or_reuse_email` | ✅ |
| 5.1.2 | 登出後原 cookie 立即失效（下一個請求回 401） | `test_logout_invalidates_the_cookie_immediately` | ✅ |
| 5.1.3 | 停用使用者或修改角色後，其既有 session 立即失效，已開的即時連線被關閉 | `test_disable_revokes_sessions`、`test_role_change_revokes_target_sessions_and_is_audited`、`test_invalid_session_closes_stream` | ✅ |

### 5.2 活動與座位管理

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 5.2.1 | 建立活動：標題、描述、分類、場地、開賣與結束時間、區域與票價；欄位驗證錯誤回 422 | Organizer → Create an event；`test_lifecycle_and_concurrent_patch` | ✅ |
| 5.2.2 | 列表支援分頁、篩選、排序；資料持續寫入時翻頁不重複不遺漏 | Events 頁；`test_list_visibility_and_single_page_snapshot_cleanup`、`test_cursor_expiry_and_lost_access` | ✅ |
| 5.2.3 | 只有建立者或 Admin 可更新；兩人同時編輯時，後送出的回 412（不覆蓋） | `test_if_match_is_required_and_checked`、`test_lifecycle_and_concurrent_patch` | ✅ |
| 5.2.4 | 只有建立者或 Admin 可刪除；已有訂單的活動刪除回 409 | `test_delete_rechecks_orders_after_waiting_for_event_lock` | ✅ |
| 5.2.5 | 草稿／已發布：草稿可匯入座位；發布後在開賣時間內才能購買，時間外下單被拒 | `test_lifecycle_and_concurrent_patch`、訂票測試的開賣時窗案例 | ✅ |
| 5.2.6 | 匯入支援 CSV 與 JSON，欄位 `zone_code,row_label,seat_number` | samples 匯入成功；`test_parser.py` | ✅ |
| 5.2.7 | 單檔上限 20 MB、100,000 列；超過即拒絕 | `test_upload_byte_cap_and_worker_row_cap` | ✅ |
| 5.2.8 | 100,000 列在 60 秒內完成（CSV 與 JSON 各自） | 完整 Docker 服務實測：閒置時 CSV 11.6–15.6 秒、JSON 17.0–22.2 秒，同時跑完整測試套件時 CSV 21.0 秒、JSON 47.8 秒；整合測試（隔離資料庫）`test_import_100k_under_60s`：CSV 15.14 秒、JSON 15.35 秒（[證據](evidence/import-100k.md)） | ✅ |
| 5.2.9 | 匯入在背景執行，使用者即時看到進度 | Organizer 匯入時的進度條（前端輪詢 job 狀態並顯示目前階段；另有 `GET /api/v1/seat-imports/{job_id}/stream` 供 SSE 訂閱）；`test_imports.py` | ✅ |
| 5.2.10 | `all_or_nothing`（預設）有任一錯誤即全部不寫；`skip_invalid` 只寫有效列 | `test_imports.py`；README 範例預期結果 | ✅ |
| 5.2.11 | 錯誤報告含原始列號（CSV 含表頭、JSON 對應實體行）與錯誤原因，可下載 | `test_csv_multiline_record_reports_physical_line`、`test_json_line_numbers_match_physical_source` | ✅ |
| 5.2.12 | 同活動重複上傳相同內容的檔案，回傳既有 job（200），不重複匯入 | `test_duplicate_upload_and_publish_serialize` | ✅ |
| 5.2.13 | 檔案內重複、或與既有座位重複的列，視為錯誤列 | `test_imports.py`；`samples/seats_duplicates.json` | ✅ |
| 5.2.14 | 座位狀態變化即時推送給所有正在觀看的人（多個副本也成立） | 開兩個瀏覽器看同一活動，一邊鎖位另一邊即時變色；`test_concurrent_writers_preserve_per_seat_order`、`test_replay_live_handoff_during_writes`；壓測 B：2,000／2,000 一致 | ✅ |

### 5.3 訂票

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 5.3.1 | 可指定座位，或指定區域與張數由系統配位；每筆 1–4 張，超出回 422 | 座位圖選位／Automatic allocation；`test_booking.py` | ✅ |
| 5.3.2 | 同一使用者同一活動最多 4 張（含併發、不同 Idempotency-Key 同時送） | `test_quota_race`、`test_quota_cannot_exceed_four` | ✅ |
| 5.3.3 | 同一座位不可重複售出（含併發搶同一座位） | `test_specific_seat_races`、`test_break_it`（移除防線即失敗）；三輪壓測一致性 0 違規 | ✅ |
| 5.3.4 | 透過模擬金流付款，結果以簽章回呼通知；回呼可重送、可去重；逾期扣款自動退款 | `test_payment_keys_and_callback_replay`、`test_callback_backoff_resigns_and_duplicates`、`test_late_capture_preserves_quota` | ✅ |
| 5.3.5 | 票價依區域；服務費為票價總和 5% 四捨五入至整數元 | `test_fee_rounds_half_up` | ✅ |
| 5.3.6 | 可取消訂單（未付款釋放座位、已付款退款後釋放），可查詢自己的訂單與狀態 | My orders 頁；`test_cancel_expire_and_page`、`test_lost_create_response_and_paid_cancel` | ✅ |
| 5.3.7 | 鎖位逾時自動釋放座位與配額（以資料庫時鐘判斷） | `test_parallel_sweepers_skip_locked_and_bad_orders`、`test_app_now_uses_clock_timestamp` | ✅ |

### 5.4 即時監控

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 5.4.1 | 每秒更新、反映整個系統：RPS、p50/p95/p99、錯誤率（5xx）、即時連線數，以圖表呈現 | Admin → Monitoring；`test_admin_metric_history_streams_and_openapi`、`test_metric_error_rate_excludes_4xx` | ✅ |
| 5.4.2 | **存取日誌**以批次寫入資料庫（`access_logs`），且可在介面查看 | 資料庫查詢 `SELECT count(*) FROM access_logs`；介面 Monitoring → **Access log**（可依時間、狀態、方法、路徑、request_id 篩選）；API `GET /api/v1/admin/logs/access` | ✅ |
| 5.4.3 | **座位狀態變化事件**以批次寫入資料庫（`seat_events`） | `test_failed_second_batch_leaves_first_committed`；壓測 SQL 稽核 `seat_event_missing／extra` 為 0 | ✅ |
| 5.4.4 | **每秒系統指標**以批次寫入資料庫（`metrics_1s`、`business_1s`） | 壓測證據中的 `metrics_1s.csv` 即由資料庫匯出；`test_minimum_delay_dedupe_wall_skew_completeness_and_gauge` | ✅ |
| 5.4.5 | 系統異常時 Admin 即時收到告警（自訂 6 種規則），可確認（Acknowledge） | [告警演練](evidence/alert-drill-20261121/README.md)：停掉 relay 後告警出現在監控頁並於恢復後 resolved；`test_rule_fires_and_resolves` | ✅ |

### 5.5 報表

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 5.5.1 | 可下載活動銷售 Excel，含摘要、區域彙總、訂單明細三部分 | 活動頁 Download sales report；`test_excel_reconciles_and_respects_dst` | ✅ |
| 5.5.2 | 時間以指定時區顯示並在表頭註明；金額為數值儲存格 | 同上 | ✅ |
| 5.5.3 | 50 萬筆訂單可在 512 MiB（536,870,912 bytes）記憶體上限的 API 實例完成匯出 | [Excel 匯出實測](evidence/cp4-excel-500k.md)：217 秒、程式記憶體峰值約 108 MiB、無 OOM；`test_export_500k_within_container_budget`（選擇性執行） | ✅ |

### 5.6 系統管理（Admin）

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 5.6.1 | 可修改使用者角色與啟用狀態；不能停用或降級自己；變更留稽核紀錄 | Admin → Users；`test_admin_cannot_demote_or_disable_self`、`test_role_change_revokes_target_sessions_and_is_audited` | ✅ |
| 5.6.2 | 輸入 `request_id` 可看到該請求經過的**所有服務**的日誌（nginx、API，以及涉及時的 worker、模擬金流） | Monitoring → Request trace（ID 可從回應標頭 `X-Request-ID` 或錯誤回應取得）；`test_admin_request_trace_returns_linked_entries_in_timestamp_sequence_order`、`test_admin_request_trace_returns_paymock_processing_and_delivery` | ✅（實測一筆付款請求的追查涵蓋 nginx、api、worker、paymock，[截圖](screenshots/13-admin-trace-and-logs.png)） |

### 5.7 操作介面

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 5.7.1 | 使用者搶票介面含即時座位圖 | README 畫面截圖；Events → 任一活動 | ✅ |
| 5.7.2 | 管理者監控介面 | Monitoring 頁 | ✅ |

## 六、測試要求

| # | 驗收條件 | 驗證方式 | 狀態 |
|---|---|---|---|
| 6.1 | 有自動化**單元測試** | `backend/tests/unit/`（pytest）；前端 `frontend/tests/*.test.ts`（Vitest） | ✅ |
| 6.2 | 有自動化**整合測試**，且使用與正式相同的資料庫 | `backend/tests/integration/`、`booking/`、`payments/`、`events/`、`imports/`、`realtime/`、`reports/`、`telemetry/`、`authz/`：pytest + httpx（直接打 FastAPI 應用）+ **PostgreSQL 16**（與正式相同映像）+ Redis 7；每個測試從 migrate 後的 template 複製獨立資料庫 | ✅ |
| 6.3 | 一行指令執行預設測試套件 | `make test`（Docker 內：Ruff、mypy、import 規則、後端單元與整合、前端 lint／型別／Vitest／預設 Playwright）。[經 nginx 的端到端流程](../README.md#測試與實測)另行執行。大型 Excel 匯出另有 [opt-in 環境設定](../backend/tests/reports/test_export_memory.py#L1)與指令 `uv run pytest -s -m slow backend/tests/reports/test_export_memory.py`。 | ✅ |
| 6.4 | 併發測試證明大量同時請求下仍正確 | `test_specific_seat_races`（200 人搶同一座位）、`test_quota_race`、`test_zone_converges`、`test_break_it` | ✅ |
| 6.5 | 壓測 A：10,000 席、3,000 VU、60 秒 | 6／3 副本分別實測 1,837.6／2,072.6 req/s，nginx 503 占 35.4%／88.1%，兩輪 502／500 皆為 0。**FAILED** 僅指 k6 自我檢查門檻未達標（非預期錯誤 620／63 筆，3 副本基準另有 28 筆重送未驗證）。[LOADTEST 第三節](../LOADTEST.md#三怎麼判斷成功或失敗k6-的-failed-是什麼意思) · [LOADTEST §2](../LOADTEST.md#2-scenario-a) · [6 副本執行紀錄](evidence/loadtest-final-6replicas-20261122/run-log.txt#L500-L541) · [3 副本執行紀錄](evidence/loadtest-final-20261122/run-log.txt#L482-L523) · [6 副本 nginx](evidence/loadtest-final-6replicas-20261122/a/nginx-summary.txt#L1-L20) · [3 副本 nginx](evidence/loadtest-final-20261122/a/nginx-summary.txt#L1-L20) | ✅ |
| 6.6 | 壓測 B：2,000 條即時連線同時訂閱同一活動 | 6 副本及 3 副本基準寫入期間的同時在線峰值均為 2,000 條，兩輪各有 2,000／2,000 個 client 與 PostgreSQL 一致。[LOADTEST §3](../LOADTEST.md#3-scenario-b) · [6 副本 SSE](evidence/loadtest-final-6replicas-20261122/b/sse-client.txt#L3) · [3 副本 SSE](evidence/loadtest-final-20261122/b/sse-client.txt#L3) | ✅ |
| 6.7 | 壓測後一致性檢查結果 | 6 副本及 3 副本基準均無超賣，排空後 `consistency.sql` 各回傳 0 列。[LOADTEST §6](../LOADTEST.md#6-一致性檢查) · [6 副本 SQL](evidence/loadtest-final-6replicas-20261122/a/consistency.txt) · [3 副本 SQL](evidence/loadtest-final-20261122/a/consistency.txt) | ✅ |

## 七、設計問答（DESIGN.md）

| # | 驗收條件 | 位置 | 狀態 |
|---|---|---|---|
| 7.1 | Q1：指定座位購買的完整路徑，標註取得與釋放保護的檔案行號；最可能雙賣的故障時間線；併發測試與拿掉保護後的失敗輸出 | DESIGN Q1、[break-it 失敗輸出](evidence/break-it-raw.txt) | ✅ |
| 7.2 | Q2：至少 3 個「資料庫已改、其他部分未知」的時間窗與最危險者；kill -9 後如何收斂；鎖位逾時用哪個時鐘與如何不 sleep 測試；兩個 worker 同時執行逾時工作仍正確的那一行 | DESIGN Q2 | ✅ |
| 7.3 | Q3：冪等三種情況的回應；兩個 key 同時超量的原子性位置；配位在資料庫內挑選與對應測試 | DESIGN Q3 | ✅ |
| 7.4 | Q4：推送完整路徑與順序保證（多副本）；漏事件偵測與恢復；慢客戶端策略與最壞情況；情境 B 實測 | DESIGN Q4 | ✅ |
| 7.5 | Q5：登出／停用／改角色後立即失效、每請求成本（實測）、快取一致性；停用者的匯入 job；系統性授權驗證與最易漏掉的端點 | DESIGN Q5、[session 成本實測](evidence/session-auth-cost.md) | ✅ |
| 7.6 | Q6：各指標的來源、窗口、儲存；「圖表正常但使用者受害」的偵測；多副本去重與時鐘慢 20 秒的現象；數字不一致時相信誰 | DESIGN Q6 | ✅ |
| 7.7 | 設計假設、替代方案、取捨、驗證方式 | DESIGN 對應章節與驗證對照表 | ✅ |
| 7.8 | 以流程圖輔助說明 Q1–Q6 | DESIGN 各題開頭的流程圖（6 張皆以 mermaid 11 實際渲染驗證） | ✅ |
