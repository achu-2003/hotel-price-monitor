#!/bin/bash
# Creates the integration-test database alongside the real one.
#
# Runs once, when Postgres initialises an empty data directory. Mounted only
# by docker-compose.override.yml, so it never runs in production.
#
# Why a separate database rather than a separate schema: the integration suite
# calls Base.metadata.create_all and drops partitions. Pointed at the working
# database, a stray run would take the accumulated price history with it — and
# that history is the entire product.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE ${POSTGRES_DB}_test OWNER ${POSTGRES_USER};
EOSQL

# pg_trgm lives per-database, and the schema's fuzzy room-name lookups need it
# in the test database too.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${POSTGRES_DB}_test" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOSQL

echo "Created ${POSTGRES_DB}_test for the integration suite."
