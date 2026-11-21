# CP4：50 萬筆訂單 Excel 匯出（512 MB API 實例）

> 需求：`requirement.txt` 六.5「50 萬筆訂單需能在記憶體上限為 512MB 的 API 實例上完成匯出」。
> 量測日期：2026-11-21（台北），執行環境見 `LOADTEST.md` §1（MacBook Intel 4 執行緒 / 8 GB，Docker Desktop VM 4 CPU / 5 GB）。

## 方法

1. 以 `docker compose up -d --build` 啟動完整堆疊；`api` 服務的 compose 設定 `deploy.resources.limits.memory: 512m`，`docker inspect` 確認三個副本 `HostConfig.Memory = 536870912`，容器內 `/sys/fs/cgroup/memory.max = 536870912`。
2. `scripts/seed_orders_500k.sql` 在 PostgreSQL 灌入一個活動與 **500,000 筆訂單**（耗時 497 秒）。
3. 在三個 API 容器內每 0.2 秒取樣 cgroup v2 的 `memory.current` 與 `memory.stat`（`anon` = 程式實際使用的記憶體，`file` = page cache，可由核心回收）。此 kernel 沒有 `memory.peak`，因此以取樣峰值代替。
4. 以 Admin 經 nginx 呼叫 `GET /api/v1/events/3/reports/sales.xlsx?tz=Asia/Taipei`。
5. 以 openpyxl（read-only）解析下載的檔案驗證內容。

## 結果

| 項目 | 數值 |
|---|---|
| HTTP 狀態 | 200 |
| 匯出時間（請求到下載完成） | 217.3 s（第一次）、236.5 s（第二次，含取樣開銷） |
| 檔案大小 | 33,419,621 bytes |
| 負責匯出的副本（api-2）`anon` 峰值 | **113,000,448 bytes ≈ 108 MiB** |
| 同副本 `file`（page cache）峰值 | 416,047,104 bytes（可回收，主要是寫入磁碟 volume 的暫存檔快取） |
| 同副本 `memory.current` 峰值 | 536,842,240 bytes（= 上限附近，被核心以回收 page cache 維持） |
| 閒置副本（api-1 / api-3）`anon` | ≈ 95–97 MiB |
| OOMKilled / RestartCount | false / 0（三個副本） |

匯出期間程式實際記憶體只比閒置多約 13 MiB：xlsxwriter `constant_memory` 模式逐列寫入、資料以 server-side cursor 每批 5,000 列讀取，記憶體用量與訂單數無關。`memory.current` 貼近上限是 page cache，不代表程式記憶體不足；核心在壓力下回收它，容器未被 OOM kill。

## 檔案內容驗證（openpyxl）

- 工作表：`Summary`、`Zones`、`Orders`
- `Orders` 資料列：**500,000**（不含表頭）
- 表頭註明時區：`Created At (Asia/Taipei)`、`Paid At (Asia/Taipei)`
- 金額為數值儲存格：`Subtotal (TWD)=1000`、`Fee (TWD)=50`、`Total (TWD)=1050`（服務費 5% 四捨五入）
- `Zones`：Capacity 500,000 = Sold 166,666 + Held 83,334 + Available 250,000

## 已知限制

- 匯出約 3.5–4 分鐘；nginx 對 `/reports/` 路徑的 `proxy_read_timeout` 設為 300 s（`infra/nginx/nginx.conf`），更大的資料量需改為背景工作（見 `PLAN.md` §9 備案）。
- 本機 Docker Desktop 的 page cache 計入容器 `memory.current`；在記憶體更緊的環境中，匯出速度可能因頻繁回收而下降。
