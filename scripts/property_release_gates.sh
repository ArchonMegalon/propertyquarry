#!/bin/bash -p
PATH=/usr/sbin:/usr/bin:/sbin:/bin
IFS=$' \t\n'
LANG=C
LC_ALL=C
builtin export PATH IFS LANG LC_ALL
builtin unset \
  BASH_ENV ENV CDPATH GLOBIGNORE \
  LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONWARNINGS \
  PERL5LIB PERL5OPT RUBYLIB RUBYOPT NODE_PATH NODE_OPTIONS
# Privileged startup ignores inherited shell functions and BASH_ENV. These
# generated variables are readonly, so remove only their export attributes.
builtin export -n SHELLOPTS BASHOPTS 2>/dev/null || :
builtin umask 077
set -euo pipefail
set +x

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  builtin printf '%s\n' "Refusing sourced execution of the release gate." >&2
  return 2
fi

# Capture the protected probe credential before any command substitution or
# child process can inherit it. Only the two explicit consumers below receive a
# one-command environment assignment; the scalar itself is not exported.
performance_release_probe_secret="${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET:-${PROPERTYQUARRY_LIVE_PROBE_SECRET:-}}"
unset PROPERTYQUARRY_PERFORMANCE_AUTH_BOOTSTRAP_URL
unset PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET PROPERTYQUARRY_LIVE_PROBE_SECRET

PATH=/usr/bin:/bin
export PATH
readonly PATH
unset BASH_ENV ENV CDPATH GLOBIGNORE
IFS=$' \t\n'

script_source="${BASH_SOURCE[0]}"
[[ "${script_source}" == */* ]] || {
  printf '%s\n' "error: release gate must be invoked with an explicit path" >&2
  exit 2
}
EA_ROOT="$(cd -P -- "${script_source%/*}/.." && pwd -P)"
if [[ -v PYTHON_BIN || -v PYTEST_PYTHON_BIN ]]; then
  echo "error: release interpreter override forbidden" >&2
  exit 2
fi
unset PYTHON_BIN PYTEST_PYTHON_BIN
PYTHON_BIN="${EA_ROOT}/scripts/propertyquarry_release_python.sh"
readonly PYTHON_BIN

gold_scope="${PROPERTYQUARRY_GOLD_SCOPE:-core}"
if [[ "${1:-}" == "--gold-scope" ]]; then
  if [[ -z "${2:-}" ]]; then
    echo "error: --gold-scope requires core or advanced_visual." >&2
    exit 2
  fi
  gold_scope="$2"
  shift 2
fi
case "${gold_scope}" in
  core|advanced_visual)
    ;;
  *)
    echo "error: PROPERTYQUARRY_GOLD_SCOPE/--gold-scope must be core or advanced_visual." >&2
    exit 2
    ;;
esac

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  ./scripts/property_release_gates.sh [--gold-scope core|advanced_visual]

PROPERTYQUARRY_GOLD_SCOPE provides the same fail-closed selector. Core is the
default; advanced_visual adds provider-media generation and exact-candidate
binding gates without weakening any Core launch/customer-loop gate.

Runs the focused PropertyQuarry release bundle:
  - property workspace redesign browser contracts
  - exact canonical-property/public-propertyquarry mirror role and commit identity
  - browser surface link/action contracts and design-system registry gates
  - notification email and action-surface contracts
  - Heyy WhatsApp adapter, opt-in, STOP/START, webhook, and receipt contracts
  - FlipLink packet privacy, publication, and webhook contracts
  - MagicFit-only promo packet contracts
  - PropertyQuarry Teable tenant/projection contracts
  - phase and master exit-gate specs plus flagship browser workflows
  - property workspace real-browser greenfield checks
  - property search run contracts
  - authenticated eight-table Teable to atomic Postgres evidence-overlay receipt, cached unavailable/stale/verified states, and no inline source indexing
  - offline ranking benchmark for hard filters, soft scoring, ordering, and scout thresholds
  - property search storage schema guard
  - fresh authenticated private SLO metrics evidence bound to the exact release image and API replica set
  - release-bound encrypted off-host Postgres backup, exact-version provider retrieval, and disposable restore-drill evidence
  - saved search-agent management contracts
  - property market catalog contracts
  - PayFunnels checkout, webhook, refund, mismatch, and billing-surface contracts
  - workspace access token redaction, keyed hashes, revocation, and one-time launch-link contracts
  - ID Austria OIDC readiness receipt and Austrian-IP sign-in gating
  - live provider smoke receipt contracts
  - hosted tour control readiness receipts for polished 3D tours, panorama imports, and walkthroughs
  - scene-video provider readiness receipt and verifier for Mootion BrowserAct, MagicFit, OMagic/Magic, Telegram, and 1min boundaries
  - consolidated PropertyQuarry gold-status receipt for mobile/performance, provider matrix, tour controls, repair, and export discovery
  - furniture-style variant contract for five request-time 3D-tour styles, UI handoff, and style-aware cached rendering
  - BTS score-PDF methodology contract for source provenance and selected-district no-reward policy
  - public-safe tour delivery contract shape for polished 3D tours, panorama imports, and walkthroughs
  - hard browser-rendered 3D and walkthrough quality gates that fail on blank viewers, loading-only states, CSP/frame/network errors, missing room coverage, or frame jumps
  - live generated-reconstruction GLB export smoke that fails when Blender/NumPy tooling is missing or generated previews leak as public 3D tours
  - service-owned generated-reconstruction smoke that fails when the app bundle writer misses the first-party walkthrough contract, human route labels, delivery metadata, or public-safe layout-preview lane
  - required live mobile surface smoke: scripts/propertyquarry_live_mobile_surface_smoke.py against a deployed stack, including a current /app/research/{id} detail route
  - property artifact provider and sent-link manifest contracts
  - Brilliant Directories public-directory projection contracts
  - privacy-safe Rybbit contracts plus real browser collector and authenticated site/data/event-arrival receipt
  - Telegram titled-link delivery contracts
  - property browser journey contracts
  - dossier writer, Dadan video request, media factory, and premium dossier screenshot/quality contracts
  - public tour privacy, live-360, Matterport/3DVista, and asset hardening contracts
  - optional local visual-watch screenshot gate when PROPERTYQUARRY_VISUAL_WATCH_URL is set
EOF
  exit 0
fi

if [[ $# -ne 0 ]]; then
  echo "error: unsupported arguments. Use --gold-scope core|advanced_visual." >&2
  exit 2
fi

cd "${EA_ROOT}"
dr_backup_receipt="${PROPERTYQUARRY_DR_BACKUP_RECEIPT:-}"
dr_restore_receipt="${PROPERTYQUARRY_DR_RESTORE_RECEIPT:-}"
dr_release_commit_sha="${PROPERTYQUARRY_RELEASE_COMMIT_SHA:-}"
dr_release_image_digest="${PROPERTYQUARRY_RELEASE_IMAGE_DIGEST:-}"
expected_release_deployment_id="${PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID:-}"
expected_release_manifest_sha256="${PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256:-}"
expected_performance_chromium_path="${PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH:-}"
expected_performance_chromium_sha256="${PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256:-}"
dr_release_max_age_seconds="${PROPERTYQUARRY_DR_RELEASE_MAX_AGE_SECONDS:-86400}"
slo_metrics_snapshot="${PROPERTYQUARRY_SLO_METRICS_SNAPSHOT:-}"
slo_metrics_probe_receipt="${PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT:-}"
slo_evidence_receipt="${PROPERTYQUARRY_SLO_EVIDENCE_RECEIPT:-_completion/propertyquarry_slo_evidence/release-gate.json}"
monitoring_runtime_receipt="${PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT:-}"
prometheus_range_receipt="${PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT:-}"
prometheus_range_response="${PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE:-}"
alert_delivery_receipt="${PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT:-}"
dashboard_render_receipt="${PROPERTYQUARRY_DASHBOARD_RENDER_RECEIPT:-}"
structured_log_query_receipt="${PROPERTYQUARRY_STRUCTURED_LOG_QUERY_RECEIPT:-}"
distributed_trace_query_receipt="${PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT:-}"
continuous_ux_receipt="${PROPERTYQUARRY_CONTINUOUS_UX_RECEIPT:-}"
failure_state_receipt="${PROPERTYQUARRY_FAILURE_STATE_RECEIPT:-}"
activation_to_value_receipt="${PROPERTYQUARRY_ACTIVATION_TO_VALUE_RECEIPT:-}"
provider_catalog_receipt="${PROPERTYQUARRY_PROVIDER_CATALOG_RECEIPT:-}"
evidence_overlay_receipt="${PROPERTYQUARRY_EVIDENCE_OVERLAY_RECEIPT:-}"
rybbit_evidence_receipt="${PROPERTYQUARRY_RYBBIT_EVIDENCE_RECEIPT:-}"
expected_public_origin="${PROPERTYQUARRY_PUBLIC_ORIGIN:-${PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN:-}}"
expected_teable_origin="${PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN:-}"
expected_teable_base_id_sha256="${PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256:-}"
expected_rybbit_origin="${PROPERTYQUARRY_RYBBIT_ORIGIN:-}"
expected_rybbit_site_id_sha256="${PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256:-}"
performance_target_url="${PROPERTYQUARRY_PERFORMANCE_TARGET_URL:-${expected_public_origin%/}/app/search}"
gold_receipt="_completion/property_gold_status/release-gate.json"
gold_latest_receipt="_completion/property_gold_status/latest.json"
gold_root_latest_receipt="_completion/propertyquarry-gold-status-latest.json"
gold_notification_report="_completion/property_gold_status/telegram-notify-report.json"
notification_prefer_container_runtime="${PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME:-1}"

clear_gold_publication() {
  local target=""
  for target in \
    "${gold_receipt}" \
    "${gold_latest_receipt}" \
    "${gold_root_latest_receipt}" \
    "${gold_notification_report}"; do
    if [[ -e "${target}" || -L "${target}" ]]; then
      if [[ ! -f "${target}" && ! -L "${target}" ]]; then
        echo "error: refusing to replace non-file Gold publication target ${target}." >&2
        return 1
      fi
      rm -f -- "${target}"
    fi
  done
}
if [[ -z "${dr_backup_receipt}" || -z "${dr_restore_receipt}" ]]; then
  echo "error: PROPERTYQUARRY_DR_BACKUP_RECEIPT and PROPERTYQUARRY_DR_RESTORE_RECEIPT are required." >&2
  exit 2
fi
if [[ -z "${dr_release_commit_sha}" || -z "${dr_release_image_digest}" || -z "${expected_release_deployment_id}" || -z "${expected_release_manifest_sha256}" || -z "${expected_performance_chromium_path}" || -z "${expected_performance_chromium_sha256}" ]]; then
  echo "error: release SHA/image/deployment, controller-bound runtime-manifest SHA-256, and performance Chromium path/SHA-256 are required for exact release binding." >&2
  exit 2
fi
if [[ ! "${expected_release_manifest_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "error: PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256 must be an exact lowercase unprefixed 64-hex digest." >&2
  exit 2
fi
manifest_digest_first_char="${expected_release_manifest_sha256:0:1}"
manifest_digest_without_first_char="${expected_release_manifest_sha256//${manifest_digest_first_char}/}"
if [[ -z "${manifest_digest_without_first_char}" ]]; then
  echo "error: PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256 must be a non-placeholder digest." >&2
  exit 2
fi
unset manifest_digest_first_char manifest_digest_without_first_char
if [[ -z "${slo_metrics_snapshot}" || -z "${slo_metrics_probe_receipt}" ]]; then
  echo "error: PROPERTYQUARRY_SLO_METRICS_SNAPSHOT and PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT are required." >&2
  echo "Capture them from the authenticated private /internal/metrics route with scripts/propertyquarry_slo_capture.py." >&2
  exit 2
fi
for required_monitoring_input in \
  "${monitoring_runtime_receipt}" \
  "${prometheus_range_receipt}" \
  "${prometheus_range_response}" \
  "${alert_delivery_receipt}" \
  "${dashboard_render_receipt}" \
  "${structured_log_query_receipt}" \
  "${distributed_trace_query_receipt}"; do
  if [[ -z "${required_monitoring_input}" || ! -f "${required_monitoring_input}" || ! -r "${required_monitoring_input}" ]]; then
    echo "error: monitoring and operations evidence inputs must be explicit readable regular files." >&2
    echo "Set PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT, PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT," >&2
    echo "PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE, PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT," >&2
    echo "PROPERTYQUARRY_DASHBOARD_RENDER_RECEIPT, PROPERTYQUARRY_STRUCTURED_LOG_QUERY_RECEIPT," >&2
    echo "and PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT." >&2
    exit 2
  fi
done
for required_launch_receipt in \
  "${continuous_ux_receipt}" \
  "${failure_state_receipt}" \
  "${activation_to_value_receipt}" \
  "${provider_catalog_receipt}" \
  "${evidence_overlay_receipt}" \
  "${rybbit_evidence_receipt}"; do
  if [[ -z "${required_launch_receipt}" || ! -f "${required_launch_receipt}" ]]; then
    echo "error: launch-profile UX and provider receipt inputs must be explicit regular files." >&2
    echo "Set PROPERTYQUARRY_CONTINUOUS_UX_RECEIPT, PROPERTYQUARRY_FAILURE_STATE_RECEIPT," >&2
    echo "PROPERTYQUARRY_ACTIVATION_TO_VALUE_RECEIPT, PROPERTYQUARRY_PROVIDER_CATALOG_RECEIPT," >&2
    echo "PROPERTYQUARRY_EVIDENCE_OVERLAY_RECEIPT, and PROPERTYQUARRY_RYBBIT_EVIDENCE_RECEIPT." >&2
    exit 2
  fi
done
if [[ -z "${expected_public_origin}" || -z "${expected_teable_origin}" || -z "${expected_teable_base_id_sha256}" || -z "${expected_rybbit_origin}" || -z "${expected_rybbit_site_id_sha256}" ]]; then
  echo "error: PROPERTYQUARRY_PUBLIC_ORIGIN (or PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN), PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN," >&2
  echo "PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256, PROPERTYQUARRY_RYBBIT_ORIGIN, and" >&2
  echo "PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256 are required for launch-bound Rybbit evidence." >&2
  exit 2
fi
if [[ -z "${performance_target_url}" || -z "${performance_release_probe_secret}" ]]; then
  echo "error: PROPERTYQUARRY_PERFORMANCE_TARGET_URL/public origin and PROPERTYQUARRY_LIVE_PROBE_SECRET are required for launch-profile constrained authenticated performance evidence." >&2
  echo "The protected release-probe credential is scoped to fresh signed /app/search navigations and is never serialized into the receipt or inherited by the browser." >&2
  exit 2
fi
# Once a fully specified gate run starts, no previous passing Gold publication
# may survive a later failure.
clear_gold_publication
mkdir -p _completion/disaster_recovery
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_postgres_dr.py release-gate \
  --backup-receipt "${dr_backup_receipt}" \
  --restore-receipt "${dr_restore_receipt}" \
  --release-commit-sha "${dr_release_commit_sha}" \
  --image-digest "${dr_release_image_digest}" \
  --max-age-seconds "${dr_release_max_age_seconds}" \
  --receipt _completion/disaster_recovery/release-gate.json \
  > /dev/null
mkdir -p "$(dirname "${slo_evidence_receipt}")"
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_slo_evidence.py \
  --flagship \
  --release-sha "${dr_release_commit_sha}" \
  --image-digest "${dr_release_image_digest}" \
  --metrics-snapshot "${slo_metrics_snapshot}" \
  --metrics-probe "${slo_metrics_probe_receipt}" \
  --prometheus-range "${prometheus_range_response}" \
  --prometheus-range-receipt "${prometheus_range_receipt}" \
  --receipt "${slo_evidence_receipt}" \
  --overwrite-receipt \
  > /dev/null
mkdir -p _completion/propertyquarry_observability
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_observability_receipts.py verify \
  --release-sha "${dr_release_commit_sha}" \
  --image-digest "${dr_release_image_digest}" \
  --monitoring-receipt "${monitoring_runtime_receipt}" \
  --prometheus-range-receipt "${prometheus_range_receipt}" \
  --prometheus-range-response "${prometheus_range_response}" \
  --alert-delivery-receipt "${alert_delivery_receipt}" \
  --dashboard-render-receipt "${dashboard_render_receipt}" \
  --structured-log-query-receipt "${structured_log_query_receipt}" \
  --distributed-trace-query-receipt "${distributed_trace_query_receipt}" \
  --metrics-snapshot "${slo_metrics_snapshot}" \
  --metrics-probe "${slo_metrics_probe_receipt}" \
  --output _completion/propertyquarry_observability/release-gate.json \
  --overwrite \
  > /dev/null
if [[ "${gold_scope}" == "advanced_visual" ]]; then
  tour_export_incoming_dir="${PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR:-${PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR:-${EA_ROOT}/state/incoming_property_tours}}"
  scene_video_shared_env_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE:-state/runtime/property_scene_video_shared.env}"
  scene_video_shared_env_runtime_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_RUNTIME_FILE:-/home/ea/property_scene_video_shared.env}"
  PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_scene_video_shared_env.py \
    --output "${scene_video_shared_env_file}" \
    > /dev/null
  mkdir -p _completion/property_tour_exports _completion/scene_video_readiness

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
    copy_scene_video_shared_env_to_container "${container}"
    docker exec "${container}" sh -lc '
      set -a
      . "$1"
      set +a
      shift
      exec python "$@"
    ' sh "${scene_video_shared_env_runtime_file}" "$@"
  }
fi

PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_docs_links.py
mkdir -p _completion/security _completion/release_hygiene _completion/whole_project_scope
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_security_posture.py \
  --write _completion/security/property-security-posture-release-gate.json
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_repo_isolation.py
mkdir -p _completion/propertyquarry_mirror_role
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_mirror_role.py \
  --require-head-at-canonical \
  --require-clean-worktree \
  --write _completion/propertyquarry_mirror_role/release-gate.json
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_release_hygiene.py \
  --write _completion/release_hygiene/property-release-hygiene-release-gate.json
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_whole_project_scope.py \
  --write _completion/whole_project_scope/property-whole-project-scope-release-gate.json
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_surface_accessibility.py
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_provider_governance.py
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_ranking_benchmark.py
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_teable_portability.py
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_search_storage_schema.py
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_public_tour_manifest_contract.py
mkdir -p _completion/property_tour_controls _completion/tours _completion/smoke _completion/property_gold_status _completion/repair _completion/provider_smoke _completion/furniture_styles _completion/bts_methodology _completion/tour_delivery
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_furniture_style_contract.py \
  --write _completion/furniture_styles/property-furniture-style-contract-release-gate.json
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_bts_methodology_contract.py \
  --write _completion/bts_methodology/property-bts-methodology-contract-release-gate.json
PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_property_tour_controls.py \
  --require-all-provider-modes \
  --gold-scope "${gold_scope}" \
  --fail-on-blocked \
  --write _completion/property_tour_controls/release-gate.json \
  --summary-only
property_api_container="${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api}"
property_render_container="${PROPERTYQUARRY_RENDER_CONTAINER_NAME:-propertyquarry-render-tools}"
property_render_service="${PROPERTYQUARRY_RENDER_SERVICE:-propertyquarry-render-tools}"
if command -v docker >/dev/null 2>&1 && docker inspect "${property_api_container}" >/dev/null 2>&1; then
  docker exec "${property_api_container}" python /app/scripts/verify_property_tour_controls.py \
    --tour-root /data/public_property_tours \
    --live-probe \
    --base-url http://127.0.0.1:8090 \
    --host-header propertyquarry.com \
    --require-all-provider-modes \
    --gold-scope "${gold_scope}" \
    --write /data/artifacts/property-tour-controls-release-gate-live-container.json \
    --summary-only
  docker cp "${property_api_container}:/data/artifacts/property-tour-controls-release-gate-live-container.json" \
    _completion/property_tour_controls/release-gate.json
  if [[ "${gold_scope}" == "advanced_visual" ]]; then
    docker exec "${property_api_container}" python /app/scripts/discover_property_tour_exports.py \
      --drop-dir /data/incoming_property_tours \
      --public-tour-dir /data/public_property_tours \
      --write /data/artifacts/property-tour-export-discovery-release-gate-live-container.json
    docker cp "${property_api_container}:/data/artifacts/property-tour-export-discovery-release-gate-live-container.json" \
      _completion/property_tour_exports/release-gate-discovery.json
    docker exec --user root "${property_api_container}" python /app/scripts/materialize_property_tour_export_manifest.py \
      --tour-root /data/public_property_tours \
      --incoming-root /data/incoming_property_tours \
      --prepare-dirs \
      --write /data/artifacts/property-tour-export-import-manifest-release-gate-live-container.json
    docker cp "${property_api_container}:/data/artifacts/property-tour-export-import-manifest-release-gate-live-container.json" \
      _completion/property_tour_exports/release-gate-import-manifest.json
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_property_tour_vendor_tooling.py \
      --drop-dir "${tour_export_incoming_dir}" \
      --tour-root "${EA_PUBLIC_TOUR_DIR:-${EA_ROOT}/state/public_property_tours}" \
      --runtime-only \
      --runtime-container "${property_api_container}" \
      --write _completion/tours/property-tour-vendor-tooling-current.json \
      > /dev/null
    docker_exec_scene_video_python "${property_api_container}" /app/scripts/property_scene_video_readiness_report.py \
      --output /data/artifacts/property-scene-video-readiness-release-gate-live-container.json
    docker_exec_scene_video_python "${property_api_container}" /app/scripts/verify_property_scene_video_readiness.py \
      --receipt /data/artifacts/property-scene-video-readiness-release-gate-live-container.json \
      --output /data/artifacts/property-scene-video-readiness-release-gate-verifier-live-container.json \
      > /dev/null
    docker_exec_scene_video_python "${property_api_container}" /app/scripts/property_scene_video_runtime_status.py \
      --receipt /data/artifacts/property-scene-video-readiness-release-gate-live-container.json \
      --output /data/artifacts/property-scene-video-runtime-status-release-gate-live-container.json \
      > /dev/null
    docker_exec_scene_video_python "${property_api_container}" /app/scripts/materialize_scene_video_provider_refresh_packet.py \
      --receipt /data/artifacts/property-scene-video-readiness-release-gate-live-container.json \
      --output /data/artifacts/property-scene-video-provider-refresh-packet-release-gate-live-container.json \
      > /dev/null
    docker_exec_scene_video_python "${property_api_container}" /app/scripts/verify_scene_video_provider_refresh_packet.py \
      --packet /data/artifacts/property-scene-video-provider-refresh-packet-release-gate-live-container.json \
      --output /data/artifacts/property-scene-video-provider-refresh-packet-release-gate-verifier-live-container.json \
      > /dev/null
    docker cp "${property_api_container}:/data/artifacts/property-scene-video-readiness-release-gate-live-container.json" \
      _completion/scene_video_readiness/release-gate.json
    docker cp "${property_api_container}:/data/artifacts/property-scene-video-readiness-release-gate-verifier-live-container.json" \
      _completion/scene_video_readiness/release-gate-verifier.json
    docker cp "${property_api_container}:/data/artifacts/property-scene-video-runtime-status-release-gate-live-container.json" \
      _completion/scene_video_readiness/runtime-status.json
    docker cp "${property_api_container}:/data/artifacts/property-scene-video-provider-refresh-packet-release-gate-live-container.json" \
      _completion/scene_video_readiness/provider-refresh-packet.json
    docker cp "${property_api_container}:/data/artifacts/property-scene-video-provider-refresh-packet-release-gate-verifier-live-container.json" \
      _completion/scene_video_readiness/provider-refresh-packet-verifier.json
  fi
else
  if [[ "${gold_scope}" == "advanced_visual" ]]; then
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/discover_property_tour_exports.py \
      --drop-dir "${tour_export_incoming_dir}" \
      --public-tour-dir "${EA_PUBLIC_TOUR_DIR:-${EA_ROOT}/state/public_property_tours}" \
      --write _completion/property_tour_exports/release-gate-discovery.json
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/materialize_property_tour_export_manifest.py \
      --tour-root "${EA_PUBLIC_TOUR_DIR:-${EA_ROOT}/state/public_property_tours}" \
      --incoming-root "${tour_export_incoming_dir}" \
      --prepare-dirs \
      --write _completion/property_tour_exports/release-gate-import-manifest.json
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_property_tour_vendor_tooling.py \
      --drop-dir "${tour_export_incoming_dir}" \
      --tour-root "${EA_PUBLIC_TOUR_DIR:-${EA_ROOT}/state/public_property_tours}" \
      --runtime-only \
      --write _completion/tours/property-tour-vendor-tooling-current.json \
      > /dev/null
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_scene_video_readiness_report.py \
      --load-shared-env \
      --output _completion/scene_video_readiness/release-gate.json
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_property_scene_video_readiness.py \
      --receipt _completion/scene_video_readiness/release-gate.json \
      --output _completion/scene_video_readiness/release-gate-verifier.json \
      > /dev/null
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_scene_video_runtime_status.py \
      --receipt _completion/scene_video_readiness/release-gate.json \
      --output _completion/scene_video_readiness/runtime-status.json \
      > /dev/null
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/materialize_scene_video_provider_refresh_packet.py \
      --receipt _completion/scene_video_readiness/release-gate.json \
      --output _completion/scene_video_readiness/provider-refresh-packet.json \
      > /dev/null
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_scene_video_provider_refresh_packet.py \
      --packet _completion/scene_video_readiness/provider-refresh-packet.json \
      --output _completion/scene_video_readiness/provider-refresh-packet-verifier.json \
      > /dev/null
  fi
fi
PYTHONPATH=ea "${PYTHON_BIN}" scripts/check_property_tour_delivery_contract.py \
  --tour-control-receipt _completion/property_tour_controls/release-gate.json \
  --write _completion/tour_delivery/property-tour-delivery-contract-release-gate.json
if ! PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_brilliant_directories_provider.py; then
  echo "warning: Brilliant Directories verifier reported a blocked external billing lane; continuing so the consolidated gold receipt can capture the blocker." >&2
fi
PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_id_austria_provider.py
PROPERTYQUARRY_PERFORMANCE_TARGET_URL="${performance_target_url}" \
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_authenticated_performance_smoke.py \
  --release-probe-secret-stdin \
  --release-commit-sha "${dr_release_commit_sha}" \
  --release-image-digest "${dr_release_image_digest}" \
  --release-deployment-id "${expected_release_deployment_id}" \
  --expected-release-manifest-sha256 "${expected_release_manifest_sha256}" \
  --expected-chromium-executable-path "${expected_performance_chromium_path}" \
  --expected-chromium-executable-sha256 "${expected_performance_chromium_sha256}" \
  --write _completion/smoke/property-auth-performance-release-gate.json \
  <<<"${performance_release_probe_secret}" >/dev/null
live_mobile_base_url="${PROPERTYQUARRY_LIVE_MOBILE_BASE_URL:-${PROPERTYQUARRY_LIVE_SMOKE_BASE_URL:-}}"
/usr/bin/env \
  -u BASH_ENV \
  -u ENV \
  PROPERTYQUARRY_LIVE_PROBE_SECRET="${performance_release_probe_secret}" \
  PYTHON_BIN="${PYTHON_BIN}" \
  /bin/bash --noprofile --norc -p scripts/propertyquarry_live_release_gates.sh
unset performance_release_probe_secret
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_map_preview_flagship_gate.py \
  --base-url "${live_mobile_base_url}" \
  --host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}" \
  --principal-id "${PROPERTYQUARRY_LIVE_PRINCIPAL_ID:-}" \
  --no-canonical-fallback \
  --write _completion/smoke/property-live-map-preview-flagship-release-gate.json \
  > /dev/null
runtime_reconstruction_container="${PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_CONTAINER:-${property_render_container}}"
runtime_reconstruction_slug="${PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_SMOKE_SLUG:-runtime-reconstruction-release-gate-$(date +%Y%m%d%H%M%S)}"
service_generated_reconstruction_slug="${PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_SMOKE_SLUG:-service-generated-reconstruction-release-gate-$(date +%Y%m%d%H%M%S)}"
PYTHONPATH=ea "${PYTHON_BIN}" scripts/ensure_propertyquarry_render_bridge_runtime.py \
  --container "${property_render_container}" \
  --service "${property_render_service}" \
  --compose-file "${PROPERTYQUARRY_COMPOSE_FILE:-docker-compose.property.yml}" \
  --project-name "${PROPERTYQUARRY_COMPOSE_PROJECT_NAME:-${COMPOSE_PROJECT_NAME:-}}" \
  --write _completion/tours/property-render-bridge-runtime-release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_runtime_reconstruction_smoke.py \
  --container "${runtime_reconstruction_container}" \
  --slug "${runtime_reconstruction_slug}" \
  --public-base-url "${PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_BASE_URL:-${live_mobile_base_url}}" \
  --host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}" \
  --require-public-contract \
  --require-browser-shell \
  --require-glb \
  --write _completion/tours/property-runtime-reconstruction-release-gate.json \
  --fail-on-error \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_service_generated_reconstruction_smoke.py \
  --container "${property_api_container}" \
  --slug "${service_generated_reconstruction_slug}" \
  --public-base-url "${PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_BASE_URL:-${live_mobile_base_url}}" \
  --host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}" \
  --require-public-contract \
  --require-browser-shell \
  --write _completion/tours/property-service-generated-reconstruction-release-gate.json \
  --fail-on-error \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_3d_browser_gate.py \
  --base-url "${PROPERTYQUARRY_3D_BROWSER_GATE_BASE_URL:-${live_mobile_base_url}}" \
  --host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}" \
  --runtime-container "${property_api_container}" \
  --screenshots-dir _completion/smoke/property-live-3d-browser-gate-release-gate-screenshots \
  --write _completion/smoke/property-live-3d-browser-gate-release-gate.json \
  > /dev/null
if [[ "${gold_scope}" == "advanced_visual" ]]; then
  walkthrough_provider_proof_tour_root="${PROPERTYQUARRY_WALKTHROUGH_PROVIDER_PROOF_TOUR_ROOT:-${EA_PUBLIC_TOUR_DIR:-${EA_ROOT}/state/public_property_tours}}"
  walkthrough_provider_proof_timeout_seconds="${PROPERTYQUARRY_WALKTHROUGH_PROVIDER_PROOF_TIMEOUT_SECONDS:-180}"
  walkthrough_quality_process_timeout_seconds="${PROPERTYQUARRY_WALKTHROUGH_QUALITY_PROCESS_TIMEOUT_SECONDS:-420}"
  walkthrough_quality_ffprobe_timeout_seconds="${PROPERTYQUARRY_WALKTHROUGH_QUALITY_FFPROBE_TIMEOUT_SECONDS:-20}"
  walkthrough_quality_frame_sample_timeout_seconds="${PROPERTYQUARRY_WALKTHROUGH_QUALITY_FRAME_SAMPLE_TIMEOUT_SECONDS:-45}"
  if ! PYTHONPATH=ea timeout "${walkthrough_provider_proof_timeout_seconds}" "${PYTHON_BIN}" scripts/propertyquarry_walkthrough_provider_proof_gate.py \
    --tour-root "${walkthrough_provider_proof_tour_root}" \
    --write _completion/smoke/property-live-walkthrough-provider-proof-release-gate.json \
    > /dev/null; then
    echo "error: PropertyQuarry MagicFit/OMagic walkthrough provider proof gate failed or timed out." >&2
    cat _completion/smoke/property-live-walkthrough-provider-proof-release-gate.json >&2 2>/dev/null || true
    exit 1
  fi
  if ! PYTHONPATH=ea timeout "${walkthrough_quality_process_timeout_seconds}" "${PYTHON_BIN}" scripts/propertyquarry_walkthrough_quality_gate.py \
    --tour-root "${walkthrough_provider_proof_tour_root}" \
    --provider-proof-receipt _completion/smoke/property-live-walkthrough-provider-proof-release-gate.json \
    --ffprobe-timeout-seconds "${walkthrough_quality_ffprobe_timeout_seconds}" \
    --frame-sample-timeout-seconds "${walkthrough_quality_frame_sample_timeout_seconds}" \
    --write _completion/smoke/property-live-walkthrough-quality-release-gate.json \
    > /dev/null; then
    echo "error: PropertyQuarry walkthrough quality gate failed or timed out." >&2
    cat _completion/smoke/property-live-walkthrough-quality-release-gate.json >&2 2>/dev/null || true
    exit 1
  fi
fi
PYTHONPATH=ea "${PYTHON_BIN}" scripts/verify_property_tour_provider_ownership.py \
  --write _completion/property_tour_ownership/release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_repair_fleet_canary.py \
  > _completion/repair/propertyquarry-repair-canary-release-gate.json
if [[ -f _completion/provider_smoke/production-e2e-provider-matrix-current.json ]]; then
  cp _completion/provider_smoke/production-e2e-provider-matrix-current.json _completion/provider_smoke/release-gate-provider-matrix.json
elif [[ -f _completion/provider_smoke/all-search-ready-current-resumed.json ]]; then
  cp _completion/provider_smoke/all-search-ready-current-resumed.json _completion/provider_smoke/release-gate-provider-matrix.json
elif [[ -f _completion/provider_smoke/all-search-ready-live.json ]]; then
  cp _completion/provider_smoke/all-search-ready-live.json _completion/provider_smoke/release-gate-provider-matrix.json
else
  PROPERTYQUARRY_LIVE_PROVIDER_SMOKE=1 \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN=1 \
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_live_provider_smoke.py \
    --all-search-ready-countries \
    --no-execute-search-matrix \
    --write _completion/provider_smoke/release-gate-provider-matrix.json \
    > /dev/null
fi
if [[ "${gold_scope}" == "advanced_visual" ]]; then
  scene_video_notification_prefer_container_runtime="${PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME:-1}"
  scene_video_refresh_notification_principal_id="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_PRINCIPAL_ID:-${EA_PRINCIPAL_ID:-propertyquarry-operator}}"
  scene_video_refresh_notification_base_url="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_BASE_URL:-${live_mobile_base_url}}"
  scene_video_refresh_notification_state="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_STATE:-_completion/scene_video_readiness/provider-refresh-telegram-state.json}"
  scene_video_refresh_notification_report="_completion/scene_video_readiness/provider-refresh-telegram-report.json"
  scene_video_refresh_notification_enabled="${PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED:-0}"
  case "${scene_video_refresh_notification_enabled,,}" in
    1|true|yes|y|on|enabled)
      if ! PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME="${scene_video_notification_prefer_container_runtime}" \
        PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_notify_scene_video_provider_refresh.py \
        --packet _completion/scene_video_readiness/provider-refresh-packet.json \
        --verifier _completion/scene_video_readiness/provider-refresh-packet-verifier.json \
        --runtime-status _completion/scene_video_readiness/runtime-status.json \
        --state-file "${scene_video_refresh_notification_state}" \
        --principal-id "${scene_video_refresh_notification_principal_id}" \
        --base-url "${scene_video_refresh_notification_base_url}" \
        --write "${scene_video_refresh_notification_report}" >/dev/null; then
        echo "warning: PropertyQuarry scene-video provider refresh notification script failed." >&2
        cat "${scene_video_refresh_notification_report}" >&2 2>/dev/null || true
      fi
      ;;
    *)
      mkdir -p "$(dirname "${scene_video_refresh_notification_report}")"
      printf '{"status":"skipped","reason":"PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED_not_set"}\n' > "${scene_video_refresh_notification_report}"
      ;;
  esac
fi

advanced_visual_gold_args=()
if [[ "${gold_scope}" == "advanced_visual" ]]; then
  advanced_visual_binding_receipt="_completion/property_gold_status/advanced-visual-candidate-binding.json"
  PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_advanced_visual_gold_binding.py \
    --release-commit-sha "${dr_release_commit_sha}" \
    --release-image-digest "${dr_release_image_digest}" \
    --walkthrough-quality-receipt _completion/smoke/property-live-walkthrough-quality-release-gate.json \
    --walkthrough-provider-proof-receipt _completion/smoke/property-live-walkthrough-provider-proof-release-gate.json \
    --scene-video-readiness-receipt _completion/scene_video_readiness/release-gate.json \
    --scene-video-readiness-verifier-receipt _completion/scene_video_readiness/release-gate-verifier.json \
    --scene-video-runtime-status-receipt _completion/scene_video_readiness/runtime-status.json \
    --scene-video-provider-refresh-packet _completion/scene_video_readiness/provider-refresh-packet.json \
    --scene-video-provider-refresh-packet-verifier-receipt _completion/scene_video_readiness/provider-refresh-packet-verifier.json \
    --privacy-receipt _completion/security/property-security-posture-release-gate.json \
    --max-age-hours "${PROPERTYQUARRY_ADVANCED_VISUAL_BINDING_MAX_AGE_HOURS:-24}" \
    --write "${advanced_visual_binding_receipt}" \
    > /dev/null
  advanced_visual_gold_args=(
    --export-discovery-receipt _completion/property_tour_exports/release-gate-discovery.json
    --import-manifest-receipt _completion/property_tour_exports/release-gate-import-manifest.json
    --vendor-tooling-receipt _completion/tours/property-tour-vendor-tooling-current.json
    --walkthrough-quality-receipt _completion/smoke/property-live-walkthrough-quality-release-gate.json
    --walkthrough-provider-proof-receipt _completion/smoke/property-live-walkthrough-provider-proof-release-gate.json
    --scene-video-readiness-receipt _completion/scene_video_readiness/release-gate.json
    --scene-video-readiness-verifier-receipt _completion/scene_video_readiness/release-gate-verifier.json
    --scene-video-runtime-status-receipt _completion/scene_video_readiness/runtime-status.json
    --scene-video-provider-refresh-packet _completion/scene_video_readiness/provider-refresh-packet.json
    --scene-video-provider-refresh-packet-verifier-receipt _completion/scene_video_readiness/provider-refresh-packet-verifier.json
    --advanced-visual-binding-receipt "${advanced_visual_binding_receipt}"
  )
fi

PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_gold_status.py \
  --profile launch \
  --claim-scope "${gold_scope}" \
  --performance-receipt _completion/smoke/property-auth-performance-release-gate.json \
  --continuous-ux-receipt "${continuous_ux_receipt}" \
  --accessibility-receipt _completion/smoke/property-live-accessibility-release-gate.json \
  --failure-state-receipt "${failure_state_receipt}" \
  --activation-to-value-receipt "${activation_to_value_receipt}" \
  --tour-control-receipt _completion/property_tour_controls/release-gate.json \
  --repair-canary-receipt _completion/repair/propertyquarry-repair-canary-release-gate.json \
  --provider-matrix-receipt _completion/provider_smoke/release-gate-provider-matrix.json \
  --live-mobile-receipt _completion/smoke/property-live-mobile-release-gate.json \
  --public-smoke-receipt _completion/smoke/property-live-public-release-gate.json \
  --authenticated-smoke-receipt _completion/smoke/property-live-authenticated-release-gate.json \
  --billing-receipt _completion/brilliant_directories/BRILLIANT_DIRECTORIES_PROVIDER_VERIFICATION.generated.json \
  --map-preview-flagship-receipt _completion/smoke/property-live-map-preview-flagship-release-gate.json \
  --tour-provider-ownership-receipt _completion/property_tour_ownership/release-gate.json \
  --whole-project-scope-receipt _completion/whole_project_scope/property-whole-project-scope-release-gate.json \
  --security-posture-receipt _completion/security/property-security-posture-release-gate.json \
  --release-hygiene-receipt _completion/release_hygiene/property-release-hygiene-release-gate.json \
  --furniture-style-contract-receipt _completion/furniture_styles/property-furniture-style-contract-release-gate.json \
  --bts-methodology-contract-receipt _completion/bts_methodology/property-bts-methodology-contract-release-gate.json \
  --tour-delivery-contract-receipt _completion/tour_delivery/property-tour-delivery-contract-release-gate.json \
  --browser-3d-gate-receipt _completion/smoke/property-live-3d-browser-gate-release-gate.json \
  --runtime-reconstruction-receipt _completion/tours/property-runtime-reconstruction-release-gate.json \
  --service-generated-reconstruction-receipt _completion/tours/property-service-generated-reconstruction-release-gate.json \
  "${advanced_visual_gold_args[@]}" \
  --slo-evidence-receipt "${slo_evidence_receipt}" \
  --slo-metrics-snapshot "${slo_metrics_snapshot}" \
  --slo-metrics-probe "${slo_metrics_probe_receipt}" \
  --monitoring-runtime-receipt "${monitoring_runtime_receipt}" \
  --prometheus-range-receipt "${prometheus_range_receipt}" \
  --prometheus-range-response "${prometheus_range_response}" \
  --alert-delivery-receipt "${alert_delivery_receipt}" \
  --id-austria-receipt _completion/id_austria/ID_AUSTRIA_PROVIDER_VERIFICATION.generated.json \
  --provider-catalog-receipt "${provider_catalog_receipt}" \
  --evidence-overlay-receipt "${evidence_overlay_receipt}" \
  --rybbit-evidence-receipt "${rybbit_evidence_receipt}" \
  --require-launch-evidence \
  --expected-release-sha "${dr_release_commit_sha}" \
  --expected-image-digest "${dr_release_image_digest}" \
  --expected-release-deployment-id "${expected_release_deployment_id}" \
  --expected-release-manifest-sha256 "${expected_release_manifest_sha256}" \
  --expected-performance-chromium-executable-path "${expected_performance_chromium_path}" \
  --expected-performance-chromium-executable-sha256 "${expected_performance_chromium_sha256}" \
  --expected-public-origin "${expected_public_origin}" \
  --expected-teable-origin "${expected_teable_origin}" \
  --expected-teable-base-id-sha256 "${expected_teable_base_id_sha256}" \
  --expected-evidence-overlay-phase staged \
  --expected-rybbit-origin "${expected_rybbit_origin}" \
  --expected-rybbit-site-id-sha256 "${expected_rybbit_site_id_sha256}" \
  --write _completion/property_gold_status/release-gate.json \
  --fail-on-blocked
gold_notification_principal_id="${PROPERTYQUARRY_GOLD_NOTIFICATION_PRINCIPAL_ID:-${EA_PRINCIPAL_ID:-propertyquarry-operator}}"
gold_notification_base_url="${PROPERTYQUARRY_GOLD_NOTIFICATION_BASE_URL:-${live_mobile_base_url}}"
gold_notification_state="${PROPERTYQUARRY_GOLD_NOTIFICATION_STATE:-_completion/propertyquarry-gold-notification-state.json}"
gold_notification_report="_completion/property_gold_status/telegram-notify-report.json"
gold_notification_enabled="${PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED:-0}"
gold_notification_prefer_container_runtime="${PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME:-1}"
case "${gold_notification_enabled,,}" in
  1|true|yes|y|on|enabled)
    if ! PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME="${gold_notification_prefer_container_runtime}" \
      PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_notify_gold_status.py \
      --receipt _completion/property_gold_status/release-gate.json \
      --state-file "${gold_notification_state}" \
      --principal-id "${gold_notification_principal_id}" \
      --base-url "${gold_notification_base_url}" \
      --write "${gold_notification_report}" >/dev/null; then
      echo "warning: PropertyQuarry gold notification script failed." >&2
      cat "${gold_notification_report}" >&2 2>/dev/null || true
    fi
    ;;
  *)
    mkdir -p "$(dirname "${gold_notification_report}")"
    printf '{"status":"skipped","reason":"PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED_not_set"}\n' > "${gold_notification_report}"
    ;;
esac
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_property_release_protocol_contracts.py \
  tests/test_property_deploy_handoff_adversarial.py \
  tests/test_property_deploy_operator_contracts.py \
  tests/test_propertyquarry_release_authority_receipts.py \
  tests/test_propertyquarry_release_request_signature.py \
  tests/test_propertyquarry_flagship_operations_evidence.py \
  tests/test_propertyquarry_release_evidence.py \
  tests/test_propertyquarry_launch_gold_validation.py \
  tests/test_propertyquarry_launch_authority.py \
  tests/test_propertyquarry_release_gate_entrypoints.py \
  tests/test_propertyquarry_slo_capture.py \
  tests/test_propertyquarry_slo_evidence.py \
  tests/test_propertyquarry_slo_release_integration.py \
  tests/test_propertyquarry_deploy_drain_receipt.py \
  tests/test_propertyquarry_deploy_controller_guard.py \
  tests/test_propertyquarry_deploy_drain_keyring.py \
  tests/test_propertyquarry_deploy_monotonic_state.py \
  tests/test_propertyquarry_deploy_writer_topology.py \
  tests/test_propertyquarry_deploy_journal.py \
  tests/test_propertyquarry_deploy_promotion.py \
  tests/test_propertyquarry_host_recovery.py \
  tests/test_propertyquarry_rollback.py \
  tests/test_property_live_http_security.py \
  tests/test_property_live_presentation_security.py \
  tests/test_property_live_release_provenance.py \
  tests/test_propertyquarry_live_telegram_delivery.py \
  tests/test_propertyquarry_postgres_dr.py \
  tests/test_property_public_tour_provider_retirement.py \
  tests/test_property_live_mobile_surface_smoke.py \
  tests/test_property_worker_queues.py \
  tests/test_property_evidence_overlays.py \
  tests/test_property_delivery_governance.py \
  tests/test_property_heyy_adapter_contracts.py \
  tests/test_property_heyy_api_contracts.py \
  tests/test_property_notification_email_templates.py \
  tests/test_propertyquarry_teable_sync.py \
  tests/test_browser_surface_contracts.py \
  tests/test_propertyquarry_design_system_gate.py \
  tests/test_propertyquarry_magicfit_promo_contract.py \
  tests/test_fliplink_packet_privacy.py \
  tests/test_property_packet_publications.py \
  tests/test_fliplink_webhook_contracts.py \
  tests/test_property_missing_facts_ooda.py \
  tests/test_property_packet_engagement_contracts.py \
  tests/test_property_feedback_spine_contracts.py \
  tests/test_property_decision_loop.py \
  tests/test_property_summary_artifacts.py \
  tests/test_property_packet_variant_contracts.py \
  tests/test_propertyquarry_timeline_contracts.py \
  tests/test_propertyquarry_offer_and_optimization_contracts.py \
  tests/test_propertyquarry_phase1_exit_gate.py \
  tests/test_propertyquarry_phase2_exit_gate.py \
  tests/test_propertyquarry_phase3_exit_gate.py \
  tests/test_propertyquarry_phase4_exit_gate.py \
  tests/test_propertyquarry_phase5_exit_gate.py \
  tests/test_propertyquarry_phase6_exit_gate.py \
  tests/test_propertyquarry_phase7_exit_gate.py \
  tests/test_propertyquarry_master_regression_gate.py \
  tests/test_propertyquarry_tester_gold_gate.py \
  tests/test_dossier_writer.py \
  tests/test_dadan_video_request_workflow.py \
  tests/test_property_media_factory.py \
  tests/test_property_artifact_contracts.py \
  tests/test_property_integration_governance.py \
  tests/test_brilliant_directories_integration.py \
  tests/test_subscribr_client_contracts.py \
  tests/test_propertyquarry_sendr_campaign_packet.py \
  tests/test_property_content_source_packets.py \
  tests/test_property_content_validation.py \
  tests/test_property_content_privacy.py \
  tests/test_property_content_studio.py \
  tests/test_property_subscribr_receipts.py \
  tests/e2e/test_property_content_studio_workflow.py \
  tests/test_crezlo_public_tour_publish.py \
  tests/test_property_tour_export_importers.py \
  tests/test_premium_dossier_contracts.py \
  tests/test_property_env_config_contracts.py \
  tests/test_public_rybbit.py \
  tests/test_telegram_delivery_service.py \
  tests/test_property_sent_links_manifest_gate.py \
  tests/test_property_search_runs.py::test_property_search_run_surfaces_and_updates_missing_fact_research_tasks
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_product_api_contracts.py -k 'property_notification_preview or property_feedback'
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_product_api_contracts.py -k 'payfunnels'
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_product_api_contracts.py -k 'workspace_access'
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_product_api_contracts.py -k 'telegram_property_link_bundle or property_scout_dossier_promotes_media or property_scout_hit_telegram_sends_dossier or property_scout_hit_email_prefers_public_dossier_link or property_alert_review_handoff_page_renders_research_packet'
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_product_api_contracts.py -k 'hosted_property_tour_writer_keeps_raw_public_manifest_narrow or hosted_floorplan_tour_revalidates_asset_suffix_after_content_type or willhaben_property_tour_route_accepts_external_live_360_source_when_panorama_images_are_absent or matterport_hosted_pure_360_bundle_uses_http_thumb_preview or 3dvista_hosted_pure_360_bundle_preserves_provider_url or kalandra_cube_360_bundle_generation_is_disabled or willhaben_property_tour_route_blocks_when_only_flat_listing_photos_exist_and_360_is_required'
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_providers_api_contracts.py -k 'public_tour_json_never_exposes_listing_or_source_urls or public_tour_routes_ignore_unsafe_live_360_source_urls or public_tour_page_does_not_fetch_live_listing_research_at_render_time or public_tour_routes_drop_untrusted_external_scene_media or public_tour_routes_embed_live_360_source_when_present or public_tour_routes_allow_matterport_thumb_preview_for_live_360'
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_propertyquarry_workspace_redesign.py \
  tests/e2e/test_propertyquarry_soft_filter_equivalence.py \
  tests/e2e/test_propertyquarry_greenfield_browser.py \
  tests/e2e/test_propertyquarry_flagship_flow.py \
  tests/e2e/test_propertyquarry_public_tour_browser.py \
  tests/e2e/test_propertyquarry_packet_engagement_browser.py \
  tests/e2e/test_propertyquarry_feedback_browser.py \
  tests/e2e/test_propertyquarry_summary_artifacts_browser.py \
  tests/e2e/test_propertyquarry_packet_publishing_browser.py \
  tests/e2e/test_propertyquarry_timeline_browser.py \
  tests/e2e/test_propertyquarry_commercial_optimization_browser.py \
  tests/e2e/test_propertyquarry_phase_regression_browser.py
if [[ -n "${PROPERTYQUARRY_SENT_LINKS_MANIFEST:-}" ]]; then
  PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q tests/e2e/test_propertyquarry_sent_links_browser.py
fi
PYTHONPATH=ea "${PYTHON_BIN}" -m pytest -q \
  tests/test_property_search_runs.py \
  tests/test_property_search_agents.py \
  tests/test_property_market_catalog.py \
  tests/test_property_live_public_smoke.py \
  tests/test_property_live_authenticated_smoke.py \
  tests/test_property_live_provider_smoke.py \
  tests/test_product_browser_journeys.py -k 'properties_workspace_surface or propertyquarry_settings_hide_generic_google_sync_metrics'
if [[ -n "${PROPERTYQUARRY_VISUAL_WATCH_URL:-}" ]]; then
  visual_watch_base="${PROPERTYQUARRY_VISUAL_WATCH_URL}"
  visual_watch_out="${PROPERTYQUARRY_VISUAL_WATCH_OUTPUT_DIR:-${EA_ROOT}/_completion/pixefy/property_release_gate}"
  PROPERTYQUARRY_ROOT="${EA_ROOT}" PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_visual_watch.py \
    "${visual_watch_base}" \
    --samples "${PROPERTYQUARRY_VISUAL_WATCH_SAMPLES:-2}" \
    --interval-seconds "${PROPERTYQUARRY_VISUAL_WATCH_INTERVAL_SECONDS:-2}" \
    --viewport "${PROPERTYQUARRY_VISUAL_WATCH_VIEWPORT:-1440x1000}" \
    --output-dir "${visual_watch_out}/desktop"
  PROPERTYQUARRY_ROOT="${EA_ROOT}" PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_visual_watch.py \
    "${visual_watch_base}" \
    --samples "${PROPERTYQUARRY_VISUAL_WATCH_SAMPLES:-2}" \
    --interval-seconds "${PROPERTYQUARRY_VISUAL_WATCH_INTERVAL_SECONDS:-2}" \
    --viewport "${PROPERTYQUARRY_VISUAL_WATCH_MOBILE_VIEWPORT:-390x844}" \
    --output-dir "${visual_watch_out}/mobile"
fi

# The pinned native suite is the final deterministic publication boundary.
# Any nonzero result aborts under `set -e` before Gold or notification writes.
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_native_release_control_gates.py

# Gold is a publication, not an intermediate checkpoint. Every preceding
# deterministic test and every explicitly configured visual gate must succeed
# before the canonical receipt, aliases, or notification can be produced.
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_gold_status.py \
  --profile launch \
  --performance-receipt _completion/smoke/property-auth-performance-release-gate.json \
  --continuous-ux-receipt "${continuous_ux_receipt}" \
  --accessibility-receipt _completion/smoke/property-live-accessibility-release-gate.json \
  --failure-state-receipt "${failure_state_receipt}" \
  --activation-to-value-receipt "${activation_to_value_receipt}" \
  --tour-control-receipt _completion/property_tour_controls/release-gate.json \
  --export-discovery-receipt _completion/property_tour_exports/release-gate-discovery.json \
  --import-manifest-receipt _completion/property_tour_exports/release-gate-import-manifest.json \
  --repair-canary-receipt _completion/repair/propertyquarry-repair-canary-release-gate.json \
  --provider-matrix-receipt _completion/provider_smoke/release-gate-provider-matrix.json \
  --live-mobile-receipt _completion/smoke/property-live-mobile-release-gate.json \
  --public-smoke-receipt _completion/smoke/property-live-public-release-gate.json \
  --authenticated-smoke-receipt _completion/smoke/property-live-authenticated-release-gate.json \
  --billing-receipt _completion/brilliant_directories/BRILLIANT_DIRECTORIES_PROVIDER_VERIFICATION.generated.json \
  --map-preview-flagship-receipt _completion/smoke/property-live-map-preview-flagship-release-gate.json \
  --tour-provider-ownership-receipt _completion/property_tour_ownership/release-gate.json \
  --vendor-tooling-receipt _completion/tours/property-tour-vendor-tooling-current.json \
  --whole-project-scope-receipt _completion/whole_project_scope/property-whole-project-scope-release-gate.json \
  --security-posture-receipt _completion/security/property-security-posture-release-gate.json \
  --release-hygiene-receipt _completion/release_hygiene/property-release-hygiene-release-gate.json \
  --furniture-style-contract-receipt _completion/furniture_styles/property-furniture-style-contract-release-gate.json \
  --bts-methodology-contract-receipt _completion/bts_methodology/property-bts-methodology-contract-release-gate.json \
  --tour-delivery-contract-receipt _completion/tour_delivery/property-tour-delivery-contract-release-gate.json \
  --browser-3d-gate-receipt _completion/smoke/property-live-3d-browser-gate-release-gate.json \
  --runtime-reconstruction-receipt _completion/tours/property-runtime-reconstruction-release-gate.json \
  --service-generated-reconstruction-receipt _completion/tours/property-service-generated-reconstruction-release-gate.json \
  --walkthrough-quality-receipt _completion/smoke/property-live-walkthrough-quality-release-gate.json \
  --walkthrough-provider-proof-receipt _completion/smoke/property-live-walkthrough-provider-proof-release-gate.json \
  --scene-video-readiness-receipt _completion/scene_video_readiness/release-gate.json \
  --scene-video-readiness-verifier-receipt _completion/scene_video_readiness/release-gate-verifier.json \
  --scene-video-runtime-status-receipt _completion/scene_video_readiness/runtime-status.json \
  --scene-video-provider-refresh-packet _completion/scene_video_readiness/provider-refresh-packet.json \
  --scene-video-provider-refresh-packet-verifier-receipt _completion/scene_video_readiness/provider-refresh-packet-verifier.json \
  --slo-evidence-receipt "${slo_evidence_receipt}" \
  --slo-metrics-snapshot "${slo_metrics_snapshot}" \
  --slo-metrics-probe "${slo_metrics_probe_receipt}" \
  --monitoring-runtime-receipt "${monitoring_runtime_receipt}" \
  --prometheus-range-receipt "${prometheus_range_receipt}" \
  --prometheus-range-response "${prometheus_range_response}" \
  --alert-delivery-receipt "${alert_delivery_receipt}" \
  --dashboard-render-receipt "${dashboard_render_receipt}" \
  --structured-log-query-receipt "${structured_log_query_receipt}" \
  --distributed-trace-query-receipt "${distributed_trace_query_receipt}" \
  --id-austria-receipt _completion/id_austria/ID_AUSTRIA_PROVIDER_VERIFICATION.generated.json \
  --provider-catalog-receipt "${provider_catalog_receipt}" \
  --evidence-overlay-receipt "${evidence_overlay_receipt}" \
  --rybbit-evidence-receipt "${rybbit_evidence_receipt}" \
  --require-launch-evidence \
  --expected-release-sha "${dr_release_commit_sha}" \
  --expected-image-digest "${dr_release_image_digest}" \
  --expected-public-origin "${expected_public_origin}" \
  --expected-teable-origin "${expected_teable_origin}" \
  --expected-teable-base-id-sha256 "${expected_teable_base_id_sha256}" \
  --expected-evidence-overlay-phase staged \
  --expected-rybbit-origin "${expected_rybbit_origin}" \
  --expected-rybbit-site-id-sha256 "${expected_rybbit_site_id_sha256}" \
  --write "${gold_receipt}" \
  --fail-on-blocked

gold_notification_principal_id="${PROPERTYQUARRY_GOLD_NOTIFICATION_PRINCIPAL_ID:-${EA_PRINCIPAL_ID:-propertyquarry-operator}}"
gold_notification_base_url="${PROPERTYQUARRY_GOLD_NOTIFICATION_BASE_URL:-${live_mobile_base_url}}"
gold_notification_state="${PROPERTYQUARRY_GOLD_NOTIFICATION_STATE:-_completion/propertyquarry-gold-notification-state.json}"
gold_notification_enabled="${PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED:-0}"
case "${gold_notification_enabled,,}" in
  1|true|yes|y|on|enabled)
    if ! PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME="${notification_prefer_container_runtime}" \
      PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_notify_gold_status.py \
      --receipt "${gold_receipt}" \
      --state-file "${gold_notification_state}" \
      --principal-id "${gold_notification_principal_id}" \
      --base-url "${gold_notification_base_url}" \
      --write "${gold_notification_report}" >/dev/null; then
      echo "warning: PropertyQuarry gold notification script failed." >&2
      cat "${gold_notification_report}" >&2 2>/dev/null || true
    fi
    ;;
  *)
    mkdir -p "$(dirname "${gold_notification_report}")"
    printf '{"status":"skipped","reason":"PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED_not_set"}\n' > "${gold_notification_report}"
    ;;
esac
