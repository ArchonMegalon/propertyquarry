#!/usr/bin/env bash
set -euo pipefail

curl() {
  # shellcheck disable=SC2317
  command curl -q "$@"
}

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_TMP_DIR=""
legacy_fixture=0
ORIGINAL_EA_API_TOKEN="${EA_API_TOKEN:-}"
API_SERVICE="${PROPERTYQUARRY_API_SERVICE:-${EA_API_SERVICE:-ea-api}}"
WORKER_SERVICE="${PROPERTYQUARRY_WORKER_SERVICE:-${EA_WORKER_SERVICE:-ea-worker}}"
SCHEDULER_SERVICE="${PROPERTYQUARRY_SCHEDULER_SERVICE:-${EA_SCHEDULER_SERVICE:-ea-scheduler}}"
DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"
PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED:-}"

create_smoke_tmp_dir() {
  local candidate=""
  candidate="$(mktemp -d -- "/tmp/propertyquarry-smoke-postgres.${BASHPID}.XXXXXXXX")"
  chmod 700 -- "${candidate}"
  SMOKE_TMP_DIR="${candidate}"
}

cleanup_smoke_tmp() {
  case "${SMOKE_TMP_DIR:-}" in
    /tmp/propertyquarry-smoke-postgres.*)
      rm -rf -- "${SMOKE_TMP_DIR}"
      SMOKE_TMP_DIR=""
      ;;
  esac
}
if [[ -z "${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}" ]]; then
  if [[ -n "${PROPERTYQUARRY_API_SERVICE:-}" ]]; then
    PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="1"
  else
    PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="0"
  fi
fi

for arg in "$@"; do
  case "${arg}" in
    --legacy-fixture)
      legacy_fixture=1
      ;;
    --print-service-selection)
      printf 'api=%s\nworker=%s\nscheduler=%s\ndb=%s\npublic_home_required=%s\n' \
        "${API_SERVICE}" "${WORKER_SERVICE}" "${SCHEDULER_SERVICE}" "${DB_SERVICE}" \
        "${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}"
      exit 0
      ;;
    --help|-h)
      cat <<'USAGE'
Usage:
  bash scripts/smoke_postgres.sh [--legacy-fixture]
  bash scripts/smoke_postgres.sh --print-service-selection

Runs a Postgres-backed smoke path against an isolated smoke database:
  1) starts the compose Postgres service with docker compose
  2) resets isolated smoke DB
  3) applies kernel migrations
  4) starts the compose API service pinned to isolated DB
  5) verifies /health/ready reason is postgres_ready
  6) runs scripts/smoke_api.sh
  7) exports OpenAPI and verifies paused session-step dependency examples
  8) verifies DB row growth for core runtime tables
  9) verifies `EA_RUNTIME_MODE=prod` fails fast instead of falling back to memory

Options:
  --legacy-fixture          Seed a legacy UUID/approval schema fixture before
                            bootstrap and validate migration-upgrade behavior.
                            In this mode, API smoke is skipped.
  --print-service-selection Print the resolved Compose service aliases and
                            public-home requirement, then exit without reading
                            files or invoking Docker.

Environment:
  EA_HOST_PORT              Optional host port override (falls back to .env or 8090)
  EA_DB_CONTAINER           Postgres container name (default: compose DB service container)
  POSTGRES_USER             Postgres user (default: postgres)
  POSTGRES_PASSWORD         Postgres password (falls back to .env)
  EA_SMOKE_DB               Isolated smoke database name (default: ea_smoke_runtime)
  PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED
                            Defaults to 1 for a PROPERTYQUARRY_API_SERVICE
                            override and 0 for the generic ea-api smoke profile
USAGE
      exit 0
      ;;
    *)
      echo "unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

create_smoke_tmp_dir
trap cleanup_smoke_tmp EXIT

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

env_template=""
if [[ -f "${EA_ROOT}/.env.example" ]]; then
  env_template="${EA_ROOT}/.env.example"
elif [[ -f "${EA_ROOT}/.env.local.example" ]]; then
  env_template="${EA_ROOT}/.env.local.example"
else
  echo "missing env template (.env.example or .env.local.example)" >&2
  exit 34
fi

created_env=0
env_had_file=0
env_backup=""
restore_api_env=0
scheduler_was_running=0
if [[ ! -f "${EA_ROOT}/.env" ]]; then
  cp "${env_template}" "${EA_ROOT}/.env"
  chmod 600 "${EA_ROOT}/.env"
  created_env=1
else
  env_had_file=1
  env_backup="$(mktemp "${SMOKE_TMP_DIR}/env-backup.XXXXXXXX")"
  cp "${EA_ROOT}/.env" "${env_backup}"
fi

HOST_PORT="${EA_HOST_PORT:-}"
if [[ -z "${HOST_PORT}" ]]; then
  HOST_PORT="$(grep -E '^EA_HOST_PORT=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
HOST_PORT="${HOST_PORT:-8090}"
BASE="http://localhost:${HOST_PORT}"

DB_CONTAINER="${EA_DB_CONTAINER:-${DB_SERVICE}}"
DB_USER="${POSTGRES_USER:-$(grep -E '^POSTGRES_USER=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)}"
DB_USER="${DB_USER:-postgres}"
DB_PASSWORD="${POSTGRES_PASSWORD:-$(grep -E '^POSTGRES_PASSWORD=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)}"
DB_PASSWORD="${DB_PASSWORD:-CHANGE_ME_STRONG}"
SMOKE_DB="${EA_SMOKE_DB:-ea_smoke_runtime}"

if [[ ! "${SMOKE_DB}" =~ ^[a-zA-Z0-9_]+$ ]]; then
  echo "EA_SMOKE_DB must match ^[a-zA-Z0-9_]+$" >&2
  exit 33
fi

cleanup() {
  local restore_services=("${API_SERVICE}" "${WORKER_SERVICE}")
  if [[ "${scheduler_was_running}" == "1" ]]; then
    restore_services+=("${SCHEDULER_SERVICE}")
  fi
  if [[ "${restore_api_env}" == "1" && "${env_had_file}" == "1" && -n "${env_backup}" && -f "${env_backup}" ]]; then
    cp "${env_backup}" "${EA_ROOT}/.env"
    "${DC[@]}" up -d --force-recreate "${restore_services[@]}" >/dev/null 2>&1 || true
  elif [[ "${scheduler_was_running}" == "1" ]]; then
    "${DC[@]}" up -d "${SCHEDULER_SERVICE}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${env_backup}" && -f "${env_backup}" ]]; then
    rm -f "${env_backup}"
  fi
  if [[ "${created_env}" == "1" ]]; then
    rm -f "${EA_ROOT}/.env"
  fi
  cleanup_smoke_tmp
}
trap cleanup EXIT

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${EA_ROOT}/.env"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "${EA_ROOT}/.env"
  else
    echo "${key}=${value}" >> "${EA_ROOT}/.env"
  fi
}

apply_legacy_fixture() {
  echo "== smoke-postgres: apply legacy fixture =="
  docker exec -i "${DB_CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${SMOKE_DB}" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS execution_sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS execution_events (
    event_id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES execution_sessions(session_id) ON DELETE CASCADE,
    level TEXT NOT NULL DEFAULT 'info',
    event_type TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS execution_steps (
    step_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES execution_sessions(session_id) ON DELETE CASCADE,
    step_order INT NOT NULL DEFAULT 0,
    step_key TEXT NOT NULL DEFAULT '',
    step_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    preconditions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_text TEXT,
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_request_id SERIAL PRIMARY KEY,
    draft_id UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_key TEXT NOT NULL DEFAULT 'default',
    principal_id TEXT NOT NULL DEFAULT 'local-user',
    request_status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS approval_decisions (
    approval_decision_id SERIAL PRIMARY KEY,
    approval_request_id BIGINT NOT NULL REFERENCES approval_requests(approval_request_id),
    decided_by TEXT NOT NULL DEFAULT 'system',
    decision TEXT NOT NULL DEFAULT 'pending',
    decision_payload_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL
}

validate_legacy_upgrade() {
  echo "== smoke-postgres: validate legacy migration upgrade =="

  type_match="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${SMOKE_DB}" -c "SELECT (SELECT data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='execution_sessions' AND column_name='session_id') = (SELECT data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='execution_steps' AND column_name='session_id');" | tr -d '[:space:]')"
  if [[ "${type_match}" != "t" ]]; then
    echo "legacy upgrade check failed: execution_steps.session_id type mismatch" >&2
    exit 41
  fi

  event_cols="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${SMOKE_DB}" -c "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='execution_events' AND column_name IN ('event_id','session_id','name','payload_json','created_at');" | tr -d '[:space:]')"
  if [[ "${event_cols}" -lt 5 ]]; then
    echo "legacy upgrade check failed: execution_events missing runtime columns" >&2
    exit 42
  fi

  event_id_type="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${SMOKE_DB}" -c "SELECT data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='execution_events' AND column_name='event_id';" | tr -d '[:space:]')"
  if [[ "${event_id_type}" != "text" ]]; then
    echo "legacy upgrade check failed: execution_events.event_id type mismatch" >&2
    exit 43
  fi

  step_cols="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${SMOKE_DB}" -c "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='execution_steps' AND column_name IN ('parent_step_id','step_kind','state','attempt_count','input_json','output_json','error_json','correlation_id','causation_id','actor_type','actor_id');" | tr -d '[:space:]')"
  if [[ "${step_cols}" -lt 11 ]]; then
    echo "legacy upgrade check failed: execution_steps missing runtime columns" >&2
    exit 44
  fi

  req_cols="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${SMOKE_DB}" -c "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='approval_requests' AND column_name IN ('approval_id','session_id','step_id','reason','requested_action_json','status','created_at','updated_at');" | tr -d '[:space:]')"
  if [[ "${req_cols}" -lt 8 ]]; then
    echo "legacy upgrade check failed: approval_requests missing runtime columns" >&2
    exit 45
  fi

  dec_cols="$(docker exec -i "${DB_CONTAINER}" psql -At -U "${DB_USER}" -d "${SMOKE_DB}" -c "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='approval_decisions' AND column_name IN ('decision_id','approval_id','session_id','step_id','decision','decided_by','reason','created_at');" | tr -d '[:space:]')"
  if [[ "${dec_cols}" -lt 8 ]]; then
    echo "legacy upgrade check failed: approval_decisions missing runtime columns" >&2
    exit 46
  fi
}

resolve_service_container() {
  local service="$1"
  local container=""
  container="$(docker ps --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -n1)"
  if [[ -z "${container}" ]]; then
    container="$(docker ps --filter "name=${service}" --format '{{.Names}}' | head -n1)"
  fi
  printf '%s' "${container}"
}

cd "${EA_ROOT}"

if "${DC[@]}" ps --status running --services 2>/dev/null | grep -Fxq "${SCHEDULER_SERVICE}"; then
  scheduler_was_running=1
  echo "== smoke-postgres: stop scheduler for isolated queue ownership =="
  "${DC[@]}" stop "${SCHEDULER_SERVICE}" >/dev/null
fi

echo "== smoke-postgres: compose up (db only) =="
"${DC[@]}" up -d --build "${DB_SERVICE}"

wait_for_postgres_sql() {
  local attempts="${1:-90}"
  local ready=""
  local consecutive=0
  for _ in $(seq 1 "${attempts}"); do
    ready="$(docker exec -i "${DB_CONTAINER}" psql -At -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres -c "SELECT 1" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "${ready}" == "1" ]]; then
      consecutive=$((consecutive + 1))
      if [[ "${consecutive}" -ge 3 ]]; then
        return 0
      fi
    else
      consecutive=0
    fi
    sleep 1
  done
  echo "postgres did not accept SQL connections in time" >&2
  docker logs --tail 120 "${DB_CONTAINER}" >&2 || true
  return 1
}

wait_for_postgres_sql 90

echo "== smoke-postgres: provision governed admission capacity owner =="
capacity_owner_state="$(
  docker exec -i "${DB_CONTAINER}" \
    psql --no-psqlrc --quiet --tuples-only --no-align \
      -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres \
    < "${EA_ROOT}/scripts/propertyquarry_disposable_capacity_owner.sql"
)"
capacity_owner_state="$(printf '%s' "${capacity_owner_state}" | tr -d '[:space:]')"
if [[ "${capacity_owner_state}" != "f|f|f|f|f|f|f|0" ]]; then
  echo "disposable admission capacity owner verification failed" >&2
  exit 47
fi

echo "== smoke-postgres: provision disposable PropertyQuarry runtime roles =="
runtime_role_count="$(
  docker exec -i "${DB_CONTAINER}" \
    psql --no-psqlrc --quiet --tuples-only --no-align \
      -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres \
    < "${EA_ROOT}/scripts/propertyquarry_disposable_runtime_roles.sql"
)"
runtime_role_count="$(printf '%s' "${runtime_role_count}" | tr -d '[:space:]')"
if [[ "${runtime_role_count}" != "3" ]]; then
  echo "disposable PropertyQuarry runtime role verification failed" >&2
  exit 48
fi

echo "== smoke-postgres: reset isolated db ${SMOKE_DB} =="
db_password_sql="${DB_PASSWORD//\'/\'\'}"
docker exec -i "${DB_CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres \
  -c "ALTER ROLE \"${DB_USER}\" WITH PASSWORD '${db_password_sql}';" >/dev/null

docker exec -i "${DB_CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${SMOKE_DB}' AND pid <> pg_backend_pid();" >/dev/null
docker exec -i "${DB_CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres \
  -c "DROP DATABASE IF EXISTS \"${SMOKE_DB}\";" >/dev/null
docker exec -i "${DB_CONTAINER}" psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d postgres \
  -c "CREATE DATABASE \"${SMOKE_DB}\";" >/dev/null

if [[ "${legacy_fixture}" == "1" ]]; then
  apply_legacy_fixture
fi

if grep -q '^DATABASE_URL=' "${EA_ROOT}/.env"; then
  sed -i "s|^DATABASE_URL=.*$|DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${DB_SERVICE}:5432/${SMOKE_DB}|" "${EA_ROOT}/.env"
else
  echo "DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${DB_SERVICE}:5432/${SMOKE_DB}" >> "${EA_ROOT}/.env"
fi
if grep -q '^EA_STORAGE_BACKEND=' "${EA_ROOT}/.env"; then
  sed -i 's|^EA_STORAGE_BACKEND=.*$|EA_STORAGE_BACKEND=postgres|' "${EA_ROOT}/.env"
elif grep -q '^EA_LEDGER_BACKEND=' "${EA_ROOT}/.env"; then
  sed -i 's|^EA_LEDGER_BACKEND=.*$|EA_STORAGE_BACKEND=postgres|' "${EA_ROOT}/.env"
else
  echo 'EA_STORAGE_BACKEND=postgres' >> "${EA_ROOT}/.env"
fi
set_env_value "EA_RUNTIME_MODE" "test"
set_env_value "EA_API_TOKEN" "smoke-postgres-token"
set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "1"
set_env_value "EA_OPERATOR_PRINCIPAL_IDS" "exec-1"
smoke_signing_secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
set_env_value "EA_SIGNING_SECRET" "${smoke_signing_secret}"
export EA_API_TOKEN="smoke-postgres-token"
export EA_ALLOW_LOOPBACK_NO_AUTH="1"
export EA_SIGNING_SECRET="${smoke_signing_secret}"

if [[ "${env_had_file}" == "1" ]]; then
  restore_api_env=1
fi

echo "== smoke-postgres: bootstrap migrations =="
POSTGRES_DB="${SMOKE_DB}" bash scripts/db_bootstrap.sh

if [[ "${legacy_fixture}" == "1" ]]; then
  validate_legacy_upgrade
  echo "smoke-postgres legacy fixture complete (${SMOKE_DB})"
  exit 0
fi

echo "== smoke-postgres: complete PropertyQuarry migration =="
"${DC[@]}" run --rm --no-deps --build "${API_SERVICE}" \
  python -m app.product.propertyquarry_schema migrate \
  --applied-by smoke-postgres

echo "== smoke-postgres: compose up (api + worker) =="
# With the default service alias, this is the override-safe equivalent of
# `docker compose up -d --no-deps --build --force-recreate ea-api ea-worker`.
"${DC[@]}" up -d --no-deps --build --force-recreate "${API_SERVICE}" "${WORKER_SERVICE}"

API_CONTAINER="$(resolve_service_container "${API_SERVICE}")"
if [[ -z "${API_CONTAINER}" ]]; then
  echo "could not resolve container for compose API service ${API_SERVICE}" >&2
  exit 40
fi

echo "== smoke-postgres: readiness check =="
ready_json=""
ready_reason=""
ready_http_code=""
ready_response_path="${SMOKE_TMP_DIR}/readiness.json"
for _ in $(seq 1 90); do
  ready_http_code="$(curl -sS --connect-timeout 2 --max-time 5 -o "${ready_response_path}" -w '%{http_code}' "${BASE}/health/ready" || true)"
  if [[ -f "${ready_response_path}" && ! -L "${ready_response_path}" ]]; then
    ready_json="$(cat "${ready_response_path}")"
  else
    ready_json=""
  fi
  ready_reason="$(python3 -c 'import json,sys
raw = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
if not raw:
    print("")
    raise SystemExit(0)
try:
    payload = json.loads(raw)
except Exception:
    print("")
    raise SystemExit(0)
if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
    print("")
else:
    print(str(payload.get("reason") or ""))' "${ready_json}")"
  if [[ "${ready_reason}" == "postgres_ready" ]]; then
    break
  fi
  sleep 1
done
if [[ "${ready_reason}" != "postgres_ready" ]]; then
  echo "expected readiness reason postgres_ready, got: ${ready_reason}" >&2
  echo "readiness http code: ${ready_http_code}" >&2
  echo "readiness payload: ${ready_json}" >&2
  docker logs --tail 120 "${API_CONTAINER}" >&2 || true
  exit 31
fi

echo "== smoke-postgres: api smoke =="
container_api_token="$(docker exec "${API_CONTAINER}" /bin/sh -lc 'printenv EA_API_TOKEN' 2>/dev/null || true)"
container_loopback_no_auth="$(docker exec "${API_CONTAINER}" /bin/sh -lc 'printenv EA_ALLOW_LOOPBACK_NO_AUTH' 2>/dev/null || true)"
container_operator_principal="$(docker exec "${API_CONTAINER}" /bin/sh -lc 'printenv EA_OPERATOR_PRINCIPAL_IDS | tr "," "\n" | sed -n "1p"' 2>/dev/null || true)"
container_operator_principal="${container_operator_principal:-${EA_OPERATOR_PRINCIPAL_ID:-exec-1}}"
if [[ "${container_loopback_no_auth}" != "1" ]]; then
  echo "expected ea-api smoke container to enable EA_ALLOW_LOOPBACK_NO_AUTH" >&2
  docker logs --tail 120 "${API_CONTAINER}" >&2 || true
  exit 39
fi
token_candidates=("${EA_API_TOKEN:-}" "${container_api_token}" "${ORIGINAL_EA_API_TOKEN}" "smoke-postgres-token" "CHANGE_ME_STRONG")
token_probe_response_path="${SMOKE_TMP_DIR}/token-probe.json"
for candidate_token in "${token_candidates[@]}"; do
  if [[ -z "${candidate_token}" ]]; then
    continue
  fi
  token_probe_code="$(curl -sS --connect-timeout 2 --max-time 5 -o "${token_probe_response_path}" -w '%{http_code}' \
    -H "Authorization: Bearer ${candidate_token}" \
    -H "X-EA-API-Token: ${candidate_token}" \
    -H "X-EA-Principal-ID: exec-1" \
    "${BASE}/v1/memory/candidates?limit=1" || true)"
  if [[ "${token_probe_code}" == "200" ]]; then
    export EA_API_TOKEN="${candidate_token}"
    break
  fi
done
smoke_api_output=""
smoke_api_status=0
for attempt in 1 2 3; do
  for _smoke_container_wait in $(seq 1 30); do
    if docker exec "${API_CONTAINER}" /bin/sh -lc 'mkdir -p /docker /app/scripts && ln -sfn /app /docker/property && ln -sfn /app /docker/EA' >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  docker cp "${EA_ROOT}/scripts/smoke_api.sh" "${API_CONTAINER}:/app/scripts/smoke_api.sh" >/dev/null
  docker cp "${EA_ROOT}/scripts/refresh_ltds_via_api.sh" "${API_CONTAINER}:/app/scripts/refresh_ltds_via_api.sh" >/dev/null
  docker cp "${EA_ROOT}/scripts/refresh_ltds_via_api.py" "${API_CONTAINER}:/app/scripts/refresh_ltds_via_api.py" >/dev/null
  set +e
  smoke_api_output="$(docker exec \
    -e EA_API_TOKEN="${EA_API_TOKEN:-}" \
    -e EA_HOST_PORT="8090" \
    -e EA_PRINCIPAL_ID="exec-1" \
    -e EA_OPERATOR_PRINCIPAL_ID="${container_operator_principal}" \
    -e PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}" \
    "${API_CONTAINER}" bash /app/scripts/smoke_api.sh 2>&1)"
  smoke_api_status=$?
  set -e
  if [[ "${smoke_api_status}" == "0" ]]; then
    printf '%s\n' "${smoke_api_output}"
    break
  fi
  printf '%s\n' "${smoke_api_output}" >&2
  if ! grep -Eq 'curl: \((7|22|52|56)\)|Connection reset by peer|Empty reply from server|HTTP 401|503 Service Unavailable|The requested URL returned error: 503' <<<"${smoke_api_output}"; then
    exit "${smoke_api_status}"
  fi
  if [[ "${attempt}" == "3" ]]; then
    exit "${smoke_api_status}"
  fi
  sleep 2
done

echo "== smoke-postgres: openapi export verification =="
bash scripts/export_openapi.sh >/dev/null
openapi_latest="${EA_ROOT}/artifacts/openapi_latest.json"
openapi_export_fields="$(python3 -c "import json,sys; from pathlib import Path; body=json.loads(Path(sys.argv[1]).read_text() or '{}'); schemas=((body.get('components') or {}).get('schemas') or {}); step_examples=((schemas.get('SessionStepOut') or {}).get('examples') or []); waiting=next((row for row in step_examples if row.get('step_id') == 'step-artifact-save-waiting-approval'), {}); blocked=next((row for row in step_examples if row.get('step_id') == 'step-artifact-save-blocked-human'), {}); rewrite_examples=((schemas.get('RewriteAcceptedOut') or {}).get('examples') or []); rewrite_approval=next((row for row in rewrite_examples if row.get('status') == 'awaiting_approval'), {}); rewrite_human=next((row for row in rewrite_examples if row.get('status') == 'awaiting_human'), {}); plan_examples=((schemas.get('PlanExecuteAcceptedOut') or {}).get('examples') or []); plan_approval=next((row for row in plan_examples if row.get('status') == 'awaiting_approval'), {}); plan_human=next((row for row in plan_examples if row.get('status') == 'awaiting_human'), {}); print('{}|{}|{}|{}|{}|{}|{}|{}|{}'.format(waiting.get('state',''), waiting.get('dependency_states') == {'step_policy_evaluate': 'completed'}, blocked.get('blocked_dependency_keys') == ['step_human_review'], rewrite_approval.get('approval_id',''), rewrite_human.get('human_task_id',''), rewrite_approval.get('next_action',''), rewrite_human.get('next_action',''), plan_approval.get('task_key',''), plan_human.get('task_key','')))" "${openapi_latest}")"
if [[ "${openapi_export_fields}" != "waiting_approval|True|True|approval-123|human-task-123|poll_or_subscribe|poll_or_subscribe|decision_brief_approval|stakeholder_briefing_review" ]]; then
  echo "expected exported OpenAPI snapshot to retain paused session-step and async acceptance examples; got ${openapi_export_fields}" >&2
  cat "${openapi_latest}" >&2
  exit 38
fi
echo "openapi export ok"

echo "== smoke-postgres: db status verification =="
status_out="$(POSTGRES_DB="${SMOKE_DB}" bash scripts/db_status.sh)"
echo "${status_out}"

sessions_count="$(awk -F': ' '/^execution_sessions:/ {v=$2} END {print v+0}' <<<"${status_out}")"
events_count="$(awk -F': ' '/^execution_events:/ {v=$2} END {print v+0}' <<<"${status_out}")"
policy_count="$(awk -F': ' '/^policy_decisions:/ {v=$2} END {print v+0}' <<<"${status_out}")"
queue_count="$(awk -F': ' '/^execution_queue:/ {v=$2} END {print v+0}' <<<"${status_out}")"

if [[ "${sessions_count}" -lt 1 || "${events_count}" -lt 1 || "${policy_count}" -lt 1 || "${queue_count}" -lt 1 ]]; then
  echo "postgres smoke failed: expected non-zero execution_sessions/execution_events/policy_decisions/execution_queue counts" >&2
  exit 32
fi

echo "== smoke-postgres: prod fail-fast check =="
set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "0"
export EA_ALLOW_LOOPBACK_NO_AUTH="0"
set_env_value "EA_RUNTIME_MODE" "prod"
set_env_value "EA_STORAGE_BACKEND" "auto"
set_env_value "EA_API_TOKEN" "smoke-prod-token"
set_env_value "DATABASE_URL" ""
"${DC[@]}" up -d --no-deps --build --force-recreate "${API_SERVICE}" >/dev/null
prod_status=""
for _ in $(seq 1 10); do
  prod_status="$(docker inspect -f '{{.State.Status}}' "${API_CONTAINER}" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "${prod_status}" == "exited" || "${prod_status}" == "dead" || "${prod_status}" == "restarting" ]]; then
    break
  fi
  sleep 1
done
prod_log_ok=0
for _ in $(seq 1 20); do
  if (docker logs "${API_CONTAINER}" 2>&1 || true) | grep -Eq "EA_RUNTIME_MODE=prod requires (EA_SIGNING_SECRET|DATABASE_URL|a durable postgres runtime profile)"; then
    prod_log_ok=1
    break
  fi
  sleep 1
done
if [[ "${prod_log_ok}" != "1" ]]; then
  echo "expected prod fail-fast log message from ea-api" >&2
  docker logs "${API_CONTAINER}" >&2 || true
  exit 36
fi
if [[ "${prod_status}" != "exited" && "${prod_status}" != "dead" && "${prod_status}" != "restarting" ]]; then
  prod_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${API_CONTAINER}" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "${prod_health}" == "healthy" ]]; then
    echo "expected prod auto-backend boot to fail fast; ea-api status=${prod_status} health=${prod_health}" >&2
    docker logs --tail 80 "${API_CONTAINER}" >&2 || true
    exit 35
  fi
fi
echo "prod fail-fast path ok"

echo "smoke-postgres complete (${SMOKE_DB})"
