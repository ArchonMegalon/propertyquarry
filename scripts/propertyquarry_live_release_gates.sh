#!/bin/bash -p
set -euo pipefail
set +x

# Capture protected probe authority before any child process or command
# substitution can inherit the caller's environment.  The retained shell
# value is deliberately not exported; individual authenticated probes receive
# it through bounded stdin instead.
release_probe_secret="${PROPERTYQUARRY_LIVE_PROBE_SECRET:-${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET:-}}"
unset PROPERTYQUARRY_LIVE_PROBE_SECRET PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET
unset PROPERTYQUARRY_RELEASE_PROBE_SECRET PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID

if [[ ${#release_probe_secret} -gt 4096 || "${release_probe_secret}" == *$'\n'* || "${release_probe_secret}" == *$'\r'* ]]; then
  unset release_probe_secret
  echo "error: release-probe credential must be one line of at most 4096 bytes" >&2
  exit 2
fi

# Resolve the repository using shell builtins only.  Keeping this script free
# of command substitutions ensures no subshell ever receives the retained
# non-exported credential either.
script_path="${BASH_SOURCE[0]}"
script_dir="${script_path%/*}"
if [[ "${script_dir}" == "${script_path}" ]]; then
  script_dir="."
fi
cd -- "${script_dir}/.."
EA_ROOT="${PWD}"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${EA_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${EA_ROOT}/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi
unset PYTHON_BIN PYTEST_PYTHON_BIN
PYTHON_BIN="${EA_ROOT}/scripts/propertyquarry_release_python.sh"
readonly PYTHON_BIN

cd "${EA_ROOT}"
live_base_url="${PROPERTYQUARRY_LIVE_MOBILE_BASE_URL:-${PROPERTYQUARRY_LIVE_SMOKE_BASE_URL:-}}"
research_detail_route="${PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE:-}"
live_principal_id="${PROPERTYQUARRY_LIVE_PRINCIPAL_ID:-}"
expected_probe_principal_id="pq-live-mobile-smoke"
expected_probe_research_detail_route="/app/research/perf-candidate-1020?run_id=run-gold-mobile"
expected_probe_shortlist_run_route="/app/shortlist/run/run-gold-mobile"
accessibility_public_tour_route="${PROPERTYQUARRY_ACCESSIBILITY_PUBLIC_TOUR_ROUTE:-${PROPERTYQUARRY_LIVE_PUBLIC_TOUR_ROUTE:-}}"
expected_release_commit_sha="${PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA:-}"
expected_release_repository="${PROPERTYQUARRY_EXPECTED_RELEASE_REPOSITORY:-}"
expected_release_public_origin="${PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN:-}"
expected_release_branch="${PROPERTYQUARRY_EXPECTED_RELEASE_BRANCH:-main}"
expected_release_deployment_id="${PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID:-}"
expected_release_artifact_set="${PROPERTYQUARRY_EXPECTED_RELEASE_ARTIFACT_SET:-}"
expected_release_label="${PROPERTYQUARRY_EXPECTED_RELEASE_LABEL:-}"
expected_release_generated_at="${PROPERTYQUARRY_EXPECTED_RELEASE_GENERATED_AT:-}"
expected_release_image_digest="${PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST:-}"
expected_replica_id="${PROPERTYQUARRY_EXPECTED_REPLICA_ID:-}"
expected_web_image="${PROPERTYQUARRY_EXPECTED_WEB_IMAGE:-}"
expected_render_image="${PROPERTYQUARRY_EXPECTED_RENDER_IMAGE:-}"
security_receipt="${PROPERTYQUARRY_RELEASE_SECURITY_RECEIPT:-}"
security_workflow_binding="${PROPERTYQUARRY_RELEASE_SECURITY_WORKFLOW_BINDING:-}"
workflow_head_sha="${PROPERTYQUARRY_WORKFLOW_HEAD_SHA:-}"
workflow_run_id="${PROPERTYQUARRY_WORKFLOW_RUN_ID:-}"
workflow_run_attempt="${PROPERTYQUARRY_WORKFLOW_RUN_ATTEMPT:-}"
live_telegram_bot_token="${PROPERTYQUARRY_LIVE_TELEGRAM_BOT_TOKEN:-}"
live_telegram_chat_id="${PROPERTYQUARRY_LIVE_TELEGRAM_CHAT_ID:-}"
evidence_overlay_receipt="${PROPERTYQUARRY_EVIDENCE_OVERLAY_RECEIPT:-_completion/smoke/property-evidence-overlay-read-model.json}"
rybbit_evidence_receipt="${PROPERTYQUARRY_RYBBIT_EVIDENCE_RECEIPT:-_completion/smoke/property-rybbit-delivery.json}"
rybbit_origin="${PROPERTYQUARRY_RYBBIT_ORIGIN:-}"
rybbit_site_id_sha256="${PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256:-}"

# The browser gates receive only the client-side release-probe credential. Drop
# legacy caller identity/token inputs and any server-side verifier authority
# before starting subprocesses.
unset EA_API_TOKEN PROPERTYQUARRY_LIVE_API_TOKEN
unset PROPERTYQUARRY_LIVE_PRINCIPAL_ID

require_provenance_value() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    echo "error: set ${name} for complete live release provenance" >&2
    exit 2
  fi
}

if [[ -z "${live_base_url}" ]]; then
  echo "error: set PROPERTYQUARRY_LIVE_MOBILE_BASE_URL or PROPERTYQUARRY_LIVE_SMOKE_BASE_URL" >&2
  exit 2
fi
if [[ -z "${research_detail_route}" ]]; then
  echo "error: set PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE to a current /app/research/{id}?run_id=... route" >&2
  exit 2
fi
if [[ "${research_detail_route}" != "${expected_probe_research_detail_route}" ]]; then
  echo "error: PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE must match the fixed synthetic release-probe route" >&2
  exit 2
fi
if ! [[ "${accessibility_public_tour_route}" =~ ^/tours/[A-Za-z0-9][A-Za-z0-9._-]{1,198}$ ]]; then
  echo "error: set PROPERTYQUARRY_ACCESSIBILITY_PUBLIC_TOUR_ROUTE to a concrete current /tours/{slug} route without query parameters or template placeholders" >&2
  exit 2
fi
if [[ "${live_principal_id}" != "${expected_probe_principal_id}" ]]; then
  echo "error: PROPERTYQUARRY_LIVE_PRINCIPAL_ID must match the fixed synthetic release-probe principal" >&2
  exit 2
fi
if [[ -z "${release_probe_secret}" ]]; then
  echo "error: set PROPERTYQUARRY_LIVE_PROBE_SECRET for protected authenticated live release probes" >&2
  exit 2
fi
if ! [[ "${expected_release_commit_sha}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "error: set PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA to the manifest runtime full Git commit SHA" >&2
  exit 2
fi
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_REPOSITORY "${expected_release_repository}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN "${expected_release_public_origin}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_BRANCH "${expected_release_branch}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID "${expected_release_deployment_id}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_ARTIFACT_SET "${expected_release_artifact_set}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_LABEL "${expected_release_label}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_GENERATED_AT "${expected_release_generated_at}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST "${expected_release_image_digest}"
require_provenance_value PROPERTYQUARRY_EXPECTED_REPLICA_ID "${expected_replica_id}"
require_provenance_value PROPERTYQUARRY_EXPECTED_WEB_IMAGE "${expected_web_image}"
require_provenance_value PROPERTYQUARRY_EXPECTED_RENDER_IMAGE "${expected_render_image}"
require_provenance_value PROPERTYQUARRY_RELEASE_SECURITY_RECEIPT "${security_receipt}"
require_provenance_value PROPERTYQUARRY_RELEASE_SECURITY_WORKFLOW_BINDING "${security_workflow_binding}"
require_provenance_value PROPERTYQUARRY_WORKFLOW_HEAD_SHA "${workflow_head_sha}"
require_provenance_value PROPERTYQUARRY_WORKFLOW_RUN_ID "${workflow_run_id}"
require_provenance_value PROPERTYQUARRY_WORKFLOW_RUN_ATTEMPT "${workflow_run_attempt}"
require_provenance_value DATABASE_URL "${DATABASE_URL:-}"
require_provenance_value TEABLE_BASE_URL "${TEABLE_BASE_URL:-}"
require_provenance_value TEABLE_API_KEY "${TEABLE_API_KEY:-}"
require_provenance_value PROPERTYQUARRY_EVIDENCE_OVERLAY_TEABLE_BASE_ID "${PROPERTYQUARRY_EVIDENCE_OVERLAY_TEABLE_BASE_ID:-}"
require_provenance_value PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN "${PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN:-}"
require_provenance_value PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256 "${PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256:-}"
require_provenance_value PROPERTYQUARRY_RYBBIT_ORIGIN "${rybbit_origin}"
require_provenance_value PROPERTYQUARRY_RYBBIT_SITE_ID "${PROPERTYQUARRY_RYBBIT_SITE_ID:-}"
require_provenance_value PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256 "${rybbit_site_id_sha256}"
require_provenance_value PROPERTYQUARRY_RYBBIT_API_KEY "${PROPERTYQUARRY_RYBBIT_API_KEY:-}"
require_provenance_value PROPERTYQUARRY_RYBBIT_SITE_API_URL "${PROPERTYQUARRY_RYBBIT_SITE_API_URL:-}"
require_provenance_value PROPERTYQUARRY_RYBBIT_HAS_DATA_API_URL "${PROPERTYQUARRY_RYBBIT_HAS_DATA_API_URL:-}"
require_provenance_value PROPERTYQUARRY_RYBBIT_EVENTS_API_URL "${PROPERTYQUARRY_RYBBIT_EVENTS_API_URL:-}"
if [[ ! -f "${security_receipt}" || ! -f "${security_workflow_binding}" ]]; then
  echo "error: current-run PropertyQuarry security receipt and workflow binding must both be regular files" >&2
  exit 2
fi
if [[ -z "${live_telegram_bot_token}" ]]; then
  echo "error: set PROPERTYQUARRY_LIVE_TELEGRAM_BOT_TOKEN for protected notification proof" >&2
  exit 2
fi
if [[ -z "${live_telegram_chat_id}" ]]; then
  echo "error: set PROPERTYQUARRY_LIVE_TELEGRAM_CHAT_ID for protected notification proof" >&2
  exit 2
fi

mkdir -p _completion/smoke
export PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE="${expected_probe_research_detail_route}"
export PROPERTYQUARRY_ACCESSIBILITY_RESEARCH_DETAIL_ROUTE="${expected_probe_research_detail_route}"
export PROPERTYQUARRY_ACCESSIBILITY_SHORTLIST_RUN_ROUTE="${expected_probe_shortlist_run_route}"
export PROPERTYQUARRY_ACCESSIBILITY_PUBLIC_TOUR_ROUTE="${accessibility_public_tour_route}"

PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_evidence_overlay_read_model.py \
  --stage-only \
  --candidate-sha "${expected_release_commit_sha}" \
  --write "${evidence_overlay_receipt}" \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_rybbit_evidence.py \
  --candidate-sha "${expected_release_commit_sha}" \
  --public-origin "${expected_release_public_origin}" \
  --analytics-origin "${rybbit_origin}" \
  --write "${rybbit_evidence_receipt}" \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" - "${evidence_overlay_receipt}" "${rybbit_evidence_receipt}" \
  "${expected_release_commit_sha}" "${expected_release_public_origin}" "${rybbit_origin}" \
  "${rybbit_site_id_sha256}" "${PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN}" \
  "${PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256}" <<'PY'
import json
import sys
from pathlib import Path

from scripts.property_evidence_overlay_read_model import verify_receipt as verify_overlay
from scripts.propertyquarry_rybbit_evidence import verify_receipt as verify_rybbit

overlay_path, rybbit_path = Path(sys.argv[1]), Path(sys.argv[2])
(
    candidate_sha,
    public_origin,
    rybbit_origin,
    site_id_sha256,
    teable_origin,
    teable_base_id_sha256,
) = sys.argv[3:]
overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
rybbit = json.loads(rybbit_path.read_text(encoding="utf-8"))
errors = verify_overlay(
    overlay,
    expected_candidate_sha=candidate_sha,
    max_age_hours=48,
    expected_teable_origin=teable_origin,
    expected_teable_base_id_sha256=teable_base_id_sha256,
    expected_phase="staged",
)
errors.extend(
    verify_rybbit(
        rybbit,
        expected_candidate_sha=candidate_sha,
        expected_public_origin=public_origin,
        expected_analytics_origin=rybbit_origin,
        expected_site_id_sha256=site_id_sha256,
        max_age_minutes=15,
    )
)
if errors:
    raise SystemExit("protected product-data evidence failed: " + "; ".join(errors))
print("ok: protected Teable/Postgres and Rybbit delivery evidence")
PY

PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_release_provenance.py \
  --base-url "${live_base_url}" \
  --expected-commit-sha "${expected_release_commit_sha}" \
  --expected-repository "${expected_release_repository}" \
  --expected-public-origin "${expected_release_public_origin}" \
  --expected-branch "${expected_release_branch}" \
  --expected-deployment-id "${expected_release_deployment_id}" \
  --expected-artifact-set "${expected_release_artifact_set}" \
  --expected-release-label "${expected_release_label}" \
  --expected-release-generated-at "${expected_release_generated_at}" \
  --expected-image-digest "${expected_release_image_digest}" \
  --expected-replica-id "${expected_replica_id}" \
  --expected-web-image "${expected_web_image}" \
  --expected-render-image "${expected_render_image}" \
  --security-receipt "${security_receipt}" \
  --security-workflow-binding "${security_workflow_binding}" \
  --expected-workflow-head-sha "${workflow_head_sha}" \
  --expected-workflow-run-id "${workflow_run_id}" \
  --expected-workflow-run-attempt "${workflow_run_attempt}" \
  --write _completion/smoke/property-live-release-provenance.json \
  > /dev/null

PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_mobile_surface_smoke.py \
  --release-probe-secret-stdin \
  --base-url "${live_base_url}" \
  --principal-id "${expected_probe_principal_id}" \
  --proof-mode browser-all \
  --required-browser-engines "${PROPERTYQUARRY_LIVE_MOBILE_REQUIRED_BROWSER_ENGINES:-chromium,firefox,webkit}" \
  --require-research-detail \
  --write _completion/smoke/property-live-mobile-release-gate.json \
  <<<"${release_probe_secret}" > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_accessibility_gate.py \
  --release-probe-secret-stdin \
  --base-url "${live_base_url}" \
  --browser-engines "${PROPERTYQUARRY_LIVE_MOBILE_REQUIRED_BROWSER_ENGINES:-chromium,firefox,webkit}" \
  --axe-core-path "${PROPERTYQUARRY_AXE_CORE_PATH:-node_modules/axe-core/axe.min.js}" \
  --principal-id "${expected_probe_principal_id}" \
  --write _completion/smoke/property-live-accessibility-release-gate.json \
  <<<"${release_probe_secret}" > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_map_preview_flagship_gate.py \
  --release-probe-secret-stdin \
  --base-url "${live_base_url}" \
  --host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}" \
  --principal-id "${expected_probe_principal_id}" \
  --no-canonical-fallback \
  --write _completion/smoke/property-live-map-preview-flagship-release-gate.json \
  <<<"${release_probe_secret}" > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_public_smoke.py \
  --base-url "${live_base_url}" \
  --write _completion/smoke/property-live-public-release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_authenticated_smoke.py \
  --release-probe-secret-stdin \
  --base-url "${live_base_url}" \
  --principal-id "${expected_probe_principal_id}" \
  --expected-plan-label "${PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL:-Free}" \
  --write _completion/smoke/property-live-authenticated-release-gate.json \
  <<<"${release_probe_secret}" > /dev/null
unset release_probe_secret
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_telegram_delivery.py \
  --release-commit-sha "${expected_release_commit_sha}" \
  --write _completion/smoke/property-live-notification-delivery.json \
  > /dev/null
