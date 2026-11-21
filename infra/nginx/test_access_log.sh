#!/bin/sh
# Exercise the repository config in isolation, including connection errors to paymock.
set -eu
config="$(dirname "$0")/nginx.conf"
logs=$(docker run --rm -i --network none --entrypoint sh nginx:1.27-alpine -eu -c '
    cat > /etc/nginx/nginx.conf
    mkdir -p /data/logs
    nginx -t
    nginx
    wget -T 2 -q -O /dev/null http://127.0.0.1:8080/pay/checkout-log-test || true
    wget -T 2 -q -O /dev/null --post-data "" http://127.0.0.1:8080/pay/checkout-log-test/confirm || true
    wget -T 2 -q -O /dev/null http://127.0.0.1:8080/healthz/unknown || true
    test "$(grep -c "\"uri\":\"/pay/:id\"" /data/logs/nginx-access.json)" -eq 2
    grep -q "\"uri\":\"/healthz/unknown\"" /data/logs/nginx-access.json
    ! grep -q "checkout-log-test" /data/logs/nginx-access.json
' < "$config" 2>&1) || { printf '%s\n' "$logs" >&2; exit 1; }
case "$logs" in
    *checkout-log-test*) printf 'Checkout ID leaked into nginx container logs\n' >&2; exit 1 ;;
esac
printf 'nginx checkout log redaction passed\n'
