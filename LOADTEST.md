# 壓測與資源驗證

## 目錄

- [1. 測試機器與方法](#1-測試機器與方法)
- [2. Scenario A](#2-scenario-a)
- [3. Scenario B](#3-scenario-b)
- [4. 座位匯入](#4-座位匯入)
- [5. Excel 匯出](#5-excel-匯出)
- [6. 一致性檢查](#6-一致性檢查)
- [7. 瓶頸分析](#7-瓶頸分析)
- [第 1 輪 → 發現 → 修正 → 第 2 輪](#第-1-輪--發現--修正--第-2-輪)
- [8. 重現指令與產物](#8-重現指令與產物)

## 1. 測試機器與方法

| 筆電試跑 | 桌機正式跑 |
|---|---|
| **CPU**　Intel MacBook，2 核心／4 執行緒。 | **CPU**　AMD Ryzen 5 5600，6 核心／12 執行緒。 |
| **RAM／容器配額**　8 GB。Docker Desktop VM 4 CPU／5 GB（Excel 匯出紀錄）。 | **WSL2 配額**　12 個邏輯 CPU、約 16 GB RAM。 |
| **OS／版本**　macOS（Intel）。Excel 匯出日期為 2026-11-21（台北）。 | **OS／版本**　Windows + WSL2、Docker Desktop（Engine 29.9.3、Compose v5.7.0）。 |
| **拓樸**　筆電壓測的產壓程式、nginx、API、PostgreSQL 和其他服務共用宿主機。先前的 Scenario B 開發試跑使用單一 Uvicorn 和測試用 DB／Redis 容器，見 §3。 | **拓樸**　k6 0.57.0 與 Python SSE client 和服務共用桌機。3 個 API 副本，各跑 1 個 Uvicorn 程序，DB pool 上限各 10 條連線。見 `compose.yaml:23`、`compose.yaml:114`、`compose.yaml:120`、`backend/src/ticketing/api/main.py:39`、`loadtest/run.sh:116`。 |

| 正式輪次 | 日期（台北） | 測試版本 | B 寫入目標 | 證據 |
|---|---|---|---:|---|
| 第 1 輪 | 2026-11-21 | `91b2345` | 300 changes/s | [環境](docs/evidence/loadtest-run1-20261121/environment.txt) · [原始輸出](docs/evidence/loadtest-run1-20261121/) |
| 第 2 輪 | 2026-11-21 | `31d87c1` | 100 changes/s | [環境與映像 digest](docs/evidence/loadtest-run2-20261121/environment.txt) · [原始輸出](docs/evidence/loadtest-run2-20261121/) |

`compose.yaml` 預設啟動三個 API 副本（各 512 MiB）與 PostgreSQL 16。宿主機 nginx 預設 `127.0.0.1:8080`，產壓容器走 Compose network 的 `http://nginx:8080`。A 使用 `grafana/k6:0.57.0`，B 的 writer 與 SSE probe 使用專案 API image 中的 Python。兩個情境各自建立活動。見 `compose.yaml:55`、`compose.yaml:120`、`compose.yaml:122`、`compose.yaml:227`、`loadtest/run.sh`。

## 2. Scenario A

**參數與流量（第 2 輪腳本）**　單一開賣活動 10,000 席、3,000 個 `constant-vus`，持續 60 秒。準備腳本以 `POST /api/v1/auth/login` 登入主辦人，透過 API 建立活動、匯入座位並發布，並直接在 PostgreSQL 建立每位買家的獨立 session。初始 idle 與 absolute deadline 均為 12 小時（`loadtest/prepare_a.py:135`）。正常登入的預設分別為 30 分鐘與 12 小時。每次購買抽 1–4 張，約 70% 指定席、30% 區域配位。成功後約 80% 模擬付款並輪詢訂單、10% 取消、10% 保留至期限，另抽約 2% 用原請求內容及 Idempotency-Key 驗證重放。重放遇 502／504 或 `503 server-busy` 時依 `Retry-After`（預設 1 秒）重送，合計最多 10 次嘗試。每筆發起的重放均須收到回應並驗證為原訂單。觸及個人配額後，確認已有 4 張的 VU 切到查訂單。請求 timeout 5 秒，指定的可重試 409 最多重送兩次。見 `loadtest/prepare_a.py:72`、`loadtest/prepare_a.py:110`、`backend/src/ticketing/core/settings.py:46`、`loadtest/scenario_a.js`。

| 正式結果 | 第 1 輪 | 第 2 輪 |
|---|---|---|
| k6 HTTP 請求數／吞吐（含重試、付款、查詢） | 218,032／3,362 req/s | 134,801／2,098 req/s，全輪約 64 秒 |
| k6 HTTP p50／p95／p99 | 157 ms／1.94 s／2.28 s | 1,043／2,036／2,263 ms |
| k6 HTTP p90／max | — | 1,964／5,001 ms |
| nginx HTTP p50／p95／p99 | — | 1.042／2.035／2.256 s |
| k6 `server_busy`（全路徑） | 93,772 | 113,642（約 84%） |
| nginx 建單路徑 503／409／201 | — | 99,864／12,494／4,610 |
| 閘道與客戶端異常 | 104,136 筆立即回 502，access log 的 upstream 欄位只有群組名 `api`（沒有可用副本） | 502、500 各 0。k6 逾時 6 筆，nginx 均記為付款請求 499 |
| k6 `unexpected_errors` | 106,186 筆（48.70%） | 6 筆（約 0.00445%） |
| 成功鎖位 | k6 新建 4,228 筆，另有 8 筆成功重放，合計 4,236 筆建單 201 | k6 新建 4,538 筆。nginx 建單 201 共 4,610 筆，含 72 筆重放驗證回應 |
| 冪等重放 | 68 筆中 60 筆遭拒（53 筆 503、7 筆 502），其餘 8 筆均回原訂單 | 抽樣 89 筆：72 筆回傳原訂單（驗證正確，72/72），17 筆用完 10 次嘗試仍收到過載回應（503），列為未驗證 |
| 正式判定 | **FAILED**。k6 非預期錯誤率與重放查核未達門檻，SQL 回報付款待辦滯留 | **FAILED**。k6 門檻 `replay_unverified`、`unexpected_errors` 未達，runner 另因重放開始 89 筆、驗證 72 筆不相等而判定失敗（[執行紀錄](docs/evidence/loadtest-run2-20261121/run-log.txt)）。失敗項皆為過載拒絕與 6 筆逾時；驗證到的重放全部正確 |

吞吐與 k6 延遲取自 `http_reqs`、`http_req_duration`。nginx 延遲以 access log 的請求時間計算。第 1 輪 502 與重放拒絕的細項見 Git 紀錄 `b117cbb`、`d651c32`。[第 1 輪 k6](docs/evidence/loadtest-run1-20261121/a/k6-summary.json) · [第 2 輪 k6](docs/evidence/loadtest-run2-20261121/a/k6-summary.json) · [第 2 輪 nginx](docs/evidence/loadtest-run2-20261121/a/nginx-summary.txt)

| 排空後狀態 | 第 1 輪 | 第 2 輪 |
|---|---:|---:|
| `pending_payment`（訂單／票） | 3,842／7,928 | 4,194／8,509 |
| `paid`（訂單／票） | 320／584 | 263／488 |
| `cancelled`（訂單／票） | 66／134 | 81／156 |
| 座位 `available`／`held`／`sold` | 1,488／7,928／584 | 1,003／8,509／488 |
| 占席（`held + sold`） | 8,512／10,000 | 8,997／10,000 |

[第 1 輪最終狀態](docs/evidence/loadtest-run1-20261121/a/final-state.txt) · [第 2 輪最終狀態](docs/evidence/loadtest-run2-20261121/a/final-state.txt) · [一致性檢查](#6-一致性檢查)

**筆電壓測**　3,000 VUs 在 2 核心／4 執行緒機器上把 CPU 壓滿，產壓端與服務互相爭用。大多數請求快速收到 `503 server-busy`，或在客戶端 timeout。總吞吐約 **86–95 req/s**，同機 CPU 競爭明顯（當時以 `pg_stat_statements` 觀察到 SQL 平均約 35–170 ms，未留存原始輸出）。一致性檢查未發現超賣。筆電試跑僅有摘要。

## 3. Scenario B

**參數與流量（第 2 輪腳本）**　對同一活動預建 2,000 個獨立 session。SSE client 依可見 CPU 數啟動最多 8 個程序，各自先取得一份起始快照，所有連線在前 10 秒均勻啟動，並維持每第十條為慢速讀取。Runner 最多等 80 秒達成 2,000 條同時連線，達標後由 4 個 writer worker 以總目標 100 seat changes/s 寫入 55 秒。量測窗口 80 秒；窗口與 writer 都結束後，最多再等 20 秒排空。第 1 輪使用 `B_RATE=300`。各 writer worker 在不重疊的範圍切換座位狀態與版本，走正式 transactional outbox。累積量、速度、連線及最終比對都從工具實際輸出擷取。見 `loadtest/run.sh`、`loadtest/scenario_b_writer.py`、`loadtest/sse_client.py`。

| 正式結果 | 第 1 輪 | 第 2 輪 |
|---|---:|---:|
| SSE client 程序數 | 1 | 8 |
| 同時 SSE 峰值（全程／寫入期間） | 2,000／— | 2,000／2,000 |
| SSE 嘗試次數（含重連）／HTTP 錯誤數／錯誤率 | 9,673／937／9.69% | 14,800／32／0.22% |
| 建連 p50／p95／p99（ms） | 7,474／26,176／54,324 | 784／4,830／11,100 |
| 快照 p50／p95／p99（ms） | 63,995／73,733／75,981 | 978／7,540／7,810 |
| Delta 套用 p50／p95／p99（ms） | 3,614／38,833／79,780 | 266／8,060／16,600 |
| Delta 套用樣本 | 248,001 | 524,637 |
| Writer 寫入變更數／實得 changes/s | 16,512／299.8 | 5,504／99.7 |
| SSE payload 流量（MB/s） | 約 3.0 | 約 2.8 |
| 進度停滯／斷層／resync | 6,064／6,064／1,111 | 12,540／12,540／228 |
| 最終對上 DB 的 client | 1,326／2,000 | 2,000／2,000 |
| `outbox_pending`／`archive_mismatches` | 0／0 | 0／0 |

Delta 套用延遲從 frame 內最早的 outbox `created_at` 計至 client 套用，含提交前剩餘時間。SSE 錯誤率以含重連的嘗試數為分母。第 2 輪 client 偵測到進度停滯後主動重連，所有 client 最終收斂，尾端延遲仍高。見 `loadtest/sse_client.py:241`。[第 1 輪 SSE client](docs/evidence/loadtest-run1-20261121/b/sse-client.txt) · [第 1 輪 writer](docs/evidence/loadtest-run1-20261121/b/writer.txt) · [第 2 輪 SSE client](docs/evidence/loadtest-run2-20261121/b/sse-client.txt) · [第 2 輪 writer](docs/evidence/loadtest-run2-20261121/b/writer.txt)

**開發試跑（macOS x86_64、單一 Uvicorn）**

| 設定 | 峰值連線 | Writer 實得 changes/s | 套用 p50／p95／p99（ms） | DB 比對 |
|---|---:|---:|---|---|
| 2,000 條同時發起、目標 300 changes/s | 86 | 13.5 | 無 delta 樣本 | 0/2,000 probes 相符。outbox 待編序 0、archive 差異 0。 |
| 24 條、3 秒 ramp、目標 50 changes/s | 24 | 48.7 | 180／2,909／3,817 | 24/24 probes 相符。outbox 待編序 0、archive 差異 0。 |

Burst 的 8,337 次串流嘗試包含 6,110 次 HTTP request errors，並出現 DB session 重驗 timeout。Paced run 的 115 次嘗試包含 10 次 request errors、81 次 EOF，無 gap／resync，全部 probe 最後收斂。這兩次開發試跑都未達 2,000 條；正式桌機結果見上表。見 [Scenario B 本機量測](loadtest/scenario_b.md#local-measurements)。

## 4. 座位匯入

| 項目 | 結果 | 量測起訖 |
|---|---|---|
| 10,000 列 CSV | **3.3 秒**（筆電 API 試跑摘要） | 從提交 multipart 檔案到輪詢 job 為 `succeeded`，包含輪詢間隔。準備腳本的量測點見 `loadtest/prepare_a.py:77`。 |
| 100,000 列 CSV | **11.38 秒**（門檻 60 秒） | 測試先產檔，才開始計時。包含 API 上傳、worker tick 與讀取 terminal job。 |
| 100,000 列 JSON | **13.46 秒**（門檻 60 秒） | 同上，各格式獨立執行。 |

開發筆電實測，原始輸出見 [10 萬列匯入實測](docs/evidence/import-100k.md)。測試也確認 job 回報及 PostgreSQL 實際座位數都為 100,000。見 [test_import_100k_under_60s](backend/tests/imports/test_import_perf.py)（`backend/tests/imports/test_import_perf.py:16`）。可在已備妥 PostgreSQL 整合測試環境執行 `uv run pytest -s backend/tests/imports/test_import_perf.py`，或透過 `make test` 跑 Compose 測試。檔案上限及列數預設見 `backend/src/ticketing/core/settings.py:89`。

## 5. Excel 匯出

以 `scripts/seed_orders_500k.sql` 灌入 **500,000** 筆訂單，以 Admin 經 nginx 呼叫 `GET /api/v1/events/{event_id}/reports/sales.xlsx?tz=Asia/Taipei`，完成下載後用 openpyxl read-only 驗證。三個 API 副本均驗證 cgroup 的 512 MiB 上限。見 `backend/src/ticketing/api/routes/reports.py:124`、`compose.yaml:122` 及 [Excel 匯出原始量測](docs/evidence/cp4-excel-500k.md)。

| 項目 | 實測 |
|---|---|
| HTTP／下載時間 | 200。首次 **217.3 秒**，第二次 **236.5 秒**（含取樣開銷）。 |
| 匯出副本 cgroup 上限 | 536,870,912 bytes。 |
| 匯出副本匿名記憶體 `anon` 峰值 | **113,000,448 bytes（約 108 MiB）**。 |
| 同副本 `memory.current`／`file` 峰值 | 536,842,240／416,047,104 bytes。每 0.2 秒取樣，`file` 包含可回收 page cache。 |
| OOM／重啟 | 三副本 `OOMKilled=false`、`RestartCount=0`。 |
| 檔案驗證 | 33,419,621 bytes。含 `Summary`、`Zones`、`Orders`，明細 500,000 列，表頭標示 `Asia/Taipei`，金額以數值存放。 |

匯出使用 server-side cursor 每批 5,000 列與 xlsxwriter `constant_memory`，暫存檔寫入容器掛載的 export volume。nginx 報表專用 `proxy_read_timeout` 為 300 秒。匯出量測涵蓋單次匯出與檔案驗證。倉庫另有要求 cgroup `memory.peak` 的 opt-in 測試，這次用 cgroup v2 取樣。見 `backend/src/ticketing/reports/sales.py:30`、`backend/src/ticketing/reports/sales.py:289`、`compose.yaml:118`、`infra/nginx/nginx.conf:151`、`backend/tests/reports/test_export_memory.py:22`。

## 6. 一致性檢查

Scenario A 結束後腳本先等 90 秒，針對該活動執行 `loadtest/consistency.sql`，再列出訂單及座位狀態。預設 hold TTL 為 600 秒，90 秒時仍可能有有效的未付款鎖位。正式紀錄保留腳本輸出。需要檢查 hold 到期後的狀態時，可在 worker 與 relay 處理完待辦後重驗。以執行成功且回傳零違規列為準。見 `loadtest/run.sh:125`、`compose.yaml:22`、`loadtest/consistency.sql:1`。

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
docker compose exec -T postgres psql -XAt -U ticketing -d ticketing \
  -v event_id="${EVENT_ID:?set EVENT_ID}" -v expected_seats="${SEATS:-10000}" \
  -f - < loadtest/consistency.sql
```

| 正式輪次 | 活動 ID | 等待 90 秒後的 `consistency.sql` 原始輸出 |
|---|---:|---|
| 第 1 輪 | [3](docs/evidence/loadtest-run1-20261121/a/event.json) | [205 列](docs/evidence/loadtest-run1-20261121/a/consistency.txt)，全為 `attempt_stalled`。該輪查核未發現超賣、配額、金額或帳本違規。 |
| 第 2 輪 | [5](docs/evidence/loadtest-run2-20261121/a/event.json) | [空檔（0 列）](docs/evidence/loadtest-run2-20261121/a/consistency.txt)，含 `attempt_stalled` 在內的查核均未回報違規。 |

兩輪均守住庫存、配額、金額與帳本約束。

## 7. 瓶頸分析

| 情境 | 觀察與正式量測判讀 |
|---|---|
| A 筆電壓測 | 筆電 CPU 飽和，k6 與 nginx、API、PostgreSQL 等服務共享 CPU。SQL mean 上升，503 和 timeout 主導結果。程式在 pool acquire、鎖及查詢逾時時會提早回可重試 503。試跑的總 RPS 反映這台機器上的混合結果。見 `backend/src/ticketing/core/db.py:39`、`backend/src/ticketing/booking/database.py:15`。 |
| A 桌機 | 第 2 輪 p50 約 1 秒，與連線池取用上限相符。滿載請求等待 DB pool，取用逾時即回可重試的 503。壓測期間 `docker stats`（[擷圖](docs/evidence/loadtest-run2-20261121/docker-stats-during-a.png)）顯示 3 個單程序 API 副本各約 105% CPU（各用滿 1 核心），PostgreSQL 約 155%、k6 約 125%（主機 12 執行緒），據此研判 API 程序 CPU 是主要瓶頸、PostgreSQL 仍有餘裕（單一時點擷圖，非持續量測；以增加 API 副本重跑可直接驗證）。管理後台截圖可見 HTTP 流量、延遲、錯誤率與連線數。見 `backend/src/ticketing/core/settings.py:34`、`backend/src/ticketing/core/db.py:48`。[第 2 輪秒級監控](docs/evidence/loadtest-run2-20261121/a/metrics_1s.csv) · [壓測中監控畫面](docs/screenshots/11-monitoring-under-load.png) |
| B 桌機 | 第 1 輪單程序 client 飽和，服務端 session 重驗失敗也造成整批斷線。第 2 輪仍有進度停滯觸發重連，但最終全部對上 DB，outbox 待編序與 archive 差異均為 0。見 `backend/src/ticketing/realtime/reader.py:161`、`backend/src/ticketing/realtime/revocation.py:101`。[第 1 輪 SSE client](docs/evidence/loadtest-run1-20261121/b/sse-client.txt) · [第 2 輪 SSE client](docs/evidence/loadtest-run2-20261121/b/sse-client.txt) |
| 座位匯入／Excel 匯出 | 匯入計時含背景作業與回報。Excel 在同機實測已完成，`memory.current` 的峰值接近上限，同時可見大量可回收 page cache。更高磁碟 I/O 或與其他流量共用資源時的影響尚待量測。 |

擴容可調高 Compose 的 `API_REPLICAS`、調整 API 啟動方式以增加每副本的 Uvicorn worker 數，或將產壓端移到另一台機器。增加 API 程序時需一併核算各程序的連線池。見 `compose.yaml:23`、`compose.yaml:120`、`backend/src/ticketing/api/main.py:39`、`loadtest/run.sh:222`。

## 第 1 輪 → 發現 → 修正 → 第 2 輪

| 第 1 輪發現 | 修正（Git commit） | 第 2 輪觀察 |
|---|---|---|
| A 過載期間，nginx 預設 `max_fails` 將 3 個 API 副本全部暫時移出 upstream，回出大量立即 502。 | `b117cbb` 設 `max_fails=0`，nginx upstream keepalive 縮至 60 秒，早於 API 的 Uvicorn 75 秒。`654e775` 對齊 paymock 的 Uvicorn 設定。 | 502 為 0，過載主要回 503。 |
| A 的付款 reconciler 來不及輪詢，SQL 回報 `attempt_stalled`。 | `b117cbb`、`a6fc12d`、`654e775` 重設各階段預算與領取批次。`b159b0e` 在逾時或關停時交還未完成的工作。 | `attempt_stalled` 為 0。 |
| A 的重放受過載拒絕，收到回應的重放均正確。 | `d651c32` 依 `Retry-After` 重試，`593316c` 加入重放驗證與進度計數，`b159b0e` 要求每筆重放均須驗證。 | 仍有重放在重試後遭拒，k6 判定 **FAILED**。 |
| B 單程序 client 飽和，副本的 session 重驗在 0.2 秒取連線／0.3 秒查詢預算下連續失敗 2 次即關閉串流。 | `8276c17`、`0c5c3a7` 分流及精簡 client 狀態，第 2 輪實際用 8 程序、10 秒爬升。`ea3f0cc` 將重驗取連線與查詢預算各調為 1 秒，連續失敗 5 次才關閉。`9a6deb3` 記錄寫入目標由 300 調至 100 changes/s。 | 峰值及最終 DB 比對均達 2,000。兩輪寫入速率不同。 |
| 冷啟動試跑曾出現 DB timeout 造成的 500，服務映像及資源設定也需更新。 | `a31d86a`、`654e775` 將 DB timeout 映射為可重試 503，`b159b0e`、`ea3f0cc` 調整連線釋放。`2b3c0d1`、`b159b0e` 更新映像，`73221cd` 設容器記憶體上限及日誌輪替，`7ff3b9a` 記錄運行映像 digest。 | A 的 500 為 0，運行映像可由 [環境紀錄](docs/evidence/loadtest-run2-20261121/environment.txt) 追溯。 |

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
