#!/usr/bin/env bash
# Load-test kit. Needs only git + Docker (Compose v2). Run from anywhere inside the repository.
#
#   loadtest/run.sh all            # Scenario A + consistency + Scenario B + bundle (default sizes)
#   loadtest/run.sh a              # Scenario A only (10,000 seats, 3,000 VUs, 60 s) + consistency
#   loadtest/run.sh b              # Scenario B only (2,000 SSE subscribers on one event)
#   loadtest/run.sh smoke          # tiny version of both, to check the kit works (~3 minutes)
#
# Tunables (environment): VUS, DURATION, SEATS, CONNECTIONS, B_DURATION, B_RATE, K6_CPUS, OUT
# Results are written to loadtest/out/<timestamp>/ and packed into loadtest/out/<timestamp>.tar.gz
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
MODE=${1:-all}
STAMP=$(date +%Y%m%d-%H%M%S)
OUT=${OUT:-loadtest/out/$STAMP}
mkdir -p "$OUT"
OUT_IN=/lt/${OUT#loadtest/}            # same directory as seen inside containers

if [[ $MODE == smoke ]]; then
  VUS=${VUS:-50}; DURATION=${DURATION:-15s}; SEATS=${SEATS:-1000}
  CONNECTIONS=${CONNECTIONS:-50}; B_DURATION=${B_DURATION:-30}; B_RATE=${B_RATE:-20}
fi
VUS=${VUS:-3000}; DURATION=${DURATION:-60s}; SEATS=${SEATS:-10000}
CONNECTIONS=${CONNECTIONS:-2000}; B_DURATION=${B_DURATION:-80}; B_RATE=${B_RATE:-300}
K6_CPUS=${K6_CPUS:-}                   # e.g. K6_CPUS=2 to cap the generator

log() { printf '\n==> %s\n' "$*" | tee -a "$OUT/run.log"; }

network() {
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' \
    "$(docker compose ps -q nginx)"
}

# Python tools run inside the backend image (it already has the ticketing package and deps).
py() {
  docker compose run --rm --no-deps -T -v "$PWD/loadtest:/lt" -w /lt --entrypoint python api "$@"
}

psql_q() { docker compose exec -T postgres psql -U ticketing -d ticketing "$@"; }

environment() {
  log "Recording environment"
  {
    echo "date: $(date -Iseconds)"
    echo "git: $(git rev-parse HEAD)"
    echo "host: $(uname -a)"
    if command -v nproc >/dev/null; then echo "cpus: $(nproc)"; fi
    if command -v sysctl >/dev/null; then sysctl -n machdep.cpu.brand_string hw.ncpu hw.memsize 2>/dev/null || true; fi
    if [[ -r /proc/cpuinfo ]]; then grep -m1 'model name' /proc/cpuinfo; grep MemTotal /proc/meminfo; fi
    docker info --format 'docker: {{.ServerVersion}} cpus={{.NCPU}} mem={{.MemTotal}} os={{.OperatingSystem}}'
    docker compose version
    echo "VUS=$VUS DURATION=$DURATION SEATS=$SEATS CONNECTIONS=$CONNECTIONS B_DURATION=$B_DURATION B_RATE=$B_RATE"
  } > "$OUT/environment.txt"
}

ready() { curl -fsS "http://127.0.0.1:${HTTP_PORT:-8080}/healthz/live" >/dev/null 2>&1; }

ensure_stack() {
  if ready && [[ ${REBUILD:-0} != 1 ]]; then
    log "Stack already running (set REBUILD=1 to rebuild)"; return
  fi
  log "Starting the stack (docker compose up -d --build)"
  docker compose up -d --build >> "$OUT/run.log" 2>&1
  for _ in $(seq 1 120); do
    if ready; then return; fi
    sleep 2
  done
  echo "stack did not become ready" >&2; exit 1
}

scenario_a() {
  log "Scenario A: preparing event with $SEATS seats and $VUS buyer sessions"
  py prepare_a.py --base http://nginx:8080 --buyers "$VUS" --seats "$SEATS" --out "$OUT_IN/a" \
    | tee -a "$OUT/run.log"
  local event_id
  event_id=$(sed -E 's/.*"event_id": *([0-9]+).*/\1/' "$OUT/a/event.json")
  local started; started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  log "Scenario A: k6 $VUS VUs for $DURATION against event $event_id"
  # shellcheck disable=SC2086
  docker run --rm ${K6_CPUS:+--cpus $K6_CPUS} --network "$(network)" \
    -v "$PWD/loadtest:/lt" -w /lt \
    -e BASE_URL=http://nginx:8080 -e ORIGIN=http://localhost:8080 -e EVENT_ID="$event_id" \
    -e RUN_ID="a-$STAMP" -e VUS="$VUS" -e DURATION="$DURATION" \
    -e SESSIONS_FILE="$OUT_IN/a/sessions.json" -e INVENTORY_FILE="$OUT_IN/a/inventory.json" \
    grafana/k6:0.57.0 run --summary-export="$OUT_IN/a/k6-summary.json" scenario_a.js \
    > "$OUT/a/k6.log" 2>&1 || true
  tail -40 "$OUT/a/k6.log" | tee -a "$OUT/run.log"
  local ended; ended=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  log "Scenario A: waiting for holds, payments and outbox to drain (90 s)"
  sleep 90
  log "Scenario A: consistency checks (zero rows = consistent)"
  psql_q -v event_id="$event_id" -v expected_seats="$SEATS" -f - < loadtest/consistency.sql \
    > "$OUT/a/consistency.txt" 2>&1 || true
  tail -20 "$OUT/a/consistency.txt" | tee -a "$OUT/run.log"
  psql_q -c "SELECT status, count(*) AS orders, sum(quantity) AS tickets FROM orders WHERE event_id=$event_id GROUP BY 1 ORDER BY 1;" \
         -c "SELECT status, count(*) FROM seats WHERE event_id=$event_id GROUP BY 1 ORDER BY 1;" \
    > "$OUT/a/final-state.txt" 2>&1 || true
  cat "$OUT/a/final-state.txt" | tee -a "$OUT/run.log"
  psql_q -c "\copy (SELECT * FROM metrics_1s WHERE ts BETWEEN '$started' AND '$ended'::timestamptz + interval '30 seconds' ORDER BY ts, source) TO STDOUT WITH CSV HEADER" \
    > "$OUT/a/metrics_1s.csv" 2>/dev/null || true
  psql_q -c "\copy (SELECT * FROM business_1s WHERE ts BETWEEN '$started' AND '$ended'::timestamptz + interval '30 seconds' ORDER BY ts) TO STDOUT WITH CSV HEADER" \
    > "$OUT/a/business_1s.csv" 2>/dev/null || true
  docker compose exec -T nginx sh -c "grep -F '\"load_run\":\"a-$STAMP\"' /data/logs/nginx-access.json" \
    > "$OUT/a/nginx-access.json" 2>/dev/null || true
}

scenario_b() {
  mkdir -p "$OUT/b"
  log "Scenario B: seeding event and $CONNECTIONS subscriber sessions"
  py scenario_b_writer.py seed --seats "$SEATS" --connections "$CONNECTIONS" \
    --sessions-file "$OUT_IN/b/sessions.json" | tee "$OUT/b/seed.txt" | tee -a "$OUT/run.log"
  local event_id
  event_id=$(grep -Eo '[Ee]vent[^0-9]*[0-9]+' "$OUT/b/seed.txt" | grep -Eo '[0-9]+' | tail -1)
  log "Scenario B: $CONNECTIONS subscribers for ${B_DURATION}s, writer at ${B_RATE} changes/s (event $event_id)"
  py scenario_b_writer.py write --event-id "$event_id" --workers 4 --rate "$B_RATE" \
    --start-delay 15 --duration $((B_DURATION - 25)) > "$OUT/b/writer.txt" 2>&1 &
  local writer=$!
  py sse_client.py --base-url http://nginx:8080 --event-id "$event_id" \
    --sessions-file "$OUT_IN/b/sessions.json" --connections "$CONNECTIONS" \
    --duration "$B_DURATION" --drain-timeout 20 > "$OUT/b/sse-client.txt" 2>&1 || true
  wait "$writer" || true
  tail -40 "$OUT/b/sse-client.txt" | tee -a "$OUT/run.log"
  tail -10 "$OUT/b/writer.txt" | tee -a "$OUT/run.log"
}

bundle() {
  find "$OUT" -name 'sessions.json' -delete          # live credentials never leave the machine
  tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
  log "Done. Send this file back: $OUT.tar.gz"
}

environment
ensure_stack
case $MODE in
  a) scenario_a ;;
  b) scenario_b ;;
  all|smoke) scenario_a; scenario_b ;;
  *) echo "usage: loadtest/run.sh [all|a|b|smoke]" >&2; exit 2 ;;
esac
bundle
