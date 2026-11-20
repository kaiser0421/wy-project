# 壓力測試報告（LOADTEST）

> **文件狀態：實作前草稿（pre-implementation draft）。**
> 本文件依 `PLAN.md` v1.2 §13 與 `requirement.txt` 第七節建立**結構、方法與驗收條件**；**目前沒有任何實測數字**。
> 所有 `TBD(Txx)` 皆為待填欄位，`Txx` 對應 `PLAN.md` §21 的 task ID（主要為 T9.1 情境 A、T9.2 情境 B、T9.3 一致性與三方對照）。
> 規模檢查點 CP1–CP4 的數據於開發期間先寫入 §9，完整壓測結果於 P9 填入 §4–§7。
> `PLAN.md` §13.2 的目標值僅為**假設**，在本文件中只出現在「目標」欄，絕不出現在「實測」欄。
> 標記「（暫定）」者為 PLAN 未定案、本文件提出的具體做法，實作時以腳本為準並同步修改本文件。

---

## 目錄

1. 測試環境（機器規格、版本與設定）
2. 方法（工具、隔離、錯誤分類、量測來源）
3. 情境定義（A、B）
4. 情境 A 結果
5. 情境 B 結果
6. 壓測後一致性檢查
7. 瓶頸分析
8. 限制與注意事項
9. 規模檢查點 CP1–CP4 紀錄
10. 如何重現

---

## 1. 測試環境

### 1.1 機器規格

| 項目 | 值 |
|---|---|
| 機器 / 型號 | TBD(T9.1) |
| CPU（型號、實體核心 / 執行緒） | TBD(T9.1) |
| 記憶體 | TBD(T9.1) |
| 磁碟（類型、檔案系統） | TBD(T9.1) |
| OS / kernel | TBD(T9.1) |
| Docker Engine / Compose 版本 | TBD(T9.1) |
| 虛擬化（Docker Desktop VM 配置的 CPU / RAM，若適用） | TBD(T9.1) |
| 網路 | 單機 loopback / docker bridge（k6 與受測系統同機，見 §2.2 隔離） |

### 1.2 cpuset 配置

| 群組 | 服務 | cpuset | 備註 |
|---|---|---|---|
| 受測系統（SUT） | nginx、api ×3、worker、outbox-relay、telemetry-collector、payment-mock、postgres、redis | TBD(T9.1) | |
| 壓測端 | k6（profile `loadtest`） | TBD(T9.1) | 與 SUT 不重疊 |

### 1.3 軟體版本與設定

| 項目 | 值 |
|---|---|
| Git commit（受測版本） | TBD(T9.1) |
| PostgreSQL | 16.x（TBD(T9.1) 精確版本） |
| Redis | 7.x（TBD(T9.1)） |
| Python / FastAPI / uvicorn / asyncpg | TBD(T9.1) |
| nginx | TBD(T9.1) |
| k6 / xk6-sse | TBD(T9.1) |
| API 副本 | 3 × 1 uvicorn worker，`mem_limit: 512m`，nginx `least_conn` |
| asyncpg pool | 每程序 10、`pool_timeout=1s`（逾時 → 503 `server-busy`）；worker/回呼獨立 pool |
| PG timeouts | `lock_timeout=500ms`、`statement_timeout=2s`、`idle_in_transaction_session_timeout=5s` |
| PG 設定 | `shared_buffers=1GB`、`max_wal_size=4GB`、`wal_compression=on`、`synchronous_commit=on`、`max_connections=200`；`seats`/`outbox` autovacuum scale factor 0.01 |
| 區域售罄快取（可選，250ms） | 情境 A 主跑：TBD(T9.1)（開/關需註明；正確性測試一律關閉） |
| `HOLD_TTL_SECONDS` | 600（預設；若壓測改值需註明） |
| 限流（下單 ~10/s/user 等） | 下單限流主跑維持預設；登入限流依 `.env.loadtest` 放寬以預先建立 session（§2.2），差異 TBD(T9.1) |
| `COOKIE_SECURE` | `false`（預設，cookie 名稱 `sid`，純 http） |
| `track_commit_timestamp` | 情境 B / CP2 開啟（量測提交時間用，非正式預設）；情境 A 主跑：TBD(T9.1) 註明 |
| 其他偏離預設值的設定 | TBD(T9.1)（`.env` diff 附於此） |

---

## 2. 方法

### 2.1 工具

- **k6** 執行 HTTP 負載；**xk6-sse** 擴充處理 SSE 訂閱。Python asyncio SSE client（`loadtest/sse_client.py`）為備援並作為 hash 探針。
- k6 以 compose profile `loadtest` 的 `k6` 服務執行（自訂 image 內建 xk6-sse）。
- 目標 URL：k6 容器在 compose 網路內打 `http://nginx`（經過 nginx，邊界指標才有紀錄）；從 host 執行時為 `http://localhost:8080`。不直連 api 容器。
- 認證：每個 VU 使用自己的 cookie jar 與 CSRF header，與瀏覽器相同路徑。預設部署 `COOKIE_SECURE=false` → cookie 名稱為 `sid`；只有 TLS 環境（`COOKIE_SECURE=true`）才是 `__Host-sid`。腳本不硬編 cookie 名稱，一律沿用登入回應的 `Set-Cookie`。
- 原始資料：k6 以 `--out json`（或 csv）保留逐請求樣本，百分位數由原始樣本或可合併 histogram 計算（不由 summary 推算）；`loadtest/report.py` 合併 k6 樣本、`metrics_1s` / `business_1s`（DB）、docker stats，產出本文件的表格。

### 2.2 隔離與干擾控制

- k6 與 SUT 以 **cpuset 隔離**（§1.2）；全程記錄 k6 容器 CPU 使用率，若 k6 任一核心持續 > TBD(T9.1)% 則該輪結果標註「壓測端可能飽和」。
- 每輪開始前：重建資料庫狀態（`loadtest/seed.py`）、清空 Redis、重啟 SUT，確保 buffer/cache 狀態可比較（暖機策略 TBD(T9.1) 並記錄）。
- **Session 準備在量測窗之前完成**。預設限流（登入 50/min/IP、10/15min/email）與 argon2id（每容器並行上限 4）使「3,000 個帳號在開賣前從同一 IP 登入」不可行，因此（暫定，TBD(T2.9)）：
  - `loadtest/seed.py` 建立壓測帳號，並以壓測專用的登入限流設定（`.env.loadtest` 提高登入 IP 上限，差異記錄於 §1.3）在開賣前分批真實登入，把每個 VU 的 cookie + CSRF token 寫入 session 檔供 k6 讀取；
  - 起跑前以 `GET /api/v1/auth/me` 逐一驗證 session 有效（失敗數必須為 0 才開始計時）；
  - 登入總耗時與失敗數另行報告（§4.7），不混入搶票數據。
- 情境 B 同理：2,000 個不同帳號的 session 以相同方式預先建立。

### 2.3 回應分類

每個回應依「狀態碼 + problem+json `code` + body 檢查」分到下列類別。k6 內建 `http_req_failed` 只看狀態碼、無法檢查 problem `code` 與 body，因此**主錯誤率使用自訂 `Rate` 指標 `unexpected_rate`**（body-aware），並以 custom `Counter` / `Trend`（tag：`endpoint`、`code`、`phase`）記錄分類；`http_req_failed`（以 `expectedStatuses` 設為 2xx/402/409）僅作為原始狀態碼參考值並列。

| 類別 | 判定 | 是否計入「錯誤率」 |
|---|---|---|
| 成功 | 2xx，且 body 通過檢查（例如下單 201 帶 order id、數量正確） | 否 |
| 預期付款拒絕 | 402 `payment-declined`——只在**付款拒絕輪**（§3.1 輔助執行）視為預期：payment-mock 設定在**建立 intent 時**對指定訂單明確拒絕（介面 TBD(T3.1)），因此 `POST /api/v1/orders/{id}/payments` 本身回 402；k6 依 order id 知道哪些付款應被拒，斷言 status = 402、`code = payment-declined`，並以同 key 重送斷言回放同一 402（`Idempotent-Replayed: true`）。主跑（拒絕比例 0）出現任何 402 → 非預期 | 否（另列付款結果） |
| 快取售罄 | 409 `zone-sold-out-cached`——**只在該輪啟用 250ms 區域售罄快取時**視為預期；未啟用時出現 → 非預期 | 否 |
| 冪等重放 | 201 + `Idempotent-Replayed: true`，且 order id 與首次相同 | 否 |
| **預期業務拒絕** | 409：`seat-unavailable`、`seat-contended`、`insufficient-seats`、`allocation-contended`、`purchase-limit-exceeded`、`event-not-on-sale`、`idempotency-in-progress`、`order-not-cancellable`（重複取消競態） | **否**（另列業務結果分佈） |
| 負載削減 | 503 `server-busy`（+ Retry-After）、429 限流 | **分開列示**（不算業務成功，也不與非預期 5xx 混算） |
| **非預期** | 其他 5xx（含 502/504）、未列出的 4xx（400/401/403/404/412/422…，因腳本不應送出這類請求）、連線錯誤/逾時、2xx 但 body 檢查失敗、重放回傳不同 order id | **是** |

錯誤率（主指標）= `unexpected_rate` = 非預期 / 全部非串流請求。另報：負載削減率、各業務 409 `code` 佔比、原始 `http_req_failed`。故障注入輪次中，注入造成的錯誤依注入時間窗另行歸類，不與穩態錯誤率混算。

### 2.4 量測來源（三方對照）

| 來源 | 層 | 內容 |
|---|---|---|
| k6 | 客戶端 | `http_reqs`、`http_req_duration`（per endpoint tag）、分類計數 |
| 邊界（edge） | nginx JSON access log → `metrics_1s(source='edge')` | 使用者感受延遲（`request_time`）、狀態碼、502/504/499 |
| 應用（app） | API handler histogram → `metrics_1s(source='app')`；業務 → `business_1s` | 歸因（per route）、業務失敗原因 |
| DB | 一致性 SQL | 業務事實（座位、訂單、配額） |

百分位數一律由原始樣本或合併 histogram bucket 重算，**不平均百分位數**。

**edge vs k6 對照規則**（避免把非 k6 流量算進來）：
- 母體：只比對 k6 送出的非串流請求。k6 每個請求帶 `X-Load-Run: <run_id>` header（暫定），nginx 原樣寫入 access log，對照時以 `access_logs` 依該值**等值**比對與 `[t_start, t_end)` 篩選；**排除** healthcheck、payment-mock 回呼、SSE 串流與其他背景流量。不使用混合多個 scope 的 `metrics_1s` 總和。
- 時間基準：以 nginx `$msec`（請求完成時間）為準；k6 端以請求結束時間落在同一窗口者計數。
- 先等 collector drain（`COLLECTOR_LAG` 歸零、log checkpoint 追上檔尾）再比對；任一側為 0 或缺資料即判定**失敗**（不可因空集合而通過）。
- 驗收：`|edge − k6| / k6 < 1%`（分母 = k6 該母體請求數）。

---

## 3. 情境定義

### 3.1 情境 A：單一活動 10,000 座位、3,000 VU 持續搶購 60 秒

**資料準備（`loadtest/seed.py`）**

| 項目 | 值 |
|---|---|
| 活動 | 1 個，已發布；`sales_start_at` = 壓測起跑時刻 |
| 座位 | 10,000，4 區（每區座位數與票價 TBD(T2.9)） |
| 使用者 | 3,000 個 `user` 角色帳號，每 VU 對應一個帳號（每人每活動上限 4 張） |

**主跑 executor**

```js
// loadtest/k6/scenario_a.js（示意，以實際腳本為準 TBD(T2.9)）
scenarios: {
  rush: {
    executor: 'constant-vus',
    vus: 3000,
    duration: '60s',
    // 所有 VU 在開賣時刻同時起跑；登入於量測窗之前完成（§2.2）
  },
},
```

**每個 iteration 的請求組合**

| 步驟 | 比例 | 請求 |
|---|---|---|
| 1. 取快照 | 指定座位路徑需要 | `GET /api/v1/events/{id}/seats`（`{gen, layout_ver, last_assigned_seq, seats(bitmap + version)}`）；CP1 時 P4 快照 API 尚未完成，改用 T2.x 提供的最小座位列表/快照（TBD(T2.9)） |
| 2a. 指定座位下單 | 70% | `POST /api/v1/events/{id}/orders` `{seat_ids:[…]}`，1–4 張，從快照中可售座位挑選 |
| 2b. 區域配位下單 | 30% | `POST /api/v1/events/{id}/orders` `{zone_code, quantity}`，quantity 1–4 |
| 3a. 付款 | 下單成功者的 80% | `POST /api/v1/orders/{id}/payments`（回 `checkout_url`）→ k6 以 HTTP 呼叫 payment-mock 模擬付款頁的「確認付款」動作（介面 TBD(T3.1)；主跑拒絕比例 = 0，拒絕情境另跑並註明）→ 輪詢 `GET /api/v1/orders/{id}` 直到 `paid` 或輪詢逾時（上限 TBD(T2.9)） |
| 3b. 取消 | 下單成功者的 10% | `POST /api/v1/orders/{id}/cancel` |
| 3c. 保留不動 | 下單成功者的 10% | 不付款（hold TTL 600s > 測試時間，不會在量測窗內逾時） |
| think time | 每步之間 | 50–300ms 均勻分佈 |

**冪等與重試**

- 下單、付款、取消**全部**帶 `Idempotency-Key`（UUID）。
- **2% 重送**：分母 = 首次回應為 201 的下單；這些請求在收到 201 後以**相同 key、相同 body** 再送一次 → 預期 201 + `Idempotent-Replayed: true` 且 order id 相同（不一致即非預期）。k6 記錄每組 (key, 首次 order id, 重送 order id) 供 C12 對帳。
- 可重試的 409（`seat-contended`、`allocation-contended`、`idempotency-in-progress`）：有界重試（暫定 ≤ 2 次、遵守 `Retry-After`，否則 100–300ms jitter）。`allocation-contended` / `idempotency-in-progress` 以**同 key 同 body** 重試；指定座位遇到 `seat-contended` / `seat-unavailable` 若要改挑座位，**必須換新 key**（同 key 不同 body 會得到 422，屬腳本錯誤）。
- 不可重試（`insufficient-seats`、`purchase-limit-exceeded`、`event-not-on-sale`）不重試。
- VU 達到 4 張上限或全場售完後：轉為低頻瀏覽（`GET .../seats`、`GET /api/v1/orders`，間隔 1–2s），以維持 3,000 個同時使用者（暫定，TBD(T2.9)）。這會改變 offered load，因此結果中報告**每秒處於「購買 / 瀏覽」模式的 VU 數**與轉換時間分佈。

**三階段報告**（`business_1s` 不存可售座位數，因此由 `report.py` 在測試期間每秒以 SQL 取樣 `count(*) FILTER (WHERE status='available')` 形成時間序列，事後依此切階段；取樣查詢本身的負載另行註明）

| 階段 | 定義 |
|---|---|
| 爆發（burst） | 開賣後最初幾秒的同時湧入（暫定：t = 0–5s） |
| 競爭（contention） | 爆發後至可售座位首次歸零 |
| 售完（sold-out） | 可售座位**首次**歸零之後；取消釋出的座位會短暫回到可售，報告每次「重新開放」的時間點與座位數 |

若 60 秒內**從未售完**：只報爆發與競爭兩段，並在結果中明確寫「未售完、剩餘可售 N 座」，不得把此情況寫成達標。

**輔助執行**

| 執行 | 設定 | 目的 |
|---|---|---|
| Arrival-rate sweep（**Could 級**，PLAN §21.3；時間不足時刪除並於 README 未完成項目列出） | `ramping-arrival-rate` 500 → 5,000 it/s（階梯與 `preAllocatedVUs` TBD(T9.1)） | 找拐點（p99 / 非預期錯誤 / 503 開始上升的 it/s） |
| Auth 成本對照 | 情境 A 以 auth on / off 各跑一次（off 僅限 test image） | DESIGN Q5.1 每請求 session 查詢成本 |
| 匯出期間搶票 | 情境 A 同時觸發 Excel 匯出（匯出在 api 內執行；量測執行匯出的同一副本） | 驗證匯出不拖垮訂票 p99 / SSE 延遲（PLAN §9；CP4） |
| 付款拒絕輪 | 情境 A 縮短版，payment-mock 設定在建立 intent 時拒絕指定比例（TBD(T3.1)） | 驗證 `POST /orders/{id}/payments` 回 402 `payment-declined` 與其冪等重放、使用者以新 key 重新付款可成功、拒絕後配額與座位不漂移（C2–C9） |

### 3.2 情境 B：2,000 條 SSE 連線同時訂閱同一活動

| 項目 | 值 |
|---|---|
| 拓撲 | 3 個 API 副本 × 1 worker，nginx `least_conn` |
| 訂閱者 | 2,000 條 `GET /api/v1/events/{id}/seat-stream`（xk6-sse），同一活動，使用 **2,000 個不同 session**（不同帳號登入，非共用 cookie）；先取快照 `GET /api/v1/events/{id}/seats` 再以 `Last-Event-ID: <gen>:<seq>` 訂閱；另有 Python 探針 client 計算最終狀態 hash |
| 安靜連線 | 一部分訂閱者（比例 TBD(T4.7)）訂閱後長時間不收到 delta（例如 writer 停止期間或只收 progress frame），用來驗證撤銷重驗不依賴推送 tick |
| 慢讀者 | 10% 訂閱者限速讀取（限速值 TBD(T4.7)）；結果中以各連線實際讀取 bytes/s 驗證確實被限速 |
| Writer | `loadtest/writer.js`（第二個 k6 程序，由 `loadtest/run.sh b` 一併啟動）透過下單/取消 API 產生座位變化，目標速率 **50 / 300 / 1,000 changes/s** 各跑一輪；每輪暖機與穩態時長 TBD(T9.2)；1,000 changes/s 為 **Could 級**（PLAN §21.3）。CP2 時若 P3 金流尚未完成，writer 只用「下單 + 取消待付款訂單」即可產生變化（不需付款） |
| 實際寫入速率 | 壓測設定開啟 `track_commit_timestamp=on`（§1.3 註明）。注意 relay 指派 seq 的 UPDATE 會改寫 `outbox.xmin`，不能用 `outbox.xmin` 取 writer 的提交時間；需在 outbox 新增 `writer_xid xid8 DEFAULT pg_current_xact_id()`（**PLAN §4 schema 需補**，TBD(T4.1)），以 `pg_xact_commit_timestamp(writer_xid::xid)` 依**提交時間**分秒，報告「實際提交的座位變化數 / 秒」；relay 指派吞吐量（依 `assigned_at` 分秒）另列，兩者差距即提交積壓 |

**故障注入**（各自獨立一輪或於同一輪不同時間點，TBD(T9.2) 記錄時間軸）

| # | 注入 | 方式 | 預期 |
|---|---|---|---|
| F1 | 10% 慢讀者 | 客戶端限速 | 落後的連線依 ring → Redis replay → PG replay log 補齊，仍不足則關閉並重連拿快照；伺服器記憶體有界、其他讀者延遲不受拖累 |
| F2 | 中途 kill 一個 API 副本 | `docker kill <api 容器>` | 該副本的連線以 `Last-Event-ID: <gen>:<applied>` 重連到其他副本並續傳（ring → Redis → PG replay） |
| F3a | Redis 重啟（AOF 保留資料） | `docker compose restart redis` | generation 可能保留：客戶端重連後續傳、不一定需要快照；記錄實際走了哪條路徑 |
| F3b | Redis 資料遺失 / generation 遺失 | `redis-cli FLUSHALL`（或停機刪 AOF 後啟動） | relay 從 PG outbox 以原 seq 重建新 generation → reader 偵測 gen 變更 → 客戶端重連拿快照 |
| F4 | 1 秒內 2,000 連線重連風暴 | 客戶端同時斷線重連 | 觀察窗 = 風暴開始後 30 秒（暫定）；100% 重新連上、首快照時間有界、期間下單 p99 記錄 |

**撤銷量測**（PLAN §7.1，與上述故障注入分開記錄）：對一組 session（含安靜連線）分別執行**登出、停用、改角色**；其中一組**刻意丟棄 Pub/Sub 撤銷通知**（注入方式 TBD(T4.6)），只靠每副本每秒批次重驗關閉。記錄：從撤銷交易提交起算的關閉延遲（目標：Pub/Sub 正常數十 ms、通知遺失 **≤ 2 秒**）、每副本每秒檢查的 session row 數、同時段訂票 p99。另測 fail-closed（Should 級）：注入讓批次重驗查詢連續 2 次失敗，通過條件為該副本上**所有**受影響串流在第 2 次失敗後 ≤ 1 秒內關閉（暫定，以 T4.6 為準）。

**量測項目**：連線成功率、建立失敗與重連失敗數、**初次快照取得時間**、**串流建立時間（首個 frame）**、**恢復用快照時間**（三者分開）、commit → 客戶端套用延遲、斷層 / resync 次數、每 API 程序 RSS、event loop lag、bytes/s、撤銷關閉延遲、writer 端 HTTP RPS / p50 / p95 / p99 / 錯誤率、最終狀態 hash 與 PG 比對。

**commit → apply 延遲量法**（暫定，TBD(T4.7)）：writer 收到 2xx 的時間不一定早於 SSE 套用時間，不能當作 commit 邊界。改用 DB 記錄的提交時間：
- 關聯鍵：outbox 的 `request_id`（writer 在請求中記錄 nginx 回傳的 `X-Request-ID`）→ 對應的 `(seat_idx, version)`。
- 主指標：`apply_time − pg_xact_commit_timestamp(outbox.writer_xid::xid)`（writer 交易的提交時間，DB 時鐘，PG 與探針同機；`writer_xid` 欄位見上表）。
- 交叉驗證上界：`apply_time − writer 送出請求時間`（commit 一定晚於送出）。
- 分段：commit → `assigned_at`（relay 指派延遲）、`assigned_at` → apply（投遞 + SSE 管線）。
- 合併：探針以「每座位 version ≥ 目標 version」判定已套用，合併（coalesced）掉的中間 version 視為隨較大 version 一起套用；**最終仍未套用**的 version 計為遺失並列入結果。
- 同機時鐘：探針、writer、PG 同一台主機；報告兩者時鐘差的量測值。

---

## 4. 情境 A 結果

> 全部 `TBD(T9.1)`。CP1（3,000 VU × 30s 短版）的數據見 §9.1。

### 4.1 總覽

| 指標 | 目標（假設，PLAN §13.2） | 實測 |
|---|---|---|
| 總請求數（k6 `http_reqs`） | – | TBD(T9.1) |
| 平均 RPS / 峰值 RPS（邊界） | – | TBD(T9.1) |
| 下單 p95 / p99 | < 300ms / < 800ms | TBD(T9.1) |
| 售罄拒絕 p95 | < 50ms | TBD(T9.1) |
| 非預期錯誤率 | 0 非預期 5xx | TBD(T9.1) |
| 負載削減（503 / 429）比例 | 另列 | TBD(T9.1) |
| 超賣座位數 | 0 | TBD(T9.3) |
| 1 萬座位全數售出/保留所需時間 | < 60s | TBD(T9.1) |
| outbox lag p99 | < 1s | TBD(T9.1) |
| k6 CPU（最高核心） | 未飽和 | TBD(T9.1) |

### 4.2 各端點延遲（k6 量測，ms）

| 端點 | 請求數 | RPS | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| `GET /events/{id}/seats` | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| `POST /events/{id}/orders`（指定座位） | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| `POST /events/{id}/orders`（區域配位） | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| `POST /events/{id}/orders`（售罄拒絕） | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| `POST /orders/{id}/payments` | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| `GET /orders/{id}`（付款輪詢） | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| `POST /orders/{id}/cancel` | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| `POST /payments/mockpay/callbacks`（app 量測） | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| 全部 | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |

### 4.3 分階段

| 階段 | 時間範圍 | RPS | 下單 p50 / p95 / p99 | 非預期錯誤率 | 503 比例 | hold 成功數 |
|---|---|---|---|---|---|---|
| 爆發 | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| 競爭 | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |
| 售完 | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) |

### 4.4 結果分類（下單請求）

| 結果 | `code` | 次數 | 佔比 |
|---|---|---|---|
| 成功 201 | – | TBD(T9.1) | TBD(T9.1) |
| 冪等重放 201 | `Idempotent-Replayed: true` | TBD(T9.1) | TBD(T9.1) |
| 座位已被占 | `seat-unavailable` | TBD(T9.1) | TBD(T9.1) |
| 座位鎖競爭（可重試） | `seat-contended` | TBD(T9.1) | TBD(T9.1) |
| 區域不足 | `insufficient-seats` | TBD(T9.1) | TBD(T9.1) |
| 配位競爭（可重試） | `allocation-contended` | TBD(T9.1) | TBD(T9.1) |
| 超過限購 | `purchase-limit-exceeded` | TBD(T9.1) | TBD(T9.1) |
| 冪等處理中 | `idempotency-in-progress` | TBD(T9.1) | TBD(T9.1) |
| 未開賣/已結束 | `event-not-on-sale` | TBD(T9.1) | TBD(T9.1) |
| 負載削減 | 503 `server-busy` / 429 | TBD(T9.1) | TBD(T9.1) |
| **非預期** | （逐一列出） | TBD(T9.1) | TBD(T9.1) |

付款 / 取消結果：付款成功數、402 `payment-declined` 數、輪詢逾時數、取消成功數、`order-not-cancellable` 數 — TBD(T9.1)。

回呼延遲：payment-mock `captured_at` → 本地 capture 紀錄提交的延遲 p50 / p99 / max（與 `PAYMENT_CALLBACK_LAG` 告警同一起點：**從金流端 capture 起算，不含買家思考時間、也不從下單時間起算**）— TBD(T9.1)；壓測期間 `PAYMENT_CALLBACK_LAG` 是否觸發 — TBD(T9.1)。

### 4.5 三方數字對照（edge vs k6 vs app）

| 指標（同一時間窗） | k6 | 邊界（nginx） | 應用 | 差異 | 說明 |
|---|---|---|---|---|---|
| 請求總數（k6 母體，依 §2.4 篩選；排除串流、healthcheck、回呼） | TBD(T9.3) | TBD(T9.3) | TBD(T9.3) | TBD(T9.3)（需 < 1%：abs(edge−k6) ÷ k6） | collector drain 後比對 |
| 5xx 數 | TBD(T9.3) | TBD(T9.3) | TBD(T9.3) | TBD(T9.3) | |
| 下單 p99（ms） | TBD(T9.3) | TBD(T9.3) | TBD(T9.3) | TBD(T9.3) | edge 含 proxy 排隊；app 只含 handler |
| hold 成功數 | TBD(T9.3) | – | `business_1s` TBD(T9.3) | DB：TBD(T9.3) | |

### 4.6 Arrival-rate sweep（Could 級；未執行時填「未執行（Could 級刪除）」）

| 目標 it/s | 實際 it/s | 下單 p95 | 下單 p99 | 非預期錯誤率 | 503 比例 | 備註 |
|---|---|---|---|---|---|---|
| 500 | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | |
| … | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | |
| 5,000 | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | TBD(T9.1) | |

拐點：TBD(T9.1)。

### 4.7 輔助量測

| 項目 | 實測 |
|---|---|
| Auth on vs off：下單 p99 差、DB 查詢數差 | TBD(T1.8, T9.1) |
| 匯出期間同副本下單 p99 / SSE 延遲 | TBD(T6.2, T9.1) |
| 3,000 個 session 預先建立：總耗時、失敗數、起跑前 `/auth/me` 驗證失敗數（須為 0） | TBD(T9.1) |
| 資源：每 API 程序 RSS 峰值、CPU、PG CPU / 連線數 / pool wait p95 | TBD(T9.1) |

### 4.8 儀表板截圖

- Admin 監控頁（RPS、錯誤率、p50/95/99、連線數；邊界與應用並列）：TBD(T9.1)
- 業務漏斗（hold 成功 / 衝突 / 售罄 / 限購）：TBD(T9.1)

---

## 5. 情境 B 結果

> 全部 `TBD(T9.2)`。CP2（2,000 SSE + 300 changes/s）的數據見 §9.2。

### 5.1 連線與延遲（依寫入速率）

| 指標 | 目標（假設） | 50 changes/s | 300 changes/s | 1,000 changes/s（Could） |
|---|---|---|---|---|
| 實際提交的座位變化數 / 秒（PG） | ≈ 目標 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 連線成功率；建立失敗 / 重連失敗數 | 100% / 0 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 初次快照取得時間 p50 / p95 / p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 串流建立時間（首個 frame）p50 / p95 / p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| commit → apply（`pg_xact_commit_timestamp` → 套用）p50 / p95 / p99 | p99 < 500ms @300 changes/s | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 送出 → 套用（上界交叉驗證）p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| commit → `assigned_at`（relay 指派延遲）p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| `assigned_at` → apply（投遞 + SSE 管線）p50 / p95 / p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 遺失（最終未套用）的 version 數 | 0 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 斷層偵測次數 / resync（重連 + 快照）次數 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 每 API 程序 RSS 峰值 | < 512MB 上限 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| event loop lag p99 / max | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 每副本送出 bytes/s | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| outbox lag p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| writer HTTP：RPS / p50 / p95 / p99 / 非預期錯誤率 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 最終狀態 hash = PG hash（C13） | 一致 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |

未執行的組合（例如刪除的 Could 級 1,000 changes/s）填「未執行（Could 級刪除）」，不留空。

### 5.2 故障注入

| # | 注入 | 注入時刻 | 受影響連線數 | 恢復路徑（ring / Redis / PG replay / 快照）分佈 | 恢復用快照時間 p99 | 全部恢復所需時間 | resync 次數 | 期間最大畫面過期 | 期間下單 p99 | 最終 hash 一致 |
|---|---|---|---|---|---|---|---|---|---|---|
| F1 | 10% 慢讀者 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| F2 | kill 一個 API 副本 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| F3a | Redis 重啟（AOF 保留） | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| F3b | Redis generation 遺失 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| F4 | 2,000 連線 1 秒內重連 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |

慢讀者：實際限速值驗證、被關閉的連線數、慢讀者存在時其他讀者的延遲是否受影響 — TBD(T9.2)。

### 5.3 撤銷與副作用

| 項目 | 目標（假設） | 實測 |
|---|---|---|
| 撤銷（登出 / 停用 / 改角色）→ SSE 關閉延遲 p50 / p99 / max（Pub/Sub 正常，含安靜連線） | 數十 ms | TBD(T4.6, T9.2) |
| 撤銷 → SSE 關閉延遲 p50 / p99 / max（**Pub/Sub 通知被丟棄**，含安靜連線） | ≤ 2 秒 | TBD(T4.6, T9.2) |
| 批次重驗連續 2 次失敗 → 該副本**全部**串流關閉的時間（fail-closed，Should 級） | 第 2 次失敗後 ≤ 1 秒（暫定，以 T4.6 為準） | TBD(T4.6, T9.2) |
| 每副本每秒批次重驗的 session row 數（2,000 distinct sessions / 3 副本） | – | TBD(T9.2) |
| 撤銷重驗期間同時下單的 p99（對照無 SSE 負載） | – | TBD(T9.2) |

---

## 6. 壓測後一致性檢查

**流程**：k6 結束 → drain 到定義好的 checkpoint（無 in-flight 請求；outbox 全部指派且投遞；reconciler 至少完成一輪；payment-mock 回呼佇列清空或已記錄；collector 追上 log 檔尾）→ 執行 `loadtest/consistency.sql` 與非 SQL 檢查 → 輸出貼於下方。drain 判定條件與等待時間：TBD(T9.3)。須在 idempotency key 清理（24h）之前執行。

> 以下 SQL 為檢查**語意**的參考寫法（依 `PLAN.md` §4 欄位），最終以 `loadtest/consistency.sql` 為準（TBD(T2.9)）。`:e` = 壓測活動 id。所有「預期 0 列」的查詢回傳任何列即為失敗。狀態值、`operation` 名稱、`resource_ref` 格式依實作（T2.3、T3.x）調整。

### C0. 前置檢查（防止「空集合通過」）— 預期全部為 true

```sql
-- :expected_seats 由 seed 產生的 fixture manifest 傳入（情境 A = 10000；B / CP 依各自 fixture）
SELECT (SELECT count(*) = 1 FROM events WHERE id = :e AND status = 'published')       AS event_ok,
       (SELECT count(*) FROM seats WHERE event_id = :e) = :expected_seats             AS seat_count_ok,
       (SELECT count(*) > 0 FROM orders WHERE event_id = :e) = :expect_orders          AS orders_ok;     -- fixture 決定：有寫入的執行為 true，空 fixture 為 false
-- 僅在 relay 已存在的執行（P4 之後：情境 A/B 完整版、CP2）才檢查；CP1 標 N/A
SELECT count(*) = 1 AS stream_state_ok FROM event_stream_state WHERE event_id = :e;
```
實際輸出：`TBD(T9.3)`

### C1. 無座位雙賣（每座位最多 1 個 active claim）— 預期 0 列

```sql
SELECT seat_id, count(*) FROM order_items
 WHERE event_id = :e AND released_at IS NULL
 GROUP BY seat_id HAVING count(*) > 1;
```
實際輸出：`TBD(T9.3)`

### C2. 座位狀態 ↔ 訂單項目一致（不變式 1）— 預期 0 列

```sql
-- available：0 個 active item 且 order_id IS NULL；held/sold：恰 1 個 active item，且 item.order_id、
-- item.event_id、item.zone_id 與座位一致（null-safe 比較，缺 order 連結也會被抓到）
SELECT s.id, s.status, s.order_id,
       count(oi.seat_id) AS active_items,
       bool_and(oi.order_id IS NOT DISTINCT FROM s.order_id
                AND oi.event_id = s.event_id AND oi.zone_id = s.zone_id) AS links_ok
  FROM seats s
  LEFT JOIN order_items oi ON oi.seat_id = s.id AND oi.released_at IS NULL
 WHERE s.event_id = :e
 GROUP BY s.id, s.status, s.order_id
HAVING (s.status = 'available' AND (count(oi.seat_id) <> 0 OR s.order_id IS NOT NULL))
    OR (s.status IN ('held','sold') AND (s.order_id IS NULL OR count(oi.seat_id) <> 1
                                         OR NOT coalesce(bool_and(oi.order_id IS NOT DISTINCT FROM s.order_id
                                                         AND oi.event_id = s.event_id AND oi.zone_id = s.zone_id), false)));
```
實際輸出：`TBD(T9.3)`

### C2b. 以訂單項目為起點的反向連結檢查 — 預期 0 列

```sql
-- 本活動訂單的每個 item：座位存在、座位屬同一活動、zone 屬同一活動、item.event_id = 訂單 event_id；
-- active item 指向的座位必須反指回同一訂單（抓出「本活動訂單佔了別的活動座位」等 C2/C3 從座位出發看不到的情況）
SELECT oi.order_id, oi.seat_id, oi.released_at, s.event_id AS seat_event, s.order_id AS seat_order, z.event_id AS zone_event
  FROM orders o
  JOIN order_items oi ON oi.order_id = o.id
  LEFT JOIN seats s ON s.id = oi.seat_id
  LEFT JOIN zones z ON z.id = oi.zone_id
 WHERE o.event_id = :e
   AND (s.id IS NULL OR s.event_id <> o.event_id OR oi.event_id <> o.event_id
        OR z.id IS NULL OR z.event_id <> o.event_id OR s.zone_id <> oi.zone_id
        OR (oi.released_at IS NULL AND s.order_id IS DISTINCT FROM o.id));
```
實際輸出：`TBD(T9.3)`

### C3. 座位狀態 ↔ 訂單狀態一致 — 預期 0 列

```sql
-- held ⇔ pending_payment；sold ⇔ paid / refund_pending；LEFT JOIN 抓出指向不存在或他活動訂單的座位
SELECT s.id, s.status, s.order_id, o.status AS order_status, o.event_id AS order_event
  FROM seats s LEFT JOIN orders o ON o.id = s.order_id
 WHERE s.event_id = :e AND s.status IN ('held','sold')
   AND (o.id IS NULL OR o.event_id <> s.event_id
        OR NOT ((s.status = 'held' AND o.status = 'pending_payment')
             OR (s.status = 'sold' AND o.status IN ('paid','refund_pending'))));
```
實際輸出：`TBD(T9.3)`

### C4. 訂單 active / 歷史 item 數（不變式 2）— 預期 0 列

```sql
SELECT o.id, o.status, o.quantity,
       count(oi.seat_id)                                         AS all_items,
       count(oi.seat_id) FILTER (WHERE oi.released_at IS NULL)   AS active_items
  FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id
 WHERE o.event_id = :e
 GROUP BY o.id, o.status, o.quantity
HAVING count(oi.seat_id) <> o.quantity                                                     -- 歷史 item 數 = quantity
    OR (o.status IN ('pending_payment','paid','refund_pending')
        AND count(oi.seat_id) FILTER (WHERE oi.released_at IS NULL) <> o.quantity)
    OR (o.status NOT IN ('pending_payment','paid','refund_pending')
        AND count(oi.seat_id) FILTER (WHERE oi.released_at IS NULL) <> 0);
```
實際輸出：`TBD(T9.3)`

### C5. 配額正確且無人超過 4 張（不變式 3）— 預期 0 列

```sql
SELECT q.user_id, q.reserved, coalesce(h.held_qty, 0) AS held_qty
  FROM user_event_quota q
  LEFT JOIN (SELECT user_id, sum(quantity) AS held_qty FROM orders
              WHERE event_id = :e AND status IN ('pending_payment','paid','refund_pending')
              GROUP BY user_id) h ON h.user_id = q.user_id
 WHERE q.event_id = :e
   AND (q.reserved <> coalesce(h.held_qty, 0) OR q.reserved > 4);
-- 反向：持有座位卻沒有 quota 列的使用者
SELECT user_id FROM orders WHERE event_id = :e AND status IN ('pending_payment','paid','refund_pending')
EXCEPT SELECT user_id FROM user_event_quota WHERE event_id = :e;
```
實際輸出：`TBD(T9.3)`

### C6. 金額 = 票價和 + 5% 服務費（四捨五入 half-up）— 預期 0 列

```sql
SELECT o.id, o.subtotal_twd, o.fee_twd, o.total_twd, sum(oi.unit_price_twd) AS items_sum, count(oi.seat_id) AS n
  FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id
 WHERE o.event_id = :e
 GROUP BY o.id
HAVING count(oi.seat_id) = 0                                   -- 沒有任何 item 的訂單
    OR o.subtotal_twd IS DISTINCT FROM sum(oi.unit_price_twd)
    OR o.fee_twd     IS DISTINCT FROM (o.subtotal_twd * 5 + 50) / 100
    OR o.total_twd   IS DISTINCT FROM o.subtotal_twd + o.fee_twd;
-- 單價快照 = 區域票價（發布後票價凍結）；LEFT JOIN 抓出 zone 遺失的 item
SELECT oi.order_id, oi.seat_id, oi.zone_id FROM order_items oi LEFT JOIN zones z ON z.id = oi.zone_id AND z.event_id = oi.event_id
 WHERE oi.event_id = :e AND (z.id IS NULL OR oi.unit_price_twd <> z.price_twd);
```
實際輸出：`TBD(T9.3)`

### C7. 無過期未釋放的 hold — 預期 0 列

```sql
SELECT id, expires_at FROM orders
 WHERE event_id = :e AND status = 'pending_payment'
   AND expires_at <= app_now() - interval '30 seconds';   -- 30s = 積壓寬限（與 HOLD_EXPIRY_BACKLOG 相同），使用 DB 時鐘 app_now()
```
實際輸出：`TBD(T9.3)`
註：主跑 TTL 600s 長於量測窗，上式只驗證「無積壓」，**不能**證明逾時路徑有效。因此另跑一輪**短 TTL 輪**（或在 test image 以 `test_clock` 推進時間）：k6 記錄刻意「保留不動」的訂單 id 清單（載入暫存表 `lt_held_orders(order_id)`），drain 後斷言：

```sql
-- C7b-0 預期 true：manifest 非空且筆數 = k6 回報的刻意保留筆數（避免空清單通過）
SELECT count(*) = :expected_holds AND count(*) > 0 AS holds_manifest_ok FROM lt_held_orders;
-- C7b 預期 0 列：每筆刻意保留的訂單都已 expired，座位已釋放，且其使用者的配額已扣回（C5 同時須通過）
SELECT h.order_id, o.status,
       (SELECT count(*) FROM seats s WHERE s.order_id = o.id)                            AS seats_still_linked,
       (SELECT count(*) FROM order_items oi WHERE oi.order_id = o.id AND oi.released_at IS NULL) AS active_items
  FROM lt_held_orders h LEFT JOIN orders o ON o.id = h.order_id
 WHERE o.id IS NULL OR o.status <> 'expired'
    OR EXISTS (SELECT 1 FROM seats s WHERE s.order_id = o.id)
    OR EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.id AND oi.released_at IS NULL);
```
實際輸出（短 TTL 輪）：`TBD(T9.3)`

### C8. 金流 capture / refund（不變式 4）— 預期 0 列

```sql
-- C8a accepted capture 只能屬於 paid / refund_pending / refunded 訂單；
--     refund_pending 必須有 refund 列；refunded 的 refund 必須已 succeeded；paid 不得有 refund 列
SELECT c.provider_capture_id, o.id, o.status, r.status AS refund_status FROM payment_captures c
  JOIN orders o ON o.id = c.order_id
  LEFT JOIN refunds r ON r.provider_capture_id = c.provider_capture_id
 WHERE o.event_id = :e AND c.disposition = 'accepted'
   AND (o.status NOT IN ('paid','refund_pending','refunded')
        OR (o.status = 'paid' AND r.id IS NOT NULL)
        OR (o.status = 'refund_pending' AND r.id IS NULL)
        OR (o.status = 'refunded' AND r.status IS DISTINCT FROM 'succeeded'));
-- C8b refund_required capture 必須有 refund 列
SELECT c.provider_capture_id FROM payment_captures c JOIN orders o ON o.id = c.order_id
  LEFT JOIN refunds r ON r.provider_capture_id = c.provider_capture_id
 WHERE o.event_id = :e AND c.disposition = 'refund_required' AND r.id IS NULL;
-- C8c paid / refund_pending / refunded 訂單恰有 1 筆 accepted capture
SELECT o.id, o.status, count(c.provider_capture_id) FROM orders o
  LEFT JOIN payment_captures c ON c.order_id = o.id AND c.disposition = 'accepted'
 WHERE o.event_id = :e AND o.status IN ('paid','refund_pending','refunded')
 GROUP BY o.id, o.status HAVING count(c.provider_capture_id) <> 1;
```
非 SQL（C8d）：payment-mock 端此活動相關 attempt 的所有 capture id 集合 = 本地 `payment_captures` 集合（`report.py` 比對）。
實際輸出：`TBD(T9.3)`

### C9. 所有付款工作為終態、處理中或已排程 — 預期 0 列

判定原則：每筆非終態工作必須「在 reconciler / 退款 worker 的期限內」——超過期限仍未推進即視為卡住。期限以 PLAN §5.4 的 reconciler 週期（15s）推導，`:grace` 暫定 30 秒；狀態值與欄位名稱以 T3.2 / T3.4 為準。

```sql
-- C9a attempt：creating 超過 reconciler 週期 + 寬限仍未推進 → 卡住；
--      awaiting_customer：訂單仍 pending_payment 時，超過寬限未被輪詢（last_polled_at，欄位 TBD）→ 卡住；
--      訂單已 cancelled / expired 時，必須有對應且未逾期的 provider_cancellations 待辦（C9c），否則 → 卡住
SELECT a.id, a.status, o.status FROM payment_attempts a JOIN orders o ON o.id = a.order_id
 WHERE o.event_id = :e
   AND ((a.status = 'creating' AND a.created_at < app_now() - :grace)
        OR (a.status = 'awaiting_customer'
            AND ((o.status = 'pending_payment' AND a.last_polled_at < app_now() - :grace)
                 OR (o.status NOT IN ('pending_payment') AND NOT EXISTS (SELECT 1 FROM provider_cancellations pc
                       WHERE pc.attempt_id = a.id AND pc.next_try_at >= app_now() - :grace)))));   -- provider_cancellations 欄位 TBD(T3.4)
-- C9b refund（限定本活動）：未 succeeded 者必須「處理中且 lease 未過期」或「已排程且未逾期」（狀態 / lease 欄位以 T3.4 為準）
SELECT r.id, r.status, r.next_try_at FROM refunds r
  JOIN payment_captures c ON c.provider_capture_id = r.provider_capture_id
  JOIN orders o ON o.id = c.order_id
 WHERE o.event_id = :e AND r.status <> 'succeeded'
   AND NOT (r.status = 'processing' AND r.lease_expires_at >= app_now())
   AND (r.next_try_at IS NULL OR r.next_try_at < app_now() - :grace);
-- C9c provider_cancellations（限定本活動）：同上規則。PLAN §4 尚未定義此表 schema → 查詢於 T3.4 補上；未補前此項標「未驗證」，不得標通過
```
實際輸出：`TBD(T9.3)`

### C10. Outbox 全部指派且投遞、seq 連續無洞（不變式 5）

```sql
-- 預期 0
SELECT count(*) AS unassigned FROM outbox WHERE event_id = :e AND seq IS NULL;
-- 預期 0 列：stream state 存在；分「空」與「非空」兩個分支判定
--   空（n = 0）：兩個 checkpoint 都必須是初始值（以 T4.1 為準，暫定 0）
--   非空：min = 1、max = n（連續無洞）、last_assigned_seq = last_delivered_seq = max
SELECT st.event_id, st.last_assigned_seq, st.last_delivered_seq, o.max_seq, o.n, o.min_seq
  FROM event_stream_state st
  CROSS JOIN LATERAL (SELECT max(seq) AS max_seq, min(seq) AS min_seq, count(*) AS n
                        FROM outbox WHERE event_id = :e AND seq IS NOT NULL) o
 WHERE st.event_id = :e
   AND CASE WHEN o.n = 0
            THEN NOT (coalesce(st.last_assigned_seq, 0) = 0 AND coalesce(st.last_delivered_seq, 0) = 0)
            ELSE (o.min_seq <> 1 OR o.max_seq <> o.n
                  OR st.last_assigned_seq IS DISTINCT FROM o.max_seq
                  OR st.last_delivered_seq IS DISTINCT FROM o.max_seq)
       END;
-- 若 outbox 有保留期清理，連續性改比對保留區間（TBD(T4.1)）
```
非 SQL：目前 generation（`event_stream_state.gen`）的 Redis stream tail（`XREVRANGE seat:{gen}:{event} + - COUNT 1` 的 ID `<seq>-0`）= `last_delivered_seq`。非空時沒有 stream 或 tail → 失敗；空分支只在 fixture 證明期間沒有任何座位變化（C0 之外再核對 `orders` 為 0 筆）且 Redis 無 tail 時允許。
實際輸出：`TBD(T9.3)`

### C11. seat_events ↔ outbox 一致（多重集合比對）— 預期兩個方向皆 0 列

```sql
WITH expanded AS (
  SELECT o.event_id, o.seq, (c->>0)::int AS seat_idx, c->>1 AS status, (c->>2)::int AS version
    FROM outbox o CROSS JOIN LATERAL jsonb_array_elements(o.changes) c
   WHERE o.event_id = :e AND o.seq IS NOT NULL AND o.kind <> 'layout_changed'   -- layout 事件不展開為座位事件（kind 名稱以 T4.1 為準）
), archived AS (
  SELECT event_id, seq, seat_idx, status, seat_version AS version FROM seat_events WHERE event_id = :e
)
(SELECT 'missing_in_seat_events' AS side, * FROM (SELECT * FROM expanded EXCEPT ALL SELECT * FROM archived) x)
UNION ALL
(SELECT 'extra_in_seat_events', * FROM (SELECT * FROM archived EXCEPT ALL SELECT * FROM expanded) y);
```
實際輸出：`TBD(T9.3)`

### C12. 重送沒有產生重複訂單 — 預期 0 列

以 k6 產生的 **run manifest** 為起點（不掃描全域 key）：k6 對每個收到 201 的下單記錄 `(user_id, key, event_id, order_public_id, resend_attempted, resend_status, resend_order_public_id)`——`resend_attempted` 區分「沒有重送」與「重送了但失敗」；`run.sh` 將其載入暫存表 `lt_manifest`。另記錄每筆**未收到回應**（逾時 / 連線錯誤）的下單 `(user_id, key)` 於 `lt_unanswered`。`:t_drain_end` = drain 完成時間。

```sql
-- C12a 預期 0 列：manifest 每一列 ↔ 恰 1 筆訂單（同使用者、同活動）↔ 恰 1 個 completed/201 的 key，
--       key 的 resource_ref 指向同一訂單；有重送者兩次 order id 必須相同
SELECT m.user_id, m.key, m.order_public_id, m.resend_attempted, m.resend_status, m.resend_order_public_id,
       count(DISTINCT o.id) AS orders, count(DISTINCT k.key) AS keys
  FROM lt_manifest m
  LEFT JOIN orders o ON o.public_id = m.order_public_id AND o.user_id = m.user_id AND o.event_id = m.event_id
  LEFT JOIN idempotency_keys k ON k.user_id = m.user_id AND k.key = m.key
        AND k.operation = 'create_order' AND k.status = 'completed' AND k.response_status = 201
        AND k.resource_ref = m.order_public_id::text
        AND k.created_at >= :t_start AND k.created_at < :t_drain_end
 GROUP BY m.user_id, m.key, m.order_public_id, m.resend_attempted, m.resend_status, m.resend_order_public_id
HAVING count(DISTINCT o.id) <> 1 OR count(DISTINCT k.key) <> 1
    OR (m.resend_attempted AND (m.resend_status IS DISTINCT FROM 201
                                OR m.resend_order_public_id IS DISTINCT FROM m.order_public_id));
-- C12b 預期 0 列：本活動在 [t_start, t_drain_end) 內建立、且不屬於壓測前 fixture 的訂單，都必須出現在 manifest，
--       或屬於 lt_unanswered 中某個 key（以該 key 的 completed 列對應）——否則就是多出來的重複訂單
SELECT o.id, o.public_id FROM orders o
 WHERE o.event_id = :e AND o.created_at >= :t_start AND o.created_at < :t_drain_end
   AND NOT EXISTS (SELECT 1 FROM lt_manifest m WHERE m.order_public_id = o.public_id)
   AND NOT EXISTS (SELECT 1 FROM lt_unanswered u JOIN idempotency_keys k ON k.user_id = u.user_id AND k.key = u.key
                    AND k.operation = 'create_order' AND k.resource_ref = o.public_id::text);
```
註：未收到回應但伺服器已提交的訂單由 `lt_unanswered` 解釋；同一個 unanswered key 對應超過 1 筆訂單仍屬失敗（由 idempotency_keys PK 保證不可能，列出即代表 bug）。
實際輸出：`TBD(T9.3)`

### C13. SSE 探針 hash = PG hash（情境 B）— 預期相等

在 drain 後的同一個 `(gen, layout_ver, last_assigned_seq)` 下比較：PG 端以 `seat_idx` 排序的 `(seat_idx, status, version)` 序列，以固定的 canonical 編碼（TBD(T4.7)，例如每列 `idx,status,version\n` 的 UTF-8）做 SHA-256；探針 client 對本地狀態做相同計算，並回報自己的 `(gen, layout_ver, applied_seq)`。三元組不一致時先等待追上，不可直接比較。
實際輸出：`TBD(T9.3)`

### C14. 邊界 vs k6 請求數 — 預期差異 < 1%

依 §2.4 規則：只比 k6 母體，排除 healthcheck、回呼、串流；collector drain 後比對；任一側為 0 即失敗。情境 A 與 B（B 比對 writer 與快照請求）都要跑。

- 母體識別：k6 每個請求帶 `X-Load-Run: <run_id>`（暫定），nginx 把該值**原樣**寫入 access log 欄位（名稱 TBD(T7.2)），以**等值**比對，不用前綴 `LIKE`（避免混到其他 run）。
- 時間窗：雙方都以「請求完成時間 ∈ [t_start, t_end)」計數（nginx `$msec`；k6 以樣本結束時間）。
- 路由排除兩側一致：`/api/v1/events/{id}/seat-stream`、`/healthz/*`、`/api/v1/payments/mockpay/callbacks`。
- 應用層欄：以 `request_id` 將 edge 母體對到應用層紀錄（`service_logs` / app 端每請求紀錄，TBD(T7.1)），只計對得上的請求；對不上的數量另列。

```sql
SELECT count(*) FROM access_logs
 WHERE ts >= :t_start AND ts < :t_end
   AND load_run = :run_id                                 -- 欄位名稱以 T7.2 為準
   AND route NOT LIKE '%/seat-stream' AND route NOT LIKE '/healthz/%'
   AND route <> '/api/v1/payments/mockpay/callbacks';
```
實際輸出：`TBD(T9.3)`

### 一致性結果彙總

| 檢查 | 情境 A | 情境 B |
|---|---|---|
| C0 前置檢查（stream state 僅 P4 之後） | TBD(T9.3) | TBD(T9.3) |
| C1 無雙賣 | TBD(T9.3) | TBD(T9.3) |
| C2 座位 ↔ 項目 | TBD(T9.3) | TBD(T9.3) |
| C2b 項目 → 座位 / 區域反向連結 | TBD(T9.3) | TBD(T9.3) |
| C3 座位 ↔ 訂單狀態 | TBD(T9.3) | TBD(T9.3) |
| C4 訂單項目數 | TBD(T9.3) | TBD(T9.3) |
| C5 配額 / ≤ 4 張 | TBD(T9.3) | TBD(T9.3) |
| C6 金額 | TBD(T9.3) | TBD(T9.3) |
| C7 無過期 hold 積壓 | TBD(T9.3) | TBD(T9.3) |
| C7b 短 TTL 輪：刻意保留的訂單全部 expired 並釋放 | TBD(T9.3) | – |
| C8 capture / refund | TBD(T9.3) | TBD(T9.3) |
| C9 付款工作狀態 | TBD(T9.3) | TBD(T9.3) |
| C10 outbox / seq / Redis tail | TBD(T9.3) | TBD(T9.3) |
| C11 seat_events 多重集合 | TBD(T9.3) | TBD(T9.3) |
| C12 冪等重送 | TBD(T9.3) | TBD(T9.3) |
| C13 SSE hash | – | TBD(T9.3) |
| C14 edge vs k6（B：writer + 快照請求） | TBD(T9.3) | TBD(T9.3) |

---

## 7. 瓶頸分析

每一個瓶頸以下列模板記錄；**每項必須有前後對照的實測數據**，沒有證據的推測寫在「待驗證假設」而非結論。

### 7.x 模板：〈瓶頸名稱〉

| 欄位 | 內容 |
|---|---|
| 觀察到的現象 | TBD（例：哪個階段、哪個端點的哪個指標異常） |
| 假設 | TBD |
| 證據 | TBD（`pg_stat_activity` wait events、pool wait、loop lag、`EXPLAIN (ANALYZE, BUFFERS)`、CPU profile、dashboard 截圖等，附原始輸出或連結） |
| 變更 | TBD（設定 / 程式碼，附 commit） |
| 變更前 | TBD（同一情境、同一指標） |
| 變更後 | TBD |
| 結論 / 副作用 | TBD |

### 7.1 已識別瓶頸

TBD(T9.1)（CP1–CP4 發現的瓶頸先記錄於 §9，P9 整理至此）

### 7.2 待驗證假設（來自 PLAN §18，非結論）

- 每請求 PG session 查詢在 3,000 VU 下造成 pool 壓力。
- `seats` 頻繁更新造成 bloat（partial index 阻止 HOT update）。
- 熱門座位 `NOWAIT` 衝突導致大量可重試 409、客戶端重試放大負載。
- k6 與受測系統同機互相干擾。

---

## 8. 限制與注意事項

- k6 與 SUT 同機（cpuset 隔離，但共用記憶體頻寬、磁碟與 kernel），數字代表單機上限而非叢集容量。
- 測試流量經 docker bridge / loopback，不含真實網路延遲與 TLS 成本（TBD(T9.1)：是否啟用 TLS 需註明）。
- commit → apply 延遲依賴 `track_commit_timestamp=on`（壓測專用設定，有少量額外寫入成本）與同機時鐘；不同機部署時此量法不適用（§3.2）。
- 壓測使用 `.env.loadtest` 放寬登入限流以便預先建立 session（§2.2），此設定不代表正式環境預設值。
- 其他限制：TBD(T9.1)

---

## 9. 規模檢查點紀錄（開發期間）

> 依 PLAN §16：檢查點沒過就停下來調整設計，不往後疊功能。每個檢查點都必須填：日期 / commit、相對於 §1 的環境差異、**實際執行的指令**、原始資料存放路徑（`docs/bench/cpN/`，暫定）、明確的通過條件與結果、發現的瓶頸與後續處理。CP 結果不可直接當作完整 A/B 結果引用。

### 9.1 CP1：短版情境 A，僅訂票（3,000 VU × 30s，不含付款）— P2 之後

> 金流在 P3 才實作，CP1 的 iteration 只有「取座位（T2.x 提供的最小座位列表，P4 快照 API 尚未存在）→ 下單（70% 指定 / 30% 區域）→（10%）取消待付款訂單」，**不含付款與付款輪詢**；結果與完整情境 A 不可直接比較。

| 項目 | 值 |
|---|---|
| 日期 / commit / 環境差異 | TBD(CP1) |
| 指令 | `loadtest/run.sh cp1`（暫定，TBD(T2.9)） |
| 原始資料 | `docs/bench/cp1/`（暫定） |
| **通過條件 / 停止條件** | 通過：0 超賣（C1）、0 非預期 5xx、C0–C7 與 C12 全通過。**停止條件**（任一成立即停下調整設計，不往後疊功能）：任何超賣或一致性失敗；任何非預期 5xx；下單 p99 > 800ms（§13.2 目標，作為「p99 失控」的操作化定義，暫定——PLAN 未給硬門檻）。 |
| 結果：0 超賣 / 0 非預期 5xx | TBD(CP1) |
| 下單 p50 / p95 / p99 | TBD(CP1) |
| RPS / 503 比例 | TBD(CP1) |
| 一致性 SQL（C0–C7、C12；C10–C11 需 P4 的 relay，標 N/A） | TBD(CP1) |
| Sweeper 吞吐量（逐筆交易 `release_order`，每輪候選 200 筆）：釋放筆數/秒、單筆交易耗時 p50/p99，於搶票負載下與無負載下各量一次 | TBD(CP1) |
| W4 收斂上界（PLAN §5.7）：先停 worker、以 `test_clock` 或短 TTL 製造 N 筆已過期 hold，再啟動 worker，量「worker 起來 → 積壓清空」時間；上界 ≈ 積壓筆數 ÷ sweeper 吞吐量 | TBD(CP1)（N 與量測值；結果回填 DESIGN Q2） |
| 瓶頸與調整（pool / semaphore / timeouts） | TBD(CP1) |
| 結論：通過 / 需調整設計 | TBD(CP1) |

### 9.2 CP2：2,000 SSE + writer 300 changes/s（kill 一個 API、Redis 重啟）— P4 之後

> 若 P3 尚未完成，writer 只用「下單 + 取消待付款訂單」產生變化。

| 項目 | 值 |
|---|---|
| 日期 / commit / 環境差異 | TBD(CP2) |
| 指令 | `loadtest/run.sh cp2`（暫定，TBD(T4.7)） |
| 原始資料 | `docs/bench/cp2/`（暫定） |
| **通過條件** | 2,000 distinct sessions 100% 連線；穩態窗口（長度 TBD(T4.7)）內**實際提交**速率 ≥ 目標 300 changes/s 的 95%（容差暫定）；kill API 與 Redis 重啟後全部恢復；最終 hash 一致（C13）；C10–C11 通過；edge 對照在 P7（collector、`access_logs`）完成前改以 nginx 原始 JSON access log 檔依 `X-Load-Run` 計數，P7 後才用 C14 SQL |
| 目標 vs **實際**寫入速率（changes/s） | 300 / TBD(CP2) |
| 連線成功率 | TBD(CP2) |
| commit → apply（上界與 `assigned_at` 分段）p50 / p95 / p99 | TBD(CP2) |
| resync 次數、RSS、loop lag | TBD(CP2) |
| kill API / Redis 重啟後恢復時間 | TBD(CP2) |
| 最終 hash 一致 | TBD(CP2) |
| 結論 | TBD(CP2) |

### 9.3 CP3：10 萬列匯入 < 60s（背景短版 k6）— P5

| 項目 | 值 |
|---|---|
| 日期 / commit / 環境差異 | TBD(CP3) |
| 指令 / 環境差異 | `docker compose -f compose.test.yaml run --rm tests pytest -m perf -k test_import_100k_under_60s`（暫定，TBD(T5.8)）；同時以 `loadtest/run.sh a --short --no-reset` 產生背景下單負載（`--no-reset`：不重建資料庫、不重啟 SUT，保留資源測試的 fixture；暫定）；1 個 import worker |
| 原始資料 | `docs/bench/cp3/`（暫定） |
| 條件 | 佇列為空、1 個 import worker、背景短版 k6 |
| **通過條件** | 上傳開始 → 終態 < 60s（需求）；目標 < 15s 僅供參考 |
| 上傳 → 終態總時間 | TBD(CP3) |
| 分段（上傳 / 解析+COPY / 驗證 / finalize） | TBD(CP3) |
| 背景 k6 下單 p99（對照無匯入） | TBD(CP3) |
| 結論 | TBD(CP3) |

### 9.4 CP4：50 萬筆 Excel 匯出 @ `mem_limit: 512m` — P6

> 匯出在 **api 容器內**（專用單執行緒 executor + 專用 DB 連線）執行，`memory.peak` 讀的是**執行匯出的那個 api 副本**的 cgroup。

| 項目 | 值 |
|---|---|
| 日期 / commit / 環境差異 | TBD(CP4) |
| 指令 / 環境差異 | `docker compose -f compose.test.yaml run --rm tests pytest -m perf -k export_500k`（暫定，TBD(T6.2)），內含 `scripts/seed_orders_500k.sql` 與 cgroup `memory.peak` 讀取；api `mem_limit: 512m` 不變；同時以 `loadtest/run.sh a --short --no-reset` 產生背景下單負載，並以 `loadtest/run.sh b --short --no-reset`（writer + SSE 探針，需 P4）量同一副本的 SSE 延遲；匯出請求以 header 或直連方式固定到被量測的副本（方法 TBD(T6.2)） |
| 原始資料 | `docs/bench/cp4/`（暫定） |
| **通過條件** | 執行匯出的 api 副本 cgroup v2 `memory.peak` < 450MiB；openpyxl 驗證筆數與總額正確 |
| `memory.peak` | TBD(CP4) |
| 匯出耗時、檔案大小 | TBD(CP4) |
| **同一副本**的下單 p99：無匯出基線 / 匯出期間 | TBD(CP4) / TBD(CP4) |
| **同一副本**的 SSE commit → apply p99：無匯出基線 / 匯出期間 | 需 P4；P4 未完成前標「待 P4 後補測」 |
| openpyxl 驗證：筆數、總額 | TBD(CP4) |
| 結論 | TBD(CP4) |

---

## 10. 如何重現

> **正式入口為 `loadtest/run.sh`**（PLAN §15.1）；`make loadtest-a` / `make loadtest-b` 只是它的薄包裝（暫定，PLAN 未定義，於 T2.9 / T4.7 建立）。<!-- TODO(T2.9, T4.7) -->

```bash
# 0. 啟動受測系統（cpuset 設定見 §1.2；壓測用的限流差異放在 .env.loadtest）
docker compose --env-file .env.loadtest up -d --build   # 套用壓測差異設定（登入限流放寬、track_commit_timestamp 等，§1.3）

# 1. 情境 A：seed → session 準備與驗證 → k6 → drain → 全部一致性檢查 → report（任一檢查失敗 run.sh 以非 0 結束）
loadtest/run.sh a                    # 或 make loadtest-a
# 2. 情境 B：seed → 2,000 sessions → 訂閱者 k6 + writer k6（writer.js）並行 → 故障注入 → drain → 全部適用的一致性檢查（C0–C14；B 以 C10–C14 為主）→ report
loadtest/run.sh b                    # 或 make loadtest-b；速率與注入以參數指定（TBD(T4.7)）

# run.sh 內部步驟（示意；參數名稱 TBD(T2.9, T4.7)）
#   - 資料準備：輸出 EVENT_ID、RUN_ID 與 session 檔
#   - k6：docker compose --profile loadtest run --rm -e EVENT_ID -e RUN_ID k6 run /loadtest/k6/scenario_a.js
#   - B 的 writer：與訂閱者同時另起一個 k6 程序執行 /loadtest/writer.js
#   - 記錄 T_START / T_END，供 C12 / C14 與 report 使用
#   - 一致性 SQL（以 stdin 傳入，不依賴 postgres 容器掛載）：
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v e="$EVENT_ID" -v expected_seats="$EXPECTED_SEATS" -v expect_orders="$EXPECT_ORDERS" -v expected_holds="$EXPECTED_HOLDS" \
  -v t_drain_end="'$T_DRAIN_END'" -v run_id="'$RUN_ID'" \
  -v t_start="'$T_START'" -v t_end="'$T_END'" -v grace="'30 seconds'::interval" \
  < loadtest/consistency.sql      # run.sh 先以 docker compose cp 把 lt_manifest / lt_held_orders 的 CSV 複製進 postgres 容器，consistency.sql 再 \copy 載入（TBD(T2.9)）
#   - 報表：python loadtest/report.py --run-id "$RUN_ID"
```

選用（Could 級）執行：arrival-rate sweep 與 1,000 changes/s 以 `run.sh` 參數啟用；未執行時在對應結果表填「未執行（Could 級刪除）」。

原始資料（k6 逐請求樣本、`metrics_1s` / `business_1s` / `access_logs` 匯出、docker stats、一致性輸出）存放位置：`docs/bench/<run_id>/`（暫定，TBD(T9.1)）。
