#!/bin/bash -p
set -euo pipefail

PATH=/usr/bin:/bin
export PATH
readonly PATH
unset BASH_ENV ENV CDPATH GLOBIGNORE
IFS=$' \t\n'

script_source="${BASH_SOURCE[0]}"
[[ "${script_source}" == */* ]] || {
  printf '%s\n' "error: live release gate must be invoked with an explicit path" >&2
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

cd "${EA_ROOT}"
live_base_url="${PROPERTYQUARRY_LIVE_MOBILE_BASE_URL:-${PROPERTYQUARRY_LIVE_SMOKE_BASE_URL:-}}"
research_detail_route="${PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE:-}"
live_principal_id="${PROPERTYQUARRY_LIVE_PRINCIPAL_ID:-}"
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
if [[ -z "${live_principal_id}" ]]; then
  echo "error: set PROPERTYQUARRY_LIVE_PRINCIPAL_ID to the principal that owns the research-detail route" >&2
  exit 2
fi
if [[ -z "${EA_API_TOKEN:-}" ]]; then
  echo "error: set EA_API_TOKEN for protected authenticated live release probes" >&2
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
export PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE="${research_detail_route}"
export PROPERTYQUARRY_ACCESSIBILITY_RESEARCH_DETAIL_ROUTE="${research_detail_route}"

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
  --base-url "${live_base_url}" \
  --principal-id "${live_principal_id}" \
  --proof-mode browser-all \
  --required-browser-engines "${PROPERTYQUARRY_LIVE_MOBILE_REQUIRED_BROWSER_ENGINES:-chromium,firefox,webkit}" \
  --require-research-detail \
  --write _completion/smoke/property-live-mobile-release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_accessibility_gate.py \
  --base-url "${live_base_url}" \
  --browser-engines "${PROPERTYQUARRY_LIVE_MOBILE_REQUIRED_BROWSER_ENGINES:-chromium,firefox,webkit}" \
  --axe-core-path "${PROPERTYQUARRY_AXE_CORE_PATH:-node_modules/axe-core/axe.min.js}" \
  --principal-id "${live_principal_id}" \
  --write _completion/smoke/property-live-accessibility-release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_map_preview_flagship_gate.py \
  --base-url "${live_base_url}" \
  --host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}" \
  --principal-id "${live_principal_id}" \
  --no-canonical-fallback \
  --write _completion/smoke/property-live-map-preview-flagship-release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_public_smoke.py \
  --base-url "${live_base_url}" \
  --write _completion/smoke/property-live-public-release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_authenticated_smoke.py \
  --base-url "${live_base_url}" \
  --principal-id "${live_principal_id}" \
  --expected-plan-label "${PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL:-Free}" \
  --write _completion/smoke/property-live-authenticated-release-gate.json \
  > /dev/null
PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_telegram_delivery.py \
  --release-commit-sha "${expected_release_commit_sha}" \
  --write _completion/smoke/property-live-notification-delivery.json \
  > /dev/null
