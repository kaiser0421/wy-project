# 高併發活動搶票與即時監控平台

主辦人可建立活動、匯入座位並發布。購票者可查看即時座位圖、指定座位或由系統配位，再透過模擬金流付款。Admin 可查看即時指標與告警、管理使用者，並以 `request_id` 查詢關聯日誌。

## 快速開始

需要 Git、Docker（含 Compose v2）、Bash、`curl`、`uv` 與 Make。以下使用 Compose 開發預設值，請先確認 host ports 8080、5432、6379 可用。

1. 取得程式。

   ```bash
   git clone https://github.com/kaiser0421/wy-project.git
   cd wy-project
   ```

2. 建置並啟動服務。

   ```bash
   docker compose up -d --build
   ```

3. 重複執行 `docker compose ps -a`，等 `migrate`、`seed` 以 0 結束，三個 `api` 副本皆為 healthy。執行 `curl -fsSi http://127.0.0.1:8080/healthz/live`，確認 HTTP 200 與 `X-Request-ID`。
4. 在 [操作介面](http://localhost:8080) 選 **Sign in**，依序以三個帳號登入，每次檢查後選 **Sign out**。`seed` 會建立預設帳號，保留已有的帳號與密碼。

   | 角色 | 預設帳號 | 預設密碼 | 登入後檢查 |
   | --- | --- | --- | --- |
   | User | `user@example.com` | `User#Dev2026!` | 開啟 **Organizer**，確認可看到 **Create an event**。 |
   | Admin | `admin@example.com` | `Admin#Dev2026!` | 開啟 **Monitoring**，確認顯示 **Live** 並可進入 **Manage users**。 |
   | Viewer | `viewer@example.com` | `Viewer#Dev2026!` | 可瀏覽 **Events**、座位圖與活動報表，導覽列沒有 **Organizer** 或 **Monitoring**。 |

5. 開啟 [Swagger UI](http://localhost:8080/api/docs)，確認可瀏覽 API operations。
6. 執行 `uv run scripts/e2e_flow.py --base http://127.0.0.1:8080`，確認輸出 `E2E flow passed`。腳本會寫入活動與已付款訂單。
7. 執行 `make test`，確認後端與前端檢查成功，指令以 0 結束。
8. 執行 `loadtest/run.sh smoke`，以小規模跑過情境 A、B 與一致性檢查。結果核對方式見 [壓測工具包](loadtest/README.md)。

首次建置需下載 base images 並安裝 Python 與 npm 依賴，時間取決於網路。已有 Docker 建置快取時，從全新 clone 執行 `docker compose up -d --build` 到全部服務 healthy 約 40 秒（開發筆電實測）。

[本機開發](#本機開發) · [測試與實測](#測試與實測) · [API 與架構](#api-與架構) · [服務與設定](#服務與設定) · [需求對照](#需求對照) · [已知問題與未完成項目](#已知問題與未完成項目)

## 技術棧

| 層級 | 實作 |
| --- | --- |
| 前端 | TypeScript SPA、Vite、Canvas 座位圖、uPlot 監控圖表。見 `frontend/package.json:6`、`frontend/src/seat-canvas.ts:8`。 |
| 應用 | Python 3.12、FastAPI、Uvicorn、Pydantic、asyncpg、Alembic、Argon2id、XlsxWriter。見 `backend/pyproject.toml:5`。 |
| 資料 | PostgreSQL 16 保存座位、訂單、session 與監控資料。Redis 7 提供限流、Pub/Sub、遙測 stream 與背景心跳。模擬金流以 SQLite 保存付款與回呼佇列。見 `compose.yaml:45`、`compose.yaml:68`、`paymock/src/paymock/main.py:86`。 |
| 部署與驗證 | Docker Compose、nginx 1.27、pytest、Vitest、Playwright、k6 與 Python SSE client。見 `infra/nginx/Dockerfile:10`、`compose.test.yaml:41`、[壓測工具包](loadtest/README.md)。 |

## 本機開發

在 repo 根目錄安裝 Python 3.12 workspace，執行靜態檢查。見 `pyproject.toml:5`、`Makefile:32`、`Makefile:41`。

```bash
uv sync --python 3.12
make lint typecheck
```

用 Node.js 22 開發 UI。以下方式將 Compose nginx 移到 host port 8081，讓 Vite 以預設允許的瀏覽器 Origin `http://localhost:8080` 開發，並將 `/api`、`/pay/` 代理回 Compose。見 `frontend/vite.config.ts:3`、`backend/src/ticketing/core/settings.py:45`、`backend/src/ticketing/api/security.py:80`。

```bash
HTTP_PORT=8081 docker compose up -d --build
cd frontend
npm ci
TICKETING_ORIGIN=http://127.0.0.1:8081 npm run dev -- --port 8080 --strictPort
```

## 測試與實測

| 指令 | 驗證範圍 |
| --- | --- |
| `make test` | Docker 內執行後端 Ruff、mypy、import-linter、pytest 單元與整合測試（PostgreSQL 16、Redis 7，含併發訂票），以及前端 typecheck、build、Vitest、Playwright 預設 UI 測試（`smoke.spec.ts`、`seat-recovery.spec.ts`、`registration.spec.ts`）。見 `frontend/playwright.config.ts:8`、`Makefile:22`、`compose.test.yaml:4`、`compose.test.yaml:41`、`compose.test.yaml:68`。 |
| `make test-fast` | 以 `uv run pytest backend/tests/unit -q` 在 host 跑後端單元測試。見 `Makefile:29`。 |

nginx 整合測試可另外執行。先啟動 Compose，準備一個已匯入座位並發布的活動，將下列 `123` 換成實際 ID。使用 Node.js 22，在 Bash 中執行。另可在可拋棄的測試堆疊設 `TICKETING_SMOKE_WRITE=1`，加入建活動與付款的案例。見 `infra/nginx/Dockerfile:3`、`frontend/playwright.config.ts:8`、`frontend/tests/e2e/nginx.spec.ts:6`、`frontend/tests/e2e/nginx.spec.ts:41`。

```bash
cd frontend
npm ci
npx playwright install chromium
TICKETING_SMOKE_ORIGIN=http://localhost:8080 TICKETING_SMOKE_EVENT_ID=123 \
  TICKETING_SMOKE_USER_EMAIL=user@example.com TICKETING_SMOKE_USER_PASSWORD='User#Dev2026!' \
  TICKETING_SMOKE_VIEWER_EMAIL=viewer@example.com TICKETING_SMOKE_VIEWER_PASSWORD='Viewer#Dev2026!' \
  TICKETING_SMOKE_ADMIN_EMAIL=admin@example.com TICKETING_SMOKE_ADMIN_PASSWORD='Admin#Dev2026!' \
  npm run test:e2e
```

併發測試包含指定座位、區域配位與同一使用者的限購競爭。破壞驗證會抓到同一座位有多筆 active claim。見 `backend/tests/booking/test_booking.py:46`、`backend/tests/booking/test_booking.py:67`、`backend/tests/booking/test_booking.py:91`、`backend/tests/booking/test_booking.py:106`、[破壞驗證輸出](docs/evidence/break-it.txt)。

- 壓測以 `loadtest/run.sh smoke` 試跑，以 `loadtest/run.sh all` 執行情境 A（10,000 席、3,000 VUs、60 秒及一致性檢查）與 B（2,000 條 SSE）。見 `loadtest/run.sh:21`。參數與輸出格式見 [壓測工具包](loadtest/README.md)，報告見 [LOADTEST.md](LOADTEST.md)。
- 匯入 10,000 列的 API 實測為 3.3 秒。`test_import_100k_under_60s` 對 CSV 與 JSON 各驗證 100,000 列從上傳到完成均在 60 秒內。見 `backend/tests/imports/test_import_perf.py:16`。
- 在 512 MiB 上限的 API 容器匯出 500,000 筆訂單，首次下載完成為 217.3 秒，`anon` 取樣峰值約 108 MiB，三個副本均無 OOM。見 [Excel 匯出實測紀錄](docs/evidence/cp4-excel-500k.md)。

## API 與架構

- API 文件可查 [Swagger UI](http://localhost:8080/api/docs)、[執行中 OpenAPI JSON](http://localhost:8080/api/openapi.json) 或 [repo 內的 OpenAPI JSON](docs/openapi.json)。見 `backend/src/ticketing/api/app.py:328`。
- 系統圖涵蓋服務拓撲、訂票交易、座位 SSE、金流與監控。[架構 Markdown](docs/architecture.md) · [架構 HTML](docs/architecture.html)（透過 jsDelivr 載入 Mermaid，需網路連線）。
- Q1–Q6 與驗證方式見 [DESIGN.md](DESIGN.md)。

## 服務與設定

| Compose 服務 | 副本 | 容器 port | 預設 host 綁定 | 功能與設定位置 |
| --- | ---: | --- | --- | --- |
| `nginx` | 1 | 8080 | `127.0.0.1:8080` | 靜態 UI、API／SSE、付款頁入口。 `compose.yaml:193` |
| `api` | 3 | 8000 | — | REST、SSE、Excel，每副本 512 MiB。 `compose.yaml:105` |
| `worker` | 1 | — | — | 匯入、鎖位逾時、付款對帳、退款、取消。 `compose.yaml:123` |
| `relay` | 1 | — | — | 指派座位 outbox 序號並寫入 `seat_events`。 `compose.yaml:137` |
| `collector` | 1 | — | — | 收集日誌、指標及告警。 `compose.yaml:151` |
| `paymock` | 1 | 8000 | — | 模擬金流、付款頁與簽章回呼。 `compose.yaml:169` |
| `postgres` | 1 | 5432 | `127.0.0.1:5432` | PostgreSQL 16。 `compose.yaml:45` |
| `redis` | 1 | 6379 | `127.0.0.1:6379` | Redis 7。 `compose.yaml:68` |
| `migrate` | 1（一次性） | — | — | 執行 Alembic migration。 `compose.yaml:80` |
| `seed` | 1（一次性） | — | — | 建立預設帳號。 `compose.yaml:90` |

可複製 [`.env.example`](.env.example) 為 `.env` 覆蓋 Compose 的插值預設值。範例也標明需直接傳給程序或以 Compose override 注入的設定。

`DB_POOL_MAX` 預設為 10，套用於每個 API 副本及各個 `worker`、`relay`、`collector` 程序的 PostgreSQL 連線池。背景維護會清除編序超過 1 小時的座位 outbox 列，已歸檔的 `seat_events` 仍會保留。若回放需要已清除的序號區間，SSE 會通知客戶端重新取得快照。見 `backend/src/ticketing/core/service.py`、`backend/src/ticketing/maintenance.py`。

## 需求對照

| 交付項目 | 位置 |
| --- | --- |
| 二.1 完整程式與歷史 | [GitHub repository 與 commit 歷史](https://github.com/kaiser0421/wy-project) |
| 二.2 啟動、技術棧、帳號、限制 | 本 README |
| 二.3 環境參數 | [`.env.example`](.env.example) |
| 二.4 座位匯入範例 | [CSV](samples/seats_sample.csv) · [JSON](samples/seats_sample.json)。在含區域 `A` 且尚未匯入座位的草稿活動，任選一份匯入，預期新增 3 席。 |
| 二.5 系統架構圖 | [Markdown](docs/architecture.md) · [HTML](docs/architecture.html)（透過 jsDelivr 載入 Mermaid） |
| 二.6 壓測腳本與報告 | [執行說明](loadtest/README.md) · [LOADTEST.md](LOADTEST.md) |
| 二.7 AI 工具與實作缺陷 | [AI_USAGE.md](AI_USAGE.md) |
| 二.8 設計問答、假設、取捨與驗證 | [DESIGN.md](DESIGN.md) |

## 已知問題與未完成項目

- **正式情境 A／B（待桌機壓測結果）**。待以 `loadtest/run.sh all` 補齊正式 RPS、p50／p95／p99、錯誤率、SSE 套用延遲與壓測後一致性結果。
- **筆電壓測**。在 2 核心／4 執行緒的 MacBook 上，3,000 VUs 與服務同機造成 CPU 飽和。多數請求收到快速的 `503 server-busy` 或 client timeout，約 86–95 req/s，SQL 平均耗時受競爭影響約 35–170 ms。該次一致性檢查未發現超賣。
- **大型 Excel 匯出耗時**。兩次下載約 217–237 秒，nginx 報表路徑的讀取逾時為 300 秒。見 `infra/nginx/nginx.conf:138`、[Excel 匯出實測紀錄](docs/evidence/cp4-excel-500k.md)。
- **固定 Docker 子網**。Compose 網路使用 `172.28.0.0/24`，nginx 固定在 `172.28.0.200`，讓 API 只信任 nginx 轉送的 client IP。若本機已有網路使用同一段位址，`docker compose up` 會回報 `Pool overlaps with other one on this address space`，需先移除該網路（例如舊的堆疊執行 `docker compose down`）。見 `compose.yaml:10`、`compose.yaml:220`。
- **監控可見性**。極晚到達的 nginx 日誌仍可依 `request_id` 查到，但可能超出每秒指標的回填窗口。Collector 也從共用目錄收集 paymock 的請求處理與回呼傳送日誌，供管理後台依 `request_id` 查詢。見 `backend/src/ticketing/telemetry/logs.py:76`、`compose.yaml:169`、[監控資料契約](backend/src/ticketing/telemetry/README.md)。
