# 監控數據（DESIGN Q6）

| 指標 | 量測與時間窗口 | 保存 |
| --- | --- | --- |
| RPS | nginx 以 access log 的完成時間 `$msec` 計算 UTC 秒。API 以路由模板統計處理完成的請求。兩個來源都排除 SSE。 | `metrics_1s` 的 `req_total`，依來源與範圍查詢 |
| 錯誤率 | 每秒的 4xx、5xx 與總請求數。監控 API 的錯誤率只計 5xx：`err_5xx / req_total`（4xx 另行計數，含客戶端錯誤及限購、座位已被搶等業務衝突）。 | `metrics_1s` 計數與各 HTTP 狀態類別 |
| p50、p95、p99 | nginx 的 `$request_time` 包含代理層延遲。應用層從 ASGI 進入到回應送出完成計時。 | `metrics_1s` 保存固定桶直方圖與 nearest-rank 估計值。同一來源的副本或時間窗可逐桶加總並重算百分位數。Edge、App 各自計算。 |
| 連線數 | collector 每秒探測 nginx `stub_status`。API SSE 連線數為近三秒內各副本最後一次 gauge 的加總。 | `metrics_1s` 的 `conns_active` 與 `conns_sse` |

API 的背景日誌寫入器使用有界佇列。佇列滿時會直接丟棄並計數，請求永遠不等佇列空位。寫入器以 `service_logs.dropped` 記錄丟棄數。見 `backend/src/ticketing/api/app.py:330`、`backend/src/ticketing/core/logging.py:199–206`、`backend/src/ticketing/core/logging.py:80–104`、`backend/src/ticketing/core/logging.py:111–126`、`backend/src/ticketing/core/logging.py:153–158`。

nginx 晚到兩秒內的日誌會補入原秒。更晚的日誌仍可用 request ID 查詢。串流推送最近完成的秒，短時間的修正可從歷史查詢取得。每日分區依 UTC 切換，日誌、監控指標與座位事件分別保留 7、14、90 天。

當使用者一直收到正常的 HTTP 回應或可預期的 409，圖表仍可能掩蓋購票失敗。例如有空位的活動持續出現 `hold_soldout`。`PURCHASE_FUNNEL_COLLAPSE` 會檢查同一活動過去 30 秒的 hold 結果。若成功率低於 20%、失敗中 conflicts 與 limits 合計少於一半，而且仍有可售座位，持續 30 秒後告警。這項檢查以活動為範圍，流量正常的大區可能掩蓋小區的問題。

每個 API 程序以單調時鐘產生一秒的快照，附上 boot UUID 與 interval number。Redis 的 stream ID 提供原始接收時間。collector 以 60 秒內最小的接收時間減單調時鐘時間估算偏移，並使用此偏移分桶。消費交易先去重，再寫入指標與業務計數，提交後 ACK。重啟會領回尚未 ACK 的項目。應用程序崩潰可能遺失記憶體內的快照，Redis 中斷或修剪也可能造成缺口。PostgreSQL 的 producer heartbeat 與已回報副本數可辨識這些秒。副本時鐘慢 20 秒時，分桶仍按單調時鐘校正，`clock_skew_ms` 則顯示約 20,000。校正使用目前可用的延遲樣本，因此首次接收延遲可能影響最初的秒，特別是量測點接近秒邊界時。

判斷實際 HTTP 結果時使用 nginx 數據，因為它記錄 499、代理逾時及排隊延遲。用 API 擷取的路由和業務結果定位原因。訂單、座位與實收款項以 PostgreSQL 的交易結果為準。

## Runtime contracts

- nginx must expose an internal `/nginx_status`. The collector defaults to `http://nginx:8080/nginx_status` and reads `NGINX_STATUS_URL` at startup. Suppressing the probe's own access log keeps idle traffic at zero.
- Keep uncompressed rotated segments under their original `.json` prefix until the collector drains them. A same-inode truncate-and-regrow is detectable when the size shrinks or the consumed prefix fingerprint changes.
- Payment reconciliation should populate `payment_attempts.provider_captured_at` from a verified capture observation when the attempt remains `awaiting_customer` and the capture row has not yet been recorded. `last_polled_at` measures polling activity. A capture that has yet to be observed by any local component is outside the signal.
- The API producer flushes business counters recorded in the API process. Background-only outcomes need a producer lifecycle in the worker to appear in the same aggregates and completeness registry.
- Admin alert SSE reads durable snapshots every second. Redis subscribers can also receive state transitions from `admin:alerts`.
