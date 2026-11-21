# 壓測與資源驗證

## 目錄

- [1. 測試機器與方法](#1-測試機器與方法)
- [2. Scenario A](#2-scenario-a)
- [3. Scenario B](#3-scenario-b)
- [4. 座位匯入](#4-座位匯入)
- [5. Excel 匯出](#5-excel-匯出)
- [6. 一致性檢查](#6-一致性檢查)
- [7. 瓶頸分析](#7-瓶頸分析)
- [8. 重現指令與產物](#8-重現指令與產物)

## 1. 測試機器與方法

| 筆電試跑 | 桌機正式跑 |
|---|---|
| **CPU**　Intel MacBook，2 核心／4 執行緒。 | **CPU**　AMD Ryzen 5 5600，6 核心／12 執行緒。 |
| **RAM／容器配額**　8 GB。Docker Desktop VM 4 CPU／5 GB（Excel 匯出紀錄）。 | **RAM／容器配額**　24 GB。Windows + WSL2 + Docker Desktop，WSL 實際可用的 CPU 與記憶體見 `environment.txt`。 |
| **OS／版本**　macOS（Intel）。Excel 匯出日期為 2026-11-21（台北）。 | **OS／版本／日期／commit**　（待桌機壓測結果，填入 `environment.txt`） |
| **拓樸**　筆電壓測的產壓程式、nginx、API、PostgreSQL 和其他服務共用宿主機。先前的 Scenario B 開發試跑使用單一 Uvicorn 和測試用 DB／Redis 容器，見 §3。 | **拓樸**　在另一台桌機執行 `loadtest/run.sh`。預設產壓端與整個 Compose stack 仍共用該機器。實際 CPU 限額與服務配置（待桌機壓測結果）。 |

`compose.yaml` 預設啟動三個 API 副本（各 512 MiB）與 PostgreSQL 16。宿主機 nginx 預設 `127.0.0.1:8080`，產壓容器走 Compose network 的 `http://nginx:8080`。A 使用 `grafana/k6:0.57.0`，B 的 writer 與 SSE probe 使用專案 API image 中的 Python。兩個情境各自建立活動。見 `compose.yaml:52`、`compose.yaml:120`、`compose.yaml:223`、`loadtest/run.sh`。

## 2. Scenario A

**參數與流量**　單一開賣活動 10,000 席、3,000 個 `constant-vus`，持續 60 秒。準備腳本以 `POST /api/v1/auth/login` 登入主辦人，透過 API 建立活動、匯入座位並發布，並直接在 PostgreSQL 建立每位買家的獨立 session。初始 idle 與 absolute deadline 均為 12 小時。正常登入的預設分別為 30 分鐘與 12 小時。每次購買抽 1–4 張，約 70% 指定席、30% 區域配位。成功後約 80% 模擬付款並輪詢訂單、10% 取消、10% 保留至期限，另抽約 2% 用原請求內容及 Idempotency-Key 驗證重放。重放遇 502／504 或 `503 server-busy` 時依 `Retry-After`（預設 1 秒）重送，合計最多 10 次嘗試。每筆有回覆的重放須符合原訂單，發起重放時至少一筆須驗證成功。觸及個人配額後，確認已有 4 張的 VU 切到查訂單。請求 timeout 5 秒，指定的可重試 409 最多重送兩次。見 `loadtest/prepare_a.py:56`、`loadtest/prepare_a.py:72`、`loadtest/prepare_a.py:110`、`backend/src/ticketing/core/settings.py:46`、`loadtest/scenario_a.js`。

| 正式結果 | 數值 | 來源／口徑 |
|---|---|---|
| k6 總 RPS | （待桌機壓測結果） | `a/k6-summary.json` 的 `metrics.http_reqs.rate`。包含重試、付款與查詢。 |
| k6 HTTP p50／p95／p99（ms） | （待桌機壓測結果） | `a/k6-summary.json` 的 `metrics.http_req_duration`，欄位為 `med`、`p(95)`、`p(99)`。 |
| nginx HTTP p50／p95／p99（ms） | （待桌機壓測結果） | `a/nginx-access.json` 中帶本輪 run ID 的 `req_ms` 實為秒，乘 1,000 後對整輪樣本計算百分位數。nginx 與 k6 各有自己的樣本口徑。 |
| 非預期錯誤率 | （待桌機壓測結果） | k6 `unexpected_errors`，例如逾時、未列入業務清單的 409、回應碼並非 `server-busy` 的 503。`http_req_failed` 將 409 視為預期回應，並計入 503。 |
| 成功鎖位、業務衝突、過載 | （待桌機壓測結果） | k6 的 `successful_holds` 計新建鎖位，`business_conflicts` 計已知業務 409 和 `503 server-busy`，後者另由 `server_busy` 計數。用同輪 nginx 狀態碼交叉核對，分別記錄客戶端逾時與 nginx 在客戶端斷線時記錄的 499。 |
| 訂單／座位最終狀態、SQL 違規列 | （待桌機壓測結果） | `a/final-state.txt`、`a/consistency.txt`，排空後重驗見 §6。 |

**筆電壓測**　3,000 VUs 在 2 核心／4 執行緒機器上把 CPU 壓滿，產壓端與服務互相爭用。大多數請求快速收到 `503 server-busy`，或在客戶端 timeout。總吞吐約 **86–95 req/s**，SQL mean 約 **35–170 ms**，同機 CPU 競爭明顯。一致性檢查未發現超賣。筆電試跑僅有摘要。完整 k6 percentile、錯誤率與 SQL 輸出待桌機正式量測。

## 3. Scenario B

**參數與流量**　對同一活動預建 2,000 個獨立 session。SSE client 依可見 CPU 數啟動最多 8 個程序，各自先取得一份起始快照，所有連線在前 10 秒均勻啟動，並維持每第十條為慢速讀取。Runner 最多等 80 秒達成 2,000 條同時連線，達標後由 4 個 writer worker 以總目標 100 seat changes/s 寫入 55 秒。至少量測 80 秒，並於 writer 結束後最多再等 20 秒排空。第 1 輪使用 `B_RATE=300`。各 writer worker 在不重疊的範圍切換座位狀態與版本，走正式 transactional outbox。累積量、速度、連線及最終比對都從工具實際輸出擷取。見 `loadtest/run.sh`、`loadtest/scenario_b_writer.py`、`loadtest/sse_client.py`。

| 正式結果 | 數值 | 來源／口徑 |
|---|---|---|
| 全程／寫入期間同時 SSE 峰值及目標 | （待桌機壓測結果）／2,000 | `peak_active`、`peak_active_during_writes`、`target_connections`。寫入期間以 writer 程序的執行時間為準。 |
| SSE 嘗試數／errors／錯誤率 | （待桌機壓測結果） | `stream_attempts`、`stream_request_errors`、`stream_error_rate`。分母為 SSE 嘗試數，含重連。 |
| SSE 建連／快照 p50／p95／p99（ms） | （待桌機壓測結果） | 合併各程序的延遲分桶後計算 `connect_ms`、`snapshot_ms`，建連計時自發出 SSE 請求起算。 |
| Delta 套用樣本數及 p50／p95／p99（ms） | （待桌機壓測結果） | `delta_apply_samples` 與 `delta_apply_ms`。延遲從合併 frame 內最早的 outbox `created_at` 計至 probe 套用，包含提交前剩餘時間。 |
| Writer 變更數／changes/s；SSE bytes/s | （待桌機壓測結果） | `b/writer.txt` 的 `changes`、`changes_per_sec`，以及 `sse_body_bytes_per_second`。長連線持續流量用變更速率與 bytes/s 表示。 |
| 斷層／resync／EOF | （待桌機壓測結果） | `b/sse-client.txt` 的 probe 計數。 |
| 最終對上 DB 的 probe／outbox 待編序／archive 差異 | （待桌機壓測結果） | `matching_clients`、`outbox_pending`、`archive_mismatches`。 |

**開發試跑（macOS x86_64、單一 Uvicorn）**

| 設定 | 峰值連線 | Writer 實得 changes/s | 套用 p50／p95／p99（ms） | DB 比對 |
|---|---:|---:|---|---|
| 2,000 條同時發起、目標 300 changes/s | 86 | 13.5 | 無 delta 樣本 | 0/2,000 probes 相符。outbox 待編序 0、archive 差異 0。 |
| 24 條、3 秒 ramp、目標 50 changes/s | 24 | 48.7 | 180／2,909／3,817 | 24/24 probes 相符。outbox 待編序 0、archive 差異 0。 |

Burst 的 8,337 次串流嘗試包含 6,110 次 HTTP request errors，並出現 DB session 重驗 timeout。Paced run 的 115 次嘗試包含 10 次 request errors、81 次 EOF，無 gap／resync，全部 probe 最後收斂。兩輪都尚未驗證 2,000 條目標達成。見 [Scenario B 本機量測](loadtest/scenario_b.md#local-measurements)。

## 4. 座位匯入

| 項目 | 結果 | 量測起訖 |
|---|---|---|
| 10,000 列 CSV | **3.3 秒**（筆電 API 試跑摘要） | 從提交 multipart 檔案到輪詢 job 為 `succeeded`，包含輪詢間隔。準備腳本的量測點見 `loadtest/prepare_a.py:77`。 |
| 100,000 列 CSV | **低於 60 秒**（`test_import_100k_under_60s`） | 測試先產檔，才開始計時。包含 API 上傳、worker tick 與讀取 terminal job。 |
| 100,000 列 JSON | **低於 60 秒**（`test_import_100k_under_60s`） | 同上，各格式獨立執行。 |

100,000 列的個別秒數未附於紀錄。測試也確認 job 回報及 PostgreSQL 實際座位數都為 100,000。見 [test_import_100k_under_60s](backend/tests/imports/test_import_perf.py)（`backend/tests/imports/test_import_perf.py:16`）。可在已備妥 PostgreSQL 整合測試環境執行 `uv run pytest -s backend/tests/imports/test_import_perf.py`，或透過 `make test` 跑 Compose 測試。檔案上限及列數預設見 `backend/src/ticketing/core/settings.py:89`。

## 5. Excel 匯出

以 `scripts/seed_orders_500k.sql` 灌入 **500,000** 筆訂單，以 Admin 經 nginx 呼叫 `GET /api/v1/events/{event_id}/reports/sales.xlsx?tz=Asia/Taipei`，完成下載後用 openpyxl read-only 驗證。三個 API 副本均驗證 cgroup 的 512 MiB 上限。見 `backend/src/ticketing/api/routes/reports.py:124`、`compose.yaml:120` 及 [Excel 匯出原始量測](docs/evidence/cp4-excel-500k.md)。

| 項目 | 實測 |
|---|---|
| HTTP／下載時間 | 200。首次 **217.3 秒**，第二次 **236.5 秒**（含取樣開銷）。 |
| 匯出副本 cgroup 上限 | 536,870,912 bytes。 |
| 匯出副本匿名記憶體 `anon` 峰值 | **113,000,448 bytes（約 108 MiB）**。 |
| 同副本 `memory.current`／`file` 峰值 | 536,842,240／416,047,104 bytes。每 0.2 秒取樣，`file` 包含可回收 page cache。 |
| OOM／重啟 | 三副本 `OOMKilled=false`、`RestartCount=0`。 |
| 檔案驗證 | 33,419,621 bytes。含 `Summary`、`Zones`、`Orders`，明細 500,000 列，表頭標示 `Asia/Taipei`，金額以數值存放。 |

匯出使用 server-side cursor 每批 5,000 列與 xlsxwriter `constant_memory`，暫存檔寫入容器掛載的 export volume。nginx 報表專用 `proxy_read_timeout` 為 300 秒。匯出量測涵蓋單次匯出與檔案驗證。倉庫另有要求 cgroup `memory.peak` 的 opt-in 測試，這次用 cgroup v2 取樣。見 `backend/src/ticketing/reports/sales.py:30`、`backend/src/ticketing/reports/sales.py:289`、`compose.yaml:116`、`infra/nginx/nginx.conf:138`、`backend/tests/reports/test_export_memory.py:22`。

## 6. 一致性檢查

Scenario A 結束後腳本先等 90 秒，針對該活動執行 `loadtest/consistency.sql`，再列出訂單及座位狀態。預設 hold TTL 為 600 秒，90 秒時仍可能有有效的未付款鎖位。正式紀錄保留腳本原輸出，並於該活動的 hold 到期、worker 處理完付款待辦且 relay 排空後再執行一次。以執行成功且回傳零違規列為準。見 `loadtest/run.sh`、`compose.yaml:22`、`loadtest/consistency.sql:1`。

| SQL 回報項 | 查核 |
|---|---|
| `published_event_missing`、`unexpected_inventory_size` | 指定活動已發布，座位總數等於預期。 |
| `seat_active_claims`、`order_item_link` | 可用席無 active item，held／sold 各有唯一且相符的 active item。核對座位 owner、活動、區域與單價。 |
| `order_items_or_amount`、`user_event_quota` | 明細與 active item 數符合訂單狀態，金額與服務費正確。配額與仍占席的訂單加總一致且在 0–4 張。 |
| `overdue_hold` | 沒有超過期限 30 秒仍為 `pending_payment` 的訂單。有效 hold 可留在第一次檢查。 |
| `capture_refund_ledger`、`accepted_capture_count` | capture 金額、分類與退款資料符合訂單狀態，已付款及退款中的訂單／退款完成訂單各有一筆 accepted capture。 |
| `attempt_stalled`、`refund_stalled`、`cancellation_stalled` | attempt 建立或 poll、退款及取消工作沒有跨過 SQL 所定的 30 秒滯留門檻。後兩者也考慮 lease、下次重試時間。 |
| `booking_replay_record` | 已完成的 create-order replay 有對應 owner 與訂單，保存的狀態碼、resource ref、Location 一致。 |
| `outbox_unassigned`、`event_sequence`、`seat_event_missing`、`seat_event_extra` | outbox 完成編序。保留的序號須連續且抵達 DB head，封存的逐席變更須在該範圍內與 outbox 相符。 |

維護程序會清除已編序超過一小時的 outbox 資料。A 的 SQL 與 B 的 SSE 稽核在保留的序號範圍內比對封存資料，範圍內缺號仍會回報。B 使用合成訂單與座位資料，寫入程式須成功結束並提交座位變更，客戶端須記錄至少一筆 delta 套用樣本。每條連線的游標、版面版本、座位索引、狀態與版本都須與同一份最終 PostgreSQL 快照相符。通過條件包括 `peak_active = matching_clients = target_connections` 及 `outbox_pending = archive_mismatches = 0`。見 `loadtest/consistency.sql`、`loadtest/sse_client.py`。

A 排空後重驗指令。將 `EVENT_ID` 設為 `a/event.json` 的活動 ID，`SEATS` 設為該輪預期庫存。

```bash
docker compose exec -T postgres psql -U ticketing -d ticketing \
  -v event_id="${EVENT_ID:?set EVENT_ID}" -v expected_seats="${SEATS:-10000}" \
  -f - < loadtest/consistency.sql
```

**正式 SQL 輸出（含活動 ID 與檢查時間）**　（待桌機壓測結果）

## 7. 瓶頸分析

| 情境 | 觀察與正式量測判讀 |
|---|---|
| A 筆電壓測 | 筆電 CPU 飽和，k6 與 nginx、API、PostgreSQL 等服務共享 CPU。SQL mean 上升，503 和 timeout 主導結果。程式在 pool acquire、鎖及查詢逾時時會提早回可重試 503。試跑的總 RPS 反映這台機器上的混合結果。見 `backend/src/ticketing/core/db.py:39`、`backend/src/ticketing/booking/database.py:15`。 |
| A 桌機待驗證 | 預期受 CPU、DB pool 等待、熱座位及同使用者配額鎖、付款回呼和 DB I/O 影響。比對同一時段的 k6、nginx、App／業務每秒資料、每容器 CPU 與 PostgreSQL wait。將成功 hold、業務 409 與 503 分開看。 |
| B 開發試跑與待驗證 | Burst 已出現 session DB 重驗 timeout。正式跑應對照連線建立、各副本連線分布、DB pool、relay backlog、重連、delta apply 與最終 probe 數。共享 reader 把 PG polling 按活動攤銷，SSE fan-out、連線傳輸及重新驗證仍需資源。見 `backend/src/ticketing/realtime/reader.py:161`、`backend/src/ticketing/realtime/revocation.py:88`。 |
| 座位匯入／Excel 匯出 | 匯入計時含背景作業與回報。Excel 在同機實測已完成，`memory.current` 的峰值接近上限，同時可見大量可回收 page cache。更高磁碟 I/O 或與其他流量共用資源時的影響尚待量測。 |

## 8. 重現指令與產物

從 repo root 使用 Git、Bash、Docker（Compose v2）及 `curl`。使用預設主辦人憑證、SQL 帳號和資料庫 `ticketing`、`sid` cookie，並允許 `http://localhost:8080` Origin。寫入輸出的容器以執行者的 UID/GID 執行，Linux bind mount 不需額外設定。腳本透過 `http://127.0.0.1:${HTTP_PORT:-8080}/healthz/live` 檢查既有服務，未啟動時執行 `docker compose up -d --build`。若要按當前 checkout 重建可加 `REBUILD=1`。Shell 設定 `API_REPLICAS` 時，runner 會在情境開始前依實際運行數調整 API 副本並等待就緒。自訂 host port 請由 shell 傳入，例如 `HTTP_PORT=8081 loadtest/run.sh smoke`。見 `loadtest/run.sh`、`loadtest/prepare_a.py:56`。

```bash
loadtest/run.sh smoke
loadtest/run.sh all
loadtest/run.sh a
loadtest/run.sh b
```

`smoke` 為 A 50 VUs／15 秒／1,000 席與 B 50 SSE／30 秒／20 changes/s。`all` 依序跑正式參數的 A、B。`a` 和 `b` 可獨立執行。B 預設 100 changes/s，可用 `B_RATE=300 loadtest/run.sh b` 重現第 1 輪速率設定。`SSE_PROCESSES` 與 `SSE_RAMP` 分別覆寫 SSE 程序數與連線爬升秒數，實際程序數可在 `b/sse-client.txt` 的 `processes` 查閱。可用 `VUS`、`DURATION`、`SEATS`、`CONNECTIONS`、`B_DURATION` 覆寫規模與時間。A、B 共用 `SEATS`。A 向下取 4 的倍數，SQL 仍用原值核對。B 至少 16 席，且 `B_DURATION` 為大於 25 的整數秒。`K6_CPUS` 可限制 A 的產壓容器 CPU，設值時需記入正式報告。自訂 `OUT` 時使用 `loadtest/` 下的相對路徑，以配合容器的掛載。見 `loadtest/run.sh`。

| 產物 | 用途 |
|---|---|
| `loadtest/out/<timestamp>/environment.txt`、`run.log` | 填機器規格、版本、commit、參數、執行摘要及情境開始前實際運行的 API 副本數。 |
| `a/k6-summary.json`、`a/k6.log`、`a/nginx-access.json` | 填 A 的 RPS、latency、錯誤率與狀態分布。 |
| `a/metrics_1s.csv`、`a/business_1s.csv`、`a/event.json` | 對照系統與業務秒級資料，定位該輪活動。 |
| `a/consistency.txt`、`a/final-state.txt` | 核對 SQL 輸出及訂單／座位統計。保留排空後重驗輸出。 |
| `b/seed.txt`、`b/writer.txt`、`b/sse-client.txt` | 填 B 的活動、寫入成績、連線與最終 audit。 |
| `loadtest/out/<timestamp>.tar.gz` | 腳本進入打包步驟時刪除 `sessions.json`。提早中斷時憑證可能留在輸出目錄。 |

A 的 k6 閾值或查核失敗、SQL 執行失敗或回傳違規列，以及 B 的寫入程式或 SSE 稽核失敗時，腳本仍會打包結果，在終端與 `run.log` 列出 `FAILED` 和失敗項目，並以非零狀態結束。可用 §6 的指令在鎖位到期及待辦排空後重驗 A。權限、各參數限制與手動執行的 `LOADTEST_DATABASE_URL` 見 [壓測工具包](loadtest/README.md)。
