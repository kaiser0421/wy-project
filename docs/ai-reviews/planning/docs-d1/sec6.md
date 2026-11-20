## 6. 壓測後一致性檢查

**流程**：k6 結束 → drain 到定義好的 checkpoint（無 in-flight 請求；outbox 全部指派且投遞；reconciler 至少完成一輪；payment-mock 回呼佇列清空或已記錄；collector 追上 log 檔尾）→ 執行 `loadtest/consistency.sql` 與非 SQL 檢查 → 輸出貼於下方。drain 判定條件與等待時間：TBD(T9.3)。須在 idempotency key 清理（24h）之前執行。

> 以下 SQL 為檢查**語意**的參考寫法（依 `PLAN.md` §4 欄位），最終以 `loadtest/consistency.sql` 為準（TBD(T2.9)）。`:e` = 壓測活動 id。所有「預期 0 列」的查詢回傳任何列即為失敗。狀態值、`operation` 名稱、`resource_ref` 格式依實作（T2.3、T3.x）調整。

### C0. 前置檢查（防止「空集合通過」）— 預期全部為 true

```sql
-- :expected_seats 由 seed 產生的 fixture manifest 傳入（情境 A = 10000；B / CP 依各自 fixture）
SELECT (SELECT count(*) = 1 FROM events WHERE id = :e AND status = 'published')       AS event_ok,
       (SELECT count(*) FROM seats WHERE event_id = :e) = :expected_seats             AS seat_count_ok,
       (SELECT count(*) > 0 FROM orders WHERE event_id = :e)                           AS has_orders;
-- 僅在 relay 已存在的執行（P4 之後：情境 A/B 完整版、CP2）才檢查；CP1 標 N/A
SELECT count(*) = 1 AS stream_state_ok FROM event_stream_state WHERE event_id = :e;
```
實際輸出：`TBD(T9.3)`

### C1. 無座位雙賣（每座位最多 1 個 active claim）— 預期 0 列

```sql
SELECT seat_id, count(*) FROM order_items
 WHERE event_id = :e AND released_at IS NULL
 GROUP BY seat_id HAVING count(*) > 1;
```
實際輸出：`TBD(T9.3)`

### C2. 座位狀態 ↔ 訂單項目一致（不變式 1）— 預期 0 列

```sql
-- available：0 個 active item 且 order_id IS NULL；held/sold：恰 1 個 active item，且 item.order_id、
-- item.event_id、item.zone_id 與座位一致（null-safe 比較，缺 order 連結也會被抓到）
SELECT s.id, s.status, s.order_id,
       count(oi.seat_id) AS active_items,
       bool_and(oi.order_id IS NOT DISTINCT FROM s.order_id
                AND oi.event_id = s.event_id AND oi.zone_id = s.zone_id) AS links_ok
  FROM seats s
  LEFT JOIN order_items oi ON oi.seat_id = s.id AND oi.released_at IS NULL
 WHERE s.event_id = :e
 GROUP BY s.id, s.status, s.order_id
HAVING (s.status = 'available' AND (count(oi.seat_id) <> 0 OR s.order_id IS NOT NULL))
    OR (s.status IN ('held','sold') AND (s.order_id IS NULL OR count(oi.seat_id) <> 1
                                         OR NOT coalesce(bool_and(oi.order_id IS NOT DISTINCT FROM s.order_id
                                                         AND oi.event_id = s.event_id AND oi.zone_id = s.zone_id), false)));
```
實際輸出：`TBD(T9.3)`

### C2b. 以訂單項目為起點的反向連結檢查 — 預期 0 列

```sql
-- 本活動訂單的每個 item：座位存在、座位屬同一活動、zone 屬同一活動、item.event_id = 訂單 event_id；
-- active item 指向的座位必須反指回同一訂單（抓出「本活動訂單佔了別的活動座位」等 C2/C3 從座位出發看不到的情況）
SELECT oi.order_id, oi.seat_id, oi.released_at, s.event_id AS seat_event, s.order_id AS seat_order, z.event_id AS zone_event
  FROM orders o
  JOIN order_items oi ON oi.order_id = o.id
  LEFT JOIN seats s ON s.id = oi.seat_id
  LEFT JOIN zones z ON z.id = oi.zone_id
 WHERE o.event_id = :e
   AND (s.id IS NULL OR s.event_id <> o.event_id OR oi.event_id <> o.event_id
        OR z.id IS NULL OR z.event_id <> o.event_id OR s.zone_id <> oi.zone_id
        OR (oi.released_at IS NULL AND s.order_id IS DISTINCT FROM o.id));
```
實際輸出：`TBD(T9.3)`

### C3. 座位狀態 ↔ 訂單狀態一致 — 預期 0 列

```sql
-- held ⇔ pending_payment；sold ⇔ paid / refund_pending；LEFT JOIN 抓出指向不存在或他活動訂單的座位
SELECT s.id, s.status, s.order_id, o.status AS order_status, o.event_id AS order_event
  FROM seats s LEFT JOIN orders o ON o.id = s.order_id
 WHERE s.event_id = :e AND s.status IN ('held','sold')
   AND (o.id IS NULL OR o.event_id <> s.event_id
        OR NOT ((s.status = 'held' AND o.status = 'pending_payment')
             OR (s.status = 'sold' AND o.status IN ('paid','refund_pending'))));
```
實際輸出：`TBD(T9.3)`

### C4. 訂單 active / 歷史 item 數（不變式 2）— 預期 0 列

```sql
SELECT o.id, o.status, o.quantity,
       count(oi.seat_id)                                         AS all_items,
       count(oi.seat_id) FILTER (WHERE oi.released_at IS NULL)   AS active_items
  FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id
 WHERE o.event_id = :e
 GROUP BY o.id, o.status, o.quantity
HAVING count(oi.seat_id) <> o.quantity                                                     -- 歷史 item 數 = quantity
    OR (o.status IN ('pending_payment','paid','refund_pending')
        AND count(oi.seat_id) FILTER (WHERE oi.released_at IS NULL) <> o.quantity)
    OR (o.status NOT IN ('pending_payment','paid','refund_pending')
        AND count(oi.seat_id) FILTER (WHERE oi.released_at IS NULL) <> 0);
```
實際輸出：`TBD(T9.3)`

### C5. 配額正確且無人超過 4 張（不變式 3）— 預期 0 列

```sql
SELECT q.user_id, q.reserved, coalesce(h.held_qty, 0) AS held_qty
  FROM user_event_quota q
  LEFT JOIN (SELECT user_id, sum(quantity) AS held_qty FROM orders
              WHERE event_id = :e AND status IN ('pending_payment','paid','refund_pending')
              GROUP BY user_id) h ON h.user_id = q.user_id
 WHERE q.event_id = :e
   AND (q.reserved <> coalesce(h.held_qty, 0) OR q.reserved > 4);
-- 反向：持有座位卻沒有 quota 列的使用者
SELECT user_id FROM orders WHERE event_id = :e AND status IN ('pending_payment','paid','refund_pending')
EXCEPT SELECT user_id FROM user_event_quota WHERE event_id = :e;
```
實際輸出：`TBD(T9.3)`

### C6. 金額 = 票價和 + 5% 服務費（四捨五入 half-up）— 預期 0 列

```sql
SELECT o.id, o.subtotal_twd, o.fee_twd, o.total_twd, sum(oi.unit_price_twd) AS items_sum, count(oi.seat_id) AS n
  FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id
 WHERE o.event_id = :e
 GROUP BY o.id
HAVING count(oi.seat_id) = 0                                   -- 沒有任何 item 的訂單
    OR o.subtotal_twd IS DISTINCT FROM sum(oi.unit_price_twd)
    OR o.fee_twd     IS DISTINCT FROM (o.subtotal_twd * 5 + 50) / 100
    OR o.total_twd   IS DISTINCT FROM o.subtotal_twd + o.fee_twd;
-- 單價快照 = 區域票價（發布後票價凍結）；LEFT JOIN 抓出 zone 遺失的 item
SELECT oi.order_id, oi.seat_id, oi.zone_id FROM order_items oi LEFT JOIN zones z ON z.id = oi.zone_id AND z.event_id = oi.event_id
 WHERE oi.event_id = :e AND (z.id IS NULL OR oi.unit_price_twd <> z.price_twd);
```
實際輸出：`TBD(T9.3)`

### C7. 無過期未釋放的 hold — 預期 0 列

```sql
SELECT id, expires_at FROM orders
 WHERE event_id = :e AND status = 'pending_payment'
   AND expires_at <= app_now() - interval '30 seconds';   -- 30s = 積壓寬限（與 HOLD_EXPIRY_BACKLOG 相同），使用 DB 時鐘 app_now()
```
實際輸出：`TBD(T9.3)`
註：主跑 TTL 600s 長於量測窗，上式只驗證「無積壓」，**不能**證明逾時路徑有效。因此另跑一輪**短 TTL 輪**（或在 test image 以 `test_clock` 推進時間）：k6 記錄刻意「保留不動」的訂單 id 清單（載入暫存表 `lt_held_orders(order_id)`），drain 後斷言：

```sql
-- C7b 預期 0 列：每筆刻意保留的訂單都已 expired，座位已釋放，且其使用者的配額已扣回（C5 同時須通過）
SELECT h.order_id, o.status,
       (SELECT count(*) FROM seats s WHERE s.order_id = o.id)                            AS seats_still_linked,
       (SELECT count(*) FROM order_items oi WHERE oi.order_id = o.id AND oi.released_at IS NULL) AS active_items
  FROM lt_held_orders h LEFT JOIN orders o ON o.id = h.order_id
 WHERE o.id IS NULL OR o.status <> 'expired'
    OR EXISTS (SELECT 1 FROM seats s WHERE s.order_id = o.id)
    OR EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.id AND oi.released_at IS NULL);
```
實際輸出（短 TTL 輪）：`TBD(T9.3)`

### C8. 金流 capture / refund（不變式 4）— 預期 0 列

```sql
-- C8a accepted capture 只能屬於 paid / refund_pending / refunded 訂單；
--     refund_pending 必須有 refund 列；refunded 的 refund 必須已 succeeded；paid 不得有 refund 列
SELECT c.provider_capture_id, o.id, o.status, r.status AS refund_status FROM payment_captures c
  JOIN orders o ON o.id = c.order_id
  LEFT JOIN refunds r ON r.provider_capture_id = c.provider_capture_id
 WHERE o.event_id = :e AND c.disposition = 'accepted'
   AND (o.status NOT IN ('paid','refund_pending','refunded')
        OR (o.status = 'paid' AND r.id IS NOT NULL)
        OR (o.status = 'refund_pending' AND r.id IS NULL)
        OR (o.status = 'refunded' AND r.status IS DISTINCT FROM 'succeeded'));
-- C8b refund_required capture 必須有 refund 列
SELECT c.provider_capture_id FROM payment_captures c JOIN orders o ON o.id = c.order_id
  LEFT JOIN refunds r ON r.provider_capture_id = c.provider_capture_id
 WHERE o.event_id = :e AND c.disposition = 'refund_required' AND r.id IS NULL;
-- C8c paid / refund_pending / refunded 訂單恰有 1 筆 accepted capture
SELECT o.id, o.status, count(c.provider_capture_id) FROM orders o
  LEFT JOIN payment_captures c ON c.order_id = o.id AND c.disposition = 'accepted'
 WHERE o.event_id = :e AND o.status IN ('paid','refund_pending','refunded')
 GROUP BY o.id, o.status HAVING count(c.provider_capture_id) <> 1;
```
非 SQL（C8d）：payment-mock 端此活動相關 attempt 的所有 capture id 集合 = 本地 `payment_captures` 集合（`report.py` 比對）。
實際輸出：`TBD(T9.3)`

### C9. 所有付款工作為終態、處理中或已排程 — 預期 0 列

判定原則：每筆非終態工作必須「在 reconciler / 退款 worker 的期限內」——超過期限仍未推進即視為卡住。期限以 PLAN §5.4 的 reconciler 週期（15s）推導，`:grace` 暫定 30 秒；狀態值與欄位名稱以 T3.2 / T3.4 為準。

```sql
-- C9a attempt：creating 超過 reconciler 週期 + 寬限仍未推進 → 卡住；
--      awaiting_customer 只能屬於仍 pending_payment 的訂單，且超過寬限未被輪詢（updated_at / last_polled_at，欄位 TBD）→ 卡住
SELECT a.id, a.status, o.status FROM payment_attempts a JOIN orders o ON o.id = a.order_id
 WHERE o.event_id = :e
   AND ((a.status = 'creating' AND a.created_at < app_now() - :grace)
        OR (a.status = 'awaiting_customer'
            AND (o.status <> 'pending_payment' OR a.updated_at < app_now() - :grace)));
-- C9b refund（限定本活動）：未 succeeded 者必須已排程且未逾期
SELECT r.id, r.status, r.next_try_at FROM refunds r
  JOIN payment_captures c ON c.provider_capture_id = r.provider_capture_id
  JOIN orders o ON o.id = c.order_id
 WHERE o.event_id = :e AND r.status <> 'succeeded'
   AND (r.next_try_at IS NULL OR r.next_try_at < app_now() - :grace);
-- C9c provider_cancellations（限定本活動）：同上規則。PLAN §4 尚未定義此表 schema → 查詢於 T3.4 補上；未補前此項標「未驗證」，不得標通過
```
實際輸出：`TBD(T9.3)`

### C10. Outbox 全部指派且投遞、seq 連續無洞（不變式 5）

```sql
-- 預期 0
SELECT count(*) AS unassigned FROM outbox WHERE event_id = :e AND seq IS NULL;
-- 預期 0 列：stream state 存在；分「空」與「非空」兩個分支判定
--   空（n = 0）：兩個 checkpoint 都必須是初始值（以 T4.1 為準，暫定 0）
--   非空：min = 1、max = n（連續無洞）、last_assigned_seq = last_delivered_seq = max
SELECT st.event_id, st.last_assigned_seq, st.last_delivered_seq, o.max_seq, o.n, o.min_seq
  FROM event_stream_state st
  CROSS JOIN LATERAL (SELECT max(seq) AS max_seq, min(seq) AS min_seq, count(*) AS n
                        FROM outbox WHERE event_id = :e AND seq IS NOT NULL) o
 WHERE st.event_id = :e
   AND CASE WHEN o.n = 0
            THEN NOT (coalesce(st.last_assigned_seq, 0) = 0 AND coalesce(st.last_delivered_seq, 0) = 0)
            ELSE (o.min_seq <> 1 OR o.max_seq <> o.n
                  OR st.last_assigned_seq IS DISTINCT FROM o.max_seq
                  OR st.last_delivered_seq IS DISTINCT FROM o.max_seq)
       END;
-- 若 outbox 有保留期清理，連續性改比對保留區間（TBD(T4.1)）
```
非 SQL：目前 generation（`event_stream_state.gen`）的 Redis stream tail（`XREVRANGE seat:{gen}:{event} + - COUNT 1` 的 ID `<seq>-0`）= `last_delivered_seq`。非空時沒有 stream 或 tail → 失敗；空分支只在 fixture 證明期間沒有任何座位變化（C0 之外再核對 `orders` 為 0 筆）且 Redis 無 tail 時允許。
實際輸出：`TBD(T9.3)`

### C11. seat_events ↔ outbox 一致（多重集合比對）— 預期兩個方向皆 0 列

```sql
WITH expanded AS (
  SELECT o.event_id, o.seq, (c->>0)::int AS seat_idx, c->>1 AS status, (c->>2)::int AS version
    FROM outbox o CROSS JOIN LATERAL jsonb_array_elements(o.changes) c
   WHERE o.event_id = :e AND o.seq IS NOT NULL AND o.kind <> 'layout_changed'   -- layout 事件不展開為座位事件（kind 名稱以 T4.1 為準）
), archived AS (
  SELECT event_id, seq, seat_idx, status, seat_version AS version FROM seat_events WHERE event_id = :e
)
(SELECT 'missing_in_seat_events' AS side, * FROM (SELECT * FROM expanded EXCEPT ALL SELECT * FROM archived) x)
UNION ALL
(SELECT 'extra_in_seat_events', * FROM (SELECT * FROM archived EXCEPT ALL SELECT * FROM expanded) y);
```
實際輸出：`TBD(T9.3)`

### C12. 重送沒有產生重複訂單 — 預期 0 列

以 k6 產生的 **run manifest** 為起點（不掃描全域 key）：k6 對每個收到 201 的下單記錄 `(user_id, key, event_id, order_public_id, resend_order_public_id)`（未重送者最後一欄為 NULL），`run.sh` 將其載入暫存表 `lt_manifest`。

```sql
-- C12a 預期 0 列：manifest 每一列 ↔ 恰 1 筆訂單（同使用者、同活動）↔ 恰 1 個 completed/201 的 key，
--       key 的 resource_ref 指向同一訂單；有重送者兩次 order id 必須相同
SELECT m.user_id, m.key, m.order_public_id, m.resend_order_public_id,
       count(DISTINCT o.id) AS orders, count(DISTINCT k.key) AS keys
  FROM lt_manifest m
  LEFT JOIN orders o ON o.public_id = m.order_public_id AND o.user_id = m.user_id AND o.event_id = m.event_id
  LEFT JOIN idempotency_keys k ON k.user_id = m.user_id AND k.key = m.key
        AND k.operation = 'create_order' AND k.status = 'completed' AND k.response_status = 201
        AND k.resource_ref = m.order_public_id::text
 GROUP BY m.user_id, m.key, m.order_public_id, m.resend_order_public_id
HAVING count(DISTINCT o.id) <> 1 OR count(DISTINCT k.key) <> 1
    OR (m.resend_order_public_id IS NOT NULL AND m.resend_order_public_id <> m.order_public_id);
-- C12b 預期 0 列：本活動在 [t_start, t_end] 內建立的訂單都出現在 manifest（沒有「多出來」的重複訂單）
SELECT o.id, o.public_id FROM orders o
 WHERE o.event_id = :e AND o.created_at >= :t_start AND o.created_at < :t_end
   AND NOT EXISTS (SELECT 1 FROM lt_manifest m WHERE m.order_public_id = o.public_id);
```
註：k6 若因逾時沒收到 201（但伺服器已提交），該訂單不在 manifest 中，C12b 會列出；此類列需以 k6 的逾時紀錄（同 key 之後的重送結果）逐筆解釋，不得直接忽略。
實際輸出：`TBD(T9.3)`

### C13. SSE 探針 hash = PG hash（情境 B）— 預期相等

在 drain 後的同一個 `(gen, layout_ver, last_assigned_seq)` 下比較：PG 端以 `seat_idx` 排序的 `(seat_idx, status, version)` 序列，以固定的 canonical 編碼（TBD(T4.7)，例如每列 `idx,status,version\n` 的 UTF-8）做 SHA-256；探針 client 對本地狀態做相同計算，並回報自己的 `(gen, layout_ver, applied_seq)`。三元組不一致時先等待追上，不可直接比較。
實際輸出：`TBD(T9.3)`

### C14. 邊界 vs k6 請求數 — 預期差異 < 1%

依 §2.4 規則：只比 k6 母體，排除 healthcheck、回呼、串流；collector drain 後比對；任一側為 0 即失敗。情境 A 與 B（B 比對 writer 與快照請求）都要跑。

- 母體識別：k6 每個請求帶 `X-Load-Run: <run_id>`（暫定），nginx 把該值**原樣**寫入 access log 欄位（名稱 TBD(T7.2)），以**等值**比對，不用前綴 `LIKE`（避免混到其他 run）。
- 時間窗：雙方都以「請求完成時間 ∈ [t_start, t_end)」計數（nginx `$msec`；k6 以樣本結束時間）。
- 路由排除兩側一致：`/api/v1/events/{id}/seat-stream`、`/healthz/*`、`/api/v1/payments/mockpay/callbacks`。
- 應用層欄：以 `request_id` 將 edge 母體對到應用層紀錄（`service_logs` / app 端每請求紀錄，TBD(T7.1)），只計對得上的請求；對不上的數量另列。

```sql
SELECT count(*) FROM access_logs
 WHERE ts >= :t_start AND ts < :t_end
   AND load_run = :run_id                                 -- 欄位名稱以 T7.2 為準
   AND route NOT LIKE '%/seat-stream' AND route NOT LIKE '/healthz/%'
   AND route <> '/api/v1/payments/mockpay/callbacks';
```
實際輸出：`TBD(T9.3)`

### 一致性結果彙總

| 檢查 | 情境 A | 情境 B |
|---|---|---|
| C0 前置檢查（stream state 僅 P4 之後） | TBD(T9.3) | TBD(T9.3) |
| C1 無雙賣 | TBD(T9.3) | TBD(T9.3) |
| C2 座位 ↔ 項目 | TBD(T9.3) | TBD(T9.3) |
| C2b 項目 → 座位 / 區域反向連結 | TBD(T9.3) | TBD(T9.3) |
| C3 座位 ↔ 訂單狀態 | TBD(T9.3) | TBD(T9.3) |
| C4 訂單項目數 | TBD(T9.3) | TBD(T9.3) |
| C5 配額 / ≤ 4 張 | TBD(T9.3) | TBD(T9.3) |
| C6 金額 | TBD(T9.3) | TBD(T9.3) |
| C7 無過期 hold 積壓 | TBD(T9.3) | TBD(T9.3) |
| C7b 短 TTL 輪：刻意保留的訂單全部 expired 並釋放 | TBD(T9.3) | – |
| C8 capture / refund | TBD(T9.3) | TBD(T9.3) |
| C9 付款工作狀態 | TBD(T9.3) | TBD(T9.3) |
| C10 outbox / seq / Redis tail | TBD(T9.3) | TBD(T9.3) |
| C11 seat_events 多重集合 | TBD(T9.3) | TBD(T9.3) |
| C12 冪等重送 | TBD(T9.3) | TBD(T9.3) |
| C13 SSE hash | – | TBD(T9.3) |
| C14 edge vs k6（B：writer + 快照請求） | TBD(T9.3) | TBD(T9.3) |

---

