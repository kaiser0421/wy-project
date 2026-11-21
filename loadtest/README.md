# 壓測工具包

## 執行前提

- 從 repository 根目錄使用 Git、Bash、Docker（含 Compose v2）與 `curl`。Python 工具在 API image 內執行，情境 A 的 k6 使用 `grafana/k6:0.57.0`。見 `loadtest/run.sh`。
- 使用可拋棄的 Compose 堆疊。Runner 假設預設主辦人 `user@example.com`／`User#Dev2026!`、SQL 帳號及資料庫皆名為 `ticketing`、cookie 名為 `sid`，且允許 `http://localhost:8080` 作為 Origin。只改 Compose 的憑證設定無法改寫 runner 的固定值。見 `loadtest/run.sh`、`loadtest/prepare_a.py:46–57`、`loadtest/prepare_a.py:69–72`、`loadtest/run.sh:53`、`loadtest/prepare_a.py:145`、`loadtest/sse_client.py`。
- 寫入 `loadtest/` 的容器（Python 工具與 k6）以執行者的 UID/GID 執行（`loadtest/run.sh`），Linux bind mount 不需額外調整權限。

## 執行與參數

```bash
loadtest/run.sh smoke
loadtest/run.sh all
```

`smoke` 預設跑 A（50 VUs／15 秒／1,000 席）與 B（50 條 SSE／30 秒／20 changes/s）。`all` 依序跑 A 與 B 的正式規模，也可用 `loadtest/run.sh a` 或 `loadtest/run.sh b` 單獨執行。腳本沿用通過 host health probe 的堆疊，否則執行 `docker compose up -d --build`。見 `loadtest/run.sh`。

| Shell 變數 | 正式預設值 | 說明 |
|---|---|---|
| `API_REPLICAS` | Compose 預設為 `3` | Shell 設為正整數時，runner 會在情境開始前依實際運行數調整 API 副本，並等待 API 就緒。未設定時沿用既有堆疊的副本數。 |
| `VUS` | `3000` | A 的虛擬使用者數（正整數，每人一組獨立帳號與 session）。 |
| `DURATION` | `60s` | A 的正值 k6 duration，需帶時間單位。 |
| `SEATS` | `10000` | A、B 共用。A 平均分至四區，會向下取 4 的倍數，但 SQL 仍以原值核對，故請用正的 4 倍數且不超過匯入的 100,000 列上限。B 至少 16 席（4 個 writer × 每批 4 席）。 |
| `CONNECTIONS` | `2000` | B 的 SSE 訂閱數（正整數，每條獨立 session）。 |
| `B_DURATION` | `80` | B 的最短量測秒數與等待全部 SSE 同時連上的上限，含連線爬升，須為大於 25 的整數。達標後啟動 writer，寫入 `B_DURATION-25` 秒。連線較晚達標時，量測會等 writer 結束再排空。未達標時跳過寫入並判定失敗。 |
| `B_RATE` | `100` | 熱門活動每秒的目標座位變更數，須為正值。第 1 輪使用 300 changes/s，可用 `B_RATE=300 loadtest/run.sh b` 重現該速率設定。 |
| `SSE_PROCESSES` | 至多 8 個可見 CPU | SSE worker 程序數，對應 `--processes`。須為正整數，實際程序數最多等於訂閱數。 |
| `SSE_RAMP` | `10` | 首次 SSE 連線均勻啟動的秒數，對應 `--ramp-seconds`。設為 `0` 可同時啟動。 |
| `K6_CPUS` | 未設定 | 限制 k6 容器可用 CPU，例如 `K6_CPUS=2`。 |
| `REBUILD` | `0` | 設為 `1` 時執行 Compose 建置及啟動，即使既有入口已通過探測。 |
| `OUT` | `loadtest/out/<時間戳>` | 使用 `loadtest/` 下的相對路徑，容器會映射到 `/lt/`。每輪用新的輸出目錄。 |
| `HTTP_PORT` | `8080` | Runner 的 host probe 直接讀 shell。自訂 host port 可執行 `HTTP_PORT=8081 loadtest/run.sh smoke`。容器內仍走 `http://nginx:8080`。 |

每個 API 副本的 PostgreSQL pool 最多使用 `DB_POOL_MAX` 條連線，預設為 10。每個副本另有座位串流 session 重驗使用的連線（最多 2 條），API 的連線預算為實際副本數 × (`DB_POOL_MAX` + 2)，例如 3 個副本最多 36 條。PostgreSQL 容量也需容納 worker、relay、collector、產壓工具及其他使用者的連線。調整 `DB_POOL_MAX` 後可用 `REBUILD=1` 套用 Compose 設定。見 `compose.yaml`。

參數實作見 `loadtest/run.sh`、`loadtest/prepare_a.py:61–65`、`loadtest/scenario_b_writer.py`、`backend/src/ticketing/core/settings.py:90`。

A 以 `POST /api/v1/auth/login` 登入主辦人。買家及其 session 直接寫入 PostgreSQL，避開登入限流。合成 session 的初始 idle 與 absolute deadline 皆為 12 小時，正常登入預設分別為 30 分鐘與 12 小時。直接執行 `prepare_a.py` 時，可透過 `LOADTEST_DATABASE_URL` 或 `--database-url` 指定寫入 fixture 的 DSN，未設定時依序使用 `DATABASE_URL` 或本機預設 DSN。Runner 使用 Compose 注入的 `DATABASE_URL`，不會從 shell 轉傳 `LOADTEST_DATABASE_URL`。見 `loadtest/prepare_a.py:51–57`、`loadtest/prepare_a.py:69–72`、`loadtest/prepare_a.py:121–146`、`backend/src/ticketing/core/settings.py:46–47`、`loadtest/run.sh`、`compose.yaml:7`。

## 座位匯入資料

在 repo 根目錄用 [`scripts/gen_seats.py`](../scripts/gen_seats.py#L28) 各產生一份 100,000 列 CSV 與 JSON。輸出格式由副檔名決定，`--zone` 預設為 `A`。

```bash
uv run python scripts/gen_seats.py 100000 /tmp/seats_100k.csv --zone A
uv run python scripts/gen_seats.py 100000 /tmp/seats_100k.json --zone A
```

每個範例檔案與模式各用一個尚無座位、只含區域 `A` 的草稿活動驗證。

| 檔案 | `all_or_nothing` | `skip_invalid` |
|---|---|---|
| [seats_sample.csv](../samples/seats_sample.csv) | `succeeded`，新增 3 席 | `succeeded`，新增 3 席 |
| [seats_sample.json](../samples/seats_sample.json) | `succeeded`，新增 3 席 | `succeeded`，新增 3 席 |
| [seats_invalid.csv](../samples/seats_invalid.csv) | `validation_failed`，新增 0 席，6 列錯誤 | `partially_succeeded`，新增 `A/R1/1`，6 列錯誤 |
| [seats_duplicates.json](../samples/seats_duplicates.json) | `validation_failed`，新增 0 席，2 列錯誤 | `partially_succeeded`，新增 `A/R1/2`，2 列錯誤 |

重複座位以區域和排號的大寫值及去除前導零的座號比對，同組每列均標記為錯誤。見 `backend/src/ticketing/imports/validation.py:17–28`、`backend/src/ticketing/imports/runner.py:145`。

## 輸出與結果核對

| 產物（相對於 `OUT`） | 內容 |
|---|---|
| `environment.txt`、`run.log` | 主機、Docker、commit、主要參數及執行摘要。包含 shell 的 `API_REPLICAS` 與情境開始前實際運行的 `API_REPLICAS_RUNNING`。 |
| `logs/<服務>.log`、`logs/<服務>-problems.log` | 本次執行期間各服務（api、worker、relay、collector、paymock、nginx、redis）的容器日誌；PostgreSQL 日誌可能含 bind 參數，不收錄。打包前所有文字檔中的付款頁 token（`/pay/<token>`）一律遮蔽。API 日誌包含 `request.completed`（健康檢查、SSE 除外），可透過 `request_id` 與 nginx 存取日誌關聯。API 日誌佇列滿時直接丟棄並計數，請求不等待空位。Compose 的 `--tail` 按容器套用，api、worker、relay、collector 的每個容器最多擷取 20,000 行，其他服務的每個容器最多 300 行。每個服務彙整後的 `.log` 檔上限為 20 MB，`-problems.log` 最多取其中 5,000 行問題紀錄。`environment.txt` 另記錄每個容器實際使用的映像檔 digest。見 `loadtest/run.sh:69–80`、`loadtest/run.sh:176–207`、`backend/src/ticketing/api/middleware.py:65–70`、`backend/src/ticketing/api/middleware.py:85–97`、`backend/src/ticketing/core/logging.py:101–104`；`request_id` 於 `backend/src/ticketing/api/middleware.py:47–54` 寫入日誌上下文，nginx 存取日誌欄位見 `infra/nginx/nginx.conf:40`。 |
| `a/k6.log`、`a/k6-summary.json` | `metrics.http_reqs.rate` 為總 RPS。`metrics.http_req_duration` 直接匯出 `med`、`p(95)`、`p(99)`（毫秒）。`successful_holds` 計新建鎖位，`business_conflicts` 計已知業務 409 和 `503 server-busy`，其中後者也由 `server_busy` 單獨計數。`replay_started` 計開始的重放，`replay_verified` 計驗證成功的重放，`replay_unverified` 計重試後仍收到 502／503／504 的重放。`replay_unverified` 必須為 0，且 runner 會比對 `replay_started` 與 `replay_verified`，不相等（例如重放在測試結束時被中斷）即判定失敗。 |
| `a/nginx-access.json` | 以同輪 run ID 篩選的 nginx 存取日誌。用 `req_ms` 欄位換算成毫秒後計算入口 p99（原值單位為秒）。 |
| `a/consistency.txt`、`a/final-state.txt` | A 結束 90 秒後的 SQL 查核及訂單、座位狀態統計。 |
| `a/event.json`、`a/metrics_1s.csv`、`a/business_1s.csv` | 活動 ID、秒級監控及業務指標。 |
| `b/seed.txt`、`b/writer.txt`、`b/sse-client.txt` | B 的 fixture、座位變更速率、SSE 建連與套用延遲、錯誤率及最終 audit。 |

B 每個 worker 先取得一份共用起始快照，再透過各自的 session 錯開建立 SSE。每個 worker 共用座位索引，並用獨立陣列維護各連線的座位狀態與版本。報告的 `processes` 為實際 worker 數，`peak_active` 為全程同時連線峰值，`peak_active_during_writes` 為 writer 程序執行期間的同時連線峰值。`connect_ms` 從發出 SSE 請求計至回應標頭，扣除排程等待時間。各 worker 的延遲分桶先合併，再算 p50／p95／p99；毫秒解析度依延遲區間為 1、10 或 100 ms，超過 ±300 秒的值歸入邊界桶。

結果存於 `OUT` 與旁邊的 `<OUT>.tar.gz`，預設為 `loadtest/out/<時間戳>/` 與 `loadtest/out/<時間戳>.tar.gz`。下列檢查失敗時，腳本仍會收集結果、移除 `sessions.json` 並打包，將 `FAILED` 與失敗項目寫入終端及 `run.log`，最後以非零狀態結束。若在打包前中斷，輸出目錄可能仍有憑證檔。

- **A**　k6 的 `checks` 必須全數成功且 `unexpected_errors` 為零。每筆已發起的重放均須驗證為原訂單，`replay_unverified` 須為零，且 runner 要求 `replay_started` 與 `replay_verified` 相等（`loadtest/scenario_a.js:29–35`、`loadtest/scenario_a.js:132–165`、`loadtest/run.sh:118–121`）。重放沿用原請求內容及 Idempotency-Key，遇 502／504 或 `503 server-busy` 時依 `Retry-After`（預設 1 秒）重送，合計最多 10 次嘗試。SQL 須執行成功且回傳零違規列。k6 將 HTTP 409 標記為預期回應，未識別的 409 仍計入 `unexpected_errors`。`503 server-busy` 仍計入 `http_req_failed`，可用 `server_busy` 判讀服務過載。90 秒後仍可能有有效鎖位，可待鎖位到期、金流待辦與 relay 排空後重驗。見 [排空後重驗指令](../LOADTEST.md#6-一致性檢查)。
- **B**　寫入程式須成功結束且提交至少一次座位變更，客戶端須至少取得一筆 `delta_apply_samples`。每條連線的游標、版面版本與座位狀態及版本都須符合 PostgreSQL 最終快照。`peak_active` 與 `matching_clients` 都須等於 `target_connections`，且 `outbox_pending` 與 `archive_mismatches` 都為零。SQL 與 SSE 稽核皆在 outbox 保留的序號範圍內比對封存資料，並檢查該範圍是否連續且抵達最新已編序位置。維護程序可清除已編序超過一小時的前段資料。

產壓端與受測服務同機，會共享 CPU。每輪會新增活動與測試資料。只可對可拋棄的專案執行 `docker compose down -v`，此指令會刪除該專案的所有資料 volumes，包含 PostgreSQL、Redis、金流及共享檔案。見 `compose.yaml:251–258`。結果與瓶頸分析見 [LOADTEST.md](../LOADTEST.md)。
