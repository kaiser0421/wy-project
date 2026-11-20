# 系統架構圖

> 對應 `requirement.txt` 二.5「系統架構圖：呈現系統的各個元件與資料流向」。設計細節見 `PLAN.md`。
> 文件狀態：實作前草稿（v1.2）；元件名稱與 compose service 名稱一致。圖面只呈現主要資料流，完整的交易步驟、錯誤碼與故障處理見 `PLAN.md` 對應章節。

## 1. 元件總覽與資料流向

```mermaid
flowchart TB
  subgraph Internet["① 使用者端（不可信）"]
    Buyer["購票者瀏覽器<br/>React SPA + Canvas 座位圖"]
    Admin["管理者瀏覽器<br/>監控儀表板"]
    K6["k6 壓測<br/>(profile: loadtest)"]
  end

  subgraph Edge["② 邊界"]
    NG["nginx :8080<br/>靜態檔 / 產生 X-Request-ID<br/>上傳上限 / SSE 不緩衝<br/>JSON access log"]
  end

  subgraph App["③ 應用層（無狀態，可水平擴充）"]
    API["api ×3<br/>FastAPI · 1 uvicorn worker/容器<br/>REST + SSE · Excel 匯出（專用 executor）· 512MB"]
    PM["payment-mock<br/>模擬金流 + 模擬付款頁"]
  end

  subgraph Bg["④ 背景處理"]
    WK["worker ×1–2<br/>匯入 pool<br/>逾時 sweeper / 付款 reconciler / 退款 pool"]
    RL["outbox-relay<br/>advisory lock + epoch fencing<br/>指派 seq → 依序投遞"]
    COL["telemetry-collector<br/>指標聚合 / 日誌入庫 / 告警"]
    MIG["migrate（one-shot）"]
    SEED["seed（one-shot）<br/>建立三角色帳號"]
  end

  subgraph Data["⑤ 資料層"]
    PG[("PostgreSQL 16 :5432（僅 127.0.0.1）<br/>唯一真實來源<br/>座位·訂單·配額·Session·Job·outbox<br/>日誌/指標分區表")]
    RD[("Redis 7 :6379（僅 127.0.0.1）<br/>Streams 投遞 · Pub/Sub · 限流<br/>不決定座位歸屬")]
    VOL[("Volumes<br/>logs spool · imports · exports")]
  end

  Buyer & Admin & K6 -->|"HTTP(S) · cookie + CSRF · X-Load-Run-ID（壓測）"| NG
  NG -->|"REST / SSE (least_conn) · X-Request-ID"| API
  NG -->|"/pay/* 模擬付款頁 · confirm/decline"| PM
  PM -->|"HMAC 簽章回呼（每次重送重新簽章）"| NG

  MIG -->|"alembic upgrade"| PG
  SEED -->|"三角色帳號"| PG
  MIG -.->|"完成後才啟動"| API
  SEED -.->|"完成後才啟動"| API

  API -->|"搶票單一短交易 · session 驗證 · 報表快照讀取"| PG
  API -->|"建立付款意圖（X-Request-ID）"| PM
  API -->|"限流 · 每秒 histogram · 撤銷 Pub/Sub"| RD
  RD -->|"座位事件 Stream · 撤銷通知 · admin:feed"| API
  API -->|"上傳暫存 · Excel 匯出檔"| VOL
  API -.->|"collector 失聯時直接寫告警"| PG

  WK -->|"SKIP LOCKED + lease + fence"| PG
  WK -->|"輪詢 intent / 退款 / 取消 intent"| PM
  WK -->|"讀上傳檔"| VOL
  WK -->|"匯入進度 publish"| RD

  RL -->|"指派連續 seq + COPY seat_events（同交易）· 記錄已投遞"| PG
  RL -->|"XADD seat:gen:event（Lua 比對 epoch）"| RD

  NG & API & WK & PM & RL -->|"JSON logs"| VOL
  VOL -->|"tail + offset checkpoint"| COL
  RD -->|"telemetry stream"| COL
  NG -->|"stub_status 每秒"| COL
  COL -->|"COPY 批次寫入"| PG
  COL -->|"admin:feed 指標/告警"| RD
```

## 2. 搶票寫入路徑（防超賣）· PLAN §5.2

```mermaid
sequenceDiagram
  autonumber
  participant C as 購票者
  participant N as nginx
  participant A as api
  participant P as PostgreSQL
  C->>N: POST /api/v1/events/{id}/orders（Idempotency-Key）
  N->>A: 加上 X-Request-ID
  A->>P: session 驗證（索引查詢）
  A->>P: BEGIN
  A->>P: ① 冪等 advisory try-lock，查既有結果
  A->>P: ② 活動閘門（已發布、開賣時窗）
  A->>P: ③ 配額列 INSERT ON CONFLICT DO NOTHING，再 guarded UPDATE（reserved ≤ 4−n）
  A->>P: ④ INSERT orders 表頭
  A->>P: ⑤ 鎖座位 NOWAIT（指定座位）或 SKIP LOCKED（區域配位）· 保護開始
  A->>P: ⑥ UPDATE seats WHERE status='available'（筆數須相符）
  A->>P: ⑦ INSERT order_items（partial unique index）· app_now() 重檢開賣時窗 · 計價 · expires_at
  A->>P: ⑧ INSERT outbox · 寫入完成的冪等回應
  A->>P: COMMIT（保護釋放）
  alt 成功或相同內容重送
    A-->>C: 201 Created（重送時帶 Idempotent-Replayed）
  else 座位被占、競爭、限購、未開賣、前一請求仍在處理
    A-->>C: 409 problem+json（區域配位不足時先 ROLLBACK，再以不加鎖計數區分 insufficient-seats 與 allocation-contended）
  else 同 key 不同內容、座位 id 重複
    A-->>C: 422 problem+json
  else 鎖等待逾時
    A-->>C: 503 server-busy + Retry-After
  end
```

## 3. 即時座位推送 · PLAN §6

```mermaid
flowchart LR
  TX["訂票或釋放交易<br/>COMMIT（含 outbox 列）"] --> OB[("outbox<br/>seq IS NULL")]
  OB -->|"每 50ms 依 id 排序"| RL["outbox-relay<br/>1. 指派連續 seq + COPY seat_events<br/>2. 依序投遞 last_delivered+1"]
  RL -->|"XADD seq-0"| RS[("Redis Stream<br/>seat:gen:event")]
  RS -->|"XREAD 連續性檢查"| RD["api 程序 reader<br/>100ms 合併 · 共享 ring"]
  RD -->|"經 nginx：SSE snapshot / delta id=gen:to<br/>每秒 progress（assigned_head、未指派延遲）"| CL["瀏覽器<br/>version 較大者勝"]
  CL -.->|"斷層或停滯時重連<br/>Last-Event-ID = 最後套用的 gen:seq"| RD
  RD -.->|"回補順序：ring → Redis XRANGE<br/>→ PG 已指派 outbox（seq > s）→ 版本化快照"| PGR[("PostgreSQL<br/>replay log / 快照")]
```

- generation 不符時直接回快照；慢客戶端落後超出回補範圍時關閉串流，重連後取快照。
- 匯入完成會 `layout_ver+1` 並發出 `layout_changed`；客戶端暫停套用 delta，取得 `layout_ver ≥ N` 的快照後再繼續。
- Redis 被清空時，relay 以新 generation 從 outbox 依原 seq 重建，舊串流關閉、客戶端重連。

## 4. 付款與回呼 · PLAN §5.4

```mermaid
sequenceDiagram
  autonumber
  participant C as 購票者
  participant N as nginx
  participant A as api
  participant P as PostgreSQL
  participant M as payment-mock
  participant W as worker
  C->>N: POST /orders/{id}/payments（Idempotency-Key）
  N->>A: 轉送
  A->>P: txn1：鎖訂單，檢查待付款且未逾時，建立或重用唯一 attempt，冪等列 in_progress
  A->>M: 建立 intent（merchant_payment_id = attempt.id）
  A->>P: txn2：鎖訂單與 attempt，轉為 awaiting_customer，存 201 回應
  A-->>C: 201 checkout_url（金流明確拒絕時 402 payment-declined）
  C->>M: 經 nginx 開啟付款頁並確認
  M->>N: 回呼（HMAC，失敗重試 24h）
  N->>A: 轉送
  A->>P: 單一交易：事件去重，鎖訂單與 attempt，在鎖內查 capture，decision_at = app_now()
  alt 同一 capture 重送
    A->>P: 回放既有結果，無副作用
  else 待付款且在期限前
    A->>P: paid，座位 held 轉 sold，寫 outbox
  else 待付款但已過期限
    A->>P: release_order 歸還座位與配額，建立退款
  else 已過期、已取消或已付款訂單的另一筆扣款
    A->>P: 只建立退款，座位與配額不動
  end
  A-->>N: 200（提交後才回應）
  loop 每 15 秒
    W->>P: 找出卡在 creating 或久無回呼的 attempt
    W->>M: 以同一 merchant id 重試建立或查詢 intent
    W->>P: 經同一處理流程套用結果
  end
  W->>M: 執行退款與取消 intent（冪等）
```

- 取消待付款訂單：`release_order` + 同交易寫入取消 intent 的待辦；取消已付款訂單：`refund_pending` → 退款確認後才釋放座位與配額。

## 5. 監控資料流 · PLAN §10

```mermaid
flowchart LR
  NGL["nginx access log<br/>使用者實際感受 · load_run"] --> SP[("logs spool")]
  APL["api、worker、payment-mock、relay<br/>JSON logs（含 request_id）"] --> SP
  HIST["api middleware<br/>每秒 histogram · sse_active · interval_no"] --> TS[("Redis telemetry:app")]
  STUB["nginx stub_status"] -->|"每秒"| COL
  SP -->|"tail + offset checkpoint"| COL["telemetry-collector"]
  TS -->|"XREADGROUP → 去重（boot_uuid, interval_no）→ 提交後 ACK"| COL
  COL -->|"COPY"| T1[("access_logs、service_logs<br/>metrics_1s、business_1s<br/>alert_events")]
  COL -->|"每秒評估告警"| FEED[("Redis admin:feed")]
  FEED --> SSEA["api → Admin SSE<br/>即時圖表與告警"]
  ADM["Admin"] -->|"GET /admin/requests/{id}/logs"| API2["api"]
  API2 -->|"依 request_id 查詢"| T1
  API2 -.->|"collector 失聯：直接寫 MONITOR_DOWN"| T1
  ADM -.->|"Redis 失聯：每 5 秒輪詢 /admin/alerts"| API2
```

- `seat_events` 由 outbox-relay 寫入（見圖 3），不經 collector。
- request_id 由 nginx 產生，傳遞到 payment-mock（回呼 body 帶 `origin_request_id`）、outbox、seat_events、匯入 job 與 worker 日誌；sweeper 日誌連結原始 hold 的 request_id。

## 6. 部署拓撲（docker compose）

| Service | 數量 | 依賴 | 對外 |
|---|---|---|---|
| nginx | 1 | api（healthy） | :8080（本機 http；TLS 為選配） |
| api | 3 | migrate、seed（completed）、postgres、redis（healthy） | 僅經 nginx |
| worker | 1–2 | migrate、postgres | – |
| outbox-relay | 1 | migrate、postgres、redis | – |
| telemetry-collector | 1 | migrate、postgres、redis | – |
| payment-mock | 1 | – | 內部網路；付款頁經 nginx |
| postgres | 1 | – | 127.0.0.1:5432 |
| redis | 1 | – | 127.0.0.1:6379 |
| migrate / seed | one-shot | postgres（seed 另依賴 migrate） | – |
