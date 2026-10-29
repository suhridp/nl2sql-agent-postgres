#!/usr/bin/env bash
# Build the "retail" database: owner role, SELECT-only "analyst" role, schema "public" (generated
# data, ~1M rows) and schema "edge" (small hand-built edge cases). Idempotent; takes ~30 s.
#   Local (Debian/Ubuntu):  sudo -u postgres ./db/init.sh
#   Docker:                 docker compose up -d   (runs this automatically)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PSQL=(psql -v ON_ERROR_STOP=1 -q -X)

"${PSQL[@]}" -d postgres <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'retail_owner') THEN
    CREATE ROLE retail_owner LOGIN PASSWORD 'owner';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analyst') THEN
    CREATE ROLE analyst LOGIN PASSWORD 'analyst' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
ALTER ROLE analyst SET default_transaction_read_only = on;
ALTER ROLE analyst SET statement_timeout = '15s';
SQL
if [ "$(psql -X -tAc "SELECT 1 FROM pg_database WHERE datname = 'retail'" -d postgres)" != "1" ]; then
  psql -X -q -d postgres -c "CREATE DATABASE retail OWNER retail_owner"
fi

export PGOPTIONS="-c role=retail_owner"
"${PSQL[@]}" -d retail -v schema=public -f "$DIR/schema.sql"
"${PSQL[@]}" -d retail -f "$DIR/data.sql"
"${PSQL[@]}" -d retail -v schema=edge -f "$DIR/schema.sql"
"${PSQL[@]}" -d retail -f "$DIR/edge.sql"
unset PGOPTIONS

"${PSQL[@]}" -d retail <<'SQL'
REVOKE ALL ON DATABASE retail FROM PUBLIC;
GRANT CONNECT ON DATABASE retail TO analyst;          -- no TEMP: the analyst cannot create temp tables
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public, edge TO analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA public, edge TO analyst;
SQL
echo "retail database ready: $(psql -X -tAc 'SELECT count(*) FROM public.order_items' -d retail) order lines"
