#!/usr/bin/env bash
# Bounded smoke check for the full stack:
#   - migrate and seed exit 0
#   - all 3 API replicas are healthy
#   - nginx answers /healthz/live and injects X-Request-ID
set -euo pipefail

PORT="${HTTP_PORT:-8080}"
DEADLINE=$((SECONDS + ${SMOKE_TIMEOUT:-240}))

docker compose up -d --build

wait_for() {
  local description=$1; shift
  until "$@"; do
    if (( SECONDS > DEADLINE )); then
      echo "smoke: timed out waiting for ${description}" >&2
      docker compose ps >&2
      exit 1
    fi
    sleep 2
  done
}

exit_code() { docker inspect -f '{{.State.ExitCode}} {{.State.Status}}' "$(docker compose ps -a -q "$1")"; }
one_shot_ok() { [[ "$(exit_code "$1")" == "0 exited" ]]; }
healthy_api() {
  local n
  n=$(docker compose ps -q api | xargs docker inspect -f '{{.State.Health.Status}}' | grep -c '^healthy$' || true)
  [[ "$n" -eq 3 ]]
}
ingress_ok() { curl -fsS "http://127.0.0.1:${PORT}/healthz/live" >/dev/null 2>&1; }

wait_for "migrate to finish" one_shot_ok migrate
wait_for "seed to finish" one_shot_ok seed
wait_for "3 healthy api replicas" healthy_api
wait_for "nginx ingress" ingress_ok

rid=$(curl -fsS -D - -o /dev/null "http://127.0.0.1:${PORT}/healthz/live" | awk 'tolower($1)=="x-request-id:" {print $2}' | tr -d '\r')
if [[ ! "$rid" =~ ^[0-9a-f]{32}$ ]]; then
  echo "smoke: missing or malformed X-Request-ID from ingress: '${rid}'" >&2
  exit 1
fi

# Every replica must have served traffic through nginx at least once (least_conn spreads load).
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:${PORT}/healthz/live" >/dev/null; done
served=$(docker compose exec -T nginx sh -c "grep -o '\"upstream\":\"[0-9.:]*\"' /data/logs/nginx-access.json | sort -u | wc -l")
if (( served < 3 )); then
  echo "smoke: expected traffic on 3 upstreams, saw ${served}" >&2
  exit 1
fi

echo "smoke: OK (request id ${rid}, ${served} upstreams)"
