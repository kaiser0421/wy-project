## 9. 規模檢查點紀錄（開發期間）

> 依 PLAN §16：檢查點沒過就停下來調整設計，不往後疊功能。每個檢查點都必須填：日期 / commit、相對於 §1 的環境差異、**實際執行的指令**、原始資料存放路徑（`docs/bench/cpN/`，暫定）、明確的通過條件與結果、發現的瓶頸與後續處理。CP 結果不可直接當作完整 A/B 結果引用。

### 9.1 CP1：短版情境 A，僅訂票（3,000 VU × 30s，不含付款）— P2 之後

> 金流在 P3 才實作，CP1 的 iteration 只有「取座位（T2.x 提供的最小座位列表，P4 快照 API 尚未存在）→ 下單（70% 指定 / 30% 區域）→（10%）取消待付款訂單」，**不含付款與付款輪詢**；結果與完整情境 A 不可直接比較。

| 項目 | 值 |
|---|---|
| 日期 / commit / 環境差異 | TBD(CP1) |
| 指令 | `loadtest/run.sh cp1`（暫定，TBD(T2.9)） |
| 原始資料 | `docs/bench/cp1/`（暫定） |
| **通過條件** | 0 超賣（C1）、0 非預期 5xx、C0–C7 與 C12 全通過；下單 p99 以 §13.2 目標 800ms 為參考線——超過時**不算自動失敗**，但必須在本表記錄原因與設計調整決定後才能往下（PLAN 未定義「p99 失控」的硬門檻） |
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
| **通過條件** | 2,000 distinct sessions 100% 連線；kill API 與 Redis 重啟後全部恢復；最終 hash 一致（C13）；C10–C11 通過 |
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
| 指令 | `test_import_100k_under_60s`（`make verify-all` 的資源驗收之一） |
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
| 指令 | `scripts/seed_orders_500k.sql` + 記憶體驗收測試（`make verify-all` 的資源驗收之一） |
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
docker compose up -d --build

# 1. 情境 A：seed → session 準備與驗證 → k6 → drain → 一致性 SQL → report
loadtest/run.sh a                    # 或 make loadtest-a
# 2. 情境 B：seed → 2,000 sessions → 訂閱者 k6 + writer k6（writer.js）並行 → 故障注入 → drain → C10–C13 → report
loadtest/run.sh b                    # 或 make loadtest-b；速率與注入以參數指定（TBD(T4.7)）

# run.sh 內部步驟（示意；參數名稱 TBD(T2.9, T4.7)）
#   - 資料準備：輸出 EVENT_ID、RUN_ID 與 session 檔
#   - k6：docker compose --profile loadtest run --rm -e EVENT_ID -e RUN_ID k6 run /loadtest/k6/scenario_a.js
#   - B 的 writer：與訂閱者同時另起一個 k6 程序執行 /loadtest/writer.js
#   - 記錄 T_START / T_END，供 C12 / C14 與 report 使用
#   - 一致性 SQL（以 stdin 傳入，不依賴 postgres 容器掛載）：
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v e="$EVENT_ID" -v expected_seats=10000 -v run_id="'$RUN_ID'" \
  -v t_start="'$T_START'" -v t_end="'$T_END'" < loadtest/consistency.sql
#   - 報表：python loadtest/report.py --run-id "$RUN_ID"
```

選用（Could 級）執行：arrival-rate sweep 與 1,000 changes/s 以 `run.sh` 參數啟用；未執行時在對應結果表填「未執行（Could 級刪除）」。

原始資料（k6 逐請求樣本、`metrics_1s` / `business_1s` / `access_logs` 匯出、docker stats、一致性輸出）存放位置：`docs/bench/<run_id>/`（暫定，TBD(T9.1)）。
