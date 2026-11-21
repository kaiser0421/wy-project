# 每請求 session 驗證成本

每個需要登入的請求都在 PostgreSQL 查一次 session（不做快取，撤銷立即生效）。本紀錄量測這次查詢的成本。

## 方法

- 環境：開發筆電（2 核心／4 執行緒），`docker compose up` 的完整堆疊，無其他負載，2026-11-21。
- 以同一個 HTTP 連線、經 nginx 依序各送 1,000 次請求（先暖身 50 次）：
  - `GET /api/v1/auth/me`：經過 session 驗證，回傳目前使用者。
  - `GET /healthz/live`：公開路由，不查 session。
- 兩者走相同的 nginx、middleware 與 Uvicorn，差值近似 session 驗證與路由本身的成本。
- 另以 `EXPLAIN ANALYZE` 檢查驗證 SQL（`backend/src/ticketing/auth/sessions.py:37`）的執行計畫，`sessions` 表當時有 3,831 列。

## 結果

| 請求 | n | p50 | p95 | p99 | 平均 | 錯誤 |
|---|---:|---:|---:|---:|---:|---:|
| `GET /api/v1/auth/me`（查 session） | 1,000 | 10.89 ms | 25.38 ms | 35.78 ms | 12.86 ms | 0 |
| `GET /healthz/live`（不查 session） | 1,000 | 7.03 ms | 18.85 ms | 34.07 ms | 8.86 ms | 0 |

- 中位數差約 **3.9 ms**，包含取得連線池連線、一次索引查詢與回應序列化。
- 查詢走 `sessions_token_hash_key` 唯一索引，再以 `users_pkey` 取使用者；索引查詢實際耗時約 0.17 ms。
- 未登入的 `/api/v1/auth/me` 回傳 401。

## 隔離量測（2026-11-22）

上面的端點差值包含路由與序列化的差異，無法單獨代表 session 驗證。此節直接量測驗證函式本身。

- 方法：在 API 容器內用同一條資料庫連線，交錯 3 輪、每輪各呼叫 2,000 次 `authenticate()`（實際的驗證 SQL，帶有效 session）與 `SELECT 1`（純往返基準），共各 6,000 次。腳本：`scripts/measure_auth_cost.py`。
- 環境：開發筆電，`docker compose` 完整堆疊，同時有其他背景工作（load average 約 4）。

| 呼叫 | n | p50 | p95 | p99 | 平均 |
|---|---:|---:|---:|---:|---:|
| `SELECT 1`（往返基準） | 6,000 | 0.683 ms | 3.632 ms | 9.368 ms | 1.128 ms |
| `authenticate()`（有效 session） | 6,000 | 0.849 ms | 2.895 ms | 8.605 ms | 1.221 ms |

- **驗證本身比一次空查詢多約 0.17 ms（p50；連跑三次為 0.13–0.19 ms）**；p95、p99 由往返與排程雜訊主導，兩者相近。
- `EXPLAIN (ANALYZE, BUFFERS)`（有效 token，回傳 1 列）：Nested Loop，`sessions_token_hash_key` 索引掃描 0.075 ms 後，以 `users_pkey` 取使用者 0.046 ms，共 4 個 shared buffer hit，皆在記憶體內。
- 不包含取得連線池連線的等待；高負載時那才是主要成本（見下方限制）。
- 以同一腳本再跑一次：`authenticate()` p50 0.832 ms、`SELECT 1` p50 0.702 ms，差 0.13 ms；`EXPLAIN ANALYZE` Execution Time 0.427 ms。第一次輸出的 Execution Time 11.555 ms 與其節點時間（0.231 ms）不符，屬單次樣本的排程雜訊，以 6,000 次的分布為準。

第一次執行的原始輸出：

```text
valid session: user_id=2 role=user
SELECT 1 (round trip)  n=6000 p50=0.683 p95=3.632 p99=9.368 mean=1.128 ms
authenticate()         n=6000 p50=0.849 p95=2.895 p99=8.605 mean=1.221 ms
isolated cost, p50 difference = 0.166 ms
sessions rows = 38 users rows = 9
Nested Loop (actual time=0.131..0.231 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using sessions_token_hash_key on sessions s (actual time=0.050..0.075 rows=1 loops=1)
        Index Cond: (token_hash = '\x352adb754e8cec7b131a2755f3ec9ba534cfbe309372dc46729023ba1a4e2934'::bytea)
        Filter: ((revoked_at IS NULL) AND (abs_expires_at > now()) AND (idle_expires_at > now()))
        Buffers: shared hit=2
  ->  Index Scan using users_pkey on users u (actual time=0.035..0.046 rows=1 loops=1)
        Index Cond: (id = s.user_id)
        Filter: is_active
        Buffers: shared hit=2
Planning Time: 0.351 ms
Execution Time: 11.555 ms
```

## 限制

- 這是閒置時的成本。高負載時主要成本是等待連線池（`DB_POOL_ACQUIRE_TIMEOUT_S`，逾時回 `503 server-busy`），需以壓測時的應用直方圖觀察，見 [LOADTEST.md](../../LOADTEST.md)。
- 量測腳本為依序請求，不代表併發下的延遲分布。
