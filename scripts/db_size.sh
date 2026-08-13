#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/db_size.sh

Prints database, table, index, TOAST, dead-tuple, autovacuum, WAL, PGDATA,
and high-water diagnostics. It is read-only and will use an already-running
container instead of trying to recreate it.

The Compose Postgres volume is on-disk state, not RAM.
Legacy EA commonly names it `ea_pgdata`; standalone PropertyQuarry commonly
names it `propertyquarry_pgdata`. Both mount at `/var/lib/postgresql/data`.

Environment:
  PROPERTYQUARRY_DB_SERVICE        Compose DB service alias
  PROPERTYQUARRY_DB_CONTAINER_NAME Deployed DB container alias
  EA_DB_CONTAINER                  Explicit Postgres container override
  POSTGRES_USER                    Postgres user (default: postgres)
  POSTGRES_DB                      Database name (default: propertyquarry; set ea for legacy EA)
  EA_DB_SIZE_LIMIT                 Largest tables to print (default: 20)
  EA_DB_SIZE_SCHEMA                Optional schema filter (for example: public)
  EA_DB_SIZE_SORT_KEY              total|table|index|toast|dead (default: total)
  EA_DB_SIZE_TABLE_PREFIX          Optional table-name prefix filter
  EA_DB_SIZE_MIN_MB                Optional minimum total relation size in MB
  EA_DB_SIZE_WARN_MB               Database warning threshold (default: 8192)
  EA_DB_SIZE_CRITICAL_MB           Database critical threshold (default: 16384)
  EA_DB_TABLE_WARN_MB              Per-relation warning threshold (default: 4096)
  EA_DB_WAL_WARN_MB                WAL warning threshold (default: 2048)
  EA_DB_DEAD_TUPLE_WARN_PERCENT    Dead tuple warning threshold (default: 20)
  EA_DB_DEAD_TUPLE_WARN_ROWS       Minimum dead rows before warning (default: 10000)
  EA_DB_FILESYSTEM_WARN_PERCENT    PGDATA filesystem warning (default: 85)
  EA_DB_FILESYSTEM_CRITICAL_PERCENT PGDATA filesystem critical (default: 92)
  EA_DB_SIZE_FAIL_ON_HIGH_WATER    1 exits 3 on any high-water warning (default: 0)
EOF
  exit 0
fi
if [[ -n "${1:-}" ]]; then
  echo "unknown argument: ${1}" >&2
  exit 2
fi

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-propertyquarry-db}}"
if [[ -n "${EA_DB_CONTAINER:-}" ]]; then
  DB_CONTAINER="${EA_DB_CONTAINER}"
elif [[ -n "${PROPERTYQUARRY_DB_CONTAINER_NAME:-}" ]]; then
  DB_CONTAINER="${PROPERTYQUARRY_DB_CONTAINER_NAME}"
elif [[ "${DB_SERVICE}" == "propertyquarry-db" ]]; then
  DB_CONTAINER="propertyquarry-db-live"
else
  DB_CONTAINER="${DB_SERVICE}"
fi
DB_USER="${POSTGRES_USER:-postgres}"
if [[ -n "${POSTGRES_DB:-}" ]]; then
  DB_NAME="${POSTGRES_DB}"
elif [[ "${DB_SERVICE}" == "propertyquarry-db" || "${DB_CONTAINER}" == propertyquarry-db* ]]; then
  DB_NAME="propertyquarry"
else
  DB_NAME="ea"
fi
SIZE_LIMIT="${EA_DB_SIZE_LIMIT:-20}"
TABLE_SCHEMA="${EA_DB_SIZE_SCHEMA:-}"
SORT_KEY="$(printf '%s' "${EA_DB_SIZE_SORT_KEY:-total}" | tr '[:upper:]' '[:lower:]')"
TABLE_PREFIX="${EA_DB_SIZE_TABLE_PREFIX:-}"
MIN_MB="${EA_DB_SIZE_MIN_MB:-0}"
DB_WARN_MB="${EA_DB_SIZE_WARN_MB:-8192}"
DB_CRITICAL_MB="${EA_DB_SIZE_CRITICAL_MB:-16384}"
TABLE_WARN_MB="${EA_DB_TABLE_WARN_MB:-4096}"
WAL_WARN_MB="${EA_DB_WAL_WARN_MB:-2048}"
DEAD_WARN_PERCENT="${EA_DB_DEAD_TUPLE_WARN_PERCENT:-20}"
DEAD_WARN_ROWS="${EA_DB_DEAD_TUPLE_WARN_ROWS:-10000}"
FILESYSTEM_WARN_PERCENT="${EA_DB_FILESYSTEM_WARN_PERCENT:-85}"
FILESYSTEM_CRITICAL_PERCENT="${EA_DB_FILESYSTEM_CRITICAL_PERCENT:-92}"
FAIL_ON_HIGH_WATER="${EA_DB_SIZE_FAIL_ON_HIGH_WATER:-0}"

require_integer() {
  local name="$1"
  local value="$2"
  if ! [[ "${value}" =~ ^[0-9]+$ ]]; then
    echo "${name} must be an integer" >&2
    exit 2
  fi
}
for pair in \
  "EA_DB_SIZE_LIMIT:${SIZE_LIMIT}" "EA_DB_SIZE_MIN_MB:${MIN_MB}" \
  "EA_DB_SIZE_WARN_MB:${DB_WARN_MB}" "EA_DB_SIZE_CRITICAL_MB:${DB_CRITICAL_MB}" \
  "EA_DB_TABLE_WARN_MB:${TABLE_WARN_MB}" "EA_DB_WAL_WARN_MB:${WAL_WARN_MB}" \
  "EA_DB_DEAD_TUPLE_WARN_PERCENT:${DEAD_WARN_PERCENT}" \
  "EA_DB_DEAD_TUPLE_WARN_ROWS:${DEAD_WARN_ROWS}" \
  "EA_DB_FILESYSTEM_WARN_PERCENT:${FILESYSTEM_WARN_PERCENT}" \
  "EA_DB_FILESYSTEM_CRITICAL_PERCENT:${FILESYSTEM_CRITICAL_PERCENT}"
do
  require_integer "${pair%%:*}" "${pair#*:}"
done
if (( SIZE_LIMIT < 1 || SIZE_LIMIT > 200 )); then
  echo "EA_DB_SIZE_LIMIT must be in [1,200]" >&2
  exit 2
fi
if (( DB_CRITICAL_MB <= DB_WARN_MB )); then
  echo "EA_DB_SIZE_CRITICAL_MB must be greater than EA_DB_SIZE_WARN_MB" >&2
  exit 2
fi
if (( DEAD_WARN_PERCENT > 100 )); then
  echo "EA_DB_DEAD_TUPLE_WARN_PERCENT must be in [0,100]" >&2
  exit 2
fi
if (( FILESYSTEM_WARN_PERCENT > 100 || FILESYSTEM_CRITICAL_PERCENT > 100 || FILESYSTEM_CRITICAL_PERCENT <= FILESYSTEM_WARN_PERCENT )); then
  echo "filesystem percentages must be in [0,100] with critical greater than warning" >&2
  exit 2
fi
if [[ "${FAIL_ON_HIGH_WATER}" != "0" && "${FAIL_ON_HIGH_WATER}" != "1" ]]; then
  echo "EA_DB_SIZE_FAIL_ON_HIGH_WATER must be 0 or 1" >&2
  exit 2
fi
if [[ -n "${TABLE_PREFIX}" && ! "${TABLE_PREFIX}" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "EA_DB_SIZE_TABLE_PREFIX must match [A-Za-z0-9_]+" >&2
  exit 2
fi
if [[ -n "${TABLE_SCHEMA}" && ! "${TABLE_SCHEMA}" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "EA_DB_SIZE_SCHEMA must match [A-Za-z0-9_]+" >&2
  exit 2
fi

case "${SORT_KEY}" in
  total) SORT_EXPR="pg_total_relation_size(relid)" ;;
  table) SORT_EXPR="pg_relation_size(relid)" ;;
  index) SORT_EXPR="pg_indexes_size(relid)" ;;
  toast) SORT_EXPR="GREATEST(pg_total_relation_size(relid)-pg_relation_size(relid)-pg_indexes_size(relid),0)" ;;
  dead) SORT_EXPR="n_dead_tup" ;;
  *)
    echo "EA_DB_SIZE_SORT_KEY must be total|table|index|toast|dead" >&2
    exit 2
    ;;
esac

FILTER_CLAUSE="TRUE"
if [[ -n "${TABLE_SCHEMA}" ]]; then
  FILTER_CLAUSE+=" AND schemaname='${TABLE_SCHEMA}'"
fi
if [[ -n "${TABLE_PREFIX}" ]]; then
  FILTER_CLAUSE+=" AND relname LIKE '${TABLE_PREFIX}%'"
fi
if (( MIN_MB > 0 )); then
  FILTER_CLAUSE+=" AND pg_total_relation_size(relid)>=(${MIN_MB}::bigint*1024*1024)"
fi

ensure_database_ready() {
  local running=""
  running="$(docker inspect -f '{{.State.Running}}' "${DB_CONTAINER}" 2>/dev/null || true)"
  if [[ "${running}" != "true" ]]; then
    "${DC[@]}" up -d "${DB_SERVICE}" >/dev/null
  fi
  for _ in $(seq 1 30); do
    if docker exec "${DB_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "database container did not become ready: ${DB_CONTAINER}/${DB_NAME}" >&2
  exit 1
}

psql_query() {
  docker exec -i "${DB_CONTAINER}" psql -X -v ON_ERROR_STOP=1 \
    -P pager=off -U "${DB_USER}" -d "${DB_NAME}" -c "$1"
}

sql_scalar() {
  docker exec -i "${DB_CONTAINER}" psql -X -qAt -v ON_ERROR_STOP=1 \
    -U "${DB_USER}" -d "${DB_NAME}" -c "$1" | tail -n 1 | tr -d '[:space:]'
}

ensure_database_ready
DB_BYTES="$(sql_scalar "SELECT pg_database_size(current_database())")"
WAL_BYTES="$(sql_scalar "SELECT COALESCE(SUM(size),0)::bigint FROM pg_ls_waldir()")"
LARGEST_RELATION_BYTES="$(sql_scalar "SELECT COALESCE(MAX(pg_total_relation_size(relid)),0)::bigint FROM pg_catalog.pg_statio_user_tables WHERE ${FILTER_CLAUSE}")"
MAX_DEAD_PERCENT="$(sql_scalar "SELECT COALESCE(MAX(CASE WHEN n_live_tup+n_dead_tup=0 THEN 0 ELSE floor(100.0*n_dead_tup/(n_live_tup+n_dead_tup)) END),0)::bigint FROM pg_stat_user_tables WHERE ${FILTER_CLAUSE} AND n_dead_tup>=${DEAD_WARN_ROWS}")"
PGDATA_BYTES="$(docker exec "${DB_CONTAINER}" sh -lc 'kb=$(du -sk "$PGDATA" 2>/dev/null | cut -f1); printf "%s\n" "$(( ${kb:-0} * 1024 ))"')"
read -r FILESYSTEM_BYTES FILESYSTEM_USED_BYTES FILESYSTEM_AVAILABLE_BYTES FILESYSTEM_USED_PERCENT < <(
  docker exec "${DB_CONTAINER}" sh -lc 'df -P -B1 "$PGDATA" | tail -n 1' |
    awk '{percent=$5; sub(/%/,"",percent); print $2,$3,$4,percent}'
)

MIB=$((1024 * 1024))
high_water=0
db_state="ok"
if (( DB_BYTES >= DB_CRITICAL_MB * MIB )); then
  db_state="critical"
  high_water=1
elif (( DB_BYTES >= DB_WARN_MB * MIB )); then
  db_state="warning"
  high_water=1
fi
wal_state="ok"
if (( WAL_BYTES >= WAL_WARN_MB * MIB )); then
  wal_state="warning"
  high_water=1
fi
table_state="ok"
if (( LARGEST_RELATION_BYTES >= TABLE_WARN_MB * MIB )); then
  table_state="warning"
  high_water=1
fi
dead_state="ok"
if (( MAX_DEAD_PERCENT >= DEAD_WARN_PERCENT && DEAD_WARN_PERCENT > 0 )); then
  dead_state="warning"
  high_water=1
fi
filesystem_state="ok"
if (( FILESYSTEM_USED_PERCENT >= FILESYSTEM_CRITICAL_PERCENT )); then
  filesystem_state="critical"
  high_water=1
elif (( FILESYSTEM_USED_PERCENT >= FILESYSTEM_WARN_PERCENT )); then
  filesystem_state="warning"
  high_water=1
fi

echo "== PropertyQuarry DB size =="
echo "target_service=${DB_SERVICE}"
echo "target_container=${DB_CONTAINER}"
echo "target_database=${DB_NAME}"
echo "pgdata_volume_note=on-disk Postgres runtime state, not RAM"
echo "high_water_database=${db_state} bytes=${DB_BYTES} warn_mb=${DB_WARN_MB} critical_mb=${DB_CRITICAL_MB}"
echo "high_water_wal=${wal_state} bytes=${WAL_BYTES} warn_mb=${WAL_WARN_MB}"
echo "high_water_relation=${table_state} largest_bytes=${LARGEST_RELATION_BYTES} warn_mb=${TABLE_WARN_MB}"
echo "high_water_dead_tuples=${dead_state} max_percent=${MAX_DEAD_PERCENT} warn_percent=${DEAD_WARN_PERCENT} min_rows=${DEAD_WARN_ROWS}"
echo "high_water_filesystem=${filesystem_state} used_percent=${FILESYSTEM_USED_PERCENT} warn_percent=${FILESYSTEM_WARN_PERCENT} critical_percent=${FILESYSTEM_CRITICAL_PERCENT}"

echo "-- database and WAL size --"
psql_query "SELECT current_database() AS db_name, pg_database_size(current_database()) AS db_bytes, pg_size_pretty(pg_database_size(current_database())) AS db_size, ${WAL_BYTES}::bigint AS wal_bytes, pg_size_pretty(${WAL_BYTES}::bigint) AS wal_size;"

echo "-- aggregate relation footprint --"
psql_query "SELECT pg_size_pretty(COALESCE(SUM(pg_relation_size(relid)),0)::bigint) AS table_bytes, pg_size_pretty(COALESCE(SUM(pg_indexes_size(relid)),0)::bigint) AS index_bytes, pg_size_pretty(COALESCE(SUM(pg_total_relation_size(relid)-pg_relation_size(relid)-pg_indexes_size(relid)),0)::bigint) AS toast_bytes, pg_size_pretty(COALESCE(SUM(pg_total_relation_size(relid)),0)::bigint) AS relation_total FROM pg_catalog.pg_statio_user_tables WHERE ${FILTER_CLAUSE};"

echo "-- largest user tables (top ${SIZE_LIMIT}; sort=${SORT_KEY}) --"
psql_query "SELECT schemaname AS schema_name, relname AS table_name, pg_relation_size(relid) AS table_bytes, pg_indexes_size(relid) AS index_bytes, GREATEST(pg_total_relation_size(relid)-pg_relation_size(relid)-pg_indexes_size(relid),0) AS toast_bytes, pg_total_relation_size(relid) AS total_bytes, n_live_tup, n_dead_tup, CASE WHEN n_live_tup+n_dead_tup=0 THEN 0 ELSE round(100.0*n_dead_tup/(n_live_tup+n_dead_tup),2) END AS dead_percent, last_autovacuum, last_autoanalyze FROM pg_catalog.pg_statio_user_tables JOIN pg_catalog.pg_stat_user_tables USING (relid,schemaname,relname) WHERE ${FILTER_CLAUSE} ORDER BY ${SORT_EXPR} DESC, relname ASC LIMIT ${SIZE_LIMIT};"

echo "-- PGDATA filesystem --"
echo "pgdata_bytes=${PGDATA_BYTES} filesystem_bytes=${FILESYSTEM_BYTES} filesystem_used_bytes=${FILESYSTEM_USED_BYTES} filesystem_available_bytes=${FILESYSTEM_AVAILABLE_BYTES} filesystem_used_percent=${FILESYSTEM_USED_PERCENT}%"

if [[ "${FAIL_ON_HIGH_WATER}" == "1" && "${high_water}" == "1" ]]; then
  echo "database high-water threshold exceeded" >&2
  exit 3
fi
