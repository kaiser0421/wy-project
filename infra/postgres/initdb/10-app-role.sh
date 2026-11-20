#!/bin/sh
# Runs once when the data directory is initialised. Creates the least-privilege runtime role used by
# the API and background services. The bootstrap user (POSTGRES_USER) owns the schema and is only
# used by the one-shot `migrate` service; table privileges are granted in the baseline migration.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_user="$APP_DB_USER" -v app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE',
              :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
SQL
