# 壓測工具包

只需要 **Git、Docker（含 Compose v2）與 bash**（Linux、macOS，或 Windows 的 WSL / Git Bash）。所有 Python 工具都在專案的後端映像檔內執行，k6 使用官方映像檔，主機不需安裝任何語言環境。

## 執行

```sh
git clone git@github.com:kaiser0421/wy-project.git && cd wy-project

# 1. 先確認工具包可用（小規模，約 3 分鐘）
loadtest/run.sh smoke

# 2. 正式壓測：情境 A（10,000 席、3,000 VU、60 秒）+ 一致性檢查 + 情境 B（2,000 條 SSE）
loadtest/run.sh all
```

腳本會自動執行 `docker compose up -d --build`（若堆疊已在運作則沿用；設 `REBUILD=1` 強制重建）、建立壓測資料、跑壓測、等待排空、執行一致性檢查並打包結果。

完成後把 **`loadtest/out/<時間戳>.tar.gz`** 傳回即可。打包前會刪除含登入憑證的 `sessions.json`。

## 單獨執行與參數

| 指令 | 內容 |
|---|---|
| `loadtest/run.sh a` | 只跑情境 A + 一致性檢查 |
| `loadtest/run.sh b` | 只跑情境 B |
| `loadtest/run.sh smoke` | 兩個情境的縮小版 |

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `VUS` | 3000 | 情境 A 的虛擬使用者數（每人一個獨立帳號與 session） |
| `DURATION` | 60s | 情境 A 持續時間 |
| `SEATS` | 10000 | 座位數 |
| `CONNECTIONS` | 2000 | 情境 B 的 SSE 訂閱數（每條一個獨立 session） |
| `B_DURATION` | 80 | 情境 B 訂閱秒數（寫入端在第 15 秒開始，持續 `B_DURATION-25` 秒） |
| `B_RATE` | 300 | 情境 B 每秒座位變更數 |
| `K6_CPUS` | 不限 | 限制 k6 容器可用 CPU，例如 `K6_CPUS=2` |

例：`VUS=1000 DURATION=30s loadtest/run.sh a`

## 輸出內容

```
loadtest/out/<時間戳>/
  environment.txt        機器規格、Docker 版本、git commit、參數
  run.log                全程摘要
  a/k6.log               k6 完整輸出（RPS、p50/p90/p95、錯誤率、業務結果分類）
  a/k6-summary.json      k6 指標 JSON
  a/consistency.txt      一致性檢查（0 列 = 一致）
  a/final-state.txt      訂單與座位的最終狀態統計
  a/metrics_1s.csv       壓測期間每秒系統指標（監控資料表）
  a/business_1s.csv      每秒業務指標
  a/nginx-access.json    本次壓測的 nginx 存取紀錄
  b/seed.txt, writer.txt 情境 B 準備與寫入端輸出
  b/sse-client.txt       訂閱端結果：連線數、首次連線與套用延遲、斷層、重同步、最終狀態比對
```

## 注意

- 產壓端（k6、SSE client）與受測系統在同一台機器上，會互相競爭 CPU；報告中請一併記錄 `environment.txt` 的規格。
- 情境 A 的 3,000 個買家與 session 直接寫入資料庫（與 `POST /auth/login` 產生的資料相同），避免準備階段觸發登入限流。
- 重複執行會建立新的活動與帳號，不影響之前的資料；要清空可執行 `docker compose down -v`。
