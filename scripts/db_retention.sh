#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/db_retention.sh [--apply]

Dry-run by default. Counts candidates without creating tables or changing data.
Apply mode uses a strict table allowlist, one global run journal, small
FOR UPDATE SKIP LOCKED batches, per-table row ceilings, and short SQL timeouts.
It never runs VACUUM FULL or deletes legal-hold/property packet records.

Environment:
  PROPERTYQUARRY_DB_SERVICE                 Compose DB service alias
  PROPERTYQUARRY_DB_CONTAINER_NAME          Deployed DB container alias
  EA_DB_CONTAINER                           Explicit Postgres container override
  POSTGRES_USER                              Postgres user (default: postgres)
  POSTGRES_DB                                Database name (ea; propertyquarry for propertyquarry-db)
  EA_RETENTION_PROFILE                       aggressive|standard|conservative (default: standard)
  EA_RETENTION_EXECUTION_EVENTS_DAYS         default: 90
  EA_RETENTION_POLICY_DECISIONS_DAYS         default: 90
  EA_RETENTION_OBSERVATIONS_DAYS             default: 60
  EA_RETENTION_DELIVERY_SENT_DAYS            default: 30
  EA_RETENTION_APPROVAL_REQUESTS_DAYS        default: 120
  EA_RETENTION_APPROVAL_DECISIONS_DAYS       default: 120
  EA_RETENTION_TABLES                        Optional CSV subset of the strict allowlist
  EA_RETENTION_SKIP_TABLES                   Optional CSV skip list
  EA_RETENTION_BATCH_SIZE                    Rows per transaction (default: 500; max: 10000)
  EA_RETENTION_MAX_ROWS_PER_TABLE             Run ceiling per table (default: 10000; max: 1000000)
  EA_RETENTION_LOCK_TIMEOUT_MS               Per-batch lock timeout (default: 2000)
  EA_RETENTION_STATEMENT_TIMEOUT_MS          Per-batch statement timeout (default: 30000)
  EA_RETENTION_JOURNAL_MAX_ROWS              Completed journal rows retained (default: 1000)
  EA_RETENTION_STALE_RUN_SECONDS             Abandon stale running journals (default: 7200)
  EA_RETENTION_VACUUM_AFTER_APPLY            1 runs ordinary VACUUM ANALYZE (default: 0)

Physical file reclamation is deliberately separate: after backup and a
maintenance window, use an operator-reviewed VACUUM FULL/pg_repack plan for an
exact relation. This command only makes tuples reusable in place.
EOF
  exit 0
fi

APPLY=0
if [[ "${1:-}" == "--apply" ]]; then
  APPLY=1
elif [[ -n "${1:-}" ]]; then
  echo "unknown argument: ${1}" >&2
  echo "use --help for usage" >&2
  exit 2
fi

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"
DB_CONTAINER="${EA_DB_CONTAINER:-${PROPERTYQUARRY_DB_CONTAINER_NAME:-${DB_SERVICE}}}"
DB_USER="${POSTGRES_USER:-postgres}"
if [[ -n "${POSTGRES_DB:-}" ]]; then
  DB_NAME="${POSTGRES_DB}"
elif [[ "${DB_SERVICE}" == "propertyquarry-db" ]]; then
  DB_NAME="propertyquarry"
else
  DB_NAME="ea"
fi

RETENTION_PROFILE="$(printf '%s' "${EA_RETENTION_PROFILE:-standard}" | tr '[:upper:]' '[:lower:]')"
case "${RETENTION_PROFILE}" in
  aggressive)
    DEFAULT_EXECUTION_EVENTS_DAYS=30
    DEFAULT_POLICY_DECISIONS_DAYS=30
    DEFAULT_OBSERVATIONS_DAYS=30
    DEFAULT_DELIVERY_SENT_DAYS=7
    DEFAULT_APPROVAL_REQUESTS_DAYS=60
    DEFAULT_APPROVAL_DECISIONS_DAYS=60
    ;;
  conservative)
    DEFAULT_EXECUTION_EVENTS_DAYS=180
    DEFAULT_POLICY_DECISIONS_DAYS=180
    DEFAULT_OBSERVATIONS_DAYS=120
    DEFAULT_DELIVERY_SENT_DAYS=60
    DEFAULT_APPROVAL_REQUESTS_DAYS=180
    DEFAULT_APPROVAL_DECISIONS_DAYS=180
    ;;
  standard)
    DEFAULT_EXECUTION_EVENTS_DAYS=90
    DEFAULT_POLICY_DECISIONS_DAYS=90
    DEFAULT_OBSERVATIONS_DAYS=60
    DEFAULT_DELIVERY_SENT_DAYS=30
    DEFAULT_APPROVAL_REQUESTS_DAYS=120
    DEFAULT_APPROVAL_DECISIONS_DAYS=120
    ;;
  *)
    echo "EA_RETENTION_PROFILE must be aggressive|standard|conservative" >&2
    exit 2
    ;;
esac

EXECUTION_EVENTS_DAYS="${EA_RETENTION_EXECUTION_EVENTS_DAYS:-${DEFAULT_EXECUTION_EVENTS_DAYS}}"
POLICY_DECISIONS_DAYS="${EA_RETENTION_POLICY_DECISIONS_DAYS:-${DEFAULT_POLICY_DECISIONS_DAYS}}"
OBSERVATIONS_DAYS="${EA_RETENTION_OBSERVATIONS_DAYS:-${DEFAULT_OBSERVATIONS_DAYS}}"
DELIVERY_SENT_DAYS="${EA_RETENTION_DELIVERY_SENT_DAYS:-${DEFAULT_DELIVERY_SENT_DAYS}}"
APPROVAL_REQUESTS_DAYS="${EA_RETENTION_APPROVAL_REQUESTS_DAYS:-${DEFAULT_APPROVAL_REQUESTS_DAYS}}"
APPROVAL_DECISIONS_DAYS="${EA_RETENTION_APPROVAL_DECISIONS_DAYS:-${DEFAULT_APPROVAL_DECISIONS_DAYS}}"
BATCH_SIZE="${EA_RETENTION_BATCH_SIZE:-500}"
MAX_ROWS_PER_TABLE="${EA_RETENTION_MAX_ROWS_PER_TABLE:-10000}"
LOCK_TIMEOUT_MS="${EA_RETENTION_LOCK_TIMEOUT_MS:-2000}"
STATEMENT_TIMEOUT_MS="${EA_RETENTION_STATEMENT_TIMEOUT_MS:-30000}"
JOURNAL_MAX_ROWS="${EA_RETENTION_JOURNAL_MAX_ROWS:-1000}"
STALE_RUN_SECONDS="${EA_RETENTION_STALE_RUN_SECONDS:-7200}"
VACUUM_AFTER_APPLY="${EA_RETENTION_VACUUM_AFTER_APPLY:-0}"

require_integer_range() {
  local name="$1"
  local value="$2"
  local minimum="$3"
  local maximum="$4"
  if ! [[ "${value}" =~ ^[0-9]+$ ]] || (( value < minimum || value > maximum )); then
    echo "${name} must be an integer in [${minimum},${maximum}]" >&2
    exit 2
  fi
}

for value_name in \
  EXECUTION_EVENTS_DAYS POLICY_DECISIONS_DAYS OBSERVATIONS_DAYS \
  DELIVERY_SENT_DAYS APPROVAL_REQUESTS_DAYS APPROVAL_DECISIONS_DAYS
do
  require_integer_range "${value_name}" "${!value_name}" 1 3650
done
require_integer_range EA_RETENTION_BATCH_SIZE "${BATCH_SIZE}" 1 10000
require_integer_range EA_RETENTION_MAX_ROWS_PER_TABLE "${MAX_ROWS_PER_TABLE}" 1 1000000
require_integer_range EA_RETENTION_LOCK_TIMEOUT_MS "${LOCK_TIMEOUT_MS}" 100 60000
require_integer_range EA_RETENTION_STATEMENT_TIMEOUT_MS "${STATEMENT_TIMEOUT_MS}" 1000 600000
require_integer_range EA_RETENTION_JOURNAL_MAX_ROWS "${JOURNAL_MAX_ROWS}" 10 100000
require_integer_range EA_RETENTION_STALE_RUN_SECONDS "${STALE_RUN_SECONDS}" 300 86400
if [[ "${VACUUM_AFTER_APPLY}" != "0" && "${VACUUM_AFTER_APPLY}" != "1" ]]; then
  echo "EA_RETENTION_VACUUM_AFTER_APPLY must be 0 or 1" >&2
  exit 2
fi

DEFAULT_TABLES=(
  execution_events
  policy_decisions
  observation_events
  delivery_outbox
  approval_decisions
  approval_requests
)

is_allowed_table() {
  case "$1" in
    execution_events|policy_decisions|observation_events|delivery_outbox|approval_decisions|approval_requests)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

parse_table_csv() {
  local raw_csv="$1"
  local -n destination="$2"
  local raw_table=""
  local table_name=""
  destination=()
  IFS=',' read -r -a raw_tables <<<"${raw_csv}"
  for raw_table in "${raw_tables[@]}"; do
    table_name="$(printf '%s' "${raw_table}" | xargs)"
    [[ -z "${table_name}" ]] && continue
    if ! is_allowed_table "${table_name}"; then
      echo "retention table is not allowlisted: ${table_name}" >&2
      exit 2
    fi
    destination+=("${table_name}")
  done
}

TABLES=("${DEFAULT_TABLES[@]}")
if [[ -n "${EA_RETENTION_TABLES:-}" ]]; then
  parse_table_csv "${EA_RETENTION_TABLES}" TABLES
fi
if [[ -n "${EA_RETENTION_SKIP_TABLES:-}" ]]; then
  SKIP_TABLES=()
  parse_table_csv "${EA_RETENTION_SKIP_TABLES}" SKIP_TABLES
  declare -A skip_table_map=()
  for table_name in "${SKIP_TABLES[@]}"; do
    skip_table_map["${table_name}"]=1
  done
  filtered_tables=()
  for table_name in "${TABLES[@]}"; do
    if [[ -z "${skip_table_map[${table_name}]+x}" ]]; then
      filtered_tables+=("${table_name}")
    fi
  done
  TABLES=("${filtered_tables[@]}")
fi
if [[ "${#TABLES[@]}" == "0" ]]; then
  echo "no retention tables selected after allowlist/skip filtering" >&2
  exit 2
fi

predicate_for_table() {
  case "$1" in
    execution_events)
      printf "created_at < CURRENT_TIMESTAMP - INTERVAL '%s days'" "${EXECUTION_EVENTS_DAYS}"
      ;;
    policy_decisions)
      printf "created_at < CURRENT_TIMESTAMP - INTERVAL '%s days'" "${POLICY_DECISIONS_DAYS}"
      ;;
    observation_events)
      printf "created_at < CURRENT_TIMESTAMP - INTERVAL '%s days'" "${OBSERVATIONS_DAYS}"
      ;;
    delivery_outbox)
      printf "status = 'sent' AND COALESCE(sent_at, created_at) < CURRENT_TIMESTAMP - INTERVAL '%s days'" "${DELIVERY_SENT_DAYS}"
      ;;
    approval_decisions)
      printf "created_at < CURRENT_TIMESTAMP - INTERVAL '%s days'" "${APPROVAL_DECISIONS_DAYS}"
      ;;
    approval_requests)
      printf "status IN ('approved','denied','expired','cancelled') AND created_at < CURRENT_TIMESTAMP - INTERVAL '%s days'" "${APPROVAL_REQUESTS_DAYS}"
      ;;
  esac
}

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

sql_scalar() {
  local query="$1"
  docker exec -i "${DB_CONTAINER}" psql -X -qAt -v ON_ERROR_STOP=1 \
    -U "${DB_USER}" -d "${DB_NAME}" -c "${query}" | tail -n 1 | tr -d '[:space:]'
}

table_exists() {
  [[ "$(sql_scalar "SELECT to_regclass('public.$1') IS NOT NULL")" == "t" ]]
}

echo "== PropertyQuarry DB retention =="
echo "mode=$([[ "${APPLY}" == "1" ]] && printf apply || printf dry-run)"
echo "target_service=${DB_SERVICE}"
echo "target_container=${DB_CONTAINER}"
echo "target_database=${DB_NAME}"
echo "profile=${RETENTION_PROFILE}"
echo "batch_size=${BATCH_SIZE}"
echo "max_rows_per_table=${MAX_ROWS_PER_TABLE}"
echo "tables=$(IFS=, ; printf '%s' "${TABLES[*]}")"

ensure_database_ready

JOURNAL_RUN_ID=""
JOURNAL_STARTED=0
if [[ "${APPLY}" == "1" ]]; then
  JOURNAL_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
  sql_scalar "
    CREATE TABLE IF NOT EXISTS public.propertyquarry_retention_runs (
      retention_run_id text PRIMARY KEY,
      scope text NOT NULL,
      mode text NOT NULL,
      status text NOT NULL,
      target_database text NOT NULL,
      profile text NOT NULL,
      actor text NOT NULL DEFAULT 'operator',
      batch_size integer NOT NULL,
      max_rows_per_table integer NOT NULL,
      candidate_rows bigint NOT NULL DEFAULT 0,
      deleted_rows bigint NOT NULL DEFAULT 0,
      compacted_rows bigint NOT NULL DEFAULT 0,
      database_bytes_before bigint NOT NULL DEFAULT 0,
      database_bytes_after bigint NOT NULL DEFAULT 0,
      policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
      result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
      error_code text NOT NULL DEFAULT '',
      started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
      completed_at timestamptz NULL,
      CHECK (char_length(retention_run_id) BETWEEN 1 AND 128),
      CHECK (scope IN ('postgres_retention','property_search_retention')),
      CHECK (mode='apply'),
      CHECK (status IN ('running','completed','failed','abandoned')),
      CHECK (char_length(target_database) BETWEEN 1 AND 128),
      CHECK (char_length(profile) BETWEEN 1 AND 32),
      CHECK (char_length(actor) BETWEEN 1 AND 128),
      CHECK (batch_size BETWEEN 1 AND 10000),
      CHECK (max_rows_per_table BETWEEN 1 AND 1000000),
      CHECK (candidate_rows>=0),
      CHECK (deleted_rows>=0),
      CHECK (compacted_rows>=0),
      CHECK (database_bytes_before>=0),
      CHECK (database_bytes_after>=0),
      CHECK (pg_column_size(policy_json)<=65536),
      CHECK (pg_column_size(result_json)<=262144),
      CHECK (char_length(error_code)<=256),
      CHECK ((status='running' AND completed_at IS NULL) OR (status<>'running' AND completed_at IS NOT NULL))
    );
    CREATE INDEX IF NOT EXISTS idx_propertyquarry_retention_runs_started
      ON public.propertyquarry_retention_runs(started_at DESC, retention_run_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_propertyquarry_retention_single_running
      ON public.propertyquarry_retention_runs(scope, target_database)
      WHERE status='running';
    UPDATE public.propertyquarry_retention_runs
       SET status='abandoned', completed_at=CURRENT_TIMESTAMP, error_code='stale_operator_run'
     WHERE scope='postgres_retention' AND target_database=current_database()
       AND status='running'
       AND started_at < CURRENT_TIMESTAMP - INTERVAL '${STALE_RUN_SECONDS} seconds';
    INSERT INTO public.propertyquarry_retention_runs (
      retention_run_id,scope,mode,status,target_database,profile,actor,
      batch_size,max_rows_per_table,database_bytes_before,policy_json
    ) VALUES (
      '${JOURNAL_RUN_ID}','postgres_retention','apply','running',current_database(),
      '${RETENTION_PROFILE}',current_user,${BATCH_SIZE},${MAX_ROWS_PER_TABLE},
      pg_database_size(current_database()),
      jsonb_build_object('tables','$(IFS=, ; printf '%s' "${TABLES[*]}")',
                         'lock_timeout_ms',${LOCK_TIMEOUT_MS},
                         'statement_timeout_ms',${STATEMENT_TIMEOUT_MS},
                         'vacuum_after_apply',${VACUUM_AFTER_APPLY})
    );
    SELECT 1;
  " >/dev/null
  JOURNAL_STARTED=1
fi

mark_failed() {
  local exit_code=$?
  if [[ "${JOURNAL_STARTED}" == "1" ]]; then
    sql_scalar "UPDATE public.propertyquarry_retention_runs SET status='failed',completed_at=CURRENT_TIMESTAMP,error_code='operator_exit_${exit_code}' WHERE retention_run_id='${JOURNAL_RUN_ID}' AND status='running'; SELECT 1" >/dev/null || true
  fi
  exit "${exit_code}"
}
trap mark_failed ERR INT TERM

total_candidates=0
total_deleted=0
for table_name in "${TABLES[@]}"; do
  if ! table_exists "${table_name}"; then
    echo "${table_name}: missing"
    continue
  fi
  predicate="$(predicate_for_table "${table_name}")"
  candidates="$(sql_scalar "SELECT COUNT(*) FROM public.${table_name} WHERE ${predicate}")"
  total_candidates=$((total_candidates + candidates))
  if [[ "${APPLY}" != "1" ]]; then
    echo "${table_name}: candidates=${candidates}"
    continue
  fi

  table_deleted=0
  while (( table_deleted < MAX_ROWS_PER_TABLE )); do
    remaining=$((MAX_ROWS_PER_TABLE - table_deleted))
    current_batch="${BATCH_SIZE}"
    if (( remaining < current_batch )); then
      current_batch="${remaining}"
    fi
    deleted="$(sql_scalar "
      BEGIN;
      SET LOCAL lock_timeout='${LOCK_TIMEOUT_MS}ms';
      SET LOCAL statement_timeout='${STATEMENT_TIMEOUT_MS}ms';
      WITH candidates AS (
        SELECT ctid FROM public.${table_name}
        WHERE ${predicate}
        FOR UPDATE SKIP LOCKED
        LIMIT ${current_batch}
      ), deleted AS (
        DELETE FROM public.${table_name} AS target
        USING candidates
        WHERE target.ctid=candidates.ctid
        RETURNING 1
      ) SELECT COUNT(*) FROM deleted;
      COMMIT;
    ")"
    table_deleted=$((table_deleted + deleted))
    if (( deleted < current_batch )); then
      break
    fi
  done
  total_deleted=$((total_deleted + table_deleted))
  sql_scalar "
    UPDATE public.propertyquarry_retention_runs
       SET candidate_rows=candidate_rows+${candidates},
           deleted_rows=deleted_rows+${table_deleted},
           result_json=result_json || jsonb_build_object(
             '${table_name}',jsonb_build_object('candidates',${candidates},'deleted',${table_deleted})
           )
     WHERE retention_run_id='${JOURNAL_RUN_ID}' AND status='running';
    SELECT 1;
  " >/dev/null
  echo "${table_name}: candidates=${candidates} deleted=${table_deleted}"
  if (( table_deleted == MAX_ROWS_PER_TABLE && candidates > table_deleted )); then
    echo "${table_name}: row ceiling reached; rerun is required for remaining candidates"
  fi
  if [[ "${VACUUM_AFTER_APPLY}" == "1" && "${table_deleted}" -gt 0 ]]; then
    docker exec -i "${DB_CONTAINER}" psql -X -q -v ON_ERROR_STOP=1 \
      -U "${DB_USER}" -d "${DB_NAME}" \
      -c "VACUUM (ANALYZE) public.${table_name};" >/dev/null
  fi
done

if [[ "${APPLY}" == "1" ]]; then
  sql_scalar "
    UPDATE public.propertyquarry_retention_runs
       SET status='completed',completed_at=CURRENT_TIMESTAMP,
           database_bytes_after=pg_database_size(current_database())
     WHERE retention_run_id='${JOURNAL_RUN_ID}' AND status='running';
    WITH expired AS (
      SELECT retention_run_id
      FROM public.propertyquarry_retention_runs
      WHERE status<>'running'
      ORDER BY started_at DESC,retention_run_id DESC
      OFFSET ${JOURNAL_MAX_ROWS}
    ) DELETE FROM public.propertyquarry_retention_runs AS runs
      USING expired WHERE runs.retention_run_id=expired.retention_run_id;
    SELECT 1;
  " >/dev/null
  JOURNAL_STARTED=0
  trap - ERR INT TERM
  echo "retention_apply_complete journal_run_id=${JOURNAL_RUN_ID} candidates=${total_candidates} deleted=${total_deleted}"
else
  echo "retention_dry_run_complete candidates=${total_candidates}"
fi
