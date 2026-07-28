#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

smoke_suffix="${PROPERTYQUARRY_POSTGRES_SMOKE_SUFFIX:-$$}"
if ! [[ "${smoke_suffix}" =~ ^[a-z0-9_-]+$ ]]; then
  echo "PROPERTYQUARRY_POSTGRES_SMOKE_SUFFIX must use only lowercase letters, digits, underscores, or hyphens" >&2
  exit 2
fi
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-propertyquarry-postgres-smoke-${smoke_suffix}}"
export PROPERTYQUARRY_API_CONTAINER_NAME="${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-postgres-smoke-api-${smoke_suffix}}"
export PROPERTYQUARRY_DB_CONTAINER_NAME="${PROPERTYQUARRY_DB_CONTAINER_NAME:-propertyquarry-postgres-smoke-db-${smoke_suffix}}"
export PROPERTYQUARRY_MIGRATE_CONTAINER_NAME="${PROPERTYQUARRY_MIGRATE_CONTAINER_NAME:-propertyquarry-postgres-smoke-migrate-${smoke_suffix}}"

run_browser_e2e=0
for arg in "$@"; do
  case "${arg}" in
    --browser-e2e)
      run_browser_e2e=1
      ;;
    --help|-h)
      cat <<'USAGE'
Usage: bash scripts/smoke_property_postgres.sh [--browser-e2e]

Boots the production-mode PropertyQuarry compose app against PostgreSQL and
verifies its public brand, authentication boundary, and storage backend.

Options:
  --browser-e2e  Also run the network-served PostgreSQL Playwright contract
                 with an internal-only ephemeral CI session.
USAGE
      exit 0
      ;;
    *)
      echo "unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

if docker compose version >/dev/null 2>&1; then
  compose_cli="v2"
else
  compose_cli="v1"
fi
DC=()

export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-propertyquarry_ci_postgres}"
export EA_HOST_PORT="${EA_HOST_PORT:-$((20000 + ($$ % 20000)))}"
export EA_API_TOKEN="${EA_API_TOKEN:-propertyquarry-ci-api-token}"
export PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN="${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN:-propertyquarry-ci-render-bridge-token}"
smoke_erasure_secret="$(
  od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
)"
if ! [[ "${smoke_erasure_secret}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "property postgres smoke erasure secret generation failed" >&2
  exit 2
fi
# Never inherit a production erasure key into the disposable database.  The
# generated value is scoped to this process and its owner-only Compose env
# snapshot, and both the migrator and API receive the same key.
export PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET="${smoke_erasure_secret}"
export PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED="0"
export PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED="0"
base="http://localhost:${EA_HOST_PORT}"
api_container="${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api}"
browser_session_file=""
app_probe_file=""
container_browser_session_file=""
smoke_tmp_dir=""
smoke_tmp_identity=""
smoke_env_file=""
smoke_env_identity=""
compose_cleanup_armed=0

create_smoke_tmp_dir() {
  local candidate=""
  local candidate_identity=""
  local candidate_metadata=""
  umask 077
  candidate="$(
    mktemp -d -- \
      "/tmp/propertyquarry-postgres-smoke.${BASHPID}.XXXXXXXX"
  )" || {
    echo "property postgres smoke temporary directory cannot be created" >&2
    return 1
  }
  candidate_metadata="$(
    stat -c '%d:%i:%u:%a' -- "${candidate}" 2>/dev/null
  )" || {
    echo "property postgres smoke temporary directory cannot be inspected" >&2
    return 1
  }
  candidate_identity="${candidate_metadata%:*:*}"
  if [[ ! -d "${candidate}" || -L "${candidate}" ||
        "${candidate_metadata}" != \
          "${candidate_identity}:$(id -u):700" ]]; then
    echo "property postgres smoke temporary directory is not private" >&2
    return 1
  fi
  smoke_tmp_dir="${candidate}"
  smoke_tmp_identity="${candidate_identity}"
}

smoke_tmp_is_exact() {
  local current_identity=""
  case "${smoke_tmp_dir:-}" in
    /tmp/propertyquarry-postgres-smoke.*)
      [[ -d "${smoke_tmp_dir}" && ! -L "${smoke_tmp_dir}" ]] || return 1
      current_identity="$(
        stat -c '%d:%i' -- "${smoke_tmp_dir}" 2>/dev/null
      )" || return 1
      [[ -n "${smoke_tmp_identity}" &&
        "${current_identity}" == "${smoke_tmp_identity}" ]]
      ;;
    *)
      return 1
      ;;
  esac
}

cleanup_smoke_tmp_dir() {
  if [[ -z "${smoke_tmp_dir}" ]]; then
    return 0
  fi
  if ! smoke_tmp_is_exact; then
    echo \
      "refusing cleanup of replaced property postgres smoke temporary directory" \
      >&2
    return 1
  fi
  rm -rf --one-file-system -- "${smoke_tmp_dir}" || return 1
  smoke_tmp_dir=""
  smoke_tmp_identity=""
}

smoke_env_is_exact() {
  local current_metadata=""
  smoke_tmp_is_exact || return 1
  [[ -n "${smoke_env_file}" &&
    -n "${smoke_env_identity}" &&
    -f "${smoke_env_file}" &&
    ! -L "${smoke_env_file}" ]] || return 1
  current_metadata="$(
    stat -c '%d:%i:%u:%a' -- "${smoke_env_file}" 2>/dev/null
  )" || return 1
  [[ "${current_metadata}" == \
    "${smoke_env_identity}:$(id -u):600" ]]
}

snapshot_smoke_env() {
  local canonical_env_present=0
  local source_path=""
  local source_label=""
  local source_identity=""
  local source_metadata_before=""
  local source_metadata_after=""
  local opened_identity=""
  local source_sha_before=""
  local source_sha_after=""
  local snapshot_sha=""
  local digest_output=""
  local snapshot_metadata=""
  local source_fd=""

  smoke_tmp_is_exact || {
    echo "property postgres smoke temporary directory changed before env snapshot" >&2
    return 1
  }
  if [[ -e "${ROOT}/.env" || -L "${ROOT}/.env" ]]; then
    if [[ ! -f "${ROOT}/.env" || -L "${ROOT}/.env" ]]; then
      echo \
        "refusing nonregular pre-existing property postgres smoke .env" \
        >&2
      return 2
    fi
    canonical_env_present=1
    source_path="${ROOT}/.env"
    source_label="property postgres smoke .env"
  else
    source_path="${ROOT}/.env.example"
    source_label="property postgres smoke .env.example"
    if [[ ! -f "${source_path}" || -L "${source_path}" ]]; then
      echo "${source_label} is not a regular non-symlink file" >&2
      return 2
    fi
  fi

  source_identity="$(
    stat -c '%d:%i' -- "${source_path}" 2>/dev/null
  )" || {
    echo "${source_label} cannot be identified before snapshot" >&2
    return 1
  }
  exec {source_fd}<"${source_path}" || {
    echo "${source_label} cannot be opened for snapshot" >&2
    return 1
  }
  opened_identity="$(
    stat -Lc '%d:%i' -- "/proc/self/fd/${source_fd}" 2>/dev/null
  )" || {
    exec {source_fd}<&-
    echo "${source_label} snapshot descriptor cannot be identified" >&2
    return 1
  }
  if [[ "${opened_identity}" != "${source_identity}" ]]; then
    exec {source_fd}<&-
    echo "${source_label} changed before snapshot" >&2
    return 1
  fi
  source_metadata_before="$(
    stat -Lc '%d:%i:%s:%y:%z' -- "/proc/self/fd/${source_fd}" 2>/dev/null
  )" || {
    exec {source_fd}<&-
    echo "${source_label} metadata cannot be captured before snapshot" >&2
    return 1
  }
  digest_output="$(
    sha256sum -- "/proc/self/fd/${source_fd}"
  )" || {
    exec {source_fd}<&-
    echo "${source_label} cannot be hashed before snapshot" >&2
    return 1
  }
  source_sha_before="${digest_output%% *}"

  smoke_env_file="$(
    mktemp "${smoke_tmp_dir}/compose.env.XXXXXXXX"
  )" || {
    exec {source_fd}<&-
    echo "private property postgres smoke env file cannot be created" >&2
    return 1
  }
  if ! cp -- "/proc/self/fd/${source_fd}" "${smoke_env_file}" ||
    ! chmod 600 -- "${smoke_env_file}"; then
    exec {source_fd}<&-
    echo "private property postgres smoke env snapshot cannot be written" >&2
    return 1
  fi
  snapshot_metadata="$(
    stat -c '%d:%i:%u:%a' -- "${smoke_env_file}" 2>/dev/null
  )" || {
    exec {source_fd}<&-
    echo "private property postgres smoke env snapshot cannot be identified" >&2
    return 1
  }
  smoke_env_identity="${snapshot_metadata%:*:*}"
  if [[ ! -f "${smoke_env_file}" || -L "${smoke_env_file}" ||
        "${snapshot_metadata}" != \
          "${smoke_env_identity}:$(id -u):600" ]]; then
    exec {source_fd}<&-
    echo "private property postgres smoke env snapshot is not secure" >&2
    return 1
  fi
  source_metadata_after="$(
    stat -Lc '%d:%i:%s:%y:%z' -- "/proc/self/fd/${source_fd}" 2>/dev/null
  )" || {
    exec {source_fd}<&-
    echo "${source_label} metadata cannot be captured after snapshot" >&2
    return 1
  }
  digest_output="$(
    sha256sum -- "/proc/self/fd/${source_fd}"
  )" || {
    exec {source_fd}<&-
    echo "${source_label} cannot be hashed after snapshot" >&2
    return 1
  }
  source_sha_after="${digest_output%% *}"
  digest_output="$(
    sha256sum -- "${smoke_env_file}"
  )" || {
    exec {source_fd}<&-
    echo "private property postgres smoke env snapshot cannot be hashed" >&2
    return 1
  }
  snapshot_sha="${digest_output%% *}"

  if [[ "${source_metadata_after}" != "${source_metadata_before}" ||
        "${source_sha_after}" != "${source_sha_before}" ||
        "${snapshot_sha}" != "${source_sha_before}" ]]; then
    exec {source_fd}<&-
    echo "${source_label} changed while it was snapshotted" >&2
    return 1
  fi
  if [[ "${canonical_env_present}" == "1" ]]; then
    if [[ ! -f "${ROOT}/.env" || -L "${ROOT}/.env" ||
          "$(stat -c '%d:%i' -- "${ROOT}/.env" 2>/dev/null)" != \
            "${source_identity}" ]]; then
      exec {source_fd}<&-
      echo "property postgres smoke .env changed while it was snapshotted" >&2
      return 1
    fi
  elif [[ -e "${ROOT}/.env" || -L "${ROOT}/.env" ]]; then
    exec {source_fd}<&-
    echo "property postgres smoke .env appeared while defaults were snapshotted" >&2
    return 1
  fi
  exec {source_fd}<&-
  smoke_env_is_exact || {
    echo "private property postgres smoke env snapshot changed after creation" >&2
    return 1
  }
}

cleanup() {
  local failed=0
  if [[ "${compose_cleanup_armed}" == "1" ]]; then
    if smoke_env_is_exact; then
      "${DC[@]}" down -v >/dev/null 2>&1 || true
    else
      echo \
        "refusing compose cleanup with a replaced private property postgres smoke env" \
        >&2
      failed=1
    fi
  fi
  cleanup_smoke_tmp_dir || failed=1
  return "${failed}"
}

cleanup_on_exit() {
  local status="$1"
  trap '' HUP INT TERM
  trap - EXIT
  if ! cleanup && [[ "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}

terminate_from_signal() {
  trap '' HUP INT TERM
  local status="$1"
  trap - EXIT
  cleanup || true
  exit "${status}"
}

trap 'cleanup_on_exit "$?"' EXIT
trap 'terminate_from_signal 129' HUP
trap 'terminate_from_signal 130' INT
trap 'terminate_from_signal 143' TERM
create_smoke_tmp_dir
app_probe_file="${smoke_tmp_dir}/app-probe.html"
browser_session_file="${smoke_tmp_dir}/browser-session.json"
container_browser_session_file="/tmp/${smoke_tmp_dir##*/}.session.json"

set_env_value() {
  local key="$1"
  local value="$2"
  local current_identity=""
  local line=""
  local replaced=0
  local temp_file=""
  local temp_identity=""
  if ! [[ "${key}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
    echo "invalid env key: ${key}" >&2
    return 2
  fi
  if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    echo "multiline env values are not supported: ${key}" >&2
    return 2
  fi
  if ! smoke_env_is_exact; then
    echo \
      "refusing replaced private property postgres smoke env during setup" \
      >&2
    return 1
  fi
  temp_file="$(
    mktemp "${smoke_tmp_dir}/compose.env.update.XXXXXXXX"
  )" || {
    echo "private property postgres smoke env update cannot be created" >&2
    return 1
  }
  chmod 600 -- "${temp_file}" || {
    rm -f -- "${temp_file}"
    echo "private property postgres smoke env update cannot be secured" >&2
    return 1
  }
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${key}="* ]]; then
      if [[ "${replaced}" == "0" ]]; then
        printf '%s=%s\n' "${key}" "${value}" >> "${temp_file}"
        replaced=1
      fi
      continue
    fi
    printf '%s\n' "${line}" >> "${temp_file}"
  done < "${smoke_env_file}"
  if [[ "${replaced}" == "0" ]]; then
    printf '%s=%s\n' "${key}" "${value}" >> "${temp_file}"
  fi
  temp_identity="$(
    stat -c '%d:%i' -- "${temp_file}" 2>/dev/null
  )" || {
    rm -f -- "${temp_file}"
    echo "private property postgres smoke env update cannot be identified" >&2
    return 1
  }
  if ! smoke_env_is_exact; then
    rm -f -- "${temp_file}"
    echo \
      "refusing replaced private property postgres smoke env during setup" \
      >&2
    return 1
  fi
  if ! mv -T -- "${temp_file}" "${smoke_env_file}"; then
    rm -f -- "${temp_file}"
    echo "private property postgres smoke env update cannot be published" >&2
    return 1
  fi
  if [[ ! -f "${smoke_env_file}" || -L "${smoke_env_file}" ]]; then
    echo "private property postgres smoke env changed during publication" >&2
    return 1
  fi
  current_identity="$(
    stat -c '%d:%i' -- "${smoke_env_file}" 2>/dev/null
  )" || {
    echo "private property postgres smoke env cannot be identified after publication" >&2
    return 1
  }
  if [[ "${current_identity}" != "${temp_identity}" ]]; then
    echo "private property postgres smoke env changed during publication" >&2
    return 1
  fi
  smoke_env_identity="${temp_identity}"
  smoke_env_is_exact || {
    echo "private property postgres smoke env is not secure after publication" >&2
    return 1
  }
}

snapshot_smoke_env
if [[ "${compose_cli}" == "v2" ]]; then
  DC=(
    docker compose
    --env-file "${smoke_env_file}"
    -f docker-compose.property.yml
  )
else
  DC=(
    docker-compose
    --env-file "${smoke_env_file}"
    -f docker-compose.property.yml
  )
fi
set_env_value "POSTGRES_PASSWORD" "${POSTGRES_PASSWORD}"
set_env_value "DATABASE_URL" ""
set_env_value "EA_RUNTIME_MODE" "prod"
set_env_value "EA_STORAGE_BACKEND" "postgres"
set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "0"
set_env_value "EA_API_TOKEN" "${EA_API_TOKEN}"
set_env_value "EA_SIGNING_SECRET" "propertyquarry-ci-signing-secret"
set_env_value "EMAILIT_API_KEY" ""
set_env_value "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" "${smoke_erasure_secret}"
set_env_value "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN" "${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN}"
set_env_value "PROPERTYQUARRY_ENABLE_PUBLIC_SIDE_SURFACES" "0"
set_env_value "PROPERTYQUARRY_ENABLE_PUBLIC_RESULTS" "0"
set_env_value "PROPERTYQUARRY_ENABLE_PUBLIC_TOURS" "0"
set_env_value "PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES" "0"
set_env_value "PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED" "0"
set_env_value "PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED" "0"
if ! smoke_env_is_exact; then
  echo "private property postgres smoke env changed after setup" >&2
  exit 2
fi

echo "== property-postgres-smoke: boot property compose =="
compose_cleanup_armed=1
"${DC[@]}" up -d --build propertyquarry-db propertyquarry-api

echo "== property-postgres-smoke: wait for readiness =="
expected_ready_reason="$(PYTHONPATH=ea python3 -c 'from app.product.property_search_schema import LATEST_PROPERTY_SEARCH_SCHEMA_VERSION; print(f"postgres_ready:property_search_schema_v{LATEST_PROPERTY_SEARCH_SCHEMA_VERSION}")')"
ready_reason=""
for _ in $(seq 1 90); do
  ready_json="$(curl -sS --connect-timeout 2 --max-time 5 "${base}/health/ready" 2>/dev/null || true)"
  ready_reason="$(python3 -c 'import json,sys
raw=(sys.argv[1] if len(sys.argv)>1 else "").strip()
try:
    payload=json.loads(raw)
except Exception:
    print("")
    raise SystemExit(0)
print(str(payload.get("reason") or "")) if isinstance(payload, dict) else print("")' "${ready_json}")"
  if [[ "${ready_reason}" == "${expected_ready_reason}" ]]; then
    break
  fi
  sleep 1
done

if [[ "${ready_reason}" != "${expected_ready_reason}" ]]; then
  echo "expected ${expected_ready_reason}, got ${ready_reason:-empty}" >&2
  docker logs --tail 160 "${api_container}" >&2 || true
  exit 31
fi

echo "== property-postgres-smoke: verify public brand and auth boundary =="
landing="$(curl -sS --connect-timeout 2 --max-time 5 "${base}/")"
if ! grep -q "PropertyQuarry" <<<"${landing}"; then
  echo "landing page did not render PropertyQuarry branding" >&2
  exit 32
fi

app_code="$(curl -sS --connect-timeout 2 --max-time 5 -o "${app_probe_file}" -w '%{http_code}' "${base}/app/properties" || true)"
if [[ "${app_code}" != "401" && "${app_code}" != "303" ]]; then
  echo "expected authenticated app boundary, got HTTP ${app_code}" >&2
  cat "${app_probe_file}" >&2 || true
  exit 33
fi

version_json="$(curl -sS --connect-timeout 2 --max-time 5 "${base}/version")"
storage_backend="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("storage_backend",""))' "${version_json}")"
if [[ "${storage_backend}" != "postgres" ]]; then
  echo "expected postgres storage backend, got ${storage_backend}" >&2
  exit 34
fi

echo "== property-postgres-smoke: verify production runtime posture =="
runtime_mode="$(docker exec "${api_container}" /usr/local/bin/python -c 'import os; print(os.environ.get("EA_RUNTIME_MODE", ""), end="")')"
runtime_storage="$(docker exec "${api_container}" /usr/local/bin/python -c 'import os; print(os.environ.get("EA_STORAGE_BACKEND", ""), end="")')"
loopback_no_auth="$(docker exec "${api_container}" /usr/local/bin/python -c 'import os; print(os.environ.get("EA_ALLOW_LOOPBACK_NO_AUTH", ""), end="")')"
legacy_runtime_surfaces="$(docker exec "${api_container}" /usr/local/bin/python -c 'import os; print(os.environ.get("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", ""), end="")')"
worker_heartbeat_required="$(docker exec "${api_container}" /usr/local/bin/python -c 'import os; print(os.environ.get("PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED", ""), end="")')"
scheduler_heartbeat_required="$(docker exec "${api_container}" /usr/local/bin/python -c 'import os; print(os.environ.get("PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED", ""), end="")')"
api_token_configured="$(docker exec "${api_container}" /usr/local/bin/python -c 'import os; print("configured" if os.environ.get("EA_API_TOKEN") else "", end="")')"
if [[ "${runtime_mode}" != "prod" || "${runtime_storage}" != "postgres" ]]; then
  echo "expected prod/postgres runtime posture, got mode=${runtime_mode:-empty} storage=${runtime_storage:-empty}" >&2
  exit 35
fi
if [[ "${loopback_no_auth}" != "0" || "${legacy_runtime_surfaces}" != "0" ]]; then
  echo "expected loopback auth and legacy runtime surfaces disabled, got loopback=${loopback_no_auth:-empty} legacy=${legacy_runtime_surfaces:-empty}" >&2
  exit 36
fi
if [[ "${worker_heartbeat_required}" != "0" ||
      "${scheduler_heartbeat_required}" != "0" ]]; then
  echo "expected isolated API smoke heartbeat requirements disabled, got worker=${worker_heartbeat_required:-empty} scheduler=${scheduler_heartbeat_required:-empty}" >&2
  exit 38
fi
if [[ "${api_token_configured}" != "configured" ]]; then
  echo "expected the production API token to be configured" >&2
  exit 37
fi

if [[ "${run_browser_e2e}" == "1" ]]; then
  echo "== property-postgres-smoke: network-served Playwright contract =="
  docker exec \
    -e PROPERTYQUARRY_POSTGRES_BROWSER_E2E=1 \
    "${api_container}" \
    python /app/scripts/propertyquarry_postgres_browser_bootstrap.py \
    --write "${container_browser_session_file}" \
    > /dev/null
  docker cp "${api_container}:${container_browser_session_file}" "${browser_session_file}" >/dev/null
  docker exec "${api_container}" rm -f -- "${container_browser_session_file}"
  chmod 600 "${browser_session_file}"
  PROPERTYQUARRY_POSTGRES_BROWSER_E2E=1 \
    PROPERTYQUARRY_POSTGRES_BROWSER_BASE_URL="${base}" \
    PROPERTYQUARRY_POSTGRES_BROWSER_EXPECTED_READY_REASON="${expected_ready_reason}" \
    PROPERTYQUARRY_POSTGRES_BROWSER_SESSION_FILE="${browser_session_file}" \
    PYTHONPATH=ea \
    python3 -m pytest -q tests/e2e/test_propertyquarry_postgres_browser.py -p no:cacheprovider
fi

echo "property-postgres-smoke complete"
