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

- 方法：在 API 容器內用同一條資料庫連線，各呼叫 6,000 次 `authenticate()`（實際的驗證 SQL，帶有效 session）與 `SELECT 1`（純往返基準），兩者逐次交替先後順序；任何一次驗證失敗就中止、不輸出結果。腳本：`scripts/measure_auth_cost.py`。
- 環境：開發筆電，`docker compose` 完整堆疊，同時有其他背景工作（load average 約 4）。

| 呼叫 | n | p50 | p95 | p99 | 平均 |
|---|---:|---:|---:|---:|---:|
| `SELECT 1`（往返基準） | 6,000 | 0.703 ms | 1.911 ms | 6.788 ms | 0.936 ms |
| `authenticate()`（有效 session） | 6,000 | 0.891 ms | 2.455 ms | 7.965 ms | 1.187 ms |

- **驗證本身比一次空查詢多約 0.19 ms（p50）**；同日以舊版腳本（未交替順序）另跑三次為 0.13–0.19 ms。p95、p99 由往返與排程雜訊主導。
- `EXPLAIN (ANALYZE, BUFFERS)`（有效 token，回傳 1 列，Execution Time 0.265 ms）：這次 `sessions` 只有 41 列，planner 直接循序掃描；上方 2026-11-21 量測時 3,831 列則走 `sessions_token_hash_key` 唯一索引。使用者都以 `users_pkey` 取得，所有 buffer 皆命中記憶體。
- 此處重用同一條資料庫連線，量測範圍為查詢與往返時間。

原始輸出（token hash 已遮蔽）：

```text
valid session: user_id=2 role=user
SELECT 1 (round trip)  n=6000 p50=0.703 p95=1.911 p99=6.788 mean=0.936 ms
authenticate()         n=6000 p50=0.891 p95=2.455 p99=7.965 mean=1.187 ms
isolated cost, p50 difference = 0.188 ms
sessions rows = 41 users rows = 9
Nested Loop (actual time=0.098..0.170 rows=1 loops=1)
  Buffers: shared hit=3
  ->  Seq Scan on sessions s (actual time=0.026..0.045 rows=1 loops=1)
        Filter: ((revoked_at IS NULL) AND (token_hash = '<token hash>'::bytea) AND (abs_expires_at > now()) AND (idle_expires_at > now()))
        Rows Removed by Filter: 40
        Buffers: shared hit=1
  ->  Index Scan using users_pkey on users u (actual time=0.037..0.046 rows=1 loops=1)
        Index Cond: (id = s.user_id)
        Filter: is_active
        Buffers: shared hit=2
Planning Time: 0.310 ms
Execution Time: 0.265 ms
```

## 限制

- 2026-11-21 的 HTTP 端點比較是在閒置堆疊上執行，端點差值混合了 pool acquire 與路由成本。2026-11-22 的隔離量測在有背景工作的堆疊上重用單一資料庫連線。兩次量測都未單獨量到高負載下的 pool acquire 等待。
- 量測腳本為依序請求，不代表併發下的延遲分布。
