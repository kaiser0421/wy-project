#!/usr/bin/env bash
# Load-test kit. Needs only git + Docker (Compose v2). Run from anywhere inside the repository.
#
#   loadtest/run.sh all            # Scenario A + consistency + Scenario B + bundle (default sizes)
#   loadtest/run.sh a              # Scenario A only (10,000 seats, 3,000 VUs, 60 s) + consistency
#   loadtest/run.sh b              # Scenario B only (2,000 SSE subscribers on one event)
#   loadtest/run.sh smoke          # tiny version of both, to check the kit works (~3 minutes)
#
# Tunables: API_REPLICAS, VUS, DURATION, SEATS, CONNECTIONS, B_DURATION, B_RATE,
# SSE_PROCESSES, SSE_RAMP, K6_CPUS, OUT
# B_RATE=300 runs the Scenario B stress variant.
# Results are written to loadtest/out/<timestamp>/ and packed into loadtest/out/<timestamp>.tar.gz
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
MODE=${1:-all}
STAMP=$(date +%Y%m%d-%H%M%S)
RUN_STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
OUT=${OUT:-loadtest/out/$STAMP}
mkdir -p "$OUT/a" "$OUT/b"
# Containers that write into loadtest/ run as the invoking user, so Linux bind mounts work too.
LT_USER="$(id -u):$(id -g)"
OUT_IN=/lt/${OUT#loadtest/}            # same directory as seen inside containers

if [[ $MODE == smoke ]]; then
  VUS=${VUS:-50}; DURATION=${DURATION:-15s}; SEATS=${SEATS:-1000}
  CONNECTIONS=${CONNECTIONS:-50}; B_DURATION=${B_DURATION:-30}; B_RATE=${B_RATE:-20}
fi
VUS=${VUS:-3000}; DURATION=${DURATION:-60s}; SEATS=${SEATS:-10000}
CONNECTIONS=${CONNECTIONS:-2000}; B_DURATION=${B_DURATION:-80}; B_RATE=${B_RATE:-100}
SSE_PROCESSES=${SSE_PROCESSES:-}; SSE_RAMP=${SSE_RAMP:-10}
K6_CPUS=${K6_CPUS:-}                   # e.g. K6_CPUS=2 to cap the generator
if [[ -n ${API_REPLICAS:-} && ! $API_REPLICAS =~ ^[1-9][0-9]*$ ]]; then
  echo "API_REPLICAS must be a positive integer" >&2
  exit 2
fi

log() { printf '\n==> %s\n' "$*" | tee -a "$OUT/run.log"; }

failures=""
record_failure() { failures+=$'\n'"  - $*"; }

network() {
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' \
    "$(docker compose ps -q nginx)"
}

# Python tools run inside the backend image (it already has the ticketing package and deps).
py() {
  docker compose run --rm --no-deps -T --user "$LT_USER" -e HOME=/tmp -v "$PWD/loadtest:/lt" -w /lt --entrypoint python api "$@"
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
    echo "API_REPLICAS=${API_REPLICAS:-unset} API_REPLICAS_RUNNING=$1"
    # Image actually used by each running container (not the current tag, which a rebuild can
    # move): the checkout SHA alone does not prove what was tested.
    local ids
    ids=$(docker compose ps -q)
    if [[ -n $ids ]]; then
      # shellcheck disable=SC2086
      docker inspect -f 'container: {{.Name}} image={{.Image}} tag={{.Config.Image}}' $ids \
        || echo "container images: inspect failed"
    else
      echo "container images: none running"
    fi
    echo "VUS=$VUS DURATION=$DURATION SEATS=$SEATS CONNECTIONS=$CONNECTIONS B_DURATION=$B_DURATION B_RATE=$B_RATE"
    echo "SSE_PROCESSES=${SSE_PROCESSES:-auto} SSE_RAMP=$SSE_RAMP"
  } > "$OUT/environment.txt"
}

running_api_replicas() {
  docker compose ps --status running -q api | awk 'NF {count++} END {print count+0}'
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
  docker run --rm --user "$LT_USER" ${K6_CPUS:+--cpus $K6_CPUS} --network "$(network)" \
    -v "$PWD/loadtest:/lt" -w /lt \
    -e BASE_URL=http://nginx:8080 -e ORIGIN=http://localhost:8080 -e EVENT_ID="$event_id" \
    -e RUN_ID="a-$STAMP" -e VUS="$VUS" -e DURATION="$DURATION" \
    -e SESSIONS_FILE="$OUT_IN/a/sessions.json" -e INVENTORY_FILE="$OUT_IN/a/inventory.json" \
    grafana/k6:0.57.0 run --summary-export="$OUT_IN/a/k6-summary.json" scenario_a.js \
    > "$OUT/a/k6.log" 2>&1 || record_failure "Scenario A k6 failed (exit $?)"
  # Every replay that started must have been verified (none cut off by the end of the run).
  py -c 'import json,sys; m=json.load(open(sys.argv[1]))["metrics"]; c=lambda k: int(m.get(k,{}).get("count",0)); s,v=c("replay_started"),c("replay_verified"); print(f"replays started={s} verified={v}"); sys.exit(s!=v)' \
    "$OUT_IN/a/k6-summary.json" | tee -a "$OUT/run.log" \
    || record_failure "Scenario A replays started and verified differ"
  tail -40 "$OUT/a/k6.log" | tee -a "$OUT/run.log"
  local ended; ended=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  log "Scenario A: waiting for holds, payments and outbox to drain (90 s)"
  sleep 90
  log "Scenario A: consistency checks (zero rows = consistent)"
  if ! psql_q -XAt -v event_id="$event_id" -v expected_seats="$SEATS" -f - \
    < loadtest/consistency.sql > "$OUT/a/consistency.txt" 2>> "$OUT/run.log"; then
    record_failure "Scenario A consistency SQL failed to run"
  elif [[ -s "$OUT/a/consistency.txt" ]]; then
    record_failure "Scenario A consistency SQL reported violations"
  fi
  tail -20 "$OUT/a/consistency.txt" | tee -a "$OUT/run.log"
  psql_q -c "SELECT status, count(*) AS orders, sum(quantity) AS tickets FROM orders WHERE event_id=$event_id GROUP BY 1 ORDER BY 1;" \
         -c "SELECT status, count(*) FROM seats WHERE event_id=$event_id GROUP BY 1 ORDER BY 1;" \
    > "$OUT/a/final-state.txt" 2>&1 || true
  tee -a "$OUT/run.log" < "$OUT/a/final-state.txt"
  psql_q -c "\copy (SELECT * FROM metrics_1s WHERE ts BETWEEN '$started' AND '$ended'::timestamptz + interval '30 seconds' ORDER BY ts, source) TO STDOUT WITH CSV HEADER" \
    > "$OUT/a/metrics_1s.csv" 2>/dev/null || true
  psql_q -c "\copy (SELECT * FROM business_1s WHERE ts BETWEEN '$started' AND '$ended'::timestamptz + interval '30 seconds' ORDER BY ts) TO STDOUT WITH CSV HEADER" \
    > "$OUT/a/business_1s.csv" 2>/dev/null || true
  with_deadline 300 docker compose exec -T nginx sh -c "grep -F '\"load_run\":\"a-$STAMP\"' /data/logs/nginx-access.json" \
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
  : > "$OUT/b/writer.txt"
  local -a sse_options=(--ramp-seconds "$SSE_RAMP")
  if [[ -n $SSE_PROCESSES ]]; then
    sse_options+=(--processes "$SSE_PROCESSES")
  fi
  py sse_client.py --base-url http://nginx:8080 --event-id "$event_id" \
    --sessions-file "$OUT_IN/b/sessions.json" --connections "$CONNECTIONS" \
    --duration "$B_DURATION" --drain-timeout 20 --writer-log "$OUT_IN/b/writer.txt" \
    --write-rate "$B_RATE" --write-duration "$((B_DURATION - 25))" \
    "${sse_options[@]}" > "$OUT/b/sse-client.txt" 2>&1 \
    || record_failure "Scenario B writer, SSE client or audit failed (exit $?)"
  tail -40 "$OUT/b/sse-client.txt" | tee -a "$OUT/run.log"
  tail -10 "$OUT/b/writer.txt" | tee -a "$OUT/run.log"
}

# Run a command with a deadline: coreutils `timeout` on Linux/WSL, perl's alarm elsewhere
# (stock macOS has perl but no `timeout`).
with_deadline() {
  # GNU timeout only sends TERM by default; a docker compose exec that does not exit on TERM
  # kept a run waiting for hours. -k sends KILL to the whole process group (the docker CLI and
  # its compose plugin) 10 s later, and stdin is /dev/null so nothing waits on the terminal.
  # The command inside the container is not killed; this only bounds how long the run waits.
  if command -v timeout >/dev/null; then
    timeout -k 10 "$@" </dev/null
  else
    perl -e 'alarm shift; exec @ARGV' "$@" </dev/null
  fi
}

service_logs() {
  # Best effort: container logs since the run started, so failures can be diagnosed from the
  # archive. <svc>-problems.log holds warnings/errors, <svc>.log the log itself, each capped.
  # PostgreSQL is excluded: its log can echo bind values (checkout URLs) and bootstrap SQL.
  mkdir -p "$OUT/logs" || return 0
  local svc keep
  for svc in api worker relay collector paymock nginx redis; do
    # Allow more lines for backend service logs, including API request.completed records.
    keep=300
    case $svc in api|worker|relay|collector) keep=20000 ;; esac
    with_deadline 60 docker compose logs --no-color --since "$RUN_STARTED" --tail "$keep" "$svc" 2>&1 \
      | head -c 20000000 > "$OUT/logs/$svc.log" || true
    grep -E '"level":"(warning|error|critical)"|Traceback|ERROR|FATAL|PANIC|\[(error|crit|alert|emerg)\]' \
      "$OUT/logs/$svc.log" | tail -n 5000 > "$OUT/logs/$svc-problems.log" || true
  done
  log "Service logs: $(cat "$OUT"/logs/*-problems.log 2>/dev/null | wc -l | tr -d ' ') warning/error lines"
}

# Checkout URLs carry a capability token; mask it in every text file of the archive.
# Fails (and so blocks packing) if any file could not be scrubbed.
redact_archive() {
  LC_ALL=C find "$OUT" -type f \( -name '*.log' -o -name '*.txt' -o -name '*.json' -o -name '*.csv' \) \
    -exec env LC_ALL=C sed -i.bak -E 's#(/pay/)[A-Za-z0-9._~%-]+#\1[redacted]#g' {} + \
    && find "$OUT" -name '*.bak' -delete
}

bundle() {
  find "$OUT" -name 'sessions.json' -delete          # live credentials never leave the machine
  service_logs
  if ! redact_archive; then
    log "Could not scrub checkout tokens from $OUT; not packing. Inspect or delete it manually."
    return 1
  fi
  # find -delete can warn yet exit 0 on macOS: verify nothing sensitive is left before packing.
  local leftovers
  if ! leftovers=$(find "$OUT" \( -name 'sessions.json' -o -name '*.bak' \) -print) || [[ -n $leftovers ]]; then
    log "Session files or unscrubbed copies may remain in $OUT; not packing. Delete them manually."
    return 1
  fi
  tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
  log "Results archive: $OUT.tar.gz"
}

ensure_stack
running=$(running_api_replicas)
if [[ -n ${API_REPLICAS:-} && $running != "$API_REPLICAS" ]]; then
  log "Scaling API from $running to $API_REPLICAS replicas"
  docker compose up -d --scale "api=$API_REPLICAS" --no-recreate --no-deps --wait api \
    >> "$OUT/run.log" 2>&1
  running=$(running_api_replicas)
fi
environment "$running"
if [[ -n ${API_REPLICAS:-} && $running != "$API_REPLICAS" ]]; then
  echo "Expected $API_REPLICAS running API replicas, found $running" >&2
  exit 1
fi
case $MODE in
  a) scenario_a ;;
  b) scenario_b ;;
  all|smoke) scenario_a; scenario_b ;;
  *) echo "usage: loadtest/run.sh [all|a|b|smoke]" >&2; exit 2 ;;
esac
if [[ -n $failures ]]; then
  log "FAILED$failures"
else
  log "PASSED"
fi
bundle
[[ -z $failures ]]
