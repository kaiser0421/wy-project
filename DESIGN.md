# 設計問答

## 目錄

- [設計假設](#設計假設)
- [Q1 座位不超賣](#q1-座位不超賣)
- [Q2 部分失敗後的狀態收斂](#q2-部分失敗後的狀態收斂)
- [Q3 重試、冪等與限購](#q3-重試冪等與限購)
- [Q4 即時推送](#q4-即時推送)
- [Q5 權限與憑證失效](#q5-權限與憑證失效)
- [Q6 監控數據](#q6-監控數據)
- [替代方案與取捨](#替代方案與取捨)
- [驗證對照表](#驗證對照表)

## 設計假設

| 假設 | 適用範圍 |
|---|---|
| PostgreSQL 16 為座位、訂單、配額、session 與 job 的權威來源 | Compose 部署使用單一 PostgreSQL、三個 API 副本。帳務收斂另仰賴模擬金流保存 intent、capture、refund 並接受同一識別碼重試。見 `compose.yaml:45`、`compose.yaml:105`、`paymock/src/paymock/store.py:153`、`paymock/src/paymock/store.py:204`。 |
| 每筆 1–4 張，每人每活動預設最多 4 張 | `pending_payment`、`paid`、`refund_pending` 佔配額。取消、逾時或退款完成而釋位時回補。預設 hold 為 600 秒，接受付款以持有訂單鎖後的 DB 時間為準。見 `backend/src/ticketing/booking/models.py:19`、`backend/src/ticketing/booking/models.py:40`、`backend/src/ticketing/core/settings.py:71`、`backend/src/ticketing/booking/orders.py:117`、`backend/src/ticketing/payments/callbacks.py:159`。 |
| 自動收斂以依賴恢復、持久資料仍可用為前提 | Compose 常駐後端使用 `restart: unless-stopped`。worker 與 relay 恢復後從 DB 繼續處理。長時間停機或 backlog 堆積會延長收斂。見 `compose.yaml:28`、`compose.yaml:42`、`backend/src/ticketing/worker/main.py:20`、`backend/src/ticketing/realtime/relay.py:55`。 |

## Q1 座位不超賣

**答案**　指定席的交易內保護在 PostgreSQL 執行 `FOR NO KEY UPDATE NOWAIT` 時取得（`backend/src/ticketing/booking/create.py:51`），在交易 context 結束時由 COMMIT 或 ROLLBACK 釋放（`backend/src/ticketing/booking/database.py:31`，呼叫端離開交易前最後一行為 `backend/src/ticketing/booking/create.py:245`）。已提交的 active claim 由 partial unique index 持續約束，直到該 item 寫入 `released_at`。

**機制**

| 順序 | 指定席路徑 |
|---|---|
| 1. API 驗證 | `POST /api/v1/events/{event_id}/orders` 限 Admin、User。policy 驗證 session、CSRF 與有提供的 Origin。要求 `Idempotency-Key`，座位 ID 驗證 1–4 個、不得重複並排序。route 將 event ID 與正規化 body 納入請求雜湊。見 `backend/src/ticketing/api/routes/orders.py:30`、`backend/src/ticketing/api/routes/orders.py:38`、`backend/src/ticketing/api/routes/orders.py:72`、`backend/src/ticketing/api/security.py:115`、`backend/src/ticketing/booking/models.py:16`。 |
| 2. 進入交易 | `READ COMMITTED`，先鎖定並重驗 user/session，再檢查冪等記錄。新請求檢查發布與開賣時間，原子增加配額，先插入 order header 取得 event FK 所需的鎖。見 `backend/src/ticketing/booking/database.py:31`、`backend/src/ticketing/auth/sessions.py:111`、`backend/src/ticketing/booking/create.py:239`、`backend/src/ticketing/booking/create.py:146`、`backend/src/ticketing/booking/create.py:158`。 |
| 3. 第一層，座位 row lock | 依座位 ID 順序取鎖。同座位的並行寫入互斥，搶不到鎖回 `409 seat-contended`。在鎖內確認座位屬於活動且都可用，否則回 `409 seat-unavailable`。見 `backend/src/ticketing/booking/create.py:48`。 |
| 4. 第二層，guarded UPDATE | 鎖後重新以 `app_now()` 確認開賣，計算期限。只有 `status='available'` 的座位能寫為 `held`、填入 `order_id` 並遞增版本。更新筆數少一筆就回滾整單。即使先行讀取改成無鎖，並行 UPDATE 等到前一個寫入提交後仍須重檢條件。見 `backend/src/ticketing/booking/create.py:87`、`backend/src/ticketing/booking/create.py:174`。 |
| 5. 第三層，partial unique index | 寫入 `order_items` 和單價。同一 `seat_id` 只容許一筆 `released_at IS NULL` 的 item。違反該 index 回 `409 seat-unavailable`，座位和配額一起回滾。見 `backend/src/ticketing/booking/create.py:96`、`backend/src/ticketing/booking/create.py:106`、`backend/migrations/versions/0001_baseline.py:189`、`backend/src/ticketing/booking/orders.py:107`。 |
| 6. 提交與回應 | 完成金額、期限，寫 outbox 和冪等結果，提交後才回 `201` 與 `Location`。服務費以整數 TWD 計算 5% half-up。見 `backend/src/ticketing/booking/create.py:27`、`backend/src/ticketing/booking/create.py:182`、`backend/src/ticketing/booking/create.py:197`、`backend/src/ticketing/booking/create.py:245`、`backend/src/ticketing/api/routes/orders.py:92`。 |

**時間線**　最容易導致雙賣的是舊付款回呼跨過期限，在同一張座位已被轉售後才恢復。

1. A 鎖住座位 S。模擬金流在期限前扣款，回呼因網路或程序停頓延後。
2. Sweeper 取 A 的訂單鎖，重檢 `pending_payment` 與期限。提交時 A 為 `expired`，S 釋出，item 關閉且配額回補。B 隨後可鎖住 S 並付款。見 `backend/src/ticketing/booking/sweeper.py:27`、`backend/src/ticketing/booking/orders.py:91`。
3. A 的回呼恢復，取 A 的訂單鎖後讀狀態並取樣 DB 時間。A 已逾時，寫入 `refund_required` 與退款工作，保留 B 的售票結果。若回呼先取鎖但已逾期，它會在同一交易先釋放 A，再安排退款。接受付款的分支還會確認每張座位都由該訂單持有，更新條件是 `order_id=$1 AND status='held'`，筆數不符即回滾。見 `backend/src/ticketing/payments/callbacks.py:87`、`backend/src/ticketing/payments/callbacks.py:105`、`backend/src/ticketing/payments/callbacks.py:113`、`backend/src/ticketing/payments/callbacks.py:159`、`backend/src/ticketing/payments/callbacks.py:190`。

**驗證**　`test_specific_seat_races` 覆蓋 200 位買家搶同一席及 150 位買家搶重疊多席。 `test_break_it[normal|indexed|naive]` 共用 SQL 不變式。 `indexed` 改用無鎖讀取、無 guard 寫入，index 仍拒絕第二個 claim。 `naive` 在測試 DB clone 再移除 index，兩筆都得到 201，測試捕捉預期的 `AssertionError`。 `test_late_capture_preserves_quota` 分別測 sweeper 先釋放與回呼先處理。 設 `BREAK_IT_RAW=1` 時 `naive` 不再包住斷言，pytest 直接以 `FAILED … Booking invariant violations` 結束，可親眼看到移除防線後的失敗。見 `backend/tests/booking/test_booking.py:46`、`backend/tests/booking/test_booking.py:106`、`backend/tests/payments/test_payments.py:209`。

**證據**　PostgreSQL 16.16 上的實際輸出如下。三個 pytest case 都通過，`naive` 的 invariant 違規是測試預期捕捉的失敗。見 [破壞驗證紀錄](docs/evidence/break-it.txt)。

```text
backend/tests/booking/test_booking.py::test_break_it[normal] PASSED      [ 33%]
backend/tests/booking/test_booking.py::test_break_it[indexed] PASSED     [ 66%]
backend/tests/booking/test_booking.py::test_break_it[naive] PASSED       [100%]

----------------------------- Captured stdout call -----------------------------
Booking invariant violations: [{'invariant': 'seat active claims', 'id': 2}, {'invariant': 'seat active claims', 'id': 1}, {'invariant': 'seat active claims', 'id': 3}]
======================= 3 passed, 9 deselected in 7.07s ========================
```

以 `BREAK_IT_RAW=1` 執行時，`naive` 不再包住斷言，三層防線都移除後測試直接失敗（[原始輸出](docs/evidence/break-it-raw.txt)）：

```text
$ BREAK_IT_RAW=1 pytest -v "backend/tests/booking/test_booking.py::test_break_it"
# PostgreSQL 16 (postgres:16 image), 2026-11-21 15:50 CST; normal and indexed are controls, naive removes all three protections

backend/tests/booking/test_booking.py::test_break_it[normal] PASSED      [ 33%]
backend/tests/booking/test_booking.py::test_break_it[indexed] PASSED     [ 66%]
backend/tests/booking/test_booking.py::test_break_it[naive] FAILED       [100%]
E       AssertionError: Booking invariant violations: [{'invariant': 'seat active claims', 'id': 2}, {'invariant': 'seat active claims', 'id': 1}, {'invariant': 'seat active claims', 'id': 3}]
backend/tests/booking/support.py:117: AssertionError
=========================== short test summary info ============================
FAILED backend/tests/booking/test_booking.py::test_break_it[naive] - Assertio...
========================= 1 failed, 2 passed in 12.60s =========================
```

## Q2 部分失敗後的狀態收斂

**答案**　最大風險是模擬金流已扣款，PostgreSQL 尚未記錄 capture，且 hold 可能在此期間到期。這同時影響使用者的金額與座位。金流回呼與 reconciler 共用付款分類，依 DB 當下狀態接受或安排退款。

**機制與時間線**　下表的 tick、lease 與 backoff 是設定或程式行為。程序重啟、外部服務延遲與排隊時間另計。

| `kill -9` 時的落差 | 恢復流程與時間 |
|---|---|
| 訂單已提交，HTTP 回應遺失 | 相同 key 重送時從 PostgreSQL 重放。沒有重送的未付款訂單在期限到達後由 sweeper 處理，迴圈每輪後等 1 秒，每輪最多 200 筆。見 `backend/src/ticketing/booking/create.py:239`、`backend/src/ticketing/booking/sweeper.py:10`、`backend/src/ticketing/worker/main.py:20`。 |
| 座位已變更，relay 或 SSE 尚未通知 | outbox 隨座位提交。relay 每輪後等 50 ms 再處理已提交且未編序的批次，編序與 archive 同一交易。掛在交易中可回滾重做，掛在提交後由 API 直接讀取已編序資料。斷線客戶端 replay 或抓快照。見 `backend/src/ticketing/realtime/outbox.py:20`、`backend/src/ticketing/relay/main.py:10`、`backend/src/ticketing/realtime/relay.py:28`、`backend/src/ticketing/realtime/stream.py:45`。 |
| payment attempt 已建立，但 provider 尚未收到請求，或已完成 create 但本地尚無結果 | attempt 和 key 先持久化，再以同 attempt ID 呼叫 provider。客戶端可重送，reconciler 也會重做 create 或 poll。attempt 至少間隔 15 秒才重新符合領取條件，reconciler 每輪後等 5 秒。見 `backend/src/ticketing/payments/attempts.py:45`、`backend/src/ticketing/payments/attempts.py:156`、`backend/src/ticketing/payments/reconciler.py:28`、`backend/src/ticketing/worker/main.py:22`。 |
| **已扣款但本地尚未記錄**，或回呼已提交但 ACK 遺失 | provider 原子保存 capture 與 callback 待辦，重送視窗為 24 小時。reconciler 也可查詢，取消流程若發現已有 capture，會交給同一個 handler。DB 以 callback event ID 與 capture ID 去重。鎖後仍為有效 hold 就售出，否則記錄退款工作。若已逾時轉售，流程同 Q1。若回呼未先抵達，attempt 自 `last_polled_at`（無值時用 `updated_at`）滿 15 秒可由 reconciler 領取。Worker 每輪完成後等 5 秒再查。若 worker 健康、前輪工作很短且無積壓，從該計時基準約 15–20 秒出現下一次 poll 機會。重啟、前輪耗時、provider 回應、積壓及重試均另計。自動恢復的暫時中斷無需人工介入。見 `paymock/src/paymock/store.py:111`、`paymock/src/paymock/delivery.py:39`、`backend/src/ticketing/payments/reconciler.py:33`、`backend/src/ticketing/payments/reconciler.py:59`、`backend/src/ticketing/payments/reconciler.py:172`、`backend/src/ticketing/worker/main.py:22`、`backend/src/ticketing/worker/main.py:34`、`backend/src/ticketing/payments/callbacks.py:149`、`backend/src/ticketing/payments/callbacks.py:215`、`backend/src/ticketing/api/routes/payments.py:149`。 |
| 本地已排入退款或取消 intent，或 provider 已成功但本地未確認 | 到期 lease 可重領（30 秒）。退款以原 refund ID 重送，取消以 attempt ID 重送。本地寫入檢查 `lease_owner`。已付款訂單直到退款成功才在同一交易釋位與回補配額。失敗 backoff 上限 300 秒。見 `backend/src/ticketing/payments/reconciler.py:65`、`backend/src/ticketing/payments/reconciler.py:85`、`backend/src/ticketing/payments/reconciler.py:110`、`backend/src/ticketing/payments/reconciler.py:125`、`backend/src/ticketing/payments/reconciler.py:161`。 |
| 匯入檔已存但 job 尚未提交，或 job 的 staging、進度、完成狀態與通知分開 | 未被活躍 job 使用的舊檔進入 24 小時清理門檻。已提交的 job 依活動與檔案 digest 去重。worker 逾 30 秒 lease 可用新 fence 重做，第三次 lease 到期仍未完成就終止。寫正式座位與 job 結果在同一交易。進度 SSE 可重新讀取 DB，遺留檔案可由清理回收。見 `backend/src/ticketing/api/routes/imports.py:107`、`backend/src/ticketing/api/routes/imports.py:149`、`backend/src/ticketing/imports/jobs.py:28`、`backend/src/ticketing/imports/runner.py:25`、`backend/src/ticketing/imports/runner.py:37`、`backend/src/ticketing/imports/runner.py:85`、`backend/src/ticketing/imports/runner.py:236`、`backend/src/ticketing/imports/runner.py:313`。 |
| session 已撤銷，串流尚未收到通知 | 新請求即時讀 DB。座位串流靠 DB 批次重驗補上 Redis 通知遺失，見 Q5。若登入已提交但 cookie 尚未送達，該 session 仍受 idle／absolute deadline 限制，使用者可再登入。見 `backend/src/ticketing/api/routes/auth.py:215`、`backend/src/ticketing/auth/sessions.py:38`。 |
| 日誌或 telemetry 已產生但尚未寫入歷史，或 collector 在提交後、ACK 前終止 | 檔案 checkpoint 隨 DB 寫入提交，Redis 已送達的 interval 可重新領取並去重。API 在 XADD 前終止的記憶體樣本可能永久遺失。告警寫入 DB 後若通知遺失，Admin SSE 下次輪詢仍可看到。見 Q6 及 `backend/src/ticketing/api/routes/monitoring.py:354`。 |

正常暫時性中斷由後續 tick 或重試自行處理。持續 provider 故障、儲存卷遺失或匯入已到終止狀態時，需要排查或重送，沒有固定的收斂時間上限。

**時鐘與重複執行**　`app_now()` 在 PostgreSQL 使用 `clock_timestamp()`，避免交易開始時間在等鎖時停住，API 主機牆鐘偏移不影響 hold 判定。測試在 DB clone 替換函式，`TestClock.set/advance` 可在 Python 牆鐘照常走時將 DB 推到期限或其後，再直接呼叫 sweeper 或回呼。Sweeper 在候選查詢用 `FOR UPDATE SKIP LOCKED`，即便同一訂單再次進入 `release_order`，決定性保護是鎖後的 `WHERE id=$1 AND status=$2 AND (... OR expires_at <= app_now())`（`backend/src/ticketing/booking/orders.py:91`）。沒有更新列便在第 97–98 行返回，座位、item、配額和 outbox 都不會再改一次。見 `backend/migrations/versions/0001_baseline.py:29`、`backend/tests/sql/test_clock.sql:10`、`backend/tests/support.py:25`、`backend/src/ticketing/booking/sweeper.py:27`。

**驗證**　`test_parallel_sweepers_skip_locked_and_bad_orders`、`test_cancel_expire_and_page`、`test_app_now_uses_clock_timestamp`、`test_test_clock_controls_app_now`、`test_lost_create_response_and_paid_cancel`、`test_open_response_after_hold_expires`、`test_stale_worker_cannot_stage_or_finalize`、`test_expired_third_attempt_becomes_terminal`。見 `backend/tests/booking/test_booking.py:306`、`backend/tests/booking/test_booking.py:157`、`backend/tests/integration/test_schema.py:65`、`backend/tests/integration/test_constraints.py:140`、`backend/tests/payments/test_payments.py:244`、`backend/tests/payments/test_payments.py:379`、`backend/tests/imports/test_imports.py:159`、`backend/tests/imports/test_imports.py:187`。

**證據**　整合測試以可控時鐘、鎖衝突、遺失 provider 回應和重領 lease 驗證狀態。上表的週期不能當作整個 Compose stack 的 `kill -9` 量測結果。

## Q3 重試、冪等與限購

**答案**　建立訂單以 `(user_id, key)` 序列化，結果與座位、配額同時提交。兩把 key 仍會競爭同一個 user/event 配額列，配位由 PostgreSQL 選取並鎖定。

**機制與流程**

| 重送時原請求的狀態 | 建立訂單的結果 |
|---|---|
| 已成功 | 回原本的 `201`、body 與 `Location`，加上 `Idempotent-Replayed: true`。即使訂單之後變更，重放的仍是原回應。以訂單 GET 查最新狀態。 |
| 處理中，仍持有 key 的交易鎖 | `pg_try_advisory_xact_lock` 立即回失敗。API 回 `409 idempotency-in-progress`、`Retry-After: 1`。此時尚未讀已提交的 hash，所以連不同 body 的並行請求也先收到 409。 |
| 同 key、已存在的 hash 不同 | 回 `422 idempotency-key-reused`。hash 涵蓋 operation、resource ID、正規化 body。指定席只換 ID 順序仍視為相同。 |
| 原交易已回滾且未留下記錄 | 同 key 可重新執行。已完成記錄超過 24 小時後可由 worker 批次清理。 |

見 `backend/src/ticketing/booking/idempotency.py:40`、`backend/src/ticketing/booking/idempotency.py:52`、`backend/src/ticketing/booking/idempotency.py:62`、`backend/src/ticketing/booking/idempotency.py:80`、`backend/src/ticketing/booking/idempotency.py:112`、`backend/src/ticketing/booking/models.py:28`、`backend/src/ticketing/payments/reconciler.py:215`。建立付款另有持久的 `in_progress` attempt，交易鎖未被占用時，同 key 可續做 provider create 或讀取結果。provider 結果仍未知時回可重試 `503 server-busy`。見 `backend/src/ticketing/payments/attempts.py:156`。

- **限購原子性**　`INSERT ... ON CONFLICT DO NOTHING` 確保配額列存在。單一 SQL `UPDATE ... SET reserved=reserved+$3 WHERE ... AND reserved <= $4 - $3 RETURNING` 以同一列鎖檢查並修改，未更新回 `409 purchase-limit-exceeded`。兩個 key 各買 3 張時，先成功的一筆使後一筆重檢失敗。若把檢查和寫入拆成無鎖讀取，兩筆可能都看到 0，合計取得 6 張。後續配位失敗會回滾配額。見 `backend/src/ticketing/booking/create.py:122`、`backend/src/ticketing/booking/create.py:128`、`backend/src/ticketing/booking/create.py:239`。
- **資料庫配位**　依活動、區域與 `available` 過濾，按 `sort_key,id` 取 `LIMIT`，使用 `FOR NO KEY UPDATE SKIP LOCKED`。多位買家同時搶同區最後幾張時，各自跳過他人正鎖住的座位。暫時不足時，原交易回滾後另查可用筆數，區分可重試的 `409 allocation-contended` 與 `409 insufficient-seats`。若先把候選讀到應用記憶體，並行者容易選中同一批，一起撞上鎖或持有已過時的座位清單。見 `backend/src/ticketing/booking/create.py:63`、`backend/src/ticketing/booking/create.py:208`、`backend/src/ticketing/booking/create.py:246`。

**驗證**　`test_idempotency_contention_and_canonical_replay` 檢查鎖衝突、原回應重放與 body 變更。 `test_quota_race` 用兩把 key 並行各買 3 張。 `test_zone_converges` 用 500 位買家爭取 300 席，每筆配 2 張，最後斷言恰為 150 筆訂單、300 張 held。 `test_allocation_distinguishes_locked_inventory_from_sold_out` 驗證兩類短缺。見 `backend/tests/booking/test_booking.py:131`、`backend/tests/booking/test_booking.py:91`、`backend/tests/booking/test_booking.py:67`、`backend/tests/booking/test_booking.py:255`。

**證據**　可從測試中的狀態碼、配額與座位不變式斷言核對，對外回應契約見 [OpenAPI](docs/openapi.json)（`docs/openapi.json:2978`）。

## Q4 即時推送

**答案**　座位通知走 PostgreSQL transactional outbox、單一 active relay、每副本每活動一個 reader，最後以 SSE 發送。所有副本讀同一組 per-event seq。同席版本有序，跨席的 seq 反映 relay 的編序。

**機制與流程**

1. 持有座位鎖的交易先更新座位版本，再寫 outbox，兩者一起提交。對同一席，下一筆要等上一筆提交才可更新並取得 outbox ID。relay 只處理已提交列，依可見 ID 批次編序，以 DB advisory lock 保證同時只有一個 relay 批次執行。在同一交易更新活動 seq、回填 outbox 並批次 archive 到 `seat_events`。無關座位的提交順序可能和編序不同。見 `backend/src/ticketing/booking/create.py:179`、`backend/src/ticketing/realtime/outbox.py:20`、`backend/src/ticketing/realtime/relay.py:14`、`backend/src/ticketing/realtime/relay.py:46`、`backend/src/ticketing/realtime/relay.py:75`。
2. 有訂閱時，每個 API process 按活動共用 reader，約每 100 ms 查已編序 outbox，`ORDER BY seq`。reader 將連續區間合併成 `delta`，同席只帶最高版本，遇 `layout` 切斷區間。frame 編碼後共用 ring。各副本合併的區間可能不同，但共用 seq 語義與同席版本。nginx 對 `seat-stream` 關閉 buffering。見 `backend/src/ticketing/realtime/reader.py:22`、`backend/src/ticketing/realtime/reader.py:47`、`backend/src/ticketing/realtime/reader.py:161`、`backend/src/ticketing/realtime/frames.py:40`、`infra/nginx/nginx.conf:115`。
3. 客戶端先讀 `GET /api/v1/events/{event_id}/seats` 的快照，再用 `last_seq` 作為 `Last-Event-ID` 連 `GET /api/v1/events/{event_id}/seat-stream`。若 `delta.from > last_applied + 1`，以最後成功套用的 seq 重連。伺服器從 ring 補，缺段則查 PostgreSQL，最多 5,000 筆、8 個並行 replay task（每 process），PG 補送預算 5 秒。缺史、超出預算或收到 `layout` 時，依需要帶 `min_layout_ver` 重新抓快照。快照以 `REPEATABLE READ` 取座位與 head，可包含已提交但未編序的新版本，因此客戶端只套用更高的座位版本。見 `backend/src/ticketing/api/routes/seats.py:58`、`backend/src/ticketing/api/routes/seats.py:112`、`backend/src/ticketing/realtime/snapshot.py:170`、`backend/src/ticketing/realtime/reader.py:28`、`backend/src/ticketing/realtime/reader.py:98`、`backend/src/ticketing/realtime/stream.py:45`、`frontend/src/seat-model.ts:33`、`frontend/src/events.ts:267`。
4. 慢客戶端共用 ring（5 秒或 2 MiB，先達者淘汰）。每連線一次寫一個 frame；落後 ring 可回查，寫入超過 5 秒則嘗試用 100 ms 最後傳送 `resync` 並關閉。這省下無界的 per-client backlog，代價是可能跳過中間狀態或重抓整張座位圖。UI 約 20 秒沒收到串流資料，或落後 progress head 約 10 秒時會主動重連。最壞時所有後續變更的座位與 layout 都可能過時，直到成功 replay 或快照。持續斷網、DB 或 relay 故障時無有限的畫面收斂上限。仍可由 PostgreSQL 的交易條件拒絕過時的選位。見 `backend/src/ticketing/realtime/reader.py:24`、`backend/src/ticketing/realtime/reader.py:78`、`backend/src/ticketing/realtime/stream.py:21`、`backend/src/ticketing/realtime/stream.py:160`、`frontend/src/events.ts:288`。

**驗證**　`test_concurrent_writers_preserve_per_seat_order`、`test_two_relays_only_one_active`、`test_snapshot_and_delta_resume_end_to_end`、`test_replay_recovers_from_ring_eviction_or_resyncs`、`test_slow_connection_resyncs_and_fast_peer_keeps_receiving`。見 `backend/tests/realtime/test_relay.py:15`、`backend/tests/realtime/test_relay.py:103`、`backend/tests/realtime/test_api.py:248`、`backend/tests/realtime/test_api.py:362`、`backend/tests/realtime/test_api.py:474`。

**證據**　正式 Scenario B（待桌機壓測結果）。先前 macOS x86_64 本機單一 Uvicorn 的 2,000 連線 burst 峰值 86 條，writer 達 13.5 changes/s，沒有 delta latency 樣本，0/2,000 probe 對上 DB。24 條連線的 paced run 全部對上 DB，實得 48.7 changes/s，apply p50/p95/p99 為 180/2,909/3,817 ms。見 [本機 Scenario B 紀錄](loadtest/scenario_b.md#local-measurements)與 [LOADTEST.md](LOADTEST.md)。

## Q5 權限與憑證失效

**答案**　每個受保護的新 HTTP 請求讀 PostgreSQL。登出及停用或改角色的撤銷一旦提交，舊 cookie 在下一個驗證讀取點回 `401`。沒有 session cache 的不一致窗口。已進入 handler 的請求可能已通過驗證，訂票等敏感交易在寫入前另有 session fence。

**機制與流程**

- **撤銷**　登出寫 `revoked_at` 再清 cookie。Admin 修改角色或啟用狀態時在同一交易更新 user、撤銷其現有 sessions。訂票交易依 `users → sessions` 順序取 `FOR SHARE` 並重驗。撤銷提交後的 Redis pub/sub 用於關閉座位 SSE。若通知遺失，座位 SSE 每個副本約每秒批次檢查已連線的 distinct session IDs，連續兩次檢查失敗即關閉該副本的座位串流。這是健康環境下的檢查週期，事件迴圈停頓時無固定上限。Admin 監控與匯入進度串流每輪另讀 session。見 `backend/src/ticketing/api/routes/auth.py:265`、`backend/src/ticketing/api/routes/admin_users.py:221`、`backend/src/ticketing/api/routes/admin_users.py:240`、`backend/src/ticketing/auth/sessions.py:111`、`backend/src/ticketing/realtime/revocation.py:96`、`backend/src/ticketing/api/routes/monitoring.py:362`、`backend/src/ticketing/api/routes/imports.py:169`。
- **每請求成本**　一次 token hash、一次 DB pool acquire 與一次按唯一 `sessions.token_hash` 查 session、JOIN user，判斷撤銷、啟用、角色及有效期。idle 續期合併後批次更新，SQL 不會復活撤銷或過期的 session。訂票另有交易內兩次鎖定查詢。以有效 cookie 重放 `GET /api/v1/auth/me`，在 API 量測 `resolve_principal` 中 acquire wait 與 `authenticate` 單獨的 latency p50/p95/p99，分低負載與 A 負載時段並記錄錯誤數。可用 `EXPLAIN (ANALYZE, BUFFERS)` 驗證查詢 plan，SQL 時間與端到端時間分開報告。閒置堆疊實測：經 nginx 依序各 1,000 次，`GET /api/v1/auth/me` p50/p95/p99 為 10.89/25.38/35.78 ms，不查 session 的 `GET /healthz/live` 為 7.03/18.85/34.07 ms，中位數差約 3.9 ms，0 錯誤；`EXPLAIN ANALYZE` 顯示走 `sessions_token_hash_key`，索引查詢約 0.17 ms。高負載時的主要成本是等待連線池，於正式 A 結果中觀察。見 [session 驗證成本紀錄](docs/evidence/session-auth-cost.md)。見 `backend/src/ticketing/api/security.py:99`、`backend/src/ticketing/auth/sessions.py:38`、`backend/src/ticketing/auth/sessions.py:50`、`backend/src/ticketing/auth/sessions.py:190`、`backend/migrations/versions/0001_baseline.py:69`。
- **匯入中停用建立者**　worker 每批檢查 `is_active`。finalize 在 DB 交易鎖住 creator、event 並驗證 fence 後，依當下角色、所有權與活動狀態決定能否套用。若停用已先提交，job 結束為 `cancelled`、原因 `creator_disabled`，只清理 staging。若 finalize 先鎖住並提交，之後的停用不回滾已匯入資料。這使正式座位的寫入與當時的有效權限一致。見 `backend/src/ticketing/imports/runner.py:125`、`backend/src/ticketing/imports/runner.py:170`、`backend/src/ticketing/imports/runner.py:279`。

**驗證**　route introspection 巡覽所有已註冊的 API route，檢查 endpoint 宣告的 policy metadata，依 HTTP method 與完整路徑比對 public allowlist。另驗證缺少 policy 或繞過 SecureRouter 註冊的行為會遭拒。OpenAPI 測試逐 operation 核對 security 與錯誤契約，並比對匯出的 JSON。Runtime 整合測試驗證指定路由的角色與物件權限，其中匯入測試涵蓋上傳、查詢、進度串流與錯誤檔下載。最容易漏掉的是 `GET /api/v1/seat-imports/{job_id}/errors.csv`。URL 只有 job UUID，下載內容有原始欄位與錯誤資訊，因此先 JOIN parent event，以 Admin 或活動 owner 身分完成授權，才讀取錯誤列與建立下載檔。見 `backend/tests/authz/test_route_policies.py:52`、`backend/tests/authz/test_route_policies.py:75`、`backend/tests/authz/test_route_policies.py:93`、`backend/tests/authz/test_route_policies.py:102`、`backend/tests/authz/test_openapi.py:22`、`backend/tests/authz/test_openapi.py:76`、`backend/tests/imports/test_imports.py:70`、`backend/src/ticketing/api/routes/imports.py:256`、`backend/src/ticketing/imports/jobs.py:64`。

**證據**　`test_logout_invalidates_the_cookie_immediately`、`test_role_change_revokes_target_sessions_and_is_audited`、`test_disable_revokes_sessions`、`test_invalid_session_closes_stream`、`test_disable_creator_after_staging_cancels_finalize`、`test_import_authorization_matrix` 的斷言與 [OpenAPI](docs/openapi.json) 可交叉核對。見 `backend/tests/integration/test_auth.py:82`、`backend/tests/integration/test_admin_users.py:24`、`backend/tests/integration/test_admin_users.py:47`、`backend/tests/realtime/test_api.py:507`、`backend/tests/imports/test_imports.py:200`、`backend/tests/imports/test_imports.py:70`、`docs/openapi.json:2701`。

## Q6 監控數據

**答案**　Edge 量測使用者經 nginx 所見的 HTTP 結果，App 量測 API 路由與業務結果。兩者各自以每秒資料保存。Edge 的連線數是 TCP gauge，App 的連線數是座位 SSE gauge。

**機制與流程**

| 指標 | 來源與時間窗口 | 儲存 |
|---|---|---|
| RPS | Edge 用 nginx 請求完成時的 `$msec` 分 UTC 秒，距 watermark 超過 2 秒的舊 bucket 只存 log。App 在 ASGI 完成時計入 route template，以 process 單調時鐘約每秒 flush。兩個 HTTP 聚合都排除 SSE。見 `infra/nginx/nginx.conf:35`、`backend/src/ticketing/telemetry/logs.py:242`、`backend/src/ticketing/telemetry/api_setup.py:35`、`backend/src/ticketing/telemetry/api_setup.py:76`。 | `metrics_1s.req_total`，key 為 `(source, scope, ts)`。 |
| 錯誤率 | 每來源／scope／秒按 HTTP status class 計數。監控 API 的 `error_rate` 為 `err_5xx / req_total`，分母 0 回 null。4xx 另查，包含 nginx 在 client disconnect 時記錄的 Edge 499，以及預期業務 409。見 `backend/src/ticketing/telemetry/metrics.py:15`、`backend/src/ticketing/api/routes/monitoring.py:236`、`infra/nginx/nginx.conf:41`。 | `metrics_1s.err_4xx`、`err_5xx`、`status_classes` 及分母。 |
| p50/p95/p99 | 同一秒的 nginx `$request_time` 含 proxy 時間。App 從進入 middleware 到 ASGI 處理返回，以 monotonic 計時。合併同來源各副本的 histogram 桶後再算 percentile。見 `infra/nginx/nginx.conf:42`、`backend/src/ticketing/telemetry/api_setup.py:39`、`backend/src/ticketing/telemetry/metrics.py:17`。 | `metrics_1s.hist` 與固定桶估計的 `p50_ms`、`p95_ms`、`p99_ms`。 |
| 即時連線數 | Collector 每秒探測內部 nginx `/nginx_status` 的 active connections，app 對近 3 秒回報的各副本加總最後一筆 SSE gauge。寫到最近完成的秒。見 `infra/nginx/nginx.conf:97`、`backend/src/ticketing/telemetry/collector.py:34`、`backend/src/ticketing/telemetry/collector.py:46`。 | `metrics_1s.conns_active`、`conns_sse`。 |

日誌以 COPY 分批寫 `access_logs` / `service_logs`。監控與業務計數分別批次寫 `metrics_1s`、`business_1s`。每日 UTC 分區保留門檻為日誌 7 天、監控與業務 14 天、`seat_events` 90 天。監控 SSE 每秒讀最近完成的秒，Edge 缺該秒時可帶上最近的 Edge 資料，判讀時看各列 `ts`。見 `backend/src/ticketing/telemetry/logs.py:316`、`backend/src/ticketing/telemetry/app_metrics.py:241`、`backend/migrations/versions/0005_monitoring.py:95`、`backend/src/ticketing/api/routes/monitoring.py:389`。

- **HTTP 全 200 仍受害**　例如模擬金流 confirm 回 200，買家輪詢訂單也一直回 200，但畫面長時間停在待付款。當 capture 經回呼或 poll 寫入，`PAYMENT_CALLBACK_LAG` 可在最近 60 秒記錄中指出超過 30 秒的落差。完全沒有觀測到 capture 時，這條規則沒有資料，需查看金流與訂單、`HOLD_EXPIRY_BACKLOG` 等旁證。另一種情境是瀏覽與監控正常，大量買家在有空位的活動反覆遇到 `hold_soldout`。 `PURCHASE_FUNNEL_COLLAPSE` 依活動看過去 30 秒，成功率低於 20%、conflict 與 limit 合計少於失敗的一半且仍有可用席，條件持續 30 秒才發出告警。單一小區的問題可能被同活動其他區的流量掩蓋。見 `backend/src/ticketing/telemetry/alerts.py:32`、`backend/src/ticketing/telemetry/alerts.py:45`、`backend/src/ticketing/telemetry/alerts.py:58`、`backend/src/ticketing/telemetry/alerts.py:205`、`backend/src/ticketing/booking/create.py:248`。
- **副本去重與遺失**　API 附上 boot UUID 與 interval number，經 Redis Stream 送監控樣本。Collector 在 PG 同一交易插入 `telemetry_ingest`，只對新 `(boot_uuid, interval_no)` 累加 histogram 和業務計數，提交後 `XACK`。重啟以 `XAUTOCLAIM` 領回 pending。API flush 前 crash、XADD 失敗或 stream 修剪仍可丟資料，`completeness` 和 `late_or_missing` 依 producer heartbeat 和回報數揭露缺口。Edge 以 device/inode 的 checkpoint 與日誌同交易提交。見 `backend/src/ticketing/telemetry/api_setup.py:57`、`backend/src/ticketing/telemetry/api_setup.py:76`、`backend/src/ticketing/telemetry/app_metrics.py:109`、`backend/src/ticketing/telemetry/app_metrics.py:194`、`backend/src/ticketing/telemetry/app_metrics.py:241`、`backend/src/ticketing/telemetry/app_metrics.py:284`、`backend/src/ticketing/telemetry/collector.py:153`、`backend/src/ticketing/telemetry/logs.py:266`。
- **一個副本牆鐘慢 20 秒**　以 Redis Stream ID 的接收時間減 producer 單調時鐘，取 60 秒樣本的最小偏移來分桶。只慢 API 的 wall clock 時，該副本的量仍落在校正後的秒，圖表的 `clock_skew_ms` 約為 20,000，nginx 的秒不受它影響。網路抖動、首次校正資料不足和秒邊界仍可能使樣本落到相鄰桶。見 `backend/src/ticketing/telemetry/app_metrics.py:96`、`backend/src/ticketing/telemetry/app_metrics.py:164`、`backend/src/ticketing/telemetry/app_metrics.py:190`、`backend/src/ticketing/telemetry/app_metrics.py:235`、`backend/src/ticketing/telemetry/collector.py:60`。
- **來源衝突**　入口 HTTP 結果與耗時以 nginx 存取日誌為準。該日誌可觀察 proxy 逾時及排隊影響，並以 Edge log status 499 記錄 client disconnect。查特定 API handler 或 hold 結果使用 App 路由與業務資料，先看 completeness。訂單成交與座位歸屬以 PostgreSQL 交易為準。對客戶端連線或畫面體感用 k6／SSE probe 的樣本，口徑見 [LOADTEST.md](LOADTEST.md)。

**驗證**　`test_edge_event_time_grace_sse_exclusion_and_connection_sample`、`test_minimum_delay_dedupe_wall_skew_completeness_and_gauge`、`test_rule_fires_and_resolves`、`test_admin_metric_history_streams_and_openapi`。見 `backend/tests/telemetry/test_logs.py:104`、`backend/tests/telemetry/test_app_metrics.py:39`、`backend/tests/telemetry/test_alerts.py:30`、`backend/tests/telemetry/test_admin.py:154`。

**證據**　時鐘測試注入慢 20 秒的 producer wall timestamp，驗證校正分桶及 `20_000` ms skew。監控合約另見 [OpenAPI](docs/openapi.json)（`docs/openapi.json:5054`）與 [telemetry README](backend/src/ticketing/telemetry/README.md)。本文件的 error rate 採目前監控查詢的 5xx 定義。

## 替代方案與取捨

| 選擇 | 取捨 |
|---|---|
| PostgreSQL 短交易、row lock 與 unique index | 指定席以 NOWAIT 回業務衝突，區域配位跳過被鎖列。座位與配額共用 DB 原子性。負載過高時 pool 或 DB 逾時可回 503。見 `backend/src/ticketing/booking/create.py:48`、`backend/src/ticketing/booking/create.py:70`、`backend/src/ticketing/booking/database.py:15`。 |
| 座位推送由原案 Redis Streams／generation 改為 PostgreSQL relay + polling | DB 保存 seq 與可 replay log，省掉第二份座位事件游標與 generation 切換。成本是每個活躍 event／API process 的 DB polling、replay 讀取與 relay 延遲。Redis Stream 用於 App telemetry。見 `docs/development/workstreams.md:44`、`backend/src/ticketing/realtime/reader.py:161`、`backend/src/ticketing/telemetry/api_setup.py:95`。 |
| Cookie session 即時查 DB | 撤銷對新的授權讀取立即生效，成本是每個受保護請求的 acquire 與查詢。串流以通知配合週期查詢。 |
| 期限以本地 DB 決策為準 | 期限前扣款、期限後才由平台看到時可能退款。在席位已釋放後仍能安全收斂。 |
| 匯入採 lease、fence 與 staging | 中途 crash 可重跑，寫入正式席位前可再驗權限。重跑需重讀檔案，租約到期前需等候。前台查 job 狀態及當前 attempt 的錯誤 CSV。見 `docs/development/workstreams.md:46`、`backend/src/ticketing/imports/runner.py:37`、`backend/src/ticketing/api/routes/imports.py:263`。 |
| Excel 採 server-side cursor 與 constant-memory 寫檔 | 降低大量明細的匿名記憶體用量，成本是暫存磁碟、持續 DB 讀取與長 HTTP 下載。見 `backend/src/ticketing/reports/sales.py:30`、`backend/src/ticketing/reports/sales.py:289`、[Excel 匯出量測](docs/evidence/cp4-excel-500k.md)。 |

## 驗證對照表

| 主張 | 入口 |
|---|---|
| Q1–Q3 座位、期限、付款、配額與冪等不變式 | PostgreSQL 整合與併發測試（`backend/tests/booking/`、`backend/tests/payments/`）、[破壞驗證](docs/evidence/break-it.txt)、[壓測後 SQL](loadtest/consistency.sql)。 |
| Q4 跨 reader 序列一致、斷線可補送、慢客戶端隔離 | `backend/tests/realtime/`、[Scenario B 本機紀錄](loadtest/scenario_b.md#local-measurements)、[正式結果欄位](LOADTEST.md#3-scenario-b)。 |
| Q5 授權、cookie 撤銷、匯入取消 | `backend/tests/authz/`、`backend/tests/integration/test_auth.py`、`backend/tests/integration/test_admin_users.py`、`backend/tests/imports/test_imports.py`。 |
| Q6 分桶、去重、延遲與告警 | `backend/tests/telemetry/`、`backend/tests/realtime/test_api.py:681`。 |
| 完整測試與端到端驗證 | `make test` 經 `compose.test.yaml` 跑 PostgreSQL 整合與前端測試（`Makefile:22`、`compose.test.yaml:6`）。`scripts/e2e_flow.py` 走匯入、發布、鎖位與模擬付款（`scripts/e2e_flow.py:91`、`scripts/e2e_flow.py:140`）。 |
