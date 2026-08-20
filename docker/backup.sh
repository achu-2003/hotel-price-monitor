#!/bin/sh
# Nightly verified dump of the price history.
#
# Two years of observations is the entire value of this system and it otherwise
# lives on exactly one Docker volume. This script is what stands between a bad
# migration -- or a `docker compose down -v` typed in the wrong window -- and
# starting the history over from zero.
#
# WHAT THIS DOES AND DOES NOT PROTECT
# ===================================
# Dumps land in a host-mounted directory, so they survive the volume being
# destroyed and the stack being rebuilt. They do NOT survive the machine dying,
# because they are still on that machine. Copy ./backups off the box on a
# schedule if the history matters more than the hardware -- and keep
# CREDENTIAL_KEK somewhere else again, or a stolen backup carries its own keys.
#
# Custom format (-Fc), not plain SQL: it compresses roughly 10x, and
# pg_restore can pull a single table out of it when the alternative is
# replaying a 900MB text file to recover one row someone deleted by mistake.
set -eu

DIR="${BACKUP_DIR:-/backups}"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"
AT_HOUR="${BACKUP_AT_HOUR:-2}"
AT_MINUTE="${BACKUP_AT_MINUTE:-30}"

DB="${POSTGRES_DB:-hotelmonitor}"
export PGUSER="${POSTGRES_USER:-hotelmonitor_app}"
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
export PGHOST="${POSTGRES_HOST:-postgres}"
export PGPORT="${POSTGRES_PORT:-5432}"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') backup $*"; }

# Seconds from now until the next HH:MM, so the loop drifts by zero rather than
# by one dump-duration per night the way `sleep 86400` does.
#
# Plain arithmetic rather than `date -d`: this runs on postgres:16-alpine,
# whose busybox date does not parse "today 02:30". Leading zeros are stripped
# because $(( )) reads "08" as octal and refuses it.
seconds_until_run() {
    h=$(date -u +%H); h=${h#0}; h=${h:-0}
    m=$(date -u +%M); m=${m#0}; m=${m:-0}
    s=$(date -u +%S); s=${s#0}; s=${s:-0}
    now=$((h * 3600 + m * 60 + s))
    target=$((AT_HOUR * 3600 + AT_MINUTE * 60))
    diff=$((target - now))
    [ "$diff" -le 0 ] && diff=$((diff + 86400))
    echo "$diff"
}

run_backup() {
    stamp=$(date -u +%Y-%m-%d)
    mkdir -p "$DIR/daily" "$DIR/weekly"
    target="$DIR/daily/${DB}-${stamp}.dump"
    partial="${target}.partial"

    log "starting db=$DB host=$PGHOST"

    # Dump to .partial and rename only on success. A dump interrupted by a
    # restart would otherwise leave a truncated file with a valid-looking name,
    # which is the kind of backup you discover is empty on the day you need it.
    if ! pg_dump --format=custom --compress=6 --file="$partial" "$DB"; then
        log "FAILED pg_dump exited non-zero"
        rm -f "$partial"
        return 1
    fi

    # Read the dump back before trusting it. This catches truncation and a full
    # disk, both of which produce a file that exists and is worthless.
    if ! pg_restore --list "$partial" >/dev/null 2>&1; then
        log "FAILED dump is not readable by pg_restore"
        rm -f "$partial"
        return 1
    fi

    mv "$partial" "$target"
    size=$(du -h "$target" | cut -f1)
    log "ok file=$target size=$size"

    # Sunday's dump is also kept on the weekly ladder, so a problem noticed
    # three weeks late still has something to go back to.
    if [ "$(date -u +%u)" = "7" ]; then
        cp "$target" "$DIR/weekly/${DB}-${stamp}.dump"
        log "promoted to weekly"
    fi

    prune "$DIR/daily" "$KEEP_DAILY"
    prune "$DIR/weekly" "$KEEP_WEEKLY"
}

# Keep the N newest dumps in a directory. Sorted by name, which is an ISO date,
# so this is chronological without depending on mtime -- a copied file's mtime
# is the copy, not the backup.
prune() {
    dir="$1"; keep="$2"
    ls -1 "$dir"/*.dump 2>/dev/null | sort -r | tail -n +$((keep + 1)) | while read -r old; do
        rm -f "$old"
        log "pruned $(basename "$old")"
    done
}

# Dump once at start-up so a fresh deployment has a backup within a minute
# rather than at 02:30 tomorrow -- the first night is when a misconfiguration
# is most likely and the data is least replaceable.
if [ "${BACKUP_ON_START:-true}" = "true" ]; then
    run_backup || log "start-up backup failed; continuing to the schedule"
fi

while true; do
    wait_for=$(seconds_until_run)
    log "next run in ${wait_for}s"
    sleep "$wait_for"
    run_backup || log "scheduled backup failed; will try again tomorrow"
done
