# 壓測工具包

## 執行前提

- 從 repository 根目錄使用 Git、Bash、Docker（含 Compose v2）與 `curl`。Python 工具在 API image 內執行，情境 A 的 k6 使用 `grafana/k6:0.57.0`。見 `loadtest/run.sh:13`、`loadtest/run.sh:35`、`loadtest/run.sh:57`、`loadtest/run.sh:81`。
- 使用可拋棄的 Compose 堆疊。Runner 假設預設主辦人 `user@example.com`／`User#Dev2026!`、SQL 帳號及資料庫皆名為 `ticketing`、cookie 名為 `sid`，且允許 `http://localhost:8080` 作為 Origin。只改 Compose 的憑證設定無法改寫 runner 的固定值。見 `loadtest/run.sh:40`、`loadtest/run.sh:83`、`loadtest/prepare_a.py:42`、`loadtest/prepare_a.py:56`、`loadtest/prepare_a.py:116`、`loadtest/sse_client.py:384`。
- 寫入 `loadtest/` 的容器（Python 工具與 k6）以執行者的 UID/GID 執行（`loadtest/run.sh:19`），Linux bind mount 不需額外調整權限。

## 執行與參數

```bash
loadtest/run.sh smoke
loadtest/run.sh all
```

`smoke` 預設跑 A（50 VUs／15 秒／1,000 席）與 B（50 條 SSE／30 秒／20 changes/s）。`all` 依序跑 A 與 B 的正式規模，也可用 `loadtest/run.sh a` 或 `loadtest/run.sh b` 單獨執行。腳本沿用通過 host health probe 的堆疊，否則執行 `docker compose up -d --build`。見 `loadtest/run.sh:20`、`loadtest/run.sh:57`、`loadtest/run.sh:134`。

| Shell 變數 | 正式預設值 | 說明 |
|---|---|---|
| `VUS` | `3000` | A 的虛擬使用者數（正整數，每人一組獨立帳號與 session）。 |
| `DURATION` | `60s` | A 的正值 k6 duration，需帶時間單位。 |
| `SEATS` | `10000` | A、B 共用。A 平均分至四區，會向下取 4 的倍數，但 SQL 仍以原值核對，故請用正的 4 倍數且不超過匯入的 100,000 列上限。B 至少 16 席（4 個 writer × 每批 4 席）。 |
| `CONNECTIONS` | `2000` | B 的 SSE 訂閱數（正整數，每條獨立 session）。 |
| `B_DURATION` | `80` | B 訂閱秒數，須為大於 25 的整數。Writer 等 15 秒後開始，持續 `B_DURATION-25` 秒。 |
| `B_RATE` | `300` | B 的每秒目標座位變更數，須為正值。 |
| `K6_CPUS` | 未設定 | 限制 k6 容器可用 CPU，例如 `K6_CPUS=2`。 |
| `REBUILD` | `0` | 設為 `1` 時執行 Compose 建置及啟動，即使既有入口已通過探測。 |
| `OUT` | `loadtest/out/<時間戳>` | 使用 `loadtest/` 下的相對路徑，容器會映射到 `/lt/`。每輪用新的輸出目錄。 |
| `HTTP_PORT` | `8080` | Runner 的 host probe 直接讀 shell。自訂 host port 可執行 `HTTP_PORT=8081 loadtest/run.sh smoke`。容器內仍走 `http://nginx:8080`。 |

參數實作見 `loadtest/run.sh:16`、`loadtest/run.sh:57`、`loadtest/run.sh:81`、`loadtest/run.sh:117`、`loadtest/prepare_a.py:72`、`loadtest/scenario_b_writer.py:99`、`loadtest/scenario_b_writer.py:179`、`backend/src/ticketing/core/settings.py:90`。

A 以 `POST /api/v1/auth/login` 登入主辦人。買家及其 session 直接寫入 PostgreSQL，避開登入限流。合成 session 的初始 idle 與 absolute deadline 皆為 12 小時，正常登入預設分別為 30 分鐘與 12 小時。直接執行 `prepare_a.py` 時，可透過 `LOADTEST_DATABASE_URL` 或 `--database-url` 指定寫入 fixture 的 DSN，未設定時依序使用 `DATABASE_URL` 或本機預設 DSN。Runner 使用 Compose 注入的 `DATABASE_URL`，不會從 shell 轉傳 `LOADTEST_DATABASE_URL`。見 `loadtest/prepare_a.py:44`、`loadtest/prepare_a.py:54`、`loadtest/prepare_a.py:97`、`backend/src/ticketing/core/settings.py:46`、`loadtest/run.sh:37`、`compose.yaml:7`。

## 輸出與結果核對

| 產物（相對於 `OUT`） | 內容 |
|---|---|
| `environment.txt`、`run.log` | 主機、Docker、commit、主要參數及執行摘要。 |
| `a/k6.log`、`a/k6-summary.json` | `metrics.http_reqs.rate` 為總 RPS。`metrics.http_req_duration.med` 與 `metrics.http_req_duration["p(95)"]` 為 p50／p95（ms）。另核對 `checks` 與 `unexpected_errors` thresholds。 |
| `a/nginx-access.json` | 以同輪 run ID 篩選的 nginx 存取日誌。用 `req_ms` 欄位換算成毫秒後計算入口 p99（原值單位為秒）。 |
| `a/consistency.txt`、`a/final-state.txt` | A 結束 90 秒後的 SQL 查核及訂單、座位狀態統計。 |
| `a/event.json`、`a/metrics_1s.csv`、`a/business_1s.csv` | 活動 ID、秒級監控及業務指標。 |
| `b/seed.txt`、`b/writer.txt`、`b/sse-client.txt` | B 的 fixture、座位變更速率、SSE 建連與套用延遲、錯誤率及最終 audit。 |

結果存於 `OUT` 與旁邊的 `<OUT>.tar.gz`，預設為 `loadtest/out/<時間戳>/` 與 `loadtest/out/<時間戳>.tar.gz`。只有進入打包步驟才會刪除含憑證的 `sessions.json`。若提早中斷，輸出目錄可能仍有該檔。部分階段失敗後仍會打包，需核對下列結果。見 `loadtest/run.sh:86`、`loadtest/run.sh:94`、`loadtest/run.sh:122`、`loadtest/run.sh:128`。

- **A**　k6 的 `checks` 必須全數成功且 `unexpected_errors` 為零。90 秒後的 SQL 可能仍有有效 hold。待 hold 到期、金流待辦與 relay 排空，再執行 SQL，確認查詢成功且回傳零違規列。見 `loadtest/scenario_a.js:27`、`loadtest/run.sh:91`、[排空後重驗指令](../LOADTEST.md#6-一致性檢查)。
- **B**　`peak_active` 與 `matching_clients` 都須等於 `target_connections`，且 `outbox_pending` 與 `archive_mismatches` 都為零。另記錄連線錯誤率與 writer 的實得速率。見 `loadtest/sse_client.py:348`、`loadtest/sse_client.py:363`。

產壓端與受測服務同機，會共享 CPU。每輪會新增活動與測試資料。`docker compose down -v` 會刪除整個 Compose 專案的 volumes，包含 PostgreSQL、Redis、金流及共享檔案。見 `compose.yaml:223`。結果與瓶頸分析見 [LOADTEST.md](../LOADTEST.md)。
