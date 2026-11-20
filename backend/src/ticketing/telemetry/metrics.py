"""Shared aggregation of per-second counts and latency distributions."""

from __future__ import annotations

from datetime import datetime

import asyncpg

from ticketing.telemetry.histogram import BOUNDS, Counts, percentiles

MetricKey = tuple[datetime, str, str]
_UPSERT_METRIC = """
INSERT INTO metrics_1s AS m
    (ts, source, scope, req_total, err_4xx, err_5xx, hist, status_classes,
     p50_ms, p95_ms, p99_ms)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (source, scope, ts) DO UPDATE SET
    req_total = m.req_total + EXCLUDED.req_total,
    err_4xx = m.err_4xx + EXCLUDED.err_4xx,
    err_5xx = m.err_5xx + EXCLUDED.err_5xx,
    status_classes = telemetry_vector_add(m.status_classes, EXCLUDED.status_classes),
    hist = telemetry_vector_add(m.hist, EXCLUDED.hist),
    p50_ms = telemetry_quantile(telemetry_vector_add(m.hist, EXCLUDED.hist), $12, 0.50),
    p95_ms = telemetry_quantile(telemetry_vector_add(m.hist, EXCLUDED.hist), $12, 0.95),
    p99_ms = telemetry_quantile(telemetry_vector_add(m.hist, EXCLUDED.hist), $12, 0.99)
"""


async def write_metrics(conn: asyncpg.Connection, aggregates: dict[MetricKey, Counts]) -> None:
    records = []
    for (ts, source, scope), counts in sorted(aggregates.items()):
        records.append(
            (
                ts,
                source,
                scope,
                sum(counts.status_classes),
                counts.status_classes[4],
                counts.status_classes[5],
                counts.hist,
                counts.status_classes,
                *percentiles(counts.hist),
                BOUNDS,
            )
        )
    await conn.executemany(_UPSERT_METRIC, records)
