### 5.1 連線與延遲（依寫入速率）

| 指標 | 目標（假設） | 50 changes/s | 300 changes/s | 1,000 changes/s（Could） |
|---|---|---|---|---|
| 實際提交的座位變化數 / 秒（PG） | ≈ 目標 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 連線成功率；建立失敗 / 重連失敗數 | 100% / 0 | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 初次快照取得時間 p50 / p95 / p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| 串流建立時間（首個 frame）p50 / p95 / p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| commit → apply 上界（送出 → 套用）p50 / p95 / p99 | p99 < 500ms @300 changes/s | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
| `assigned_at` → apply（commit 後管線）p50 / p95 / p99 | – | TBD(T9.2) | TBD(T9.2) | TBD(T9.2) |
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
| 批次重驗連續 2 次失敗 → 串流關閉時間（fail-closed，Should 級） | 依 T4.6 | TBD(T4.6, T9.2) |
| 每副本每秒批次重驗的 session row 數（2,000 distinct sessions / 3 副本） | – | TBD(T9.2) |
| 撤銷重驗期間同時下單的 p99（對照無 SSE 負載） | – | TBD(T9.2) |
