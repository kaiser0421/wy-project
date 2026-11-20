-- Run with psql against a disposable, migrated PostgreSQL database.
-- psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/seed_orders_500k.sql
-- Override the default count with -v rows=3000 for a smaller smoke run.
\if :{?rows}
\else
\set rows 500000
\endif

BEGIN;

-- Separate buyers keep active reservations within the per-user limit.
INSERT INTO users(email, password_hash)
VALUES ('sales-seed-owner-' || gen_random_uuid() || '@example.invalid', 'seed-only')
RETURNING id AS owner_id \gset

INSERT INTO events(owner_id, title, description, category, venue, status,
                   sales_start_at, sales_end_at, published_at)
VALUES (:owner_id, 'Sales report seed', 'Synthetic orders', 'music', 'Test venue', 'published',
        '2025-03-01T00:00:00Z', '2025-04-01T00:00:00Z', '2025-02-01T00:00:00Z')
RETURNING id AS event_id \gset

INSERT INTO zones(event_id, code, name, price_twd)
VALUES (:event_id, 'A', 'Floor', 1000)
RETURNING id AS zone_id \gset

CREATE TEMP TABLE sales_seed ON COMMIT DROP AS
SELECT g, nextval('users_id_seq') AS user_id, nextval('orders_id_seq') AS order_id,
       nextval('seats_id_seq') AS seat_id,
       (ARRAY['paid', 'pending_payment', 'expired', 'cancelled', 'refund_pending', 'refunded']
       ::order_status[])[1 + g % 6] AS status,
       '2025-03-09T06:59:59Z'::timestamptz + (g % 2) * interval '1 second' AS created
FROM generate_series(1, :rows) AS series(g);

INSERT INTO users(id, email, password_hash)
SELECT user_id, 'sales-seed-buyer-' || user_id || '@example.invalid', 'seed-only' FROM sales_seed;

INSERT INTO orders(id, user_id, event_id, status, quantity, subtotal_twd, fee_twd, total_twd,
                   created_at, expires_at, paid_at, closed_at)
SELECT order_id, user_id, :event_id, status, 1, 1000, 50, 1050, created,
       CASE WHEN status = 'pending_payment' THEN app_now() + interval '1 day'
            ELSE created + interval '5 minutes' END,
       CASE WHEN status IN ('paid', 'refund_pending', 'refunded') THEN created + interval '1 minute' END,
       CASE WHEN status IN ('expired', 'cancelled', 'refunded') THEN created + interval '5 minutes' END
FROM sales_seed;

INSERT INTO seats(id, event_id, zone_id, row_label, seat_number, norm_key, sort_key,
                  seat_idx, status, order_id)
SELECT seat_id, :event_id, :zone_id, 'R', g::text, 'A|R|' || g, g - 1, g - 1,
       CASE WHEN status = 'pending_payment' THEN 'held'
            WHEN status IN ('paid', 'refund_pending') THEN 'sold' ELSE 'available' END::seat_status,
       CASE WHEN status IN ('paid', 'pending_payment', 'refund_pending') THEN order_id END
FROM sales_seed;

INSERT INTO order_items(order_id, seat_id, event_id, zone_id, unit_price_twd, released_at)
SELECT order_id, seat_id, :event_id, :zone_id, 1000,
       CASE WHEN status IN ('expired', 'cancelled', 'refunded') THEN created + interval '5 minutes' END
FROM sales_seed;

INSERT INTO user_event_quota(user_id, event_id, reserved)
SELECT user_id, :event_id, 1
FROM sales_seed WHERE status IN ('paid', 'pending_payment', 'refund_pending');

COMMIT;
\echo Seeded :rows orders for event :event_id
