# Booking load scenario

Prepare a published event with 10,000 seats and a currently open sales window.
Export the seeded inventory as an array of objects with `id` and `zone_code`.
Prepare 3,000 distinct signed-in buyers before the run. Each entry in the session
array has `user_id`, `csrf_token`, and `cookie` (the cookie name and value, such
as `sid=...`, without attributes). Keep the session file outside version control.

```sh
EVENT_ID=42 BASE_URL=http://localhost:8080 RUN_ID=booking-run-42 \
SESSIONS_FILE=/tmp/buyers.json INVENTORY_FILE=/tmp/seats.json \
k6 run --log-format=json loadtest/scenario_a.js
```

The defaults are 3,000 VUs for 60 seconds. Use `VUS` and `DURATION` for a
smoke run. Set `ORIGIN` when the gateway address differs from the configured
browser origin. Each VU is mapped to a different buyer. k6 emits JSON manifests
for successful bookings and unanswered requests. The `scenario_a_outcomes`
metric distinguishes business conflicts from unexpected failures.

After traffic stops, wait for the reconciler and realtime relay to drain. Run
the SQL against the same PostgreSQL database. A consistent run prints zero
rows. On an earlier deployment without the realtime relay, pass
`-v check_realtime=false`.

```sh
psql "$DATABASE_URL" -v event_id=42 -v expected_seats=10000 -f loadtest/consistency.sql
```
