# 高併發活動搶票與即時監控平台

> **文件狀態：實作前草稿（pre-implementation draft）。**
> 本 README 依 `PLAN.md` v1.2 撰寫，描述的是**目標狀態**；目前 repo 尚無程式碼。
> 凡標記 `<!-- TODO(Txx) -->` 的段落，內容依賴 `PLAN.md` §21 對應 task 的實作，完成後需回頭核對/改寫；
> 本機入口固定為 `http://localhost:8080`（nginx，純 http）；測試帳號的 email、密碼與環境變數名稱以本文件 §7 為規格，實作須對齊。
> 所有效能數字在實測前皆為目標，實測值只記錄於 [`LOADTEST.md`](LOADTEST.md)。
> 最終定稿 task：T9.6（README、`.env.example`、架構圖、OpenAPI 靜態檔）。

---

## 1. 專案介紹

一場熱門演唱會在指定時間開賣，數千名使用者在同一秒湧入搶購有限座位。本專案提供：

- **活動搶票平台**：活動與座位管理（含 CSV/JSON 批量匯入、背景進度）、指定座位或「區域 + 張數」配位下單（每單 1–4 張、每人每活動上限 4 張）、模擬金流付款與回呼、取消與訂單查詢、**SSE 即時座位圖**。
- **即時流量監控與分析後台**（Admin）：每秒更新的 RPS、p50/p95/p99、錯誤率、即時連線數圖表；存取日誌 / 座位狀態事件 / 每秒指標批次入庫；告警；依 `request_id` 查詢跨服務日誌；使用者角色與啟用狀態管理；Excel 銷售報表。

設計原則（詳見 `PLAN.md` §0 與 [`DESIGN.md`](DESIGN.md)）：

| 原則 | 做法摘要 |
|---|---|
| 單一真實來源 | PostgreSQL 16 決定座位、訂單、配額、Session、Job 狀態；Redis 只做 fan-out / 限流，**永不決定座位歸屬或權限** |
| 正確性優先 | 防超賣三層防護（row lock + guarded UPDATE + partial unique index）；限購與座位寫入同一交易；逾時判斷一律用 DB 時鐘 |
| 崩潰可自行收斂 | Transactional outbox、lease + fencing token、reconciler、Idempotency-Key；`kill -9` 任一元件不需人工介入 |
| 可驗證 | 每項保證對應自動化測試；「把保護拿掉 → 測試失敗」證明測試有效 |

---

## 2. 技術棧

| 層 | 選擇 | 理由（簡述） |
|---|---|---|
| API | FastAPI、Pydantic v2、uvicorn + uvloop + httptools、orjson | 需求指定 FastAPI；高效 JSON 與 event loop |
| DB 存取 | SQLAlchemy 2.0 Core（async）+ asyncpg；關鍵路徑手寫 SQL | 鎖與 guard 語意需精確可控 |
| 資料庫 | PostgreSQL 16 | `NOWAIT` / `SKIP LOCKED`、partial unique index、`COPY`、單一 ACID 邊界 |
| Migration | Alembic | CI 驗證 upgrade → downgrade -1 → upgrade、`alembic check` |
| 匯流排 / 限流 | Redis 7（Streams、Pub/Sub、Lua） | Streams 可 replay；真實 replay log 仍在 PG |
| 背景工作 | 自建 PG 佇列（`FOR UPDATE SKIP LOCKED` + DB 時鐘 lease + fencing token） | Job 狀態本來就在 PG，避免 DB/broker 雙寫 |
| 即時推送 | SSE（`id: <gen>:<seq>` + `Last-Event-ID` 續傳） | 單向即可；原生續傳；同源 cookie 驗證 |
| 認證 | Opaque session token（DB 只存 SHA-256）+ session cookie（本機 http：`sid`；TLS：`__Host-sid` + Secure，由 `COOKIE_SECURE` 切換）+ CSRF header；argon2id | 登出/停用/改角色需**立即**失效，JWT 無優勢 |
| 匯入 / 匯出 | `csv`、`streaming-form-data`、`ijson`；xlsxwriter `constant_memory` + server-side cursor | 串流處理；Excel 在 `api` 實例內以專用單執行緒 executor + 專用 DB 連線產生（不阻塞 event loop），證明 50 萬筆 @512MB |
| 前端 | React 18 + Vite + TypeScript、Zustand、TanStack Query、Canvas 座位圖、uPlot | 1 萬座位 DOM 太重；uPlot 適合即時圖 |
| 反向代理 | nginx | 產生 `X-Request-ID`、JSON access log（邊界指標來源）、SSE 不緩衝、上傳大小限制 |
| 壓測 | k6 + xk6-sse（Python asyncio SSE client 為備援） | 單機壓測目標：情境 A 3,000 VU；情境 B 2,000 SSE |
| 測試 / 品質 | pytest、pytest-asyncio、hypothesis、Playwright；uv、ruff、mypy --strict、import-linter | |
| 供應鏈 | pip-audit、npm audit、trivy、gitleaks、Dependabot | |

---

## 3. 系統架構

架構圖與資料流：[`docs/architecture.md`](docs/architecture.md)（Mermaid 原始碼）與 [`docs/architecture.png`](docs/architecture.png)。<!-- TODO(T9.6) -->

```
Browser / k6 ──► nginx ──least_conn──► api ×3 ──► PostgreSQL 16（唯一真實來源）
                   ▲                     │  ▲
     payment-mock ─┘ (HMAC 回呼)         │  └── Redis Streams ◄── outbox-relay ◄── outbox (PG)
                                         └──► payment-mock
  worker（import、hold sweeper、reconciler、退款）  telemetry-collector（日誌/指標入庫、告警）
  Excel 匯出：api 內專用 executor 產生 → exports volume → 串流下載
```

---

## 4. 快速開始：Docker 一鍵部署

### 4.1 前置需求

- Docker Engine 24+ 與 Docker Compose v2 <!-- TODO(T0.2): 以實際測試過的最低版本為準 -->
- 建議 ≥ 4 CPU / 8 GB RAM（3 個 API 副本 + PostgreSQL + Redis；此為估計值，非實測，壓測機規格見 `LOADTEST.md` §1）
- 可用的 host port：見 §4.3

### 4.2 啟動

```bash
git clone <repo-url> && cd <repo>
cp .env.example .env          # 開發預設值即可啟動；正式環境務必修改所有 secret 與密碼
docker compose up -d --build  # 一行指令啟動全部服務
docker compose ps             # 確認所有長駐服務為 healthy、migrate/seed 為 exited (0)
```

啟動後：

| 用途 | URL |
|---|---|
| 前端（搶票頁 / Admin 監控） | <http://localhost:8080/> |
| API base | <http://localhost:8080/api/v1> |
| Swagger UI | <http://localhost:8080/api/docs> |
| Health | <http://localhost:8080/healthz/ready> <!-- TODO(T0.3): 確認 nginx 是否對外暴露 healthz 路徑 --> |

本機一鍵啟動走純 **http://localhost:8080**（不開 443）：預設 `COOKIE_SECURE=false`，session cookie 名稱為 `sid`、不帶 Secure 屬性。`__Host-` 前綴強制要求 Secure，在 http 下瀏覽器會拒收，因此只有在 TLS 環境設定 `COOKIE_SECURE=true` 時才改用 `__Host-sid` + Secure（TLS 終結設定不在預設 compose 內 <!-- TODO(T9.4): 若提供 TLS profile 再補 -->）。

### 4.3 服務清單

啟動順序：`postgres`、`redis` healthy → `migrate` 成功結束 → `seed` 成功結束 → `api`（`api` 的 `depends_on` 明確包含 `seed: service_completed_successfully`，因此 API 可接受請求時三個測試帳號必定已存在）→ 其餘服務（`depends_on` 搭配 `service_healthy` / `service_completed_successfully`）。<!-- TODO(T0.2) -->

| Service | 角色 | 副本 | 對外 port | Healthcheck |
|---|---|---|---|---|
| `nginx` | 反向代理、靜態檔、`$request_id`、JSON access log、上傳大小限制（匯入 21MB、其他 1MB）、SSE `proxy_buffering off` | 1 | `8080` → 80 | HTTP 檢查 <!-- TODO(T0.2) --> |
| `api` | FastAPI，每容器 1 個 uvicorn worker、`mem_limit: 512m` | 3 | 無（經 nginx） | `GET /healthz/ready`（檢查 PG、Redis、migration head） |
| `worker` | 座位匯入 job、hold 逾時 sweeper、付款 reconciler、退款、向金流取消 intent（**Excel 匯出不在 worker**，在 `api` 內專用 executor 執行，見 `PLAN.md` §9） | 1–2 | 無 | heartbeat <!-- TODO(T0.2) --> |
| `outbox-relay` | outbox → 指派 seq → Redis Streams、`seat_events` 封存；advisory lock 保證單一 active | 1（`restart: always`） | 無 | heartbeat（API 監看，遺失 → `RELAY_DOWN`） |
| `telemetry-collector` | 指標聚合、日誌批次入庫、告警評估 | 1 | 無 | heartbeat（API 監看，遺失 → `MONITOR_DOWN`） |
| `payment-mock` | 模擬金流（intent / capture / refund / callback retry、chaos flags） | 1 | 無（內部網路；模擬付款頁經 nginx）<!-- TODO(T3.1) --> | HTTP <!-- TODO(T3.1) --> |
| `postgres` | PostgreSQL 16（`synchronous_commit=on`、`max_connections=200`） | 1 | 預設不對外 <!-- TODO(T0.2) --> | `pg_isready` |
| `redis` | Redis 7（AOF everysec） | 1 | 預設不對外 | `redis-cli ping` |
| `migrate` | Alembic upgrade（one-shot） | – | – | `service_completed_successfully` |
| `seed` | 建立 Admin/User/Viewer 測試帳號（冪等） | – | – | `service_completed_successfully` |
| `k6` | 壓測（含 xk6-sse）；**僅在 `--profile loadtest` 啟用**，cpuset 隔離 | – | – | – |

### 4.4 常用維運指令

```bash
docker compose logs -f api            # 追蹤 API log（JSON，每行帶 request_id）
docker compose restart outbox-relay   # relay 重啟後會從 PG replay log 補投
docker compose down                   # 停止（保留 volume）
docker compose down -v                # 停止並刪除所有資料（PG、Redis、logs/imports/exports volume）
```

Graceful shutdown：readiness 先轉 503 → SSE 送 `resync` 後關閉 → flush buffer → worker 釋放 lease（`stop_grace_period: 30s`）。

---

## 5. 本地開發（不經 Docker 跑 API）

> **建議的本地運行方式就是 §4 的 `docker compose up -d --build`。** 以下「API 在 host 上以 `--reload` 執行」的流程僅供開發迭代；整合測試一律在 compose 內、使用與正式環境相同的 PostgreSQL 16 image。<!-- TODO(T0.1, T0.2): 以下 override 檔、變數名稱與模組路徑於實作時確認 -->

預設 compose 不對 host 開放 PostgreSQL / Redis，host 上的 API 需要一個開發用 override 檔把它們發佈到 localhost：

```bash
# 1. 工具：Python 3.12+、uv、Node.js 20+、Docker
uv sync                                                        # 安裝 Python workspace 依賴
docker compose -f compose.yaml -f compose.dev.yaml up -d postgres redis   # compose.dev.yaml 發佈 5432 / 6379 到 127.0.0.1（暫定）

# 2. 讓 host 程序連到 localhost 的 PG / Redis（變數名稱以 .env.example 為準，暫定）
export DATABASE_URL='postgresql+asyncpg://<user>:<password>@127.0.0.1:5432/<db>'
export REDIS_URL='redis://127.0.0.1:6379/0'
export COOKIE_SECURE=false

uv run alembic upgrade head                                    # 套用 migration
uv run python -m ticketing.seed                                # 建立 §7 的測試帳號（冪等）
uv run uvicorn ticketing.main:app --reload --host 0.0.0.0 --port 8000   # 0.0.0.0：讓 compose 內的 payment-mock 能回呼

# 3. 前端（Vite dev server，proxy /api 至 8000，同源 cookie）
cd apps/frontend && npm ci && npm run dev
```

金流回呼與 API 在 host 上執行時的路由（皆為暫定，TODO(T3.1)）：
- 回呼 URL 設定在 **payment-mock** 服務上（變數名稱暫定 `PAYMENT_CALLBACK_URL`），改成 `http://host.docker.internal:8000/api/v1/payments/mockpay/callbacks`；Linux 需在 `compose.dev.yaml` 為 payment-mock 加上 `extra_hosts: ["host.docker.internal:host-gateway"]`。
- host 上的 API 呼叫 payment-mock 需要它的 port 也由 `compose.dev.yaml` 發佈到 127.0.0.1（API 端變數名稱暫定 `PAYMENT_PROVIDER_URL`）。
- compose 內的 worker / outbox-relay / telemetry-collector **維持原本的 compose 內部 DSN**（`postgres:5432`、`redis:6379`），不需要改。

最簡單的做法是只把 API 拿出來跑，其他照常：

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d worker outbox-relay telemetry-collector payment-mock
```

`make` 目標速查：`make lint`、`make test`、`make test-fast`、`make verify-all`（見 §8）。<!-- TODO(T0.1, T0.6) -->

---

## 6. API 文件

- **Swagger UI**：<http://localhost:8080/api/docs>（FastAPI OpenAPI；正式環境可由環境變數關閉）<!-- TODO(T0.3): 確認關閉用的 env 名稱 -->
- **靜態 OpenAPI**：[`docs/openapi.json`](docs/openapi.json)（不需啟動服務即可閱讀）<!-- TODO(T9.6) -->

規範摘要（完整見 `PLAN.md` §11）：

- Base path `/api/v1`；JSON snake_case；時間 RFC 3339 UTC；金額 `*_twd` 整數（TWD 元）；每個回應帶 `X-Request-ID`。
- 錯誤一律 RFC 9457 `application/problem+json`：`{type, title, status, detail, instance, code, request_id, retryable, errors[]}`。
- 並行編輯：強 ETag `"v{version}"`，PATCH / 發布必須帶 `If-Match`（缺 → 428、不符 → 412）。
- 座位快照 `GET /api/v1/events/{id}/seats?min_layout_ver=` 回傳 `{gen, layout_ver, last_assigned_seq, seats(bitmap + 每座位 version)}`；SSE `GET /api/v1/events/{id}/seat-stream` 的事件 id 為 `<gen>:<seq>`，斷線以 `Last-Event-ID` 續傳——續傳游標是客戶端**最後套用**的 seq（`<gen>:<applied>`），不是最後收到的 id（前端用 `fetch-event-source` 自行設定），因為因斷層被拒絕套用的 frame 不能被跳過。
- 付款被金流明確拒絕 → 402 `payment-declined`（同 key 重送回放 402；可用新 key 重新付款）。
- 下單、付款、取消必須帶 `Idempotency-Key`。同 key 重送時：

  | 情況 | 下單 `POST /events/{id}/orders` | 付款 `POST /orders/{id}/payments` | 取消 `POST /orders/{id}/cancel` |
  |---|---|---|---|
  | 第一次已完成（idempotency 列 `completed`） | 重放原 201 + 原 body（`Idempotent-Replayed: true`） | 重放已持久化的**終態回應**：成功建立付款的 201（`checkout_url`），或金流明確拒絕的 402 `payment-declined`；皆帶 `Idempotent-Replayed: true` | 重放原成功回應（狀態碼與 body 同第一次，暫定 200 + 訂單表示 <!-- TODO(T2.5) -->） |
  | 第一次仍在處理 | 409 `idempotency-in-progress` + `Retry-After: 1` | 409 `idempotency-in-progress`：付款的 `in_progress` 列綁定 attempt，結果不明時不清除，直到 reconciler 推進為終態後才可重放 | 409 `idempotency-in-progress` |
  | 相同 key 不同內容（第一次已提交後） | 422 `idempotency-key-reused` | 同左 | 同左 |

  下單 / 取消的業務 409/422 不會被儲存，重試會重新執行；付款的終態失敗（402）會被儲存。completed 的 key 保留 24 小時，之後同 key 視為新請求。
- 認證：session cookie（本機 `sid`；TLS 下 `__Host-sid`）+ 非安全方法需帶 CSRF header。Swagger UI 以同源 cookie 運作（先呼叫 `POST /api/v1/auth/login`）。

主要端點群組：`/auth/*`、`/events`（分頁/篩選/排序）、`/events/{id}/zones`、`/events/{id}/seats?min_layout_ver=` + `/events/{id}/seat-stream`（SSE）、`/events/{id}/seat-imports` + `/seat-imports/{job}/*`、`/events/{id}/orders`、`/orders/*`、`/payments/mockpay/callbacks`、`/events/{id}/reports/sales.xlsx?tz=`、`/admin/*`、`/healthz/live|ready`。

---

## 7. 測試帳號

`seed` 服務在每次 `docker compose up` 時**冪等**建立以下三個帳號（已存在則不重建、不覆寫密碼）。登入頁：<http://localhost:8080/>。

| 角色 | Email | 開發預設密碼 | 覆寫用環境變數 |
|---|---|---|---|
| Admin | `admin@example.com` | `Admin#Dev2026!` | `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` |
| User | `user@example.com` | `User#Dev2026!` | `SEED_USER_EMAIL` / `SEED_USER_PASSWORD` |
| Viewer | `viewer@example.com` | `Viewer#Dev2026!` | `SEED_VIEWER_EMAIL` / `SEED_VIEWER_PASSWORD` |

<!-- TODO(T1.7): seed 實作與 .env.example 必須使用上表的 email、密碼與變數名稱（此表為規格，實作須對齊）；CI smoke 以這三組帳號登入驗證 -->

```bash
# 以 curl 驗證（本機 http，cookie 名稱為 sid）
curl -i -c /tmp/cj -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"Admin#Dev2026!"}' \
  http://localhost:8080/api/v1/auth/login
curl -b /tmp/cj http://localhost:8080/api/v1/auth/me
```

角色權限摘要（完整矩陣見 `PLAN.md` §7.3）：

| 能力 | Viewer | User | Admin |
|---|---|---|---|
| 瀏覽活動 / 座位 / 即時座位圖 | 已發布 | 已發布 + 自己的草稿 | 全部 |
| 建立活動 | ✗ | ✓ | ✓ |
| 修改 / 刪除 / 發布活動、匯入座位 | ✗ | 自己的 | 全部 |
| 下單、付款（已發布且開賣中的活動） | ✗ | ✓ | ✓ |
| 查看 / 取消訂單 | ✗ | 自己的 | 全部 |
| 銷售 Excel 報表 | **所有活動**（依需求第四節字面）。草稿活動沒有訂單，但報表摘要仍會揭露草稿的標題、場地、開賣時間等活動資訊；購買者以 HMAC 假名化——兩者皆待確認，見 §14 | 自己活動的 | 全部 |
| `/admin/*`（使用者管理、監控、告警、request_id 日誌） | ✗ | ✗ | ✓ |

- 開發模式（`.env.example` 預設）：使用 `.env.example` 中的示範密碼，可直接登入。
- **正式模式（prod）**：config validator 在啟動時**拒絕預設 secret 與示範密碼**（`PLAN.md` §15.2），必須在 `.env` 中自行設定強密碼，否則 `seed` / `api` 不會啟動。
- 註冊 API（`POST /api/v1/auth/register`）建立的帳號一律為 `user` 角色；角色變更需由 Admin 於 `/admin/users` 執行（變更後該使用者既有 session 立即失效）。
- 壓測用的大量使用者（例如情境 A 的 3,000 個帳號）由 `loadtest/seed.py` 另行建立，不屬於 seed 服務。

---

## 8. 執行測試

| 指令 | 內容 | 用途 |
|---|---|---|
| `make test` | `docker compose -f compose.test.yaml run --rm tests`：ruff + mypy + unit + integration + authz + concurrency + fault-injection + E2E（Playwright） | **需求要求的「一行指令執行全部測試」** |
| `make test-fast` | unit + 受影響的 integration | 日常開發 |
| `make verify-all` | `make test` + **破壞驗證**（同一組不變式斷言在破壞版必須失敗）+ 資源驗收（10 萬列匯入 < 60s、50 萬筆匯出 @512MB、2,000 SSE smoke）+ 短版 k6 + 一致性 SQL | 交付前 / nightly；**P9 交付關卡**（乾淨機器 clone → `docker compose up -d --build` → `make verify-all`） |

<!-- TODO(T0.6): compose.test.yaml 與 Makefile 目標實作後核對 -->

- 整合測試使用**與正式環境相同的 PostgreSQL 16 image**（真 ASGI app + 真 PG + 真 Redis），每個測試從已 migrate 的 template DB 複製。
- 併發測試（同座位 200 人搶恰 1 成功、區域配位收斂、同人兩個 key 限購、雙 sweeper、回呼 vs sweeper 等）使用真實平行連線 + 伺服器端 barrier。
- 逾時邏輯以測試專用 `test_clock`（`migrations_test/`）推進時間，**不 sleep 真實時間**；此 migration 與破壞驗證掛鉤只存在於 test image，prod config validator 會拒絕。
- 破壞驗證（DESIGN Q1.3，納入 `make verify-all`）：同一組不變式斷言在 `BOOKING_STRATEGY=naive` 等破壞版下必須失敗，輸出記錄於 `DESIGN.md`。<!-- TODO(T2.7) -->

---

## 9. 壓力測試

情境、方法、結果、瓶頸分析與壓測後一致性檢查見 [`LOADTEST.md`](LOADTEST.md)。

- 情境 A：單一活動 10,000 座位、3,000 VU 持續搶購 60 秒。
- 情境 B：2,000 條 SSE 連線同時訂閱同一活動（含故障注入）。
- 腳本位於 [`loadtest/`](loadtest/)（`k6/scenario_a.js`、`k6/scenario_b.js`、`writer.js`、`seed.py`、`consistency.sql`、`run.sh`、`report.py`、`sse_client.py`）；k6 以 compose profile `loadtest` 執行。<!-- TODO(T2.9, T4.7) -->

---

## 10. 範例資料

`samples/` 提供座位匯入範例檔（欄位 `zone_code,row_label,seat_number`）：<!-- TODO(T5.8) -->

| 檔案 | 用途 |
|---|---|
| `samples/seats_valid_100k.csv` | 10 萬列有效座位（驗證 60 秒匯入） |
| `samples/seats_valid.json` | JSON 格式有效範例 |
| `samples/seats_invalid.csv` | 各類錯誤列（格式、未知區域等），用於錯誤報告示範 |
| `samples/seats_dupes.json` | 檔內重複列（每一列都會被標為錯誤） |

可用 `scripts/gen_seats.py` 產生任意規模的測試檔。

---

## 11. Repo 結構

```
apps/api/src/ticketing/{api/v1, core, policies, domain, services, repositories, realtime, jobs, observability}
apps/api/tests/{unit, integration, authz, concurrency, fault, perf}
apps/worker/   apps/relay/   apps/collector/   apps/payment-mock/   apps/frontend/
migrations/ (Alembic)   migrations_test/ (test_clock 等測試專用)
infra/{nginx, postgres, redis, docker}
loadtest/{k6/scenario_a.js, k6/scenario_b.js, writer.js, seed.py, consistency.sql, run.sh, report.py, sse_client.py}
samples/{seats_valid_100k.csv, seats_valid.json, seats_invalid.csv, seats_dupes.json}
scripts/{gen_seats.py, seed_orders_500k.sql}
docs/{architecture.md, architecture.png, adr/, openapi.json}
compose.yaml  compose.test.yaml  Makefile  pyproject.toml  uv.lock  .env.example
.github/workflows/ci.yml
README.md  DESIGN.md  LOADTEST.md  AI_USAGE.md
```

---

## 12. 相關文件

| 文件 | 內容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 系統架構圖（元件與資料流） |
| [`DESIGN.md`](DESIGN.md) | 設計問答 Q1–Q6（附 `檔案:行號`）、設計假設、替代方案、取捨與驗證方式 <!-- TODO(T9.5) --> |
| [`LOADTEST.md`](LOADTEST.md) | 壓測情境、結果、瓶頸分析、一致性檢查 <!-- TODO(T9.1–T9.3) --> |
| [`AI_USAGE.md`](AI_USAGE.md) | AI 工具使用說明與實際發生的 AI 程式碼錯誤（來源：`docs/ai-defects.log`）<!-- TODO(T9.6) --> |
| [`.env.example`](.env.example) | 所有可調整參數與說明 <!-- TODO(T0.7, T9.6) --> |
| `PLAN.md` | 開發計畫（需求追蹤矩陣、task 拆解） |

---

## 13. 設定重點

所有參數與說明見 `.env.example`。以下幾項影響行為較大（名稱以 `.env.example` 為準）：

| 參數 | 預設 | 說明 |
|---|---|---|
| `HOLD_TTL_SECONDS` | `600` | 待付款保留時間（逾時由 sweeper 以 DB 時鐘判斷並釋放） |
| `COOKIE_SECURE` | `false` | `false`：cookie 名稱 `sid`、不帶 Secure，供本機 `http://localhost:8080` 使用；`true`：`__Host-sid` + Secure，**必須搭配 TLS**（http 下瀏覽器會拒收 `__Host-` cookie，導致無法登入） |
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD`（User、Viewer 同理） | 見 §7 | seed 建立的測試帳號；prod 模式拒絕開發預設密碼 |
| 正式模式開關 <!-- TODO(T0.3): 確認變數名稱 --> | 開發 | prod 模式拒絕預設 secret / 示範密碼 / 測試 migration / 破壞掛鉤 |

---

## 14. 已知問題與未完成項目

> 實作前草稿：本節目前列出**設計上已知的限制與刻意延後的範圍**；實作與壓測過程中發現的問題於 T9.6 補入。<!-- TODO(T9.6) -->

**設計上已知的限制（已文件化）**

- `outbox-relay` 為單一 active 實例（advisory lock 防止誤啟動第二個）；熱備援列為延伸項，relay 停擺期間即時推送延遲（`OUTBOX_LAG` / `RELAY_DOWN` 告警，客戶端顯示「資料可能過期」橫幅），但不影響購買正確性。
- 慢速 SSE 客戶端畫面過期的**設計估算**上界約 14 秒（由 ring 5s + write deadline 5s + 重連 1–3s + 快照追趕 ~1s 推算，非實測；實測見 `LOADTEST.md` §5）（落後 → 關閉 → 重連拿快照）；Redis 與 relay 同時故障時畫面過期無上界，但 5 秒內出現過期橫幅。購買永遠由 DB 重新驗證，最壞只會得到 409 `seat-unavailable`。
- 遙測在程序記憶體中、尚未 flush 時被 `kill -9` → 遺失 ≤ 1 個 interval 的應用層指標（圖表標示不完整秒；邊界指標不受影響）。
- 已交給 TCP 的 SSE 位元組在撤銷後、連線關閉前仍可能送達。
- 不使用 OpenTelemetry / Jaeger / Grafana；監控 UI 內建（React + uPlot），header 兼容 W3C `traceparent` 以便日後接入。
- 預設不使用 pgbouncer（transaction mode 會失去 prepared statement）。
- Excel 匯出在 API 實例內執行（每實例同時 1 個 + 全域 advisory lock，忙碌回 429）；改由 worker 執行列為未來備案。
- 開啟中的 SSE 在登出 / 停用後：Pub/Sub 正常時數十 ms 內關閉；Pub/Sub 通知遺失時由每副本每秒批次重驗兜底，**≤ 2 秒**關閉。
- 告警僅 P0 規則為必做（`EDGE_5XX`、`EDGE_P99`、`PURCHASE_FUNNEL_COLLAPSE`、`PAYMENT_CALLBACK_LAG`、`PAYMENT_PROVIDER_DOWN`、`RECONCILER_DOWN`、`REFUND_STUCK`、`HOLD_EXPIRY_BACKLOG`、`OUTBOX_LAG`、`RELAY_DOWN`/`MONITOR_DOWN`、`DB_DOWN`），其餘規則為 P1/延伸。`PAYMENT_CALLBACK_LAG` 從金流端 `captured_at` 起算（不含買家付款前的思考時間）。
- 金流或 reconciler 持續故障時，「已扣款但回呼遺失」的收斂沒有有限上界，只能靠上述告警通知處理基礎設施。
- 活動列表分頁採第一頁物化快照，單一查詢最多 50,000 列，超過回 422 `refine-filters`（需縮小篩選條件）；cursor 10 分鐘過期（410）。
- 退款完成後該使用者在該活動的購買名額恢復（配額只計仍持有座位的訂單）。

**待確認事項（`PLAN.md` §20）**

- Hold TTL 預設 10 分鐘；Viewer 可下載所有活動（含草稿）的報表——草稿報表沒有訂單資料，但摘要頁會揭露草稿活動的中繼資料（標題、場地、開賣時間等）；購買者資料以 HMAC 假名化或完全不顯示（待確認）；座位號正規化（`001` 與 `1` 視為重複、區域/排號不分大小寫）。

**未完成項目**

- 全部功能尚未實作（見 `PLAN.md` §21 task 清單）。<!-- TODO(T9.6): 交付時改為實際未完成清單 -->
- 範圍依 `PLAN.md` §21.3 分為 Must / Should / Could：Must（需求字面要求與正確性核心）必定完成；**Could 級項目在時間不足時會被刪除**，並於交付時逐項列在此處。Could 級目前包含：`CANARY_FAIL` 合成交易、`EDGE_APP_DIVERGENCE` 告警、relay 熱備援、無障礙 DOM 選位器（保留區域 + 張數表單作為替代）、情境 B 的 1,000 changes/s 與情境 A 的 arrival-rate sweep、Canvas 進階圖層、匯出改由 worker 執行的備案。<!-- TODO(T9.6): 標記實際刪除的 Could / 未完成的 Should 項目 -->
