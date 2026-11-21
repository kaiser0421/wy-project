# 10 萬列座位匯入實測

- 指令：`pytest -s -m slow backend/tests/imports/test_import_perf.py`（`test_import_100k_under_60s`，CSV 與 JSON 各一次）
- 環境：開發筆電（Intel 2 核心／4 執行緒），PostgreSQL 16 與 Redis 7 容器，2026-11-21，commit `5be8721`
- 計時範圍：從 API 上傳開始，經 worker 領取、分批 COPY 至 staging、SQL 驗證、單一交易 finalize，到 job 進入終態為止（測試先產生檔案，產檔不計時）。

| 格式 | 列數 | 上傳到完成 | 門檻 |
|---|---:|---:|---:|
| CSV | 100,000 | **11.38 秒** | 60 秒 |
| JSON | 100,000 | **13.46 秒** | 60 秒 |

```text
CSV 100,000 rows from upload to terminal status took 11.38s
JSON 100,000 rows from upload to terminal status took 13.46s
2 passed in 30.76s
```

測試同時斷言 job 狀態為成功且資料庫中恰好新增 100,000 席。
