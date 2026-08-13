#!/bin/sh
set -eu

umask 077

backup_dir="${PROPERTYQUARRY_BACKUP_DIR:-/backups}"
db_host="${PROPERTYQUARRY_BACKUP_DB_HOST:-propertyquarry-db}"
db_port="${PROPERTYQUARRY_BACKUP_DB_PORT:-5432}"
db_name="${PROPERTYQUARRY_BACKUP_DB_NAME:-propertyquarry}"
db_user="${PROPERTYQUARRY_BACKUP_DB_USER:-postgres}"
interval_seconds="${PROPERTYQUARRY_BACKUP_INTERVAL_SECONDS:-86400}"
retry_seconds="${PROPERTYQUARRY_BACKUP_RETRY_SECONDS:-300}"
retention_days="${PROPERTYQUARRY_BACKUP_RETENTION_DAYS:-7}"
max_files="${PROPERTYQUARRY_BACKUP_MAX_FILES:-8}"
min_free_bytes="${PROPERTYQUARRY_BACKUP_MIN_FREE_BYTES:-5368709120}"
health_max_age_seconds="${PROPERTYQUARRY_BACKUP_HEALTH_MAX_AGE_SECONDS:-129600}"
public_cache_retention_days="${PROPERTYQUARRY_PUBLIC_CACHE_EVENT_RETENTION_DAYS:-7}"
maintenance_batch_size="${PROPERTYQUARRY_DATABASE_MAINTENANCE_BATCH_SIZE:-5000}"
maintenance_max_batches="${PROPERTYQUARRY_DATABASE_MAINTENANCE_MAX_BATCHES:-32}"

case "$backup_dir" in
    /*) ;;
    *) echo "propertyquarry_backup_dir_must_be_absolute" >&2; exit 2 ;;
esac
if [ "$backup_dir" = "/" ]; then
    echo "propertyquarry_backup_dir_too_broad" >&2
    exit 2
fi

for value in "$db_port" "$interval_seconds" "$retry_seconds" "$retention_days" "$max_files" "$min_free_bytes" "$health_max_age_seconds" "$public_cache_retention_days" "$maintenance_batch_size" "$maintenance_max_batches"; do
    case "$value" in
        ''|*[!0-9]*) echo "propertyquarry_backup_numeric_config_invalid" >&2; exit 2 ;;
    esac
done

mkdir -p "$backup_dir"

backup_once() {
    available_kb="$(df -Pk "$backup_dir" | awk 'NR == 2 {print $4}')"
    case "$available_kb" in
        ''|*[!0-9]*) echo "propertyquarry_backup_free_space_unknown" >&2; return 1 ;;
    esac
    if [ "$((available_kb * 1024))" -lt "$min_free_bytes" ]; then
        echo "propertyquarry_backup_free_space_below_floor" >&2
        return 1
    fi

    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    filename="propertyquarry-${timestamp}.dump"
    target="$backup_dir/$filename"
    partial="$target.partial"
    checksum_partial="$target.sha256.partial"
    rm -f "$partial" "$checksum_partial"

    if ! PGPASSWORD="${PGPASSWORD:-}" pg_dump \
        --host="$db_host" \
        --port="$db_port" \
        --username="$db_user" \
        --dbname="$db_name" \
        --format=custom \
        --compress=6 \
        --no-password \
        --file="$partial"; then
        rm -f "$partial" "$checksum_partial"
        return 1
    fi
    if ! pg_restore --list "$partial" >/dev/null; then
        rm -f "$partial" "$checksum_partial"
        echo "propertyquarry_backup_archive_invalid" >&2
        return 1
    fi

    digest="$(sha256sum "$partial" | awk '{print $1}')"
    printf '%s  %s\n' "$digest" "$filename" > "$checksum_partial"
    mv "$partial" "$target"
    mv "$checksum_partial" "$target.sha256"
    date -u +%s > "$backup_dir/.last-success"

    find "$backup_dir" -maxdepth 1 -type f \
        \( -name 'propertyquarry-*.dump' -o -name 'propertyquarry-*.dump.sha256' \) \
        -mtime "+$retention_days" -delete

    index=0
    for archive in $(ls -1t "$backup_dir"/propertyquarry-*.dump 2>/dev/null || true); do
        index=$((index + 1))
        if [ "$index" -gt "$max_files" ]; then
            rm -f "$archive" "$archive.sha256"
        fi
    done
    printf '{"status":"verified","archive":"%s","sha256":"%s","created_at":"%s"}\n' \
        "$filename" "$digest" "$timestamp"
}

healthcheck() {
    if [ ! -s "$backup_dir/.last-success" ]; then
        echo "propertyquarry_backup_never_succeeded" >&2
        return 1
    fi
    last_success="$(tr -d '[:space:]' < "$backup_dir/.last-success")"
    case "$last_success" in
        ''|*[!0-9]*) echo "propertyquarry_backup_timestamp_invalid" >&2; return 1 ;;
    esac
    now="$(date -u +%s)"
    if [ "$((now - last_success))" -gt "$health_max_age_seconds" ]; then
        echo "propertyquarry_backup_stale" >&2
        return 1
    fi
    newest="$(ls -1t "$backup_dir"/propertyquarry-*.dump 2>/dev/null | head -n 1 || true)"
    if [ -z "$newest" ] || [ ! -s "$newest.sha256" ]; then
        echo "propertyquarry_backup_archive_missing" >&2
        return 1
    fi
    (
        cd "$backup_dir"
        sha256sum -c "$(basename "$newest.sha256")" >/dev/null
    )
    pg_restore --list "$newest" >/dev/null
}

maintenance_once() {
    batches=0
    deleted_total=0
    while [ "$batches" -lt "$maintenance_max_batches" ]; do
        deleted="$(PGPASSWORD="${PGPASSWORD:-}" psql \
            --host="$db_host" \
            --port="$db_port" \
            --username="$db_user" \
            --dbname="$db_name" \
            --no-password \
            --set=ON_ERROR_STOP=1 \
            --tuples-only \
            --no-align \
            --command="WITH victims AS (
                SELECT ctid
                FROM observation_events
                WHERE principal_id = 'propertyquarry:public-cache'
                  AND created_at < now() - make_interval(days => $public_cache_retention_days)
                ORDER BY created_at ASC
                LIMIT $maintenance_batch_size
            ), deleted AS (
                DELETE FROM observation_events AS events
                USING victims
                WHERE events.ctid = victims.ctid
                RETURNING 1
            )
            SELECT COUNT(*) FROM deleted;" | tr -d '[:space:]')"
        case "$deleted" in
            ''|*[!0-9]*) echo "propertyquarry_maintenance_delete_count_invalid" >&2; return 1 ;;
        esac
        deleted_total=$((deleted_total + deleted))
        batches=$((batches + 1))
        if [ "$deleted" -lt "$maintenance_batch_size" ]; then
            break
        fi
    done
    PGPASSWORD="${PGPASSWORD:-}" psql \
        --host="$db_host" \
        --port="$db_port" \
        --username="$db_user" \
        --dbname="$db_name" \
        --no-password \
        --set=ON_ERROR_STOP=1 \
        --command="VACUUM (ANALYZE) observation_events;" >/dev/null
    printf '{"status":"maintained","public_cache_events_deleted":%s,"retention_days":%s}\n' \
        "$deleted_total" "$public_cache_retention_days"
}

case "${1:-daemon}" in
    once)
        backup_once
        ;;
    health)
        healthcheck
        ;;
    maintenance)
        maintenance_once
        ;;
    daemon)
        while :; do
            if backup_once; then
                if ! maintenance_once; then
                    echo "propertyquarry_database_maintenance_failed" >&2
                fi
                sleep "$interval_seconds"
            else
                sleep "$retry_seconds"
            fi
        done
        ;;
    *)
        echo "usage: propertyquarry_postgres_backup.sh [once|health|maintenance|daemon]" >&2
        exit 2
        ;;
esac
