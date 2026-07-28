#!/bin/bash -p
set -euo pipefail

PATH=/usr/bin:/bin
export PATH
readonly PATH
unset BASH_ENV ENV CDPATH GLOBIGNORE
unset GNUMAKEFLAGS MAKECMDGOALS MAKEFILES MAKEFLAGS MAKELEVEL MAKEOVERRIDES MAKE_RESTARTS MFLAGS

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_PYTHON="${EA_ROOT}/scripts/propertyquarry_release_python.sh"
RELEASE_DISPATCH="${EA_ROOT}/scripts/propertyquarry_release_make_dispatch.py"
readonly EA_ROOT RELEASE_PYTHON RELEASE_DISPATCH

if [[ -v PYTHON_BIN || -v PYTEST_PYTHON_BIN ]]; then
  printf '%s\n' "error: release interpreter override forbidden" >&2
  exit 2
fi
for override in \
  PYTHONHOME \
  PYTHONOPTIMIZE \
  PYTHONPATH \
  PYTHONPLATLIBDIR \
  PYTHONUSERBASE; do
  if [[ -v "${override}" ]]; then
    printf 'error: Python runtime override forbidden: %s\n' "${override}" >&2
    exit 2
  fi
done
unset override
unset PYTHONHOME PYTHONOPTIMIZE PYTHONPATH PYTHONPLATLIBDIR PYTHONUSERBASE
if [[ -v EA_TEST_PYTHON || -v EA_TEST_FILES ]]; then
  printf '%s\n' "error: postgres contract test override forbidden" >&2
  exit 2
fi
unset EA_TEST_PYTHON EA_TEST_FILES
if [[ -v PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED ]]; then
  printf '%s\n' "error: PropertyQuarry public-home smoke override forbidden" >&2
  exit 2
fi
unset PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED
for override in \
  BUILDKIT_HOST \
  BUILDX_BUILDER \
  BUILDX_CONFIG \
  COMPOSE_BAKE \
  COMPOSE_CONVERT_WINDOWS_PATHS \
  COMPOSE_DISABLE_ENV_FILE \
  COMPOSE_DOCKER_CLI_BUILD \
  COMPOSE_ENV_FILES \
  COMPOSE_FILE \
  COMPOSE_PATH_SEPARATOR \
  COMPOSE_PROJECT_NAME \
  COMPOSE_REMOVE_ORPHANS \
  DATABASE_URL \
  DOCKER_API_VERSION \
  DOCKER_CERT_PATH \
  DOCKER_CLI_PLUGIN_EXTRA_DIRS \
  DOCKER_CONFIG \
  DOCKER_CONTEXT \
  DOCKER_DEFAULT_PLATFORM \
  DOCKER_HOST \
  DOCKER_TLS \
  DOCKER_TLS_VERIFY \
  EA_API_SERVICE \
  EA_DB_CONTAINER \
  EA_DB_SERVICE \
  EA_RUNTIME_MODE \
  EA_SCHEDULER_SERVICE \
  EA_SMOKE_DB \
  EA_WORKER_SERVICE \
  POSTGRES_DB \
  POSTGRES_PASSWORD \
  POSTGRES_USER \
  PROPERTYQUARRY_API_SERVICE \
  PROPERTYQUARRY_API_CONTAINER_NAME \
  PROPERTYQUARRY_DB_SERVICE \
  PROPERTYQUARRY_DB_CONTAINER_NAME \
  PROPERTYQUARRY_SCHEDULER_SERVICE \
  PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME \
  PROPERTYQUARRY_WORKER_SERVICE \
  PROPERTYQUARRY_WORKER_CONTAINER_NAME; do
  if [[ -v "${override}" ]]; then
    printf 'error: postgres smoke topology override forbidden: %s\n' \
      "${override}" >&2
    exit 2
  fi
done
unset override
unset \
  BUILDKIT_HOST \
  BUILDX_BUILDER \
  BUILDX_CONFIG \
  COMPOSE_BAKE \
  COMPOSE_CONVERT_WINDOWS_PATHS \
  COMPOSE_DISABLE_ENV_FILE \
  COMPOSE_DOCKER_CLI_BUILD \
  COMPOSE_ENV_FILES \
  COMPOSE_FILE \
  COMPOSE_PATH_SEPARATOR \
  COMPOSE_PROJECT_NAME \
  COMPOSE_REMOVE_ORPHANS \
  DATABASE_URL \
  DOCKER_API_VERSION \
  DOCKER_CERT_PATH \
  DOCKER_CLI_PLUGIN_EXTRA_DIRS \
  DOCKER_CONFIG \
  DOCKER_CONTEXT \
  DOCKER_DEFAULT_PLATFORM \
  DOCKER_HOST \
  DOCKER_TLS \
  DOCKER_TLS_VERIFY \
  EA_API_SERVICE \
  EA_DB_CONTAINER \
  EA_DB_SERVICE \
  EA_RUNTIME_MODE \
  EA_SCHEDULER_SERVICE \
  EA_SMOKE_DB \
  EA_WORKER_SERVICE \
  POSTGRES_DB \
  POSTGRES_PASSWORD \
  POSTGRES_USER \
  PROPERTYQUARRY_API_SERVICE \
  PROPERTYQUARRY_API_CONTAINER_NAME \
  PROPERTYQUARRY_DB_SERVICE \
  PROPERTYQUARRY_DB_CONTAINER_NAME \
  PROPERTYQUARRY_SCHEDULER_SERVICE \
  PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME \
  PROPERTYQUARRY_WORKER_SERVICE \
  PROPERTYQUARRY_WORKER_CONTAINER_NAME
if [[ -v EA_HOST_PORT ]]; then
  printf '%s\n' "error: smoke target override forbidden: EA_HOST_PORT" >&2
  exit 2
fi
unset EA_HOST_PORT
for override in \
  ALL_PROXY \
  HTTP_PROXY \
  HTTPS_PROXY \
  all_proxy \
  http_proxy \
  https_proxy; do
  if [[ -v "${override}" ]]; then
    printf 'error: smoke proxy override forbidden: %s\n' "${override}" >&2
    exit 2
  fi
done
unset override
unset ALL_PROXY HTTP_PROXY HTTPS_PROXY all_proxy http_proxy https_proxy
export DOCKER_HOST=unix:///var/run/docker.sock
export DOCKER_CONFIG=/nonexistent
export NO_PROXY=localhost,127.0.0.1,::1
export no_proxy=localhost,127.0.0.1,::1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=0
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  ./scripts/bootstrap_propertyquarry_release_python.sh
  ./scripts/hard_exit_gates.sh

Runs the full flagship hard exit bundle:
  - full pytest suite
  - release preflight
  - LTD critical inventory/env verification
  - LTD flagship verified-subset verification
  - postgres contract tests
  - postgres smoke
  - postgres legacy smoke
  - Tibor smoke
  - pocket audio archive verification
EOF
  exit 0
fi

cd "${EA_ROOT}"
run_flagship_postgres_smoke() {
  COMPOSE_FILE="${EA_ROOT}/docker-compose.property.yml" \
  COMPOSE_PROJECT_NAME=propertyquarry \
  EA_DB_CONTAINER=propertyquarry-db-live \
  EA_SMOKE_DB=ea_smoke_runtime \
  POSTGRES_DB=ea_smoke_runtime \
  POSTGRES_USER=postgres \
  PROPERTYQUARRY_API_CONTAINER_NAME=propertyquarry-api \
  PROPERTYQUARRY_API_SERVICE=propertyquarry-api \
  PROPERTYQUARRY_DB_CONTAINER_NAME=propertyquarry-db-live \
  PROPERTYQUARRY_DB_SERVICE=propertyquarry-db \
  PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME=propertyquarry-scheduler \
  PROPERTYQUARRY_SCHEDULER_SERVICE=propertyquarry-scheduler \
  PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED=1 \
  PROPERTYQUARRY_WORKER_CONTAINER_NAME=propertyquarry-worker \
  PROPERTYQUARRY_WORKER_SERVICE=propertyquarry-worker \
    /bin/bash -p scripts/smoke_postgres.sh "$@"
}

"${RELEASE_PYTHON}" -m pytest -q
"${RELEASE_PYTHON}" "${RELEASE_DISPATCH}" release-preflight
"${RELEASE_PYTHON}" "${RELEASE_DISPATCH}" verify-ltd-critical-entries-authenticated
"${RELEASE_PYTHON}" "${RELEASE_DISPATCH}" verify-ltd-flagship-subset-authenticated
EA_TEST_PYTHON="${RELEASE_PYTHON}" /bin/bash -p scripts/test_postgres_contracts.sh
run_flagship_postgres_smoke
run_flagship_postgres_smoke --legacy-fixture
/bin/bash -p scripts/smoke_api_tibor.sh
"${RELEASE_PYTHON}" "${RELEASE_DISPATCH}" verify-pocket-audio-archive
