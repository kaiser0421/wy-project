-- Run after draining provider work and the realtime relay.
-- psql -v event_id=42 -v expected_seats=10000 -f loadtest/consistency.sql
\set ON_ERROR_STOP on
\if :{?event_id}
\else
\set event_id NULL
\endif
\if :{?expected_seats}
\else
\set expected_seats NULL
\endif
\if :{?check_realtime}
\else
\set check_realtime true
\endif

WITH scope AS (
    SELECT id FROM events WHERE (:event_id IS NULL OR id = :event_id)
),
scoped_orders AS (
    SELECT o.* FROM orders o JOIN scope e ON e.id=o.event_id
),
claims AS (
    SELECT s.id, s.status, count(i.seat_id) AS active,
           count(*) FILTER (
               WHERE (i.order_id,i.event_id,i.zone_id)=(s.order_id,s.event_id,s.zone_id)
                 AND ((s.status='held' AND o.status='pending_payment') OR
                      (s.status='sold' AND o.status IN ('paid','refund_pending')))
           ) AS matching
    FROM seats s JOIN scope e ON e.id=s.event_id
    LEFT JOIN order_items i ON i.seat_id=s.id AND i.released_at IS NULL
    LEFT JOIN orders o ON o.id=i.order_id
    GROUP BY s.id
),
expected_quota AS (
    SELECT user_id,event_id,sum(quantity) AS reserved FROM scoped_orders
    WHERE status IN ('pending_payment','paid','refund_pending') GROUP BY user_id,event_id
),
actual_quota AS (
    SELECT q.* FROM user_event_quota q JOIN scope e ON e.id=q.event_id
),
ledger AS (
    SELECT c.provider_capture_id,c.disposition,o.status,o.total_twd,c.amount_twd,
           r.status AS refund_status,r.reason
    FROM payment_captures c JOIN scoped_orders o ON o.id=c.order_id
    LEFT JOIN refunds r USING (provider_capture_id)
),
event_sequences AS (
    SELECT o.event_id,count(*) AS n,min(seq) AS lo,max(seq) AS hi
    FROM outbox o JOIN scope e ON e.id=o.event_id WHERE o.seq IS NOT NULL
    GROUP BY o.event_id
),
expanded AS (
    SELECT o.event_id,o.seq,(change->>0)::int AS seat_idx,change->>1 AS status,
           (change->>2)::int AS seat_version
    FROM outbox o JOIN scope e ON e.id=o.event_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN o.kind='seat_changes' THEN o.changes ELSE '[]'::jsonb END
    ) change
    WHERE :check_realtime AND o.seq IS NOT NULL
),
archived AS (
    SELECT s.event_id,s.seq,s.seat_idx,s.status,s.seat_version
    FROM seat_events s JOIN scope e ON e.id=s.event_id WHERE :check_realtime
),
missing_archived AS (SELECT * FROM expanded EXCEPT ALL SELECT * FROM archived),
extra_archived AS (SELECT * FROM archived EXCEPT ALL SELECT * FROM expanded)
SELECT 'published_event_missing' AS invariant, (:event_id)::text AS subject
WHERE :event_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM events WHERE id=:event_id AND status='published')
UNION ALL
SELECT 'unexpected_inventory_size', (:event_id)::text
WHERE :expected_seats IS NOT NULL AND :expected_seats <>
    (SELECT count(*) FROM seats s JOIN scope e ON e.id=s.event_id)
UNION ALL
SELECT 'seat_active_claims',id::text FROM claims
WHERE (status='available' AND active<>0) OR (status<>'available' AND (active<>1 OR matching<>1))
UNION ALL
SELECT 'order_item_link', i.order_id::text || '/' || i.seat_id::text
FROM scoped_orders o JOIN order_items i ON i.order_id=o.id
LEFT JOIN seats s ON s.id=i.seat_id
LEFT JOIN zones z ON z.id=i.zone_id
WHERE i.event_id<>o.event_id OR s.event_id IS DISTINCT FROM i.event_id
   OR z.event_id IS DISTINCT FROM i.event_id OR s.zone_id IS DISTINCT FROM i.zone_id
   OR (i.released_at IS NULL AND s.order_id IS DISTINCT FROM i.order_id)
   OR i.unit_price_twd IS DISTINCT FROM z.price_twd
UNION ALL
SELECT 'order_items_or_amount',o.id::text FROM scoped_orders o
LEFT JOIN order_items i ON i.order_id=o.id
GROUP BY o.id,o.status,o.quantity,o.subtotal_twd,o.fee_twd,o.total_twd
HAVING count(i.seat_id)<>o.quantity OR count(i.seat_id) FILTER (WHERE i.released_at IS NULL)<>
    CASE WHEN o.status IN ('pending_payment','paid','refund_pending') THEN o.quantity ELSE 0 END
    OR o.subtotal_twd IS DISTINCT FROM sum(i.unit_price_twd)
    OR o.fee_twd IS DISTINCT FROM (o.subtotal_twd*5+50)/100
    OR o.total_twd IS DISTINCT FROM o.subtotal_twd+o.fee_twd
UNION ALL
SELECT 'user_event_quota',concat(user_id,'/',event_id)
FROM expected_quota q FULL JOIN actual_quota a USING (user_id,event_id)
WHERE coalesce(q.reserved,0)<>coalesce(a.reserved,0)
   OR coalesce(a.reserved,0) NOT BETWEEN 0 AND 4 OR q.reserved>4
UNION ALL
SELECT 'overdue_hold',id::text FROM scoped_orders
WHERE status='pending_payment' AND expires_at<=app_now()-interval '30 seconds'
UNION ALL
SELECT 'capture_refund_ledger',provider_capture_id FROM ledger
WHERE amount_twd<>total_twd
   OR (disposition='accepted' AND (
       status NOT IN ('paid','refund_pending','refunded')
       OR (status='paid' AND refund_status IS NOT NULL)
       OR (status='refund_pending' AND (refund_status IS NULL OR refund_status='succeeded'))
       OR (status='refunded' AND refund_status IS DISTINCT FROM 'succeeded')
       OR (refund_status IS NOT NULL AND reason IS DISTINCT FROM 'order_cancelled')
   ))
   OR (disposition='refund_required' AND (
       refund_status IS NULL OR reason NOT IN ('late_capture','duplicate_capture')
   ))
UNION ALL
SELECT 'accepted_capture_count',o.id::text FROM scoped_orders o
LEFT JOIN payment_captures c ON c.order_id=o.id AND c.disposition='accepted'
GROUP BY o.id,o.status
HAVING count(c.provider_capture_id) <>
  CASE WHEN o.status IN ('paid','refund_pending','refunded') THEN 1 ELSE 0 END
UNION ALL
SELECT 'attempt_stalled',p.id::text FROM payment_attempts p JOIN scoped_orders o ON p.order_id=o.id
WHERE (p.status='creating' AND p.created_at<app_now()-interval '30 seconds')
   OR (p.status='awaiting_customer' AND (
       coalesce(p.last_polled_at,p.updated_at)<app_now()-interval '30 seconds'
       OR (o.status<>'pending_payment' AND NOT EXISTS (
           SELECT 1 FROM provider_cancellations w
           WHERE w.attempt_id=p.id AND w.status<>'succeeded'
       ))
   ))
UNION ALL
SELECT 'refund_stalled',r.id::text FROM refunds r JOIN payment_captures c USING (provider_capture_id)
JOIN scoped_orders o ON o.id=c.order_id
WHERE r.status<>'succeeded' AND coalesce(r.lease_until,'-infinity'::timestamptz)<=app_now()
  AND r.next_try_at<app_now()-interval '30 seconds'
UNION ALL
SELECT 'cancellation_stalled',w.attempt_id::text
FROM provider_cancellations w JOIN payment_attempts p ON p.id=w.attempt_id
JOIN scoped_orders o ON o.id=p.order_id
WHERE w.status<>'succeeded' AND coalesce(w.lease_until,'-infinity'::timestamptz)<=app_now()
  AND w.next_try_at<app_now()-interval '30 seconds'
UNION ALL
SELECT 'booking_replay_record',concat(k.user_id,'/',k.key)
FROM idempotency_keys k
LEFT JOIN orders o ON o.public_id::text=k.response_body->>'public_id' AND o.user_id=k.user_id
WHERE k.operation='create-order' AND k.status='completed'
  AND (:event_id IS NULL OR k.response_body->>'event_id'=(:event_id)::text)
  AND (o.id IS NULL OR k.response_status<>201
       OR k.resource_ref IS DISTINCT FROM o.public_id::text
       OR k.response_headers->>'Location' IS DISTINCT FROM '/api/v1/orders/' || o.public_id::text)
UNION ALL
SELECT 'outbox_unassigned',o.id::text FROM outbox o JOIN scope e ON e.id=o.event_id
WHERE :check_realtime AND o.seq IS NULL
UNION ALL
SELECT 'event_sequence',s.event_id::text FROM event_sequences s
LEFT JOIN event_stream_state st USING (event_id)
WHERE :check_realtime
  AND (s.lo<>1 OR s.hi<>s.n OR st.last_assigned_seq IS DISTINCT FROM s.hi)
UNION ALL
SELECT 'seat_event_missing',concat(event_id,'/',seq,'/',seat_idx) FROM missing_archived
UNION ALL
SELECT 'seat_event_extra',concat(event_id,'/',seq,'/',seat_idx) FROM extra_archived
ORDER BY invariant,subject;
