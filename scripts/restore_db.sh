#!/bin/sh
# Restore a dump -- or, by default, just prove one would restore.
#
#   ./scripts/restore_db.sh --verify              newest dump -> scratch db -> drop
#   ./scripts/restore_db.sh --verify backups/daily/hotelmonitor-2026-08-19.dump
#   ./scripts/restore_db.sh --into hotelmonitor_recovered <file>
#   ./scripts/restore_db.sh --force-into hotelmonitor <file>    # the real thing
#
# WHY --verify IS THE DEFAULT
# ===========================
# An untested backup is a guess. `pg_restore --list` (which the nightly job
# already runs) proves a file is structurally readable; it does not prove the
# data comes back. This restores into a throwaway database and counts what
# landed, which is the only check that answers the question you will actually
# be asking at the time.
#
# Run it monthly. It costs a few minutes and it is the difference between
# having backups and believing you have backups.
set -eu

MODE="verify"
TARGET_DB=""
FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --verify)      MODE="verify" ;;
        --into)        MODE="into";       TARGET_DB="${2:?--into needs a database name}"; shift ;;
        --force-into)  MODE="force-into"; TARGET_DB="${2:?--force-into needs a database name}"; shift ;;
        -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
        -*)            echo "unknown option: $1" >&2; exit 2 ;;
        *)             FILE="$1" ;;
    esac
    shift
done

export PGUSER="${POSTGRES_USER:-hotelmonitor_app}"
export PGPASSWORD="${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD (source your .env)}"
export PGHOST="${POSTGRES_HOST:-127.0.0.1}"
export PGPORT="${POSTGRES_PORT:-5432}"
SOURCE_DB="${POSTGRES_DB:-hotelmonitor}"

# Newest dump wins when no file is named: daily first, then weekly.
if [ -z "$FILE" ]; then
    FILE=$(ls -1 backups/daily/*.dump backups/weekly/*.dump 2>/dev/null | sort -r | head -1 || true)
    [ -n "$FILE" ] || { echo "No dump found under backups/. Has the backup service run?" >&2; exit 1; }
    echo "Using newest dump: $FILE"
fi
[ -f "$FILE" ] || { echo "No such file: $FILE" >&2; exit 1; }

if [ "$MODE" = "verify" ]; then
    TARGET_DB="restore_check_$(date -u +%Y%m%d%H%M%S)"
fi

# Restoring over a live database is not something to do by accident, so the
# flag that permits it is spelled differently from the one that does not.
if [ "$MODE" = "into" ] && [ "$TARGET_DB" = "$SOURCE_DB" ]; then
    echo "Refusing to restore over the live database '$SOURCE_DB'." >&2
    echo "Use --force-into $SOURCE_DB if that is genuinely what you want." >&2
    exit 2
fi

echo "Restoring $FILE -> $TARGET_DB on $PGHOST:$PGPORT"

if [ "$MODE" != "force-into" ]; then
    psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$TARGET_DB\";" >/dev/null
    psql -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$TARGET_DB\";" >/dev/null
fi

# --no-owner: the dump's owner may not exist on the machine doing the restore,
# and a recovery that fails on role names is a recovery that fails.
# --clean --if-exists only for force-into, where the target already has objects.
if [ "$MODE" = "force-into" ]; then
    pg_restore --dbname="$TARGET_DB" --no-owner --clean --if-exists --jobs=4 "$FILE" || true
else
    pg_restore --dbname="$TARGET_DB" --no-owner --jobs=4 "$FILE" || true
fi
# `|| true`: pg_restore warns about extensions and roles it cannot recreate and
# exits non-zero for them. The row counts below are the real verdict.

echo
echo "What came back:"
psql -d "$TARGET_DB" -X -q -A -F'  ' -c "
    SELECT 'hotels',             count(*) FROM hotels
    UNION ALL SELECT 'recipients',         count(*) FROM recipients
    UNION ALL SELECT 'hotel_recipients',   count(*) FROM hotel_recipients
    UNION ALL SELECT 'price_observations', count(*) FROM price_observations
    UNION ALL SELECT 'price_changes',      count(*) FROM price_changes
    UNION ALL SELECT 'users',              count(*) FROM users;
"

echo
echo "Newest observation in the restored copy:"
psql -d "$TARGET_DB" -X -q -A -c "SELECT max(checked_at) FROM price_observations;"

if [ "$MODE" = "verify" ]; then
    psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE \"$TARGET_DB\";" >/dev/null
    echo
    echo "Scratch database dropped. The dump restores."
    echo "Note the observation timestamp above: that is how much history a"
    echo "restore from this file would actually give you back."
fi
