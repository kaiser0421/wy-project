# 高併發活動搶票與即時監控平台

主辦人可建立活動、匯入座位並發布。購票者可查看即時座位圖、指定座位或由系統配位，再透過模擬金流付款。Admin 可查看即時指標與告警、管理使用者，並以 `request_id` 查詢關聯日誌。

## 快速開始

需要 Git 與 Docker（含 Compose v2）。使用預設值即可啟動。

```bash
git clone git@github.com:kaiser0421/wy-project.git
cd wy-project
docker compose up -d --build
```

開啟 [購票與管理介面](http://localhost:8080) 或 [Swagger UI](http://localhost:8080/api/docs)。

| 角色 | 預設帳號 | 預設密碼 |
| --- | --- | --- |
| Admin | `admin@example.com` | `Admin#Dev2026!` |
| User | `user@example.com` | `User#Dev2026!` |
| Viewer | `viewer@example.com` | `Viewer#Dev2026!` |

`seed` 在 migration 完成後自動建立三個帳號，並保留已存在的帳號與密碼。見 `compose.yaml:90`、`backend/src/ticketing/seed/main.py:22`。

```bash
docker compose ps -a
curl -fsSi http://localhost:8080/healthz/live
```

確認 `migrate`、`seed` 以 0 結束，三個 `api` 副本均為 healthy，入口回傳 HTTP 200 與 `X-Request-ID`。API readiness 由各容器內的 `/healthz/ready` 探測。也可用 `make smoke` 啟動並檢查三個 upstream 是否實際收到流量。見 `compose.yaml:97`、`infra/nginx/nginx.conf:79`、`backend/src/ticketing/api/health.py:45`、`scripts/smoke.sh:25`。

[本機開發](#本機開發) · [測試與實測](#測試與實測) · [API 與架構](#api-與架構) · [服務與設定](#服務與設定) · [需求對照](#需求對照) · [已知問題與未完成項目](#已知問題與未完成項目)

## 技術棧

| 層級 | 實作 |
| --- | --- |
| 前端 | TypeScript SPA、Vite、Canvas 座位圖、uPlot 監控圖表。見 `frontend/package.json:6`、`frontend/src/seat-canvas.ts:8`。 |
| 應用 | Python 3.12、FastAPI、Uvicorn、Pydantic、asyncpg、Alembic、Argon2id、XlsxWriter。見 `backend/pyproject.toml:5`。 |
| 資料 | PostgreSQL 16 保存座位、訂單、session 與監控資料。Redis 7 提供限流、Pub/Sub、遙測 stream 與背景心跳。模擬金流以 SQLite 保存付款與回呼佇列。見 `compose.yaml:45`、`compose.yaml:68`、`paymock/src/paymock/main.py:86`。 |
| 部署與驗證 | Docker Compose、nginx 1.27、pytest、Vitest、Playwright、k6 與 Python SSE client。見 `infra/nginx/Dockerfile:3`、`compose.test.yaml:41`、[壓測工具包](loadtest/README.md)。 |

## 本機開發

在 repo 根目錄安裝 Python 3.12 workspace，執行靜態檢查或對已啟動的 Compose 堆疊驗證購票流程。E2E 使用上表的預設帳號，涵蓋建活動、匯入、發布、鎖位、付款、報表與日誌追蹤。見 `pyproject.toml:5`、`Makefile:32`、`scripts/e2e_flow.py:1`、`scripts/e2e_flow.py:22`。

```bash
uv sync --python 3.12
make lint typecheck
uv run python scripts/e2e_flow.py --base http://127.0.0.1:8080
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
| `make test` | Docker 內執行後端 Ruff、mypy、import-linter、pytest 單元與整合測試（PostgreSQL 16、Redis 7，含併發訂票），以及前端 typecheck、build、Vitest、Playwright。見 `Makefile:22`、`compose.test.yaml:4`、`compose.test.yaml:41`、`compose.test.yaml:54`。 |
| `make test-fast` | 以 `uv run pytest backend/tests/unit -q` 在 host 跑後端單元測試。見 `Makefile:29`。 |

併發測試包含指定座位競爭、區域配位與同一使用者限購競爭。破壞驗證移除防線後會抓到同一座位有多筆 active claim。見 `backend/tests/booking/test_booking.py:45`、`backend/tests/booking/test_booking.py:90`、[破壞驗證輸出](docs/evidence/break-it.txt)。

- 壓測以 `loadtest/run.sh smoke` 試跑，以 `loadtest/run.sh all` 執行情境 A（10,000 席、3,000 VUs、60 秒及一致性檢查）與 B（2,000 條 SSE）。見 `loadtest/run.sh:21`。參數與輸出格式見 [壓測工具包](loadtest/README.md)，報告見 [LOADTEST.md](LOADTEST.md)。
- 匯入 10,000 列的 API 實測為 3.3 秒。Workstream B 的 `test_import_100k_under_60s` 對 CSV 與 JSON 各驗證 100,000 列從上傳到完成均在 60 秒內。見 `backend/tests/imports/test_import_perf.py:16`。
- CP4 在 512 MB 上限的 API 容器匯出 500,000 筆訂單，首次下載完成為 217.3 秒，`anon` 取樣峰值約 108 MiB，三個副本均無 OOM。見 [CP4 實測紀錄](docs/evidence/cp4-excel-500k.md)。

## API 與架構

- API 文件可查 [Swagger UI](http://localhost:8080/api/docs)、[執行中 OpenAPI JSON](http://localhost:8080/api/openapi.json) 或 [repo 內的 OpenAPI JSON](docs/openapi.json)。見 `backend/src/ticketing/api/app.py:328`。
- 系統圖涵蓋服務拓撲、訂票交易、座位 SSE、金流與監控。[架構 Markdown](docs/architecture.md) · [架構 HTML](docs/architecture.html)（下載後可用瀏覽器開啟）。
- Q1–Q6 與驗證方式見 [DESIGN.md](DESIGN.md)。

## 服務與設定

| Compose 服務 | 副本 | 容器 port | 預設 host 綁定 | 功能與設定位置 |
| --- | ---: | --- | --- | --- |
| `nginx` | 1 | 8080 | `127.0.0.1:8080` | 靜態 UI、API／SSE、付款頁入口。 `compose.yaml:193` |
| `api` | 3 | 8000 | — | REST、SSE、Excel，每副本 512 MB。 `compose.yaml:97` |
| `worker` | 1 | — | — | 匯入、鎖位逾時、付款對帳、退款、取消。 `compose.yaml:123` |
| `relay` | 1 | — | — | 指派座位 outbox 序號並寫入 `seat_events`。 `compose.yaml:137` |
| `collector` | 1 | — | — | 收集日誌、指標及告警。 `compose.yaml:151` |
| `paymock` | 1 | 8000 | — | 模擬金流、付款頁與簽章回呼。 `compose.yaml:169` |
| `postgres` | 1 | 5432 | `127.0.0.1:5432` | PostgreSQL 16。 `compose.yaml:45` |
| `redis` | 1 | 6379 | `127.0.0.1:6379` | Redis 7。 `compose.yaml:68` |
| `migrate` | 1（一次性） | — | — | 執行 Alembic migration。 `compose.yaml:80` |
| `seed` | 1（一次性） | — | — | 建立預設帳號。 `compose.yaml:90` |

可複製 [`.env.example`](.env.example) 為 `.env` 覆蓋 Compose 的插值預設值。範例也標明需直接傳給程序或以 Compose override 注入的設定。

## 需求對照

| 交付項目 | 位置 |
| --- | --- |
| 二.1 完整程式與歷史 | [GitHub repository 與 commit 歷史](https://github.com/kaiser0421/wy-project) |
| 二.2 啟動、技術棧、帳號、限制 | 本 README |
| 二.3 環境參數 | [`.env.example`](.env.example) |
| 二.4 座位匯入範例 | [CSV](samples/seats_sample.csv) · [JSON](samples/seats_sample.json) |
| 二.5 系統架構圖 | [Markdown](docs/architecture.md) · [HTML](docs/architecture.html) |
| 二.6 壓測腳本與報告 | [執行說明](loadtest/README.md) · [LOADTEST.md](LOADTEST.md) |
| 二.7 AI 工具與實作缺陷 | [AI_USAGE.md](AI_USAGE.md) |
| 二.8 設計問答、假設、取捨與驗證 | [DESIGN.md](DESIGN.md) |

## 已知問題與未完成項目

- **正式情境 A／B（待桌機壓測結果）**。待以 `loadtest/run.sh all` 補齊正式 RPS、p50／p95／p99、錯誤率、SSE 套用延遲與壓測後一致性結果。
- **筆電 CP1 容量限制**。在 2 核心／4 執行緒的 MacBook 上，3,000 VUs 與服務同機造成 CPU 飽和。多數請求收到快速的 `503 server-busy` 或 client timeout，約 86–95 req/s，SQL 平均耗時受競爭影響約 35–170 ms。該次一致性檢查未發現超賣。
- **大型 Excel 匯出耗時**。CP4 兩次下載約 217–237 秒，nginx 報表路徑的讀取逾時為 300 秒。見 `infra/nginx/nginx.conf:138`、[CP4 實測紀錄](docs/evidence/cp4-excel-500k.md)。
- **監控可見性**。極晚到達的 nginx 日誌仍可依 request ID 查到，但可能超出每秒指標的回填窗口。付款服務的內部／回呼配送日誌輸出至自身容器，collector 讀取的是 nginx 與共享目錄的服務日誌。見 `backend/src/ticketing/telemetry/logs.py:76`、`compose.yaml:169`、[監控資料契約](backend/src/ticketing/telemetry/README.md)。
