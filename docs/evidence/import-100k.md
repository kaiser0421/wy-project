# 10 萬列座位匯入實測

## 2026-11-22 完整堆疊實測

- 環境：開發筆電（Intel 2 核心／4 執行緒），`docker compose` 啟動的完整服務（nginx、3 個 API 副本、worker、PostgreSQL 等），commit `c4c684d`。
- 方法：以 User 身分經 nginx 呼叫匯入 API 上傳 4 區共 100,000 席的檔案，輪詢 job 至終態並核對資料庫座位數；同時以 Viewer 上傳確認被拒（403）。計時從上傳開始到 job 終態。
- 閒置時各跑兩次；另有一次在同一台電腦同時執行 `make test`（完整 Docker 測試套件）時量測，用來觀察資源競爭下的表現。

| 情境 | CSV | JSON | 門檻 |
|---|---:|---:|---:|
| 閒置，第 1 次 | **15.61 秒** | **22.22 秒** | 60 秒 |
| 閒置，第 2 次 | **11.59 秒** | **17.00 秒** | 60 秒 |
| 同時執行 `make test` | 20.96 秒 | 47.75 秒 | 60 秒 |

- 每次都成功寫入 100,000 席，worker 日誌沒有逾時或重試。
- 資源競爭下 JSON 的寫入階段明顯變慢：最長的一個 finalize 陳述句約 29 秒，可調上限為 45 秒（`IMPORT_FINALIZE_TIMEOUT_MS`）。若逾時，重試時已寫入 staging 的批次會保留並跳過（`backend/src/ticketing/imports/runner.py:331–341`），只重做驗證與 finalize。

```text
csv: upload 202 in 0.13s, finished succeeded at 20.96s, db seats 100000, viewer -> 403
json: upload 202 in 0.52s, finished succeeded at 47.75s, db seats 100000, viewer -> 403
csv: upload 202 in 0.13s, finished succeeded at 20.96s, db seats 100000, viewer -> 403
json: upload 202 in 0.52s, finished succeeded at 47.75s, db seats 100000, viewer -> 403
csv: upload 202 in 0.13s, finished succeeded at 20.96s, db seats 100000, viewer -> 403
json: upload 202 in 0.52s, finished succeeded at 47.75s, db seats 100000, viewer -> 403
```

## 2026-11-22 整合測試（隔離資料庫）

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
