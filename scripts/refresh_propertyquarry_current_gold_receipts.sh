#!/usr/bin/env bash
set -uo pipefail

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${EA_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${EA_ROOT}/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

fail_on_blocked=0
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/refresh_propertyquarry_current_gold_receipts.sh [--fail-on-blocked]

Refresh the current PropertyQuarry gold-proof receipts against the live stack
without running the full release-gate or deploy bundle.

Defaults:
  base URL:   PROPERTYQUARRY_LIVE_SMOKE_BASE_URL, PROPERTYQUARRY_LIVE_MOBILE_BASE_URL, or http://localhost:8097
  host:       PROPERTYQUARRY_LIVE_HOST_HEADER or propertyquarry.com
  API token:  EA_API_TOKEN or the running propertyquarry-api container env
  API ctr:    PROPERTYQUARRY_API_CONTAINER_NAME or propertyquarry-api
  adapter runtime proof: PROPERTYQUARRY_API_CONTAINER_NAME or propertyquarry-api

The script refreshes the current receipts that propertyquarry_gold_status.py
consumes for flagship live proof: public/auth/mobile/provider smokes, tour
controls, export discovery, vendor tooling, walkthrough provider proof and quality, browser-rendered
3D, billing/ID Austria, scene-video readiness, runtime reconstruction, service-owned
generated reconstruction, and the
static contract receipts that were previously going stale.

Optional notifications:
  PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED=1
    sends the current scene-video provider refresh ask to Telegram when the
    refreshed runtime receipts still show actionable MagicFit/OMagic gaps.
EOF
  exit 0
fi
if [[ "${1:-}" == "--fail-on-blocked" ]]; then
  fail_on_blocked=1
  shift
fi
if (( $# > 0 )); then
  echo "error: unsupported arguments: $*" >&2
  exit 2
fi

cd "${EA_ROOT}"
scene_video_shared_env_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE:-state/runtime/property_scene_video_shared.env}"
scene_video_shared_env_runtime_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_RUNTIME_FILE:-/home/ea/property_scene_video_shared.env}"
python3 scripts/property_scene_video_shared_env.py --output "${scene_video_shared_env_file}" >/dev/null

BASE_URL="${PROPERTYQUARRY_LIVE_SMOKE_BASE_URL:-${PROPERTYQUARRY_LIVE_MOBILE_BASE_URL:-http://localhost:8097}}"
HOST_HEADER="${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}"
API_CONTAINER="${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api}"
RENDER_CONTAINER="${PROPERTYQUARRY_RENDER_CONTAINER_NAME:-propertyquarry-render-tools}"
RENDER_SERVICE="${PROPERTYQUARRY_RENDER_SERVICE:-propertyquarry-render-tools}"
LIVE_PRINCIPAL_ID="${PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_PRINCIPAL_ID:-${EA_PRINCIPAL_ID:-cf-email:tibor.girschele@gmail.com}}"
LIVE_MOBILE_PRINCIPAL_ID="${PROPERTYQUARRY_LIVE_MOBILE_SMOKE_PRINCIPAL_ID:-pq-live-mobile-smoke}"
LIVE_PRESENTATION_PRINCIPAL_ID="${PROPERTYQUARRY_LIVE_PRESENTATION_E2E_PRINCIPAL_ID:-pq-live-presentation-e2e}"
LIVE_PLAN_LABEL="${PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL:-Agent}"
LIVE_COUNTRY_CODE="${PROPERTYQUARRY_LIVE_SMOKE_COUNTRY_CODE:-AT}"
SEARCH_RUN_TIMEOUT_SECONDS="${PROPERTYQUARRY_DEPLOY_PROVIDER_SEARCH_RUN_TIMEOUT_SECONDS:-60}"
PROVIDER_TIMEOUT_SECONDS="${PROPERTYQUARRY_DEPLOY_PROVIDER_SMOKE_TIMEOUT_SECONDS:-20}"
PUBLIC_TIMEOUT_SECONDS="${PROPERTYQUARRY_DEPLOY_PUBLIC_SMOKE_TIMEOUT_SECONDS:-8}"
AUTH_TIMEOUT_SECONDS="${PROPERTYQUARRY_DEPLOY_AUTHENTICATED_SMOKE_TIMEOUT_SECONDS:-20}"
MAP_PREVIEW_TIMEOUT_SECONDS="${PROPERTYQUARRY_DEPLOY_MAP_PREVIEW_GATE_TIMEOUT_SECONDS:-60}"
MOBILE_TIMEOUT_MS="${PROPERTYQUARRY_DEPLOY_MOBILE_SMOKE_TIMEOUT_MS:-30000}"
MOBILE_PROCESS_TIMEOUT_SECONDS="${PROPERTYQUARRY_DEPLOY_MOBILE_SMOKE_PROCESS_TIMEOUT_SECONDS:-300}"
WALKTHROUGH_QUALITY_PROCESS_TIMEOUT_SECONDS="${PROPERTYQUARRY_WALKTHROUGH_QUALITY_PROCESS_TIMEOUT_SECONDS:-180}"
WALKTHROUGH_QUALITY_FFPROBE_TIMEOUT_SECONDS="${PROPERTYQUARRY_WALKTHROUGH_QUALITY_FFPROBE_TIMEOUT_SECONDS:-20}"
WALKTHROUGH_QUALITY_FRAME_SAMPLE_TIMEOUT_SECONDS="${PROPERTYQUARRY_WALKTHROUGH_QUALITY_FRAME_SAMPLE_TIMEOUT_SECONDS:-45}"
WALKTHROUGH_PROVIDER_PROOF_TIMEOUT_SECONDS="${PROPERTYQUARRY_WALKTHROUGH_PROVIDER_PROOF_TIMEOUT_SECONDS:-180}"
RUNTIME_RECONSTRUCTION_CONTAINER="${PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_CONTAINER:-${API_CONTAINER}}"
RUNTIME_RECONSTRUCTION_SLUG="${PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_SMOKE_SLUG:-runtime-reconstruction-current-$(date +%Y%m%d%H%M%S)}"
SERVICE_GENERATED_RECONSTRUCTION_SLUG="${PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_SMOKE_SLUG:-service-generated-reconstruction-current-$(date +%Y%m%d%H%M%S)}"
TOUR_EXPORT_INCOMING_DIR="${PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR:-${PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR:-${EA_ROOT}/state/incoming_property_tours}}"

CURRENT_API_TOKEN="${EA_API_TOKEN:-}"
CURRENT_PUBLIC_TOUR_DIR="${EA_PUBLIC_TOUR_DIR:-}"
had_failures=0

mkdir -p \
  _completion/bts_methodology \
  _completion/furniture_styles \
  _completion/id_austria \
  _completion/property_gold_status \
  _completion/property_tour_controls \
  _completion/property_tour_exports \
  _completion/property_tour_ownership \
  _completion/provider_smoke \
  _completion/repair \
  _completion/release_hygiene \
  _completion/scene_video_readiness \
  _completion/security \
  _completion/smoke \
  _completion/tour_delivery \
  _completion/tours \
  _completion/whole_project_scope

log_step() {
  printf '\n==> %s\n' "$1"
}

warn_step() {
  had_failures=1
  printf 'warning: %s\n' "$1" >&2
}

run_allow_fail() {
  local label="$1"
  shift
  log_step "${label}"
  if "$@"; then
    return 0
  fi
  local code=$?
  warn_step "${label} failed with exit code ${code}"
  return 0
}

run_allow_fail_shell() {
  local label="$1"
  local command="$2"
  log_step "${label}"
  if bash -lc "${command}"; then
    return 0
  fi
  local code=$?
  warn_step "${label} failed with exit code ${code}"
  return 0
}

require_container() {
  local name="$1"
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is required for the live PropertyQuarry receipt refresh bundle." >&2
    exit 2
  fi
  if ! docker inspect "${name}" >/dev/null 2>&1; then
    echo "error: required container ${name} is not running." >&2
    exit 2
  fi
}

copy_scene_video_shared_env_to_container() {
  local container="$1"
  if [[ ! -f "${scene_video_shared_env_file}" ]]; then
    echo "error: missing scene-video shared env file ${scene_video_shared_env_file}" >&2
    return 1
  fi
  docker exec -i "${container}" sh -lc '
    umask 077
    cat > "$1"
    chmod 600 "$1"
  ' sh "${scene_video_shared_env_runtime_file}" < "${scene_video_shared_env_file}"
}

docker_exec_scene_video_python() {
  local container="$1"
  shift
  copy_scene_video_shared_env_to_container "${container}" || return 1
  docker exec "${container}" sh -lc '
    set -a
    . "$1"
    set +a
    shift
    exec python "$@"
  ' sh "${scene_video_shared_env_runtime_file}" "$@"
}

resolve_api_token() {
  if [[ -n "${CURRENT_API_TOKEN}" ]]; then
    return 0
  fi
  require_container "${API_CONTAINER}"
  CURRENT_API_TOKEN="$(docker exec "${API_CONTAINER}" sh -lc 'printf %s "${EA_API_TOKEN:-}"')"
  if [[ -z "${CURRENT_API_TOKEN}" ]]; then
    echo "error: EA_API_TOKEN is unset and the ${API_CONTAINER} container does not expose one." >&2
    exit 2
  fi
}

resolve_public_tour_dir() {
  if [[ -n "${CURRENT_PUBLIC_TOUR_DIR}" ]]; then
    return 0
  fi
  if [[ -d "${EA_ROOT}/state/public_property_tours" ]]; then
    CURRENT_PUBLIC_TOUR_DIR="${EA_ROOT}/state/public_property_tours"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker inspect "${API_CONTAINER}" >/dev/null 2>&1; then
    CURRENT_PUBLIC_TOUR_DIR="$(docker exec "${API_CONTAINER}" sh -lc 'printf %s "${EA_PUBLIC_TOUR_DIR:-/data/public_property_tours}"')"
  fi
  if [[ -z "${CURRENT_PUBLIC_TOUR_DIR}" ]]; then
    CURRENT_PUBLIC_TOUR_DIR="${EA_ROOT}/state/public_property_tours"
  fi
}

provider_e2e_receipt="_completion/provider_smoke/production-e2e-provider-matrix-current.json"
provider_catalog_receipt="_completion/smoke/property-live-provider-catalog-latest.json"
provider_latest_receipt="_completion/smoke/property-live-provider-latest.json"
tour_control_receipt="_completion/tours/property-tour-controls-live-container-current.json"
tour_control_release_gate_receipt="_completion/property_tour_controls/release-gate.json"
export_discovery_receipt="_completion/tours/property-tour-export-discovery-full-current.json"
import_manifest_receipt="_completion/property_tour_exports/import-manifest-current.json"
vendor_tooling_receipt="_completion/tours/property-tour-vendor-tooling-current.json"
scene_video_receipt="_completion/scene_video_readiness/release-gate.json"
scene_video_verifier_receipt="_completion/scene_video_readiness/release-gate-verifier.json"
scene_video_runtime_status_receipt="_completion/scene_video_readiness/runtime-status.json"
scene_video_refresh_packet="_completion/scene_video_readiness/provider-refresh-packet.json"
scene_video_refresh_packet_verifier="_completion/scene_video_readiness/provider-refresh-packet-verifier.json"
gold_status_receipt="_completion/property_gold_status/latest.json"
service_generated_reconstruction_receipt="_completion/tours/property-service-generated-reconstruction-current.json"
walkthrough_provider_proof_receipt="_completion/smoke/property-live-walkthrough-provider-proof-latest.json"
walkthrough_quality_receipt="_completion/smoke/property-live-walkthrough-quality-latest.json"

resolve_public_tour_dir
resolve_api_token
require_container "${API_CONTAINER}"
walkthrough_tour_root="${PROPERTYQUARRY_WALKTHROUGH_PROVIDER_PROOF_TOUR_ROOT:-${CURRENT_PUBLIC_TOUR_DIR}}"

run_allow_fail \
  "Security posture receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_security_posture.py \
    --write _completion/security/property-security-posture-latest.json
run_allow_fail \
  "Whole-project scope receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_whole_project_scope.py \
    --write _completion/whole_project_scope/property-whole-project-scope-latest.json
run_allow_fail \
  "Release hygiene receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_release_hygiene.py \
    --write _completion/release_hygiene/property-release-hygiene-latest.json
run_allow_fail \
  "Furniture-style contract receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_furniture_style_contract.py \
    --write _completion/furniture_styles/property-furniture-style-contract-latest.json
run_allow_fail \
  "BTS methodology contract receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_bts_methodology_contract.py \
    --write _completion/bts_methodology/property-bts-methodology-contract-latest.json

run_allow_fail_shell \
  "Live tour controls receipt" \
  "docker exec '${API_CONTAINER}' python /app/scripts/verify_property_tour_controls.py \
    --tour-root /data/public_property_tours \
    --live-probe \
    --base-url http://127.0.0.1:8090 \
    --host-header '${HOST_HEADER}' \
    --require-all-provider-modes \
    --write /data/artifacts/property-tour-controls-live-container-current.json \
    --summary-only >/dev/null && \
   docker cp '${API_CONTAINER}:/data/artifacts/property-tour-controls-live-container-current.json' '${tour_control_receipt}' >/dev/null && \
   cp '${tour_control_receipt}' '${tour_control_release_gate_receipt}'"
run_allow_fail_shell \
  "Live export-discovery receipt" \
  "docker exec '${API_CONTAINER}' python /app/scripts/discover_property_tour_exports.py \
    --drop-dir /data/incoming_property_tours \
    --public-tour-dir /data/public_property_tours \
    --write /data/artifacts/property-tour-export-discovery-full-current.json >/dev/null && \
   docker cp '${API_CONTAINER}:/data/artifacts/property-tour-export-discovery-full-current.json' '${export_discovery_receipt}' >/dev/null"
run_allow_fail_shell \
  "Live import-manifest receipt" \
  "docker exec --user root '${API_CONTAINER}' python /app/scripts/materialize_property_tour_export_manifest.py \
    --tour-root /data/public_property_tours \
    --incoming-root /data/incoming_property_tours \
    --prepare-dirs \
    --write /data/artifacts/property-tour-import-manifest-current.json >/dev/null && \
   docker cp '${API_CONTAINER}:/data/artifacts/property-tour-import-manifest-current.json' '${import_manifest_receipt}' >/dev/null"

run_allow_fail \
  "Vendor-tooling receipt from host with API runtime adapter proof" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_property_tour_vendor_tooling.py \
    --drop-dir "${TOUR_EXPORT_INCOMING_DIR}" \
    --tour-root "${CURRENT_PUBLIC_TOUR_DIR}" \
    --runtime-only \
    --runtime-container "${API_CONTAINER}" \
    --write "${vendor_tooling_receipt}"

refresh_scene_video_receipts() {
  docker_exec_scene_video_python "${API_CONTAINER}" /app/scripts/property_scene_video_readiness_report.py \
    --output /data/artifacts/property-scene-video-readiness-current.json >/dev/null
  docker_exec_scene_video_python "${API_CONTAINER}" /app/scripts/verify_property_scene_video_readiness.py \
    --receipt /data/artifacts/property-scene-video-readiness-current.json \
    --output /data/artifacts/property-scene-video-readiness-verifier-current.json >/dev/null
  docker_exec_scene_video_python "${API_CONTAINER}" /app/scripts/property_scene_video_runtime_status.py \
    --receipt /data/artifacts/property-scene-video-readiness-current.json \
    --output /data/artifacts/property-scene-video-runtime-status-current.json >/dev/null
  docker_exec_scene_video_python "${API_CONTAINER}" /app/scripts/materialize_scene_video_provider_refresh_packet.py \
    --receipt /data/artifacts/property-scene-video-readiness-current.json \
    --output /data/artifacts/property-scene-video-provider-refresh-packet-current.json >/dev/null
  docker_exec_scene_video_python "${API_CONTAINER}" /app/scripts/verify_scene_video_provider_refresh_packet.py \
    --packet /data/artifacts/property-scene-video-provider-refresh-packet-current.json \
    --output /data/artifacts/property-scene-video-provider-refresh-packet-verifier-current.json >/dev/null
  docker cp "${API_CONTAINER}:/data/artifacts/property-scene-video-readiness-current.json" "${scene_video_receipt}" >/dev/null
  docker cp "${API_CONTAINER}:/data/artifacts/property-scene-video-readiness-verifier-current.json" "${scene_video_verifier_receipt}" >/dev/null
  docker cp "${API_CONTAINER}:/data/artifacts/property-scene-video-runtime-status-current.json" "${scene_video_runtime_status_receipt}" >/dev/null
  docker cp "${API_CONTAINER}:/data/artifacts/property-scene-video-provider-refresh-packet-current.json" "${scene_video_refresh_packet}" >/dev/null
  docker cp "${API_CONTAINER}:/data/artifacts/property-scene-video-provider-refresh-packet-verifier-current.json" "${scene_video_refresh_packet_verifier}" >/dev/null
}

run_allow_fail \
  "Scene-video readiness receipts" \
  refresh_scene_video_receipts

run_allow_fail \
  "Tour-delivery contract receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_tour_delivery_contract.py \
    --tour-control-receipt "${tour_control_release_gate_receipt}" \
    --write _completion/tour_delivery/property-tour-delivery-contract-latest.json
run_allow_fail \
  "Billing handoff verification receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_brilliant_directories_provider.py
run_allow_fail \
  "ID Austria verification receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_id_austria_provider.py
run_allow_fail \
  "Authenticated performance receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_authenticated_performance_smoke.py \
    --write _completion/smoke/property-auth-performance-latest.json
run_allow_fail \
  "Public smoke receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_public_smoke.py \
    --base-url "${BASE_URL}" \
    --timeout-seconds "${PUBLIC_TIMEOUT_SECONDS}" \
    --write _completion/smoke/property-live-public-latest.json
run_allow_fail \
  "Authenticated smoke receipt" \
  env EA_API_TOKEN="${CURRENT_API_TOKEN}" PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_authenticated_smoke.py \
    --base-url "${BASE_URL}" \
    --principal-id "${LIVE_PRINCIPAL_ID}" \
    --expected-plan-label "${LIVE_PLAN_LABEL}" \
    --country-code "${LIVE_COUNTRY_CODE}" \
    --timeout-seconds "${AUTH_TIMEOUT_SECONDS}" \
    --write _completion/smoke/property-live-authenticated-latest.json
run_allow_fail \
  "Live mobile surface receipt" \
  timeout "${MOBILE_PROCESS_TIMEOUT_SECONDS}" \
    env EA_API_TOKEN="${CURRENT_API_TOKEN}" PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_mobile_surface_smoke.py \
    --base-url "${BASE_URL}" \
    --host-header "${HOST_HEADER}" \
    --api-token "${CURRENT_API_TOKEN}" \
    --principal-id "${LIVE_MOBILE_PRINCIPAL_ID}" \
    --seed-research-detail-fixture \
    --timeout-ms "${MOBILE_TIMEOUT_MS}" \
    --write _completion/smoke/property-live-mobile-surface-latest.json
run_allow_fail \
  "Map-preview flagship receipt" \
  env EA_API_TOKEN="${CURRENT_API_TOKEN}" PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_map_preview_flagship_gate.py \
    --base-url "${BASE_URL}" \
    --host-header "${HOST_HEADER}" \
    --principal-id "${LIVE_MOBILE_PRINCIPAL_ID}" \
    --timeout-seconds "${MAP_PREVIEW_TIMEOUT_SECONDS}" \
    --write _completion/smoke/property-live-map-preview-flagship-latest.json
run_allow_fail \
  "Provider catalog smoke receipt" \
  env EA_API_TOKEN="${CURRENT_API_TOKEN}" \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE=1 \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN=0 \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_PRINCIPAL_ID="${LIVE_PRINCIPAL_ID}" \
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_live_provider_smoke.py \
      --base-url "${BASE_URL}" \
      --no-execute-search-matrix \
      --no-cross-country-sanitization \
      --timeout-seconds "${PROVIDER_TIMEOUT_SECONDS}" \
      --write "${provider_catalog_receipt}"
run_allow_fail \
  "Provider E2E matrix receipt" \
  env EA_API_TOKEN="${CURRENT_API_TOKEN}" \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE=1 \
    PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E=1 \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN=0 \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_PRINCIPAL_ID="${LIVE_PRINCIPAL_ID}" \
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_live_provider_smoke.py \
      --base-url "${BASE_URL}" \
      --all-search-ready-countries \
      --execute-search-matrix \
      --resume-from "${provider_e2e_receipt}" \
      --timeout-seconds "${PROVIDER_TIMEOUT_SECONDS}" \
      --search-run-timeout-seconds "${SEARCH_RUN_TIMEOUT_SECONDS}" \
      --write "${provider_e2e_receipt}"
run_allow_fail_shell \
  "Provider latest alias" \
  "if [[ -f '${provider_e2e_receipt}' ]]; then cp '${provider_e2e_receipt}' '${provider_latest_receipt}'; else cp '${provider_catalog_receipt}' '${provider_latest_receipt}'; fi"
run_allow_fail_shell \
  "Repair canary receipt" \
  "env PYTHONPATH=ea '${PYTHON_BIN}' scripts/propertyquarry_repair_fleet_canary.py > _completion/repair/propertyquarry-repair-canary-latest.json"
run_allow_fail \
  "Tour-provider ownership receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_property_tour_provider_ownership.py \
    --write _completion/property_tour_ownership/release-gate.json
run_allow_fail \
  "Render-bridge runtime receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/ensure_propertyquarry_render_bridge_runtime.py \
    --container "${RENDER_CONTAINER}" \
    --service "${RENDER_SERVICE}" \
    --compose-file "${PROPERTYQUARRY_COMPOSE_FILE:-docker-compose.property.yml}" \
    --project-name "${PROPERTYQUARRY_COMPOSE_PROJECT_NAME:-${COMPOSE_PROJECT_NAME:-}}" \
    --write _completion/tours/property-render-bridge-runtime-current.json

if command -v docker >/dev/null 2>&1 && docker inspect "${RUNTIME_RECONSTRUCTION_CONTAINER}" >/dev/null 2>&1; then
  run_allow_fail \
    "Runtime reconstruction GLB receipt" \
    env PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_runtime_reconstruction_smoke.py \
      --container "${RUNTIME_RECONSTRUCTION_CONTAINER}" \
      --slug "${RUNTIME_RECONSTRUCTION_SLUG}" \
      --public-base-url "${BASE_URL}" \
      --host-header "${HOST_HEADER}" \
      --require-public-contract \
      --require-browser-shell \
      --require-glb \
      --write _completion/tours/property-runtime-reconstruction-release-gate.json \
      --fail-on-error
else
  warn_step "Runtime reconstruction container ${RUNTIME_RECONSTRUCTION_CONTAINER} is unavailable; leaving the existing reconstruction receipt in place."
fi
run_allow_fail \
  "Service generated-reconstruction receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_service_generated_reconstruction_smoke.py \
    --container "${API_CONTAINER}" \
    --slug "${SERVICE_GENERATED_RECONSTRUCTION_SLUG}" \
    --public-base-url "${BASE_URL}" \
    --host-header "${HOST_HEADER}" \
    --require-public-contract \
    --require-browser-shell \
    --write "${service_generated_reconstruction_receipt}" \
    --fail-on-error

run_allow_fail \
  "Browser-rendered 3D receipt" \
  env PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_3d_browser_gate.py \
    --base-url "${BASE_URL}" \
    --host-header "${HOST_HEADER}" \
    --runtime-container "${API_CONTAINER}" \
    --screenshots-dir _completion/smoke/property-live-3d-browser-gate-screenshots \
    --write _completion/smoke/property-live-3d-browser-gate-latest.json
if ! rm -f "${walkthrough_provider_proof_receipt}" "${walkthrough_quality_receipt}"; then
  echo "error: could not clear stale walkthrough provider-proof and quality receipts." >&2
  exit 1
fi
run_allow_fail \
  "Walkthrough provider-proof receipt" \
  timeout "${WALKTHROUGH_PROVIDER_PROOF_TIMEOUT_SECONDS}" \
    env PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_walkthrough_provider_proof_gate.py \
      --tour-root "${walkthrough_tour_root}" \
      --write "${walkthrough_provider_proof_receipt}"
run_allow_fail \
  "Walkthrough quality receipt" \
  timeout "${WALKTHROUGH_QUALITY_PROCESS_TIMEOUT_SECONDS}" \
    env PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_walkthrough_quality_gate.py \
      --tour-root "${walkthrough_tour_root}" \
      --provider-proof-receipt "${walkthrough_provider_proof_receipt}" \
      --ffprobe-timeout-seconds "${WALKTHROUGH_QUALITY_FFPROBE_TIMEOUT_SECONDS}" \
      --frame-sample-timeout-seconds "${WALKTHROUGH_QUALITY_FRAME_SAMPLE_TIMEOUT_SECONDS}" \
      --write "${walkthrough_quality_receipt}"

gold_args=(
  "--performance-receipt" "_completion/smoke/property-auth-performance-latest.json"
  "--tour-control-receipt" "${tour_control_receipt}"
  "--export-discovery-receipt" "${export_discovery_receipt}"
  "--import-manifest-receipt" "${import_manifest_receipt}"
  "--repair-canary-receipt" "_completion/repair/propertyquarry-repair-canary-latest.json"
  "--provider-catalog-receipt" "${provider_catalog_receipt}"
  "--provider-matrix-receipt" "${provider_latest_receipt}"
  "--billing-receipt" "_completion/brilliant_directories/BRILLIANT_DIRECTORIES_PROVIDER_VERIFICATION.generated.json"
  "--live-mobile-receipt" "_completion/smoke/property-live-mobile-surface-latest.json"
  "--public-smoke-receipt" "_completion/smoke/property-live-public-latest.json"
  "--authenticated-smoke-receipt" "_completion/smoke/property-live-authenticated-latest.json"
  "--tour-provider-ownership-receipt" "_completion/property_tour_ownership/release-gate.json"
  "--vendor-tooling-receipt" "${vendor_tooling_receipt}"
  "--whole-project-scope-receipt" "_completion/whole_project_scope/property-whole-project-scope-latest.json"
  "--security-posture-receipt" "_completion/security/property-security-posture-latest.json"
  "--release-hygiene-receipt" "_completion/release_hygiene/property-release-hygiene-latest.json"
  "--furniture-style-contract-receipt" "_completion/furniture_styles/property-furniture-style-contract-latest.json"
  "--bts-methodology-contract-receipt" "_completion/bts_methodology/property-bts-methodology-contract-latest.json"
  "--tour-delivery-contract-receipt" "_completion/tour_delivery/property-tour-delivery-contract-latest.json"
  "--map-preview-flagship-receipt" "_completion/smoke/property-live-map-preview-flagship-latest.json"
  "--browser-3d-gate-receipt" "_completion/smoke/property-live-3d-browser-gate-latest.json"
  "--runtime-reconstruction-receipt" "_completion/tours/property-runtime-reconstruction-release-gate.json"
  "--service-generated-reconstruction-receipt" "${service_generated_reconstruction_receipt}"
  "--walkthrough-provider-proof-receipt" "${walkthrough_provider_proof_receipt}"
  "--walkthrough-quality-receipt" "${walkthrough_quality_receipt}"
  "--scene-video-readiness-receipt" "${scene_video_receipt}"
  "--scene-video-readiness-verifier-receipt" "${scene_video_verifier_receipt}"
  "--scene-video-runtime-status-receipt" "${scene_video_runtime_status_receipt}"
  "--scene-video-provider-refresh-packet" "${scene_video_refresh_packet}"
  "--scene-video-provider-refresh-packet-verifier-receipt" "${scene_video_refresh_packet_verifier}"
  "--id-austria-receipt" "_completion/id_austria/ID_AUSTRIA_PROVIDER_VERIFICATION.generated.json"
  "--write" "${gold_status_receipt}"
)
if (( fail_on_blocked == 1 )); then
  gold_args+=("--fail-on-blocked")
fi

scene_video_refresh_notification_principal_id="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_PRINCIPAL_ID:-${EA_PRINCIPAL_ID:-propertyquarry-operator}}"
scene_video_refresh_notification_base_url="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_BASE_URL:-https://propertyquarry.com}"
scene_video_refresh_notification_state="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_STATE:-_completion/scene_video_readiness/provider-refresh-telegram-state.json}"
notification_prefer_container_runtime="${PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME:-1}"
scene_video_refresh_notification_report="_completion/scene_video_readiness/provider-refresh-telegram-report.json"
scene_video_refresh_notification_enabled="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED:-0}"
case "${scene_video_refresh_notification_enabled,,}" in
  1|true|yes|y|on|enabled)
    log_step "Scene-video provider refresh notification"
    if ! env PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME="${notification_prefer_container_runtime}" \
      PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_notify_scene_video_provider_refresh.py \
      --packet "${scene_video_refresh_packet}" \
      --verifier "${scene_video_refresh_packet_verifier}" \
      --runtime-status "${scene_video_runtime_status_receipt}" \
      --state-file "${scene_video_refresh_notification_state}" \
      --principal-id "${scene_video_refresh_notification_principal_id}" \
      --base-url "${scene_video_refresh_notification_base_url}" \
      --write "${scene_video_refresh_notification_report}" >/dev/null; then
      had_failures=1
      warn_step "Scene-video provider refresh notification failed"
      cat "${scene_video_refresh_notification_report}" >&2 2>/dev/null || true
    fi
    ;;
  *)
    mkdir -p "$(dirname "${scene_video_refresh_notification_report}")"
    printf '{"status":"skipped","reason":"PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED_not_set"}\n' > "${scene_video_refresh_notification_report}"
    ;;
esac

log_step "Gold-status receipt"
if ! env PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_gold_status.py "${gold_args[@]}" >/dev/null; then
  code=$?
  had_failures=1
  warn_step "Gold-status refresh failed with exit code ${code}"
fi

log_step "Gold-status summary"
python3 - <<'PY'
import json
from pathlib import Path

path = Path("_completion/property_gold_status/latest.json")
if not path.exists():
    print("gold_status: missing")
    raise SystemExit(0)
data = json.loads(path.read_text())
print("status:", data.get("status"))
blockers = list(data.get("blockers") or [])
print("blocker_count:", len(blockers))
for row in blockers[:12]:
    print("-", row.get("area"), "|", row.get("status"), "|", row.get("action"))
freshness = dict(data.get("receipt_freshness") or {})
print("receipt_freshness:", freshness.get("status"))
stale = list(freshness.get("stale_receipts") or [])
print("stale_receipt_count:", len(stale))
for row in stale[:12]:
    print("  *", row.get("area"), "| age_hours=", row.get("age_hours"))
PY

if (( had_failures == 1 )); then
  echo "PropertyQuarry gold-proof refresh finished with warnings; inspect the refreshed receipts and gold-status summary above." >&2
  exit 1
fi

echo "PropertyQuarry gold-proof receipts refreshed."
