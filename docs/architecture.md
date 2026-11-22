# 系統架構圖

[服務拓撲](#1-服務拓撲) · [訂票交易](#2-訂票交易) · [座位即時推送](#3-座位即時推送) · [付款與回呼](#4-付款與回呼) · [監控資料流](#5-監控資料流)

[瀏覽器版架構圖](architecture.html)（透過 jsDelivr 載入 Mermaid，需網路連線） · [啟動與服務 port](../README.md#服務與設定) · [設計問答](../DESIGN.md)

## 1. 服務拓撲

```mermaid
flowchart TB
  Browser["瀏覽器 · TypeScript SPA · Canvas 座位圖"]
  Edge["nginx · 8080"]
  API["api ×3 · FastAPI · 512 MiB / 副本"]
  Pay["paymock · FastAPI · SQLite volume"]
  Worker["worker · 匯入 · 鎖位逾時 · 對帳 · 退款 · 取消"]
  Relay["relay · 座位事件序號與歸檔"]
  Collector["collector · 日誌 · 指標 · 告警"]
  DB[("PostgreSQL 16")]
  Redis[("Redis 7")]
  Logs[("nginx 與應用 JSON 日誌")]
  Imports[("匯入檔 volume")]
  Exports[("Excel 暫存 volume")]
  Init["migrate → seed"]
  Organizer["Organizer · 匯入進度"]
  Staging["PostgreSQL · 每批 5,000 列 COPY staging<br/>同一交易提交 processed_rows 檢查點"]
  Finalize["活動鎖內重驗 fence 與權限 · finalize"]

  Browser -->|"UI、REST、SSE、付款頁"| Edge
  Edge -->|"least_conn · X-Request-ID"| API
  Edge -->|"/pay/"| Pay
  API -->|"建立付款 intent"| Pay
  Pay -->|"簽章回呼"| API
  Worker -->|"查詢、退款、取消"| Pay
  API --> DB
  Worker --> DB
  Relay --> DB
  Collector --> DB
  Init -->|"schema 與預設帳號"| DB
  API <-->|"限流、撤銷、遙測"| Redis
  Worker -->|"匯入進度、心跳"| Redis
  Relay -->|"心跳"| Redis
  Collector <-->|"遙測、心跳、告警通知"| Redis
  Organizer -->|"上傳座位檔"| Edge
  Organizer -->|"定期 GET job 狀態"| Edge
  Edge -->|"選用 job 進度 SSE"| Organizer
  API -->|"保存上傳檔"| Imports
  Imports --> Worker --> Staging --> Finalize
  Finalize -->|"同時提交座位、outbox、job 結果"| DB
  API --> Exports
  Edge --> Logs
  API --> Logs
  Worker --> Logs
  Relay --> Logs
  Logs --> Collector
  Edge -->|"stub_status"| Collector
```

- PostgreSQL 保存座位、訂單、配額、session、匯入 job、outbox 與監控資料。nginx、PostgreSQL 與 Redis 的 host port 預設綁在 loopback。三個 API 副本各由一個 Uvicorn 程序處理 REST 與 SSE。見 `compose.yaml:52`、`compose.yaml:67`、`compose.yaml:85`、`compose.yaml:120`、`compose.yaml:231`、`backend/src/ticketing/api/main.py:36`。
- `migrate` 以 schema owner 執行，`seed` 與 runtime 使用低權限角色。Worker 分別排程匯入、hold expiry、付款 reconciler 與 maintenance。見 `compose.yaml:7`、`compose.yaml:99`、`backend/src/ticketing/worker/main.py:20`。
- Organizer 輪詢 `GET /api/v1/seat-imports/{job_id}`，也可另外訂閱 `GET /api/v1/seat-imports/{job_id}/stream`。租約重領後從已提交的列數檢查點接續，並保留已提交的解析結果與檔案驗證狀態。見 `backend/src/ticketing/api/routes/imports.py:137`、`backend/src/ticketing/api/routes/imports.py:208`、`backend/src/ticketing/imports/runner.py:331–394`。
- Redis Pub/Sub 通知 API 有 session 撤銷與匯入進度變化，持久狀態由 PostgreSQL 提供。見 `backend/src/ticketing/auth/sessions.py:89`、`backend/src/ticketing/auth/sessions.py:134`、`backend/src/ticketing/imports/runner.py:323`、`backend/src/ticketing/api/routes/imports.py:188–204`。

## 2. 訂票交易

```mermaid
sequenceDiagram
  participant C as 瀏覽器
  participant N as nginx
  participant A as api
  participant P as PostgreSQL
  C->>N: POST /api/v1/events/{event_id}/orders + Idempotency-Key
  N->>A: 轉送請求與 X-Request-ID
  A->>P: BEGIN · session fence · 冪等檢查
  alt 已完成且內容相同
    A->>P: 結束交易
    A-->>C: 回放 201
  else 新請求
    A->>P: 檢查活動開賣 · 原子保留配額 · 建立訂單表頭
    A->>P: 指定座位 NOWAIT 或區域配位 SKIP LOCKED
    A->>P: 取樣 app_now() · 重檢時窗 · 更新 available → held
    A->>P: 寫入 order_items · 計價與期限 · outbox · 冪等回應
    A->>P: COMMIT · 釋放列鎖
    A-->>C: 201 Created
  end
```

指定座位的保護取自 `FOR NO KEY UPDATE NOWAIT`。更新再檢查座位為 `available`，partial unique index 保證每席最多一筆未釋放的 order item。配額透過同一交易內的 guarded UPDATE 保留，失敗時一併回滾。見 `backend/src/ticketing/api/routes/orders.py:40`、`backend/src/ticketing/booking/create.py:54`、`backend/src/ticketing/booking/create.py:91–92`、`backend/src/ticketing/booking/create.py:131–132`、`backend/src/ticketing/booking/create.py:242–248`、`backend/src/ticketing/booking/database.py:31`、`backend/migrations/versions/0001_baseline.py:189`。

## 3. 座位即時推送

```mermaid
flowchart LR
  TX["鎖位、付款、釋放或匯入<br/>業務交易與 outbox 一起提交"]
  Pending[("outbox · 待指派 seq")]
  Relay["relay · PostgreSQL advisory lock"]
  Assigned[("outbox · 已指派 seq<br/>event_stream_state · head")]
  Archive[("seat_events · 座位狀態歸檔")]
  Reader["每個 API 的活動 reader<br/>依 seq 輪詢 · 合併變更 · 共用 ring"]
  SSE["nginx → 瀏覽器 SSE<br/>delta / layout / progress / resync"]
  Browser["先讀 REST 快照<br/>以 last_seq 建立 SSE"]
  TX --> Pending --> Relay
  Relay -->|"同一 PostgreSQL 交易<br/>依活動指派連續 seq"| Assigned
  Relay -->|"同一交易歸檔座位變更"| Archive
  Assigned -->|"每 100 ms 輪詢"| Reader
  Reader --> SSE --> Browser
  Browser -.->|"Last-Event-ID 續傳"| Reader
  Reader -.->|"ring 缺段則查 PostgreSQL"| Assigned
```

- relay 依可見 outbox 列排序，為每個活動指派連續的 `seq`。同一座位的寫入受列鎖序列化，不同座位的序號可與交易提交順序不同。所有 API 副本讀取相同序號，各自合併連續列。同一座位保留較新的版本，layout 事件切開合併區間。見 `backend/src/ticketing/realtime/relay.py:14`、`backend/src/ticketing/realtime/relay.py:46`、`backend/src/ticketing/realtime/frames.py:40`、`backend/src/ticketing/realtime/reader.py:161`。
- 瀏覽器先讀 `GET /api/v1/events/{event_id}/seats`，再連 `GET /api/v1/events/{event_id}/seat-stream`，以 `Last-Event-ID` 帶入快照的 `last_seq` 或最後套用的數字序號。遇到缺段可重連回放。收到 layout、resync 或回放失敗時重新取快照。寫入逾時會結束串流。nginx 對此路徑停用 buffering。見 `backend/src/ticketing/api/routes/seats.py:58`、`backend/src/ticketing/api/routes/seats.py:112`、`backend/src/ticketing/realtime/stream.py:45`、`frontend/src/events.ts:300`、`infra/nginx/nginx.conf:128`。

## 4. 付款與回呼

```mermaid
sequenceDiagram
  participant C as 瀏覽器
  participant N as nginx
  participant A as api
  participant P as PostgreSQL
  participant M as paymock
  participant W as worker
  C->>N: POST /api/v1/orders/{order_id}/payments + Idempotency-Key
  N->>A: 轉送
  A->>P: 交易內檢查訂單，建立或重用 payment attempt
  A->>M: 以 service token 建立 intent
  A->>P: 新交易保存 intent 與冪等結果
  A-->>C: checkout_url
  C->>N: 開啟付款頁 /pay/
  N->>M: 轉送付款頁與確認操作
  M->>M: SQLite 保存付款結果與待送回呼
  M->>A: HMAC 回呼至固定 URL
  A->>P: 驗簽後鎖訂單與 attempt，去重並套用結果
  alt 期限內有效扣款
    A->>P: 訂單 paid，座位 sold，寫入 outbox
  else 晚到或額外的扣款
    A->>P: 排入退款，符合逾期條件時釋放 hold
  end
  A-->>M: 提交後回應
  W->>M: 用 service token 對帳、退款或取消
  W->>P: 套用查得的結果與退款狀態
```

- Merchant 請求使用 `X-Payment-Provider-Token`。Paymock 啟動時讀取固定 `PAYMOCK_CALLBACK_URL`，Compose 預設為 `http://api:8000/api/v1/payments/callbacks`。配送重試會重新簽章。見 `compose.yaml:198`、`paymock/src/paymock/main.py:75`、`paymock/src/paymock/main.py:150`、`paymock/src/paymock/delivery.py:17`、`backend/src/ticketing/payments/provider.py:42`。
- 付款建立先提交 attempt，再發出外部呼叫。回呼與對帳共用結果處理，扣款判定在鎖內使用 PostgreSQL `app_now()`。已付款訂單取消後先進入 `refund_pending`，退款確認後釋放座位與配額。見 `backend/src/ticketing/payments/attempts.py:147`、`backend/src/ticketing/api/routes/payments.py:126`、`backend/src/ticketing/payments/callbacks.py:144`、`backend/src/ticketing/payments/reconciler.py:51`、`backend/src/ticketing/payments/reconciler.py:90`、`backend/src/ticketing/booking/orders.py:148`。

## 5. 監控資料流

```mermaid
flowchart LR
  N["nginx · edge access log"]
  S["後端服務 · JSON log"]
  Conn["nginx stub_status"]
  API["API middleware · 每秒路由直方圖<br/>業務結果與 SSE gauge"]
  T[("Redis · telemetry:app")]
  C["collector · tail、聚合、告警"]
  PG[("PostgreSQL · access_logs / service_logs<br/>metrics_1s / business_1s / alert_events")]
  R[("Redis Pub/Sub · admin:alerts")]
  Admin["Admin · 監控與全站訂單"]
  Orders[("PostgreSQL · orders")]
  Read["api · Admin 專用 REST / SSE"]
  N --> C
  S --> C
  Conn -->|"每秒取樣"| C
  API --> T --> C
  C -->|"分批入庫"| PG
  C -->|"告警狀態變化"| R
  PG -->|"查詢已保存的資料"| Read
  Orders --> Read
  Read -->|"metrics / alerts SSE"| Admin
  Read -->|"All orders · Access log · Recent warnings and errors · Request trace"| Admin
```

- Collector tail nginx 與共享目錄中的應用 JSON 日誌，COPY 與 checkpoint 在同一交易內完成。Edge RPS 與延遲依 nginx access log 分秒聚合，連線數來自 `stub_status`。應用遙測按 boot UUID 與 interval number 去重，入庫後才 ACK Redis stream。見 `backend/src/ticketing/telemetry/logs.py:266`、`backend/src/ticketing/telemetry/collector.py:153`、`backend/src/ticketing/telemetry/app_metrics.py:132`。
- Admin 的 `/api/v1/admin/metrics/stream` 與 `/api/v1/admin/alerts/stream` 讀 PostgreSQL 快照。告警狀態變更另送 Redis 的 `admin:alerts`。`GET /api/v1/admin/requests/{request_id}/logs` 查找入口與關聯服務日誌。見 `backend/src/ticketing/telemetry/common.py:10`、`backend/src/ticketing/telemetry/collector.py:170`、`backend/src/ticketing/api/routes/monitoring.py:462`、`backend/src/ticketing/api/routes/monitoring.py:515`、`backend/src/ticketing/api/routes/monitoring.py:696`。
- Admin 透過 `GET /api/v1/admin/orders` 讀全站訂單，透過 `GET /api/v1/admin/logs/access` 讀存取日誌，透過 `GET /api/v1/admin/logs/recent` 讀近期警告與錯誤。
- 時鐘校正、保留期與缺測判讀見 [監控資料契約](../backend/src/ticketing/telemetry/README.md)。
