#!/usr/bin/env bash
set -euo pipefail

PATH=/usr/local/bin:/usr/bin:/bin
export PATH
umask 077

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${APP_ROOT}"

COMPOSE_PROJECT_NAME="${PROPERTYQUARRY_COMPOSE_PROJECT_NAME:-property}"
LOCAL_RECEIPT="${PROPERTYQUARRY_LOCAL_DEPLOYMENT_RECEIPT:-${APP_ROOT}/state/release/propertyquarry-local-deployment.v1.json}"
ADMISSION_RECEIPT="${PROPERTYQUARRY_ADMISSION_DATABASE_RECEIPT:-${APP_ROOT}/state/release/propertyquarry-admission-database.v1.json}"
PREFLIGHT_ONLY=0
SKIP_BUILD=0

usage() {
  /usr/bin/printf '%s\n' \
    "Usage: scripts/deploy_propertyquarry.sh [--preflight-only] [--no-build]" \
    "" \
    "Authoritative local-Docker PropertyQuarry deployment." \
    "GitHub Actions and remote runners are not used." \
    "" \
    "--preflight-only  Validate repository, environment, Compose and images without mutation." \
    "--no-build        Deploy already-built immutable local images."
}

while (($#)); do
  case "$1" in
    --preflight-only)
      PREFLIGHT_ONLY=1
      ;;
    --no-build)
      SKIP_BUILD=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      /usr/bin/printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

load_env_file() {
  local source_path="$1"
  [[ -f "${source_path}" ]] || return 0
  if [[ -L "${source_path}" ]] ||
     [[ "$(/usr/bin/stat -c '%u' "${source_path}")" != "$(/usr/bin/id -u)" ]] ||
     [[ "$(/usr/bin/stat -c '%a' "${source_path}")" != "600" ]]; then
    /usr/bin/printf 'Local deployment env file must be owned by the operator and mode 0600: %s\n' \
      "${source_path}" >&2
    exit 2
  fi
  set -a
  # The local operator owns these ignored credential files.
  # shellcheck disable=SC1090
  source "${source_path}"
  set +a
}

import_existing_runtime_environment() {
  local container_id=""
  local row=""
  local key=""
  local value=""
  container_id="$(/usr/bin/docker ps --all --quiet \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter "label=com.docker.compose.service=propertyquarry-api" | /usr/bin/head -n 1)"
  [[ -n "${container_id}" ]] || return 0
  while IFS= read -r row; do
    [[ "${row}" == *=* ]] || continue
    key="${row%%=*}"
    value="${row#*=}"
    [[ "${key}" =~ ^(EA|PROPERTYQUARRY|TEABLE|EMAILIT|THREEDVISTA)_[A-Z0-9_]+$ ]] || continue
    [[ "${key}" != "POSTGRES_PASSWORD" ]] || continue
    if [[ -z "${!key:-}" ]]; then
      builtin printf -v "${key}" '%s' "${value}"
      export "${key}"
    fi
  done < <(
    /usr/bin/docker inspect "${container_id}" \
      --format '{{range .Config.Env}}{{println .}}{{end}}'
  )
}

require_value() {
  local key="$1"
  if [[ -z "${!key:-}" ]]; then
    /usr/bin/printf 'Required local deployment value is missing: %s\n' "${key}" >&2
    exit 2
  fi
}

load_env_file "${APP_ROOT}/.env"
load_env_file "${APP_ROOT}/.env.local"
load_env_file "${APP_ROOT}/state/runtime/propertyquarry_database_roles.env"
load_env_file "${APP_ROOT}/state/runtime/propertyquarry_admission.env"
load_env_file "${APP_ROOT}/state/runtime/propertyquarry_auth.env"
load_env_file "${APP_ROOT}/state/runtime/propertyquarry_google_identity.env"
load_env_file "${APP_ROOT}/state/runtime/propertyquarry_registration_email.env"
load_env_file "${APP_ROOT}/state/runtime/propertyquarry_render_bridge.env"
load_env_file "${APP_ROOT}/state/runtime/propertyquarry_magicfit_reviewer.env"
import_existing_runtime_environment

requested_mode="${EA_RUNTIME_MODE:-prod}"
requested_mode="${requested_mode,,}"
case "${requested_mode}" in
  prod|production)
    ;;
  *)
    /usr/bin/printf '%s\n' \
      "EA_RUNTIME_MODE must select production for the authoritative local deployment." >&2
    exit 2
    ;;
esac

: "${PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID:=${EA_GOOGLE_OAUTH_CLIENT_ID:-}}"
: "${PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET:=${EA_GOOGLE_OAUTH_CLIENT_SECRET:-}}"
: "${PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET:=${EA_GOOGLE_OAUTH_STATE_SECRET:-}}"
export \
  PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID \
  PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET \
  PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET

for required in \
  EA_SIGNING_SECRET \
  POSTGRES_PASSWORD \
  PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID \
  PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET \
  PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET \
  PROPERTYQUARRY_IDENTITY_SESSION_SECRET \
  PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN \
  PROPERTYQUARRY_API_DATABASE_URL \
  PROPERTYQUARRY_API_ADMISSION_DATABASE_URL \
  PROPERTYQUARRY_API_INGRESS_DATABASE_URL \
  PROPERTYQUARRY_MIGRATION_DATABASE_URL \
  PROPERTYQUARRY_WORKER_DATABASE_URL \
  PROPERTYQUARRY_SCHEDULER_DATABASE_URL \
  PROPERTYQUARRY_RENDER_DATABASE_URL \
  PROPERTYQUARRY_CF_TUNNEL_TOKEN; do
  require_value "${required}"
done

if [[ "${DOCKER_HOST:-unix:///var/run/docker.sock}" != "unix:///var/run/docker.sock" ]]; then
  /usr/bin/printf '%s\n' "Only the local Docker Unix socket is release authority." >&2
  exit 2
fi
if [[ -n "${DOCKER_CONTEXT:-}" && "${DOCKER_CONTEXT}" != "default" ]]; then
  /usr/bin/printf '%s\n' "A non-default Docker context is not allowed." >&2
  exit 2
fi

head_sha="$(/usr/bin/git rev-parse --verify 'HEAD^{commit}')"
python3 scripts/check_property_repository_role.py \
  --expected-repository ArchonMegalon/propertyquarry \
  --expected-role canonical \
  --expected-head-sha "${head_sha}" \
  --require-clean-worktree
manifest_authority="$(
  python3 - <<'PY'
from scripts.propertyquarry_launch_room import ROOT, _manifest_values

values = _manifest_values(ROOT)
for key in (
    "release_commit_sha",
    "release_repository",
    "release_branch",
    "release_public_origin",
    "release_artifact_set",
    "release_deployment_id",
    "release_label",
    "release_generated_at",
):
    value = values[key]
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        raise SystemExit(f"release manifest field is not canonical: {key}")
    print(value)
PY
)" || {
  /usr/bin/printf '%s\n' "Release manifest authority could not be loaded." >&2
  exit 2
}
mapfile -t release_authority <<<"${manifest_authority}"
if ((${#release_authority[@]} != 8)); then
  /usr/bin/printf '%s\n' "Release manifest authority has an invalid field count." >&2
  exit 2
fi
runtime_sha="${release_authority[0]}"
release_repository="${release_authority[1]}"
release_branch="${release_authority[2]}"
release_public_origin="${release_authority[3]}"
release_artifact_set="${release_authority[4]}"
release_deployment_id="${release_authority[5]}"
release_label="${release_authority[6]}"
release_generated_at="${release_authority[7]}"
if [[ ! "${runtime_sha}" =~ ^[0-9a-f]{40}$ ]] ||
   ! /usr/bin/git merge-base --is-ancestor "${runtime_sha}" "${head_sha}"; then
  /usr/bin/printf '%s\n' \
    "Release manifest runtime candidate is not a valid ancestor of the envelope HEAD." >&2
  exit 2
fi

unexpected="$(
  /usr/bin/docker ps --all \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --format '{{.Label "com.docker.compose.service"}}' |
    /usr/bin/sort -u |
    while IFS= read -r service; do
      case "${service}" in
        ""|propertyquarry-api|propertyquarry-migrate|propertyquarry-worker|propertyquarry-scheduler|propertyquarry-render-tools|propertyquarry-db|propertyquarry-cloudflared)
          ;;
        *)
          /usr/bin/printf '%s\n' "${service}"
          ;;
      esac
    done
)"
if [[ -n "${unexpected}" ]]; then
  /usr/bin/printf 'Refusing --remove-orphans with unexpected project services: %s\n' "${unexpected}" >&2
  exit 2
fi

export PROPERTYQUARRY_API_CONTAINER_NAME="${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api}"
export PROPERTYQUARRY_DB_CONTAINER_NAME="${PROPERTYQUARRY_DB_CONTAINER_NAME:-propertyquarry-db-live}"
export PROPERTYQUARRY_MIGRATE_CONTAINER_NAME="${PROPERTYQUARRY_MIGRATE_CONTAINER_NAME:-propertyquarry-migrate-live}"
export PROPERTYQUARRY_WORKER_CONTAINER_NAME="${PROPERTYQUARRY_WORKER_CONTAINER_NAME:-propertyquarry-worker-live}"
export PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME="${PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME:-propertyquarry-scheduler-live}"
export PROPERTYQUARRY_RENDER_CONTAINER_NAME="${PROPERTYQUARRY_RENDER_CONTAINER_NAME:-propertyquarry-render-live}"
export EA_RUNTIME_MODE=prod

release_compose_files=(
  --file docker-compose.property.yml
  --file docker-compose.cloudflared.yml
)
if [[ -n "${PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR:-}" ]]; then
  reviewer_trust_dir="$(
    /usr/bin/realpath -e -- "${PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR}"
  )" || {
    /usr/bin/printf '%s\n' \
      "MagicFit reviewer trust directory must resolve to an existing path." >&2
    exit 2
  }
  reviewer_trust_store="${reviewer_trust_dir}/trust-store.json"
  reviewer_dir_mode="$((8#$(/usr/bin/stat -c '%a' "${reviewer_trust_dir}")))"
  reviewer_file_mode="$((8#$(/usr/bin/stat -c '%a' "${reviewer_trust_store}")))"
  if [[ "${reviewer_trust_dir}" != /* ]] ||
     [[ -L "${PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR}" ]] ||
     [[ ! -d "${reviewer_trust_dir}" ]] ||
     [[ "$(/usr/bin/stat -c '%u' "${reviewer_trust_dir}")" != "0" ]] ||
     ((reviewer_dir_mode & 8#022)) ||
     [[ -L "${reviewer_trust_store}" ]] ||
     [[ ! -f "${reviewer_trust_store}" ]] ||
     [[ "$(/usr/bin/stat -c '%u' "${reviewer_trust_store}")" != "0" ]] ||
     ((reviewer_file_mode & 8#022)); then
    /usr/bin/printf '%s\n' \
      "MagicFit reviewer trust must be a non-writable root-owned directory and regular trust-store file." >&2
    exit 2
  fi
  PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR="${reviewer_trust_dir}"
  export PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR
  release_compose_files+=(--file docker-compose.property-magicfit-reviewer.yml)
fi

short_sha="${runtime_sha:0:12}"
web_tag="propertyquarry-standalone-web-runtime:local-${short_sha}"
render_tag="propertyquarry-standalone-render-runtime:local-${short_sha}"

if ((SKIP_BUILD == 0 && PREFLIGHT_ONLY == 0)); then
  PROPERTYQUARRY_WEB_IMAGE="${web_tag}" \
  PROPERTYQUARRY_RENDER_IMAGE="${render_tag}" \
    /usr/bin/docker compose \
      --project-name "${COMPOSE_PROJECT_NAME}" \
      --file docker-compose.property.yml \
      build propertyquarry-api propertyquarry-render-tools
fi

web_image="$(
  /usr/bin/docker image inspect "${web_tag}" --format '{{.Id}}' 2>/dev/null || true
)"
render_image="$(
  /usr/bin/docker image inspect "${render_tag}" --format '{{.Id}}' 2>/dev/null || true
)"
if [[ ! "${web_image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  /usr/bin/printf 'Expected local web image is unavailable: %s\n' "${web_tag}" >&2
  exit 2
fi
if [[ ! "${render_image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  /usr/bin/printf 'Expected local render image is unavailable: %s\n' "${render_tag}" >&2
  exit 2
fi

export PROPERTYQUARRY_WEB_IMAGE="${web_image}"
export PROPERTYQUARRY_RENDER_IMAGE="${render_image}"
export PROPERTYQUARRY_RELEASE_REPOSITORY="${release_repository}"
export PROPERTYQUARRY_RELEASE_BRANCH="${release_branch}"
export PROPERTYQUARRY_RELEASE_COMMIT_SHA="${runtime_sha}"
export PROPERTYQUARRY_RELEASE_IMAGE_DIGEST="${web_image}"
export PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID="${release_deployment_id}"
export PROPERTYQUARRY_RELEASE_PUBLIC_ORIGIN="${release_public_origin}"
export PROPERTYQUARRY_RELEASE_ARTIFACT_SET="${release_artifact_set}"
export PROPERTYQUARRY_RELEASE_LABEL="${release_label}"
export PROPERTYQUARRY_RELEASE_GENERATED_AT="${release_generated_at}"

/usr/bin/docker compose \
  --project-name "${COMPOSE_PROJECT_NAME}" \
  "${release_compose_files[@]}" \
  config --quiet

if ((PREFLIGHT_ONLY == 1)); then
  /usr/bin/printf 'READY local Docker deployment runtime=%s envelope=%s web=%s render=%s\n' \
    "${runtime_sha}" "${head_sha}" "${web_image}" "${render_image}"
  exit 0
fi

/usr/bin/docker compose \
  --project-name "${COMPOSE_PROJECT_NAME}" \
  --file docker-compose.property.yml \
  up --detach --wait --wait-timeout 120 propertyquarry-db

python3 scripts/provision_propertyquarry_admission_database.py \
  --runtime-image "${web_image}" \
  --database-container "${PROPERTYQUARRY_DB_CONTAINER_NAME}" \
  --database-host propertyquarry-db \
  --docker-network "${COMPOSE_PROJECT_NAME}_default" \
  --env-file "${APP_ROOT}/state/runtime/propertyquarry_admission.env" \
  --receipt "${ADMISSION_RECEIPT}" \
  > /dev/null

/usr/bin/docker compose \
  --project-name "${COMPOSE_PROJECT_NAME}" \
  "${release_compose_files[@]}" \
  up --detach --remove-orphans --wait --wait-timeout 420

local_port="${EA_HOST_PORT:-8097}"
python3 scripts/propertyquarry_local_deployment_receipt.py \
  --expected-commit "${runtime_sha}" \
  --expected-web-image "${web_image}" \
  --expected-render-image "${render_image}" \
  --compose-project "${COMPOSE_PROJECT_NAME}" \
  --local-origin "http://127.0.0.1:${local_port}" \
  --write "${LOCAL_RECEIPT}"

/usr/bin/printf 'DEPLOYED local Docker deployment runtime=%s envelope=%s receipt=%s\n' \
  "${runtime_sha}" "${head_sha}" "${LOCAL_RECEIPT}"
