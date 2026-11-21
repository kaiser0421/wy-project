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

## 限制

- 這是閒置時的成本。高負載時主要成本是等待連線池（`DB_POOL_ACQUIRE_TIMEOUT_S`，逾時回 `503 server-busy`），需以壓測時的應用直方圖觀察，見 [LOADTEST.md](../../LOADTEST.md)。
- 量測腳本為依序請求，不代表併發下的延遲分布。
