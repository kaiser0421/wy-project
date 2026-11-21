# 10 萬列座位匯入實測

## 2026-11-22 閒置筆電實測

- 測試：`backend/tests/imports/test_import_perf.py` 的 `test_import_100k_under_60s`。可在整合測試環境以 `uv run pytest -s -m slow backend/tests/imports/test_import_perf.py` 重現。
- 環境：閒置開發筆電，目前合併後的程式（本工作樹基於 `af3b147`）。
- 計時範圍：產檔後從 API 上傳開始，到 worker 處理完畢且 API 回報終態為止。測試同時驗證 job 成功及資料庫座位數。

| 格式 | 列數 | 上傳到完成 | 門檻 |
|---|---:|---:|---:|
| CSV | 100,000 | **15.14 秒** | 60 秒 |
| JSON | 100,000 | **15.35 秒** | 60 秒 |

## 歷史量測（2026-11-21，`5be8721`）

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
