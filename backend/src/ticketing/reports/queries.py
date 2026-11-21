"""Report projections evaluated in one consistent PostgreSQL snapshot."""

SUMMARY = """
    SELECT state.status::text, coalesce(o.orders, 0), coalesce(o.subtotal, 0),
           coalesce(o.fees, 0), coalesce(o.total, 0)
    FROM unnest(enum_range(NULL::order_status)) AS state(status)
    LEFT JOIN (
        SELECT status, count(*) AS orders, sum(subtotal_twd) AS subtotal,
               sum(fee_twd) AS fees, sum(total_twd) AS total
        FROM orders WHERE event_id = %s GROUP BY status
    ) o ON o.status = state.status
"""

ZONES = """
    WITH inventory AS (
        SELECT zone_id, count(*) AS capacity,
               count(*) FILTER (WHERE status = 'sold') AS sold,
               count(*) FILTER (WHERE status = 'held') AS held,
               count(*) FILTER (WHERE status = 'available') AS available
        FROM seats WHERE event_id = %s GROUP BY zone_id
    ), sales AS (
        SELECT i.zone_id, sum(i.unit_price_twd) AS revenue
        FROM order_items i JOIN orders o ON o.id = i.order_id
        WHERE i.event_id = %s AND o.status = 'paid'
        GROUP BY i.zone_id
    )
    SELECT z.code, z.name, z.price_twd, coalesce(i.capacity, 0), coalesce(i.sold, 0),
           coalesce(i.held, 0), coalesce(i.available, 0), coalesce(s.revenue, 0)
    FROM zones z LEFT JOIN inventory i ON i.zone_id = z.id
    LEFT JOIN sales s ON s.zone_id = z.id
    WHERE z.event_id = %s ORDER BY z.code
"""

ORDERS = """
    SELECT o.public_id, o.created_at, o.paid_at, o.status::text, coalesce(items.labels, ''),
           o.quantity, o.subtotal_twd, o.fee_twd, o.total_twd, u.email::text
    FROM orders o LEFT JOIN users u ON %s AND u.id = o.user_id
    LEFT JOIN LATERAL (
        SELECT string_agg(z.code || ':' || s.row_label || '-' || s.seat_number,
                          ', ' ORDER BY s.seat_idx) AS labels
        FROM order_items i JOIN seats s ON s.id = i.seat_id
        JOIN zones z ON z.id = i.zone_id
        WHERE i.order_id = o.id
    ) items ON true
    WHERE o.event_id = %s ORDER BY o.id
"""
