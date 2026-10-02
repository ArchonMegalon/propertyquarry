#!/usr/bin/env bash
set -euo pipefail

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${EA_ROOT}"

OPERATOR_ACTION_ONLY=0
if [[ "${1:-}" == "--propertyquarry-action-only" ]]; then
  OPERATOR_ACTION_ONLY=1
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/operator_summary.sh
  bash scripts/operator_summary.sh --propertyquarry-action-only

Print a compact operator command summary including deploy, smoke, readiness,
release, support, and documentation shortcuts plus current version metadata,
the current mirrored product-control pulse, and grounded help/support/operator
packet guidance plus codex lane governance from the local design mirror. The
action-only mode prints only a fresh manifest-approved, consent-gated
PropertyQuarry action, if any, without performing or sending it. A displayed
projection is recorded in a private presentation ledger that is independent
of notification delivery state.
EOF
  exit 0
fi

OPERATOR_PYTHONPATH="${EA_ROOT}/ea:${EA_ROOT}"

# Raw action-only JSON passthrough is opt-in for controller/CI debugging
# via PROPERTYQUARRY_OPERATOR_ACTION_ONLY_RAW=1. Default flag mode falls
# through to the rendered print_propertyquarry_action_summary dispatch.
if ((OPERATOR_ACTION_ONLY == 1)) && [[ "${PROPERTYQUARRY_OPERATOR_ACTION_ONLY_RAW:-0}" == "1" ]]; then
  ACTION_ONLY_PYTHON="${PROPERTYQUARRY_OPERATOR_PYTHON:-python3}"
  if [[ "${ACTION_ONLY_PYTHON}" == */* ]]; then
    [[ -x "${ACTION_ONLY_PYTHON}" ]] || {
      echo "operator summary: action-only Python is not executable" >&2
      exit 1
    }
  elif ! command -v "${ACTION_ONLY_PYTHON}" >/dev/null 2>&1; then
    echo "operator summary: action-only Python is unavailable" >&2
    exit 1
  fi
  PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONPATH="${OPERATOR_PYTHONPATH}" \
    "${ACTION_ONLY_PYTHON}" -B \
      scripts/propertyquarry_ooda_action_only.py \
      --receipt "${PROPERTYQUARRY_OPERATOR_OODA_RECEIPT:-_completion/propertyquarry_ooda_notification_cycle/latest.json}" \
      --signal-dir "${PROPERTYQUARRY_OPERATOR_OODA_SIGNAL_DIR:-_completion/propertyquarry_ooda_signal_ingress}" \
      --presentation-state "${PROPERTYQUARRY_OPERATOR_OODA_PRESENTATION_STATE:-_completion/propertyquarry_ooda_notification_cycle/operator-presentation-state.json}" \
      --trust-notification-receipt "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_NOTIFICATION_RECEIPT:-_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-notification.json}" \
      --trust-presentation-state "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_PRESENTATION_STATE:-_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-presentation.json}" \
      --trust-decision-dir "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_DECISION_DIR:-_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-decisions}" \
      --trust-claim-verification "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_CLAIM_VERIFICATION:-_completion/propertyquarry_ooda_notification_cycle/source-refresh-claims-verification.json}" \
      --trust-registry "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_REGISTRY:-config/propertyquarry_ooda_source_refresh_producer_trust.v1.json}" \
      --trust-candidate-dir "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_CANDIDATE_DIR:-_completion/propertyquarry_ooda_producer_trust_candidates}" \
      --trust-intake-receipt "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_INTAKE_RECEIPT:-_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-intake.json}" \
      --trust-intake-verification "${PROPERTYQUARRY_OPERATOR_OODA_TRUST_INTAKE_VERIFICATION:-_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-intake-verification.json}"
  exit $?
fi

operator_python_usable() {
  local candidate="$1"
  local probe

  if [[ "${candidate}" == */* ]]; then
    [[ -x "${candidate}" ]] || return 1
  else
    command -v "${candidate}" >/dev/null 2>&1 || return 1
  fi

  if ((OPERATOR_ACTION_ONLY == 1)); then
    probe='from scripts.propertyquarry_ooda_authority_posture import refresh_current_authority_posture; from scripts.propertyquarry_ooda_authorization_decision import verify_current_authorization_decision; from scripts.propertyquarry_ooda_authorization_request import stage_current_authorization_handoff; from scripts.propertyquarry_ooda_configuration_action_status import apply_manual_action_presentation_state, inspect_manual_action_status, record_manual_action_presentation; from scripts.propertyquarry_ooda_configuration_change_preview import stage_current_configuration_change_preview; from scripts.propertyquarry_ooda_configuration_manual_action import stage_current_manual_action_handoff; from scripts.propertyquarry_ooda_configuration_plan import stage_current_configuration_handoff; from scripts.propertyquarry_ooda_operator_consent import project_operator_consent, record_operator_consent; from scripts.propertyquarry_ooda_operator_status import apply_operator_presentation_state, load_operator_status, record_operator_presentation; from scripts.propertyquarry_ooda_runtime_review import materialize_current_runtime_observation_bundle, operator_presentation_context, verify_current_review_packet; from scripts.propertyquarry_ooda_safe_tick import run_safe_tick; from scripts.propertyquarry_ooda_scheduler_activation_authorization import stage_current_scheduler_activation_authorization_handoff; from scripts.propertyquarry_ooda_scheduler_activation_decision import verify_current_scheduler_activation_authorization_decision; from scripts.propertyquarry_ooda_scheduler_activation_readiness import materialize_current_scheduler_activation_readiness_bundle; from scripts.propertyquarry_ooda_scheduler_continuity import materialize_current_scheduler_continuity_bundle'
  else
    probe='import yaml; from app.product.service import _public_guide_freshness_projection; from app.api.routes.responses import _codex_governance_payload, _codex_profiles; from scripts.propertyquarry_ooda_authority_posture import refresh_current_authority_posture; from scripts.propertyquarry_ooda_authorization_decision import verify_current_authorization_decision; from scripts.propertyquarry_ooda_authorization_request import stage_current_authorization_handoff; from scripts.propertyquarry_ooda_configuration_action_status import apply_manual_action_presentation_state, inspect_manual_action_status, record_manual_action_presentation; from scripts.propertyquarry_ooda_configuration_change_preview import stage_current_configuration_change_preview; from scripts.propertyquarry_ooda_configuration_manual_action import stage_current_manual_action_handoff; from scripts.propertyquarry_ooda_configuration_plan import stage_current_configuration_handoff; from scripts.propertyquarry_ooda_operator_consent import project_operator_consent, record_operator_consent; from scripts.propertyquarry_ooda_operator_status import apply_operator_presentation_state, load_operator_status, record_operator_presentation; from scripts.propertyquarry_ooda_runtime_review import materialize_current_runtime_observation_bundle, operator_presentation_context, verify_current_review_packet; from scripts.propertyquarry_ooda_safe_tick import run_safe_tick; from scripts.propertyquarry_ooda_scheduler_activation_authorization import stage_current_scheduler_activation_authorization_handoff; from scripts.propertyquarry_ooda_scheduler_activation_decision import verify_current_scheduler_activation_authorization_decision; from scripts.propertyquarry_ooda_scheduler_activation_readiness import materialize_current_scheduler_activation_readiness_bundle; from scripts.propertyquarry_ooda_scheduler_continuity import materialize_current_scheduler_continuity_bundle'
  fi
  probe="${probe}; from scripts.propertyquarry_ooda_scheduler_activation_execution import inspect_current_scheduler_activation_execution; from scripts.propertyquarry_ooda_scheduler_activation_settlement import materialize_current_scheduler_activation_settlement_bundle, project_scheduler_activation_settlement_handoff"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_handoff import inspect_source_refresh_handoff_bundle, materialize_source_refresh_handoff_bundle"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_claims import inspect_claim_lifecycle_bundle, materialize_claim_lifecycle_bundle"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_decision import project_candidate_review_decision, verify_candidate_review_decision_for_report"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_notification import run_candidate_notification"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_enrollment_preview import materialize_enrollment_preview"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_enrollment_authorization import verify_authorization_for_preview"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_enrollment_execution import inspect_execution_for_readiness"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_intake import apply_candidate_presentation_state, inspect_trust_intake_bundle, materialize_trust_intake_bundle, record_candidate_presentation"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_candidate_import import apply_candidate_import_presentation_state, inspect_candidate_import_readiness, record_candidate_import_presentation"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_candidate_artifact_request import inspect_candidate_artifact_request, materialize_candidate_artifact_request"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification import inspect_candidate_artifact_notification, run_candidate_artifact_notification"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_trust_candidate_manual_action import inspect_candidate_manual_action, materialize_candidate_manual_action"
  probe="${probe}; from scripts.propertyquarry_ooda_source_refresh_settlement import inspect_settlement_bundle, materialize_settlement_bundle"

  PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONPATH="${OPERATOR_PYTHONPATH}" \
    "${candidate}" -B -c "${probe}" \
      >/dev/null 2>&1
}

select_operator_python() {
  local explicit="${PROPERTYQUARRY_OPERATOR_PYTHON:-}"
  local candidate

  if [[ -n "${explicit}" ]]; then
    if ! operator_python_usable "${explicit}"; then
      echo "operator summary: PROPERTYQUARRY_OPERATOR_PYTHON is not a usable application interpreter: ${explicit}" >&2
      return 1
    fi
    printf '%s\n' "${explicit}"
    return 0
  fi

  if ((OPERATOR_ACTION_ONLY == 1)); then
    for candidate in \
      python3 \
      "${EA_ROOT}/.venv/bin/python" \
      "${EA_ROOT}/.propertyquarry_release_tools/release-venv/bin/python" \
      "${EA_ROOT}/scripts/propertyquarry_release_python.sh"
    do
      if operator_python_usable "${candidate}"; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    done
  else
    for candidate in \
      "${EA_ROOT}/.venv/bin/python" \
      "${EA_ROOT}/.propertyquarry_release_tools/release-venv/bin/python" \
      "${EA_ROOT}/scripts/propertyquarry_release_python.sh" \
      python3
    do
      if operator_python_usable "${candidate}"; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    done
  fi

  echo "operator summary: no usable application Python interpreter found" >&2
  return 1
}

OPERATOR_PYTHON="$(select_operator_python)"

run_operator_python() {
  PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONPATH="${OPERATOR_PYTHONPATH}" \
    "${OPERATOR_PYTHON}" -B "$@"
}

print_propertyquarry_action_summary() {
  run_operator_python - <<'PY'
from __future__ import annotations

import os
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.propertyquarry_ooda_authorization_decision import (
    verify_current_authorization_decision,
)
from scripts.propertyquarry_ooda_authority_posture import (
    refresh_current_authority_posture,
)
from scripts.propertyquarry_ooda_authorization_request import (
    stage_current_authorization_handoff,
)
from scripts.propertyquarry_ooda_configuration_plan import (
    stage_current_configuration_handoff,
)
from scripts.propertyquarry_ooda_configuration_change_preview import (
    stage_current_configuration_change_preview,
)
from scripts.propertyquarry_ooda_configuration_action_status import (
    apply_manual_action_presentation_state,
    inspect_manual_action_status,
    record_manual_action_presentation,
)
from scripts.propertyquarry_ooda_configuration_manual_action import (
    stage_current_manual_action_handoff,
)
from scripts.propertyquarry_ooda_operator_status import (
    apply_operator_presentation_state,
    load_operator_status,
    record_operator_presentation,
)
from scripts.propertyquarry_ooda_operator_consent import (
    project_operator_consent,
    record_operator_consent,
)
from scripts.propertyquarry_ooda_runtime_review import (
    materialize_current_review_packet,
    materialize_current_runtime_observation_bundle,
    operator_presentation_context,
    verify_current_review_packet,
)
from scripts.propertyquarry_secure_file_io import SecureFileIOError
from scripts.propertyquarry_ooda_safe_tick import run_safe_tick
from scripts.propertyquarry_ooda_source_refresh_request import (
    inspect_source_refresh_request_bundle,
    materialize_source_refresh_request_bundle,
)
from scripts.propertyquarry_ooda_source_refresh_handoff import (
    inspect_source_refresh_handoff_bundle,
    materialize_source_refresh_handoff_bundle,
)
from scripts.propertyquarry_ooda_source_refresh_claims import (
    inspect_claim_lifecycle_bundle,
    materialize_claim_lifecycle_bundle,
)
from scripts.propertyquarry_ooda_source_refresh_trust_decision import (
    project_candidate_review_decision,
    verify_candidate_review_decision_for_report,
)
from scripts.propertyquarry_ooda_source_refresh_trust_notification import (
    run_candidate_notification,
)
from scripts.propertyquarry_ooda_source_refresh_trust_enrollment_preview import (
    materialize_enrollment_preview,
)
from scripts.propertyquarry_ooda_source_refresh_trust_enrollment_authorization import (
    verify_authorization_for_preview,
)
from scripts.propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness import (
    materialize_execution_readiness,
)
from scripts.propertyquarry_ooda_source_refresh_trust_enrollment_execution import (
    inspect_execution_for_readiness,
)
from scripts.propertyquarry_ooda_source_refresh_trust_intake import (
    apply_candidate_presentation_state,
    inspect_trust_intake_bundle,
    materialize_trust_intake_bundle,
    record_candidate_presentation,
)
from scripts.propertyquarry_ooda_source_refresh_trust_candidate_import import (
    apply_candidate_import_presentation_state,
    inspect_candidate_import_readiness,
    record_candidate_import_presentation,
)
from scripts.propertyquarry_ooda_source_refresh_trust_candidate_artifact_request import (
    inspect_candidate_artifact_request,
    materialize_candidate_artifact_request,
)
from scripts.propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification import (
    inspect_candidate_artifact_notification,
    run_candidate_artifact_notification,
)
from scripts.propertyquarry_ooda_source_refresh_trust_candidate_manual_action import (
    inspect_candidate_manual_action,
    materialize_candidate_manual_action,
)
from scripts.propertyquarry_ooda_source_refresh_settlement import (
    inspect_settlement_bundle,
    materialize_settlement_bundle,
)
from scripts.propertyquarry_ooda_scheduler_activation_authorization import (
    stage_current_scheduler_activation_authorization_handoff,
)
from scripts.propertyquarry_ooda_scheduler_activation_decision import (
    verify_current_scheduler_activation_authorization_decision,
)
from scripts.propertyquarry_ooda_scheduler_activation_execution import (
    inspect_current_scheduler_activation_execution,
)
from scripts.propertyquarry_ooda_scheduler_activation_settlement import (
    materialize_current_scheduler_activation_settlement_bundle,
    project_scheduler_activation_settlement_handoff,
)
from scripts.propertyquarry_ooda_scheduler_activation_readiness import (
    _canonical as scheduler_activation_readiness_canonical,
    materialize_current_scheduler_activation_readiness_bundle,
)
from scripts.propertyquarry_ooda_scheduler_continuity import (
    materialize_current_scheduler_continuity_bundle,
)


root = Path.cwd()
configured_receipt = str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_RECEIPT") or "").strip()
configured_signals = str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SIGNAL_DIR") or "").strip()
presentation_state_path = Path(
    str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_PRESENTATION_STATE") or "").strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/operator-presentation-state.json"
).expanduser()
consent_state_path = Path(
    str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_CONSENT_STATE") or "").strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/operator-consent-state.json"
).expanduser()
source_refresh_request_path = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_REQUEST")
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-request.json"
).expanduser()
source_refresh_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-request-verification.json"
).expanduser()
source_refresh_handoff_path = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_HANDOFF")
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-handoff.json"
).expanduser()
source_refresh_handoff_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_HANDOFF_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-handoff-verification.json"
).expanduser()
source_refresh_claims_path = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_CLAIMS")
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-claims.json"
).expanduser()
source_refresh_claims_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-claims-verification.json"
).expanduser()
source_refresh_claim_dir = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_CLAIM_DIR")
        or ""
    ).strip()
    or root / "_completion/propertyquarry_ooda_producer_claims"
).expanduser()
source_refresh_claim_trust_registry_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY"
        )
        or ""
    ).strip()
    or root
    / "config/propertyquarry_ooda_source_refresh_producer_trust.v1.json"
).expanduser()
source_refresh_trust_intake_candidate_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_DIR"
        )
        or ""
    ).strip()
    or root / "_completion/propertyquarry_ooda_producer_trust_candidates"
).expanduser()
source_refresh_trust_intake_path = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_INTAKE")
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-intake.json"
).expanduser()
source_refresh_trust_intake_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-intake-verification.json"
).expanduser()
source_refresh_trust_candidate_import_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-imports"
).expanduser()
source_refresh_trust_candidate_source_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_SOURCE_DIR"
        )
        or ""
    ).strip()
    or root / "state/incoming_propertyquarry_trust"
).expanduser()
source_refresh_trust_candidate_import_presentation_state_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_PRESENTATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-import-presentation.json"
).expanduser()
source_refresh_trust_candidate_artifact_request_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_REQUEST"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-artifact-request.json"
).expanduser()
source_refresh_trust_candidate_manual_action_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_MANUAL_ACTION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-manual-action.json"
).expanduser()
source_refresh_trust_candidate_artifact_notification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_NOTIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-artifact-notification.json"
).expanduser()
source_refresh_trust_candidate_presentation_state_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_PRESENTATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-presentation.json"
).expanduser()
source_refresh_trust_decision_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-candidate-decisions"
).expanduser()
source_refresh_trust_notification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_NOTIFICATION"
        )
        or ""
    ).strip()
    or source_refresh_trust_intake_path.parent
    / "source-refresh-trust-candidate-notification.json"
).expanduser()
source_refresh_trust_enrollment_preview_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-preview.json"
).expanduser()
source_refresh_trust_enrollment_preview_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-preview-verification.json"
).expanduser()
source_refresh_trust_enrollment_authorization_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_AUTHORIZATION_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-authorizations"
).expanduser()
source_refresh_trust_enrollment_authorization_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_AUTHORIZATION_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-authorization-verification.json"
).expanduser()
source_refresh_trust_enrollment_execution_readiness_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_READINESS"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-execution-readiness.json"
).expanduser()
source_refresh_trust_enrollment_execution_readiness_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_READINESS_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-execution-readiness-verification.json"
).expanduser()
source_refresh_trust_enrollment_execution_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-executions"
).expanduser()
source_refresh_trust_enrollment_backup_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_BACKUP_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-trust-enrollment-backups"
).expanduser()
source_refresh_completion_dir = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_COMPLETION_DIR")
        or ""
    ).strip()
    or root / "_completion/propertyquarry_ooda_producer_completions"
).expanduser()
source_refresh_settlement_path = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_SETTLEMENT")
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-settlement.json"
).expanduser()
source_refresh_settlement_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SOURCE_REFRESH_SETTLEMENT_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/source-refresh-settlement-verification.json"
).expanduser()
runtime_container_id = ""
runtime_presentation_state = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "operator-presentation-state.json"
)
runtime_scheduler_witness_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "scheduler-iteration.json"
)
runtime_cycle_receipt_path = (
    "/data/artifacts/propertyquarry-ooda-notification/latest.json"
)
runtime_source_refresh_request_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-request.json"
)
runtime_source_refresh_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-request-verification.json"
)
runtime_source_refresh_handoff_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-handoff.json"
)
runtime_source_refresh_handoff_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-handoff-verification.json"
)
runtime_source_refresh_claims_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-claims.json"
)
runtime_source_refresh_claims_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-claims-verification.json"
)
runtime_source_refresh_claim_dir = (
    "/run/propertyquarry/ooda-producer-claims"
)
runtime_source_refresh_claim_trust_registry_path = (
    "/config/propertyquarry_ooda_source_refresh_producer_trust.v1.json"
)
runtime_source_refresh_trust_intake_candidate_dir = (
    "/run/propertyquarry/ooda-producer-trust-candidates"
)
runtime_source_refresh_trust_intake_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-intake.json"
)
runtime_source_refresh_trust_intake_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-intake-verification.json"
)
runtime_source_refresh_trust_candidate_import_dir = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-imports"
)
runtime_source_refresh_trust_candidate_source_dir = (
    "/run/propertyquarry/ooda-producer-trust-source"
)
runtime_source_refresh_trust_candidate_import_presentation_state_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-import-presentation.json"
)
runtime_source_refresh_trust_candidate_artifact_request_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-artifact-request.json"
)
runtime_source_refresh_trust_candidate_manual_action_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-manual-action.json"
)
runtime_source_refresh_trust_candidate_artifact_notification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-artifact-notification.json"
)
runtime_source_refresh_trust_decision_dir = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-decisions"
)
runtime_source_refresh_trust_decision_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-decision-verification.json"
)
runtime_source_refresh_trust_presentation_state_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-presentation.json"
)
runtime_source_refresh_trust_notification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-candidate-notification.json"
)
runtime_source_refresh_trust_enrollment_preview_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-preview.json"
)
runtime_source_refresh_trust_enrollment_preview_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-preview-verification.json"
)
runtime_source_refresh_trust_enrollment_authorization_dir = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-authorizations"
)
runtime_source_refresh_trust_enrollment_authorization_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-authorization-verification.json"
)
runtime_source_refresh_trust_enrollment_execution_readiness_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-execution-readiness.json"
)
runtime_source_refresh_trust_enrollment_execution_readiness_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-execution-readiness-verification.json"
)
runtime_source_refresh_trust_enrollment_execution_dir = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-executions"
)
runtime_source_refresh_trust_enrollment_backup_dir = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-trust-enrollment-backups"
)
runtime_source_refresh_completion_dir = (
    "/run/propertyquarry/ooda-producer-completions"
)
runtime_source_refresh_settlement_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-settlement.json"
)
runtime_source_refresh_settlement_verification_path = (
    "/data/artifacts/propertyquarry-ooda-notification/"
    "source-refresh-settlement-verification.json"
)


def blocked(reason: str, *, source_type: str) -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_operator_status.v1",
        "status": "blocked",
        "action_required": False,
        "interrupt_operator": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": reason,
        "next_action": "inspect the read-only runtime projection and regenerate the approved OODA cycle",
        "actions": [],
        "source": {"type": source_type},
    }


def runtime_operator_projection(
    container_id: str,
    *,
    record_presentation: bool = False,
    expected_cycle_receipt_sha256: str = "",
) -> dict[str, object]:
    command = [
        "/usr/bin/docker",
        "exec",
        container_id,
        "/usr/local/bin/python",
        "/app/scripts/propertyquarry_ooda_operator_status.py",
        "--receipt",
        "/data/artifacts/propertyquarry-ooda-notification/latest.json",
        "--signal-dir",
        "/run/propertyquarry/ooda-signals",
        "--presentation-state",
        runtime_presentation_state,
        "--source-type",
        "runtime_container",
    ]
    if record_presentation:
        expected_digest = str(expected_cycle_receipt_sha256 or "").strip()
        if len(expected_digest) != 64:
            raise ValueError("runtime_presentation_binding_not_admissible")
        command.extend(
            [
                "--record-presentation",
                "--expected-cycle-receipt-sha256",
                expected_digest,
            ]
        )
    runtime = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 65536:
        raise ValueError("runtime_projection_not_admissible")
    payload = json.loads(runtime.stdout)
    if not isinstance(payload, dict) or payload.get("schema") != "propertyquarry.ooda_operator_status.v1":
        raise ValueError("runtime_projection_not_admissible")
    return payload


def runtime_scheduler_witness_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_scheduler_witness.py",
            "--verify",
            "--receipt",
            runtime_scheduler_witness_path,
            "--cycle-receipt",
            runtime_cycle_receipt_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 65536:
        raise ValueError("runtime_scheduler_witness_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_scheduler_iteration_verification.v1"
    ):
        raise ValueError("runtime_scheduler_witness_not_admissible")
    return payload


def runtime_source_refresh_request_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_request.py",
            "--inspect",
            "--cycle-receipt",
            runtime_cycle_receipt_path,
            "--signal-dir",
            "/run/propertyquarry/ooda-signals",
            "--request",
            runtime_source_refresh_request_path,
            "--verification",
            runtime_source_refresh_verification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError("runtime_source_refresh_request_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_request_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError("runtime_source_refresh_request_not_admissible")
    return payload


def runtime_source_refresh_handoff_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_handoff.py",
            "--inspect",
            "--cycle-receipt",
            runtime_cycle_receipt_path,
            "--signal-dir",
            "/run/propertyquarry/ooda-signals",
            "--request",
            runtime_source_refresh_request_path,
            "--request-verification",
            runtime_source_refresh_verification_path,
            "--handoff",
            runtime_source_refresh_handoff_path,
            "--verification",
            runtime_source_refresh_handoff_verification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError("runtime_source_refresh_handoff_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_handoff_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError("runtime_source_refresh_handoff_not_admissible")
    return payload


def runtime_source_refresh_claims_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_claims.py",
            "--inspect",
            "--cycle-receipt",
            runtime_cycle_receipt_path,
            "--signal-dir",
            "/run/propertyquarry/ooda-signals",
            "--request",
            runtime_source_refresh_request_path,
            "--request-verification",
            runtime_source_refresh_verification_path,
            "--handoff",
            runtime_source_refresh_handoff_path,
            "--handoff-verification",
            runtime_source_refresh_handoff_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--claim-dir",
            runtime_source_refresh_claim_dir,
            "--receipt",
            runtime_source_refresh_claims_path,
            "--verification",
            runtime_source_refresh_claims_verification_path,
            "--require-claim-dir",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError("runtime_source_refresh_claims_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_claim_lifecycle_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError("runtime_source_refresh_claims_not_admissible")
    return payload


def runtime_source_refresh_trust_intake_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_intake.py",
            "--inspect",
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--receipt",
            runtime_source_refresh_trust_intake_path,
            "--verification",
            runtime_source_refresh_trust_intake_verification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError("runtime_source_refresh_trust_intake_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_intake_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError("runtime_source_refresh_trust_intake_not_admissible")
    return payload


def runtime_source_refresh_trust_candidate_import_projection(
    container_id: str,
    *,
    record_presentation: bool = False,
    expected_presentation_digest: str = "",
) -> dict[str, object]:
    command = [
        "/usr/bin/docker",
        "exec",
        container_id,
        "/usr/local/bin/python",
        "/app/scripts/propertyquarry_ooda_source_refresh_trust_candidate_import.py",
        "--inspect",
        "--claim-verification",
        runtime_source_refresh_claims_verification_path,
        "--trust-registry",
        runtime_source_refresh_claim_trust_registry_path,
        "--candidate-dir",
        runtime_source_refresh_trust_intake_candidate_dir,
        "--intake-receipt",
        runtime_source_refresh_trust_intake_path,
        "--intake-verification",
        runtime_source_refresh_trust_intake_verification_path,
        "--import-dir",
        runtime_source_refresh_trust_candidate_import_dir,
        "--source-discovery-dir",
        runtime_source_refresh_trust_candidate_source_dir,
        "--presentation-state",
        runtime_source_refresh_trust_candidate_import_presentation_state_path,
    ]
    if record_presentation:
        expected_digest = str(expected_presentation_digest or "").strip()
        if len(expected_digest) != 64 or any(
            value not in "0123456789abcdef" for value in expected_digest
        ):
            raise ValueError(
                "runtime_source_refresh_trust_candidate_import_presentation_binding_not_admissible"
            )
        command.extend(
            [
                "--record-presentation",
                "--expected-presentation-digest",
                expected_digest,
            ]
        )
    runtime = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError(
            "runtime_source_refresh_trust_candidate_import_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_candidate_import_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
        and payload.get("import_state")
        in {
            "awaiting_external_artifact",
            "external_artifact_not_admissible",
            "ready_for_manual_import",
            "not_required",
            "succeeded",
            "recovery_required",
            "blocked",
        }
    ):
        raise ValueError(
            "runtime_source_refresh_trust_candidate_import_not_admissible"
        )
    if record_presentation:
        presentation_receipt = dict(
            payload.get("presentation_receipt") or {}
        )
        if not (
            presentation_receipt.get("status") == "recorded"
            and presentation_receipt.get("presentation_digest")
            == expected_digest
            and presentation_receipt.get("delivery_state_updated") is False
            and presentation_receipt.get("provider_quota_consumed") is False
            and presentation_receipt.get("trust_registry_modified") is False
            and presentation_receipt.get("protected_operation_executed")
            is False
        ):
            raise ValueError(
                "runtime_source_refresh_trust_candidate_import_presentation_not_admissible"
            )
    return payload


def runtime_source_refresh_trust_candidate_artifact_request_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_candidate_artifact_request.py",
            "--inspect-current",
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--import-dir",
            runtime_source_refresh_trust_candidate_import_dir,
            "--source-discovery-dir",
            runtime_source_refresh_trust_candidate_source_dir,
            "--receipt",
            runtime_source_refresh_trust_candidate_artifact_request_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError(
            "runtime_source_refresh_trust_candidate_artifact_request_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_candidate_artifact_request_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError(
            "runtime_source_refresh_trust_candidate_artifact_request_not_admissible"
        )
    return payload


def runtime_source_refresh_trust_candidate_manual_action_projection(
    container_id: str,
    *,
    materialize: bool = False,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_candidate_manual_action.py",
            "--materialize-current" if materialize else "--inspect-current",
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--import-dir",
            runtime_source_refresh_trust_candidate_import_dir,
            "--source-discovery-dir",
            runtime_source_refresh_trust_candidate_source_dir,
            "--presentation-state",
            runtime_source_refresh_trust_candidate_import_presentation_state_path,
            "--artifact-request-receipt",
            runtime_source_refresh_trust_candidate_artifact_request_path,
            "--receipt",
            runtime_source_refresh_trust_candidate_manual_action_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError(
            "runtime_source_refresh_trust_candidate_manual_action_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_candidate_manual_action_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError(
            "runtime_source_refresh_trust_candidate_manual_action_not_admissible"
        )
    return payload


def runtime_source_refresh_trust_candidate_artifact_notification_projection(
    container_id: str,
    *,
    evaluate: bool = False,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification.py",
            "--evaluate-current" if evaluate else "--inspect-current",
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--import-dir",
            runtime_source_refresh_trust_candidate_import_dir,
            "--source-discovery-dir",
            runtime_source_refresh_trust_candidate_source_dir,
            "--presentation-state",
            runtime_source_refresh_trust_candidate_import_presentation_state_path,
            "--artifact-request-receipt",
            runtime_source_refresh_trust_candidate_artifact_request_path,
            "--manual-action-receipt",
            runtime_source_refresh_trust_candidate_manual_action_path,
            "--receipt",
            runtime_source_refresh_trust_candidate_artifact_notification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError(
            "runtime_source_refresh_trust_candidate_artifact_notification_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_candidate_artifact_notification_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError(
            "runtime_source_refresh_trust_candidate_artifact_notification_not_admissible"
        )
    return payload


def runtime_source_refresh_trust_decision_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_decision.py",
            "--verify-current",
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--decision-dir",
            runtime_source_refresh_trust_decision_dir,
            "--verification-write",
            runtime_source_refresh_trust_decision_verification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError("runtime_source_refresh_trust_decision_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_candidate_decision_verification.v1"
        and payload.get("status")
        in {"not_required", "pending", "verified", "blocked"}
    ):
        raise ValueError("runtime_source_refresh_trust_decision_not_admissible")
    return payload


def runtime_source_refresh_trust_notification_projection(
    container_id: str,
    *,
    record_presentation: bool = False,
    expected_candidate_review_id: str = "",
) -> dict[str, object]:
    command = [
        "/usr/bin/docker",
        "exec",
        container_id,
        "/usr/local/bin/python",
        "/app/scripts/propertyquarry_ooda_source_refresh_trust_notification.py",
        "--decision-dir",
        runtime_source_refresh_trust_decision_dir,
        "--claim-verification",
        runtime_source_refresh_claims_verification_path,
        "--trust-registry",
        runtime_source_refresh_claim_trust_registry_path,
        "--candidate-dir",
        runtime_source_refresh_trust_intake_candidate_dir,
        "--intake-receipt",
        runtime_source_refresh_trust_intake_path,
        "--intake-verification",
        runtime_source_refresh_trust_intake_verification_path,
        "--presentation-state",
        runtime_source_refresh_trust_presentation_state_path,
        "--receipt",
        runtime_source_refresh_trust_notification_path,
    ]
    if record_presentation:
        expected_review_id = str(expected_candidate_review_id or "").strip()
        if not expected_review_id:
            raise ValueError(
                "runtime_source_refresh_trust_notification_presentation_binding_not_admissible"
            )
        command.extend(
            [
                "--record-presentation",
                "--expected-candidate-review-id",
                expected_review_id,
            ]
        )
    runtime = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError("runtime_source_refresh_trust_notification_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_candidate_notification_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError("runtime_source_refresh_trust_notification_not_admissible")
    if record_presentation:
        presentation_receipt = dict(payload.get("presentation_receipt") or {})
        if not (
            payload.get("status") == "verified"
            and payload.get("notification_status") == "deduplicated"
            and payload.get("action_required") is True
            and payload.get("interrupt_operator") is False
            and payload.get("delivery_authorized") is False
            and payload.get("delivery_attempted") is False
            and payload.get("sent") is False
            and payload.get("producer_dispatch_authorized") is False
            and payload.get("producer_refresh_authorized") is False
            and payload.get("automatic_source_refresh_allowed") is False
            and payload.get("automatic_execution_allowed") is False
            and payload.get("execution_authorized") is False
            and payload.get("deployment_or_restart_authorized") is False
            and payload.get("protected_operation_executed") is False
            and payload.get("provider_quota_consumption_allowed") is False
            and payload.get("private_key_material_requested") is False
            and payload.get("private_key_material_recorded") is False
            and payload.get("trust_enrollment_authorized") is False
            and payload.get("trust_registry_modified") is False
            and payload.get("presentation_transition_recorded") is True
            and presentation_receipt.get("status")
            in {"recorded", "unchanged"}
            and presentation_receipt.get("candidate_review_id")
            == expected_review_id
            and presentation_receipt.get("delivery_state_updated") is False
            and presentation_receipt.get("provider_quota_consumed") is False
            and presentation_receipt.get("trust_registry_modified") is False
            and presentation_receipt.get("protected_operation_executed")
            is False
        ):
            raise ValueError(
                "runtime_source_refresh_trust_notification_presentation_not_admissible"
            )
    return payload


def runtime_source_refresh_trust_enrollment_preview_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_enrollment_preview.py",
            "--decision-dir",
            runtime_source_refresh_trust_decision_dir,
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--receipt",
            runtime_source_refresh_trust_enrollment_preview_path,
            "--verification",
            runtime_source_refresh_trust_enrollment_preview_verification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 1048576:
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_preview_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_enrollment_preview_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_preview_not_admissible"
        )
    return payload


def runtime_source_refresh_trust_enrollment_authorization_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_enrollment_authorization.py",
            "--verify-current",
            "--decision-dir",
            runtime_source_refresh_trust_enrollment_authorization_dir,
            "--verification-write",
            runtime_source_refresh_trust_enrollment_authorization_verification_path,
            "--preview-decision-dir",
            runtime_source_refresh_trust_decision_dir,
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--preview-receipt",
            runtime_source_refresh_trust_enrollment_preview_path,
            "--preview-verification",
            runtime_source_refresh_trust_enrollment_preview_verification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_authorization_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_enrollment_authorization_verification.v1"
        and payload.get("status")
        in {"not_required", "pending", "verified", "blocked"}
    ):
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_authorization_not_admissible"
        )
    return payload


def runtime_source_refresh_trust_enrollment_execution_readiness_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness.py",
            "--authorization-dir",
            runtime_source_refresh_trust_enrollment_authorization_dir,
            "--preview-decision-dir",
            runtime_source_refresh_trust_decision_dir,
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--preview-receipt",
            runtime_source_refresh_trust_enrollment_preview_path,
            "--preview-verification",
            runtime_source_refresh_trust_enrollment_preview_verification_path,
            "--receipt",
            runtime_source_refresh_trust_enrollment_execution_readiness_path,
            "--verification",
            runtime_source_refresh_trust_enrollment_execution_readiness_verification_path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 262144:
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_execution_readiness_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_enrollment_execution_readiness_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_execution_readiness_not_admissible"
        )
    return payload


def runtime_source_refresh_trust_enrollment_execution_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_trust_enrollment_execution.py",
            "--inspect",
            "--authorization-dir",
            runtime_source_refresh_trust_enrollment_authorization_dir,
            "--preview-decision-dir",
            runtime_source_refresh_trust_decision_dir,
            "--claim-verification",
            runtime_source_refresh_claims_verification_path,
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--candidate-dir",
            runtime_source_refresh_trust_intake_candidate_dir,
            "--intake-receipt",
            runtime_source_refresh_trust_intake_path,
            "--intake-verification",
            runtime_source_refresh_trust_intake_verification_path,
            "--preview-receipt",
            runtime_source_refresh_trust_enrollment_preview_path,
            "--preview-verification",
            runtime_source_refresh_trust_enrollment_preview_verification_path,
            "--readiness-receipt",
            runtime_source_refresh_trust_enrollment_execution_readiness_path,
            "--readiness-verification",
            runtime_source_refresh_trust_enrollment_execution_readiness_verification_path,
            "--execution-dir",
            runtime_source_refresh_trust_enrollment_execution_dir,
            "--backup-dir",
            runtime_source_refresh_trust_enrollment_backup_dir,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 1048576:
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_execution_not_admissible"
        )
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_trust_enrollment_execution_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError(
            "runtime_source_refresh_trust_enrollment_execution_not_admissible"
        )
    return payload


def runtime_source_refresh_settlement_projection(
    container_id: str,
) -> dict[str, object]:
    runtime = subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            container_id,
            "/usr/local/bin/python",
            "/app/scripts/propertyquarry_ooda_source_refresh_settlement.py",
            "--inspect",
            "--cycle-receipt",
            runtime_cycle_receipt_path,
            "--signal-dir",
            "/run/propertyquarry/ooda-signals",
            "--trust-registry",
            runtime_source_refresh_claim_trust_registry_path,
            "--completion-dir",
            runtime_source_refresh_completion_dir,
            "--receipt",
            runtime_source_refresh_settlement_path,
            "--verification",
            runtime_source_refresh_settlement_verification_path,
            "--require-completion-dir",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if runtime.returncode not in {0, 1} or len(runtime.stdout.encode()) > 524288:
        raise ValueError("runtime_source_refresh_settlement_not_admissible")
    payload = json.loads(runtime.stdout)
    if not (
        isinstance(payload, dict)
        and payload.get("schema")
        == "propertyquarry.ooda_source_refresh_settlement_verification.v1"
        and payload.get("status") in {"verified", "blocked"}
    ):
        raise ValueError("runtime_source_refresh_settlement_not_admissible")
    return payload


if configured_receipt or configured_signals:
    receipt_path = (
        Path(configured_receipt).expanduser()
        if configured_receipt
        else root / "_completion/propertyquarry_ooda_notification_cycle/latest.json"
    )
    signal_dir = (
        Path(configured_signals).expanduser()
        if configured_signals
        else root / "_completion/propertyquarry_ooda_signal_ingress"
    )
    summary = load_operator_status(
        cycle_receipt_path=receipt_path,
        signal_dir=signal_dir,
    )
    summary["source"] = {
        "type": "configured_operator_filesystem",
        "cycle_receipt": str(receipt_path),
        "signal_dir": str(signal_dir),
        "presentation_state": str(presentation_state_path),
    }
else:
    try:
        query = subprocess.run(
            [
                "/usr/bin/docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={os.getenv('PROPERTYQUARRY_COMPOSE_PROJECT_NAME', 'property')}",
                "--filter",
                "label=com.docker.compose.service=propertyquarry-scheduler",
                "--format",
                "{{.ID}}\t{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        summary = blocked("runtime_container_query_failed", source_type="runtime_container")
    else:
        runtime_rows = [row.split("\t", 1) for row in query.stdout.splitlines() if row.strip()]
        if query.returncode != 0:
            summary = blocked("runtime_container_query_failed", source_type="runtime_container")
        elif len(runtime_rows) > 1 or any(len(row) != 2 for row in runtime_rows):
            summary = blocked("runtime_scheduler_cardinality_not_admissible", source_type="runtime_container")
        elif runtime_rows:
            container_id, container_name = runtime_rows[0]
            runtime_container_id = container_id
            try:
                summary = runtime_operator_projection(
                    container_id,
                    record_presentation=False,
                )
                try:
                    scheduler_witness = runtime_scheduler_witness_projection(
                        container_id
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    scheduler_witness = {
                        "schema": "propertyquarry.ooda_scheduler_iteration_verification.v1",
                        "status": "blocked",
                        "blocking_reason": "runtime_scheduler_witness_unavailable",
                        "persistent_reevaluation_verified": False,
                        "progress": {"current_evidence_verified": False},
                        "execution_authorized": False,
                        "deployment_or_restart_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_enrollment_authorization = (
                        runtime_source_refresh_trust_enrollment_authorization_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_enrollment_authorization = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_authorization_verification.v1",
                        "status": "blocked",
                        "authorization_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_enrollment_authorization_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "explicit_authorization_recorded": False,
                        "exact_preview_authorized": False,
                        "trust_enrollment_authorized": False,
                        "trust_registry_modified": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_enrollment_execution_readiness = (
                        runtime_source_refresh_trust_enrollment_execution_readiness_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_enrollment_execution_readiness = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_execution_readiness_verification.v1",
                        "status": "blocked",
                        "readiness_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_enrollment_execution_readiness_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "execution_request_staged": False,
                        "execution_readiness_verified": False,
                        "governed_execution_available": False,
                        "trust_registry_modified": False,
                        "execution_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_enrollment_execution = (
                        runtime_source_refresh_trust_enrollment_execution_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_enrollment_execution = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_execution_verification.v1",
                        "status": "blocked",
                        "execution_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_enrollment_execution_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "authorization_consumed": False,
                        "manual_execution_authorized": False,
                        "automatic_execution_allowed": False,
                        "execution_authorized": False,
                        "trust_registry_write_attempted": False,
                        "trust_registry_modified": False,
                        "rollback_available": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                        "delivery_attempted": False,
                        "sent": False,
                    }
                try:
                    runtime_source_refresh_request = (
                        runtime_source_refresh_request_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_request = {
                        "schema": "propertyquarry.ooda_source_refresh_request_verification.v1",
                        "status": "blocked",
                        "request_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_request_unavailable",
                        "request_staged": False,
                        "requested_lanes": [],
                        "action_required": False,
                        "interrupt_operator": False,
                        "producer_dispatch_authorized": False,
                        "producer_refresh_authorized": False,
                        "deployment_or_restart_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_handoff = (
                        runtime_source_refresh_handoff_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_handoff = {
                        "schema": "propertyquarry.ooda_source_refresh_handoff_verification.v1",
                        "status": "blocked",
                        "handoff_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_handoff_unavailable",
                        "handoff_available": False,
                        "work_items": [],
                        "action_required": False,
                        "interrupt_operator": False,
                        "handoff_confers_authority": False,
                        "producer_claim_recorded": False,
                        "producer_dispatch_authorized": False,
                        "producer_refresh_authorized": False,
                        "deployment_or_restart_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_claims = (
                        runtime_source_refresh_claims_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_claims = {
                        "schema": "propertyquarry.ooda_source_refresh_claim_lifecycle_verification.v1",
                        "status": "blocked",
                        "claim_state": "blocked",
                        "settlement_state": "unverified",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_claims_unavailable",
                        "producer_claim_recorded": False,
                        "claims": [],
                        "action_required": False,
                        "interrupt_operator": False,
                        "claim_confers_authority": False,
                        "producer_dispatch_authorized": False,
                        "producer_refresh_authorized": False,
                        "deployment_or_restart_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_intake = (
                        runtime_source_refresh_trust_intake_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_intake = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_intake_verification.v1",
                        "status": "blocked",
                        "intake_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_intake_unavailable",
                        "request_staged": False,
                        "requested_lanes": [],
                        "action_required": False,
                        "interrupt_operator": False,
                        "public_key_candidate_recorded": False,
                        "private_key_material_requested": False,
                        "trust_enrollment_authorized": False,
                        "trust_registry_modified": False,
                        "producer_dispatch_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_candidate_import = (
                        runtime_source_refresh_trust_candidate_import_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_candidate_import = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_import_verification.v1",
                        "status": "blocked",
                        "import_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_candidate_import_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "operator_review_required": False,
                        "candidate_import_authorized": False,
                        "candidate_import_attempted": False,
                        "candidate_imported": False,
                        "public_key_candidate_recorded": False,
                        "private_key_material_requested": False,
                        "trust_enrollment_authorized": False,
                        "trust_registry_modified": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_candidate_artifact_request = (
                        runtime_source_refresh_trust_candidate_artifact_request_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_candidate_artifact_request = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_artifact_request_verification.v1",
                        "status": "blocked",
                        "request_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_candidate_artifact_request_unavailable",
                        "artifact_request_staged": False,
                        "action_required": False,
                        "interrupt_operator": False,
                        "receipt_persisted": False,
                        "producer_contacted": False,
                        "transport_delivery_attempted": False,
                        "candidate_import_authorized": False,
                        "trust_registry_modified": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_candidate_manual_action = (
                        runtime_source_refresh_trust_candidate_manual_action_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_candidate_manual_action = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_manual_action_verification.v1",
                        "status": "blocked",
                        "action_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_candidate_manual_action_unavailable",
                        "operator_action_receipt_staged": False,
                        "action_required": False,
                        "interrupt_operator": False,
                        "receipt_persisted": False,
                        "transport_delivery_authorized": False,
                        "transport_delivery_attempted": False,
                        "candidate_import_authorized": False,
                        "trust_registry_modified": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_candidate_artifact_notification = (
                        runtime_source_refresh_trust_candidate_artifact_notification_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_candidate_artifact_notification = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_artifact_notification_verification.v1",
                        "status": "blocked",
                        "notification_status": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_candidate_artifact_notification_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "delivery_authorized": False,
                        "delivery_attempted": False,
                        "sent": False,
                        "presentation_recorded": False,
                        "receipt_persisted": False,
                        "producer_contacted": False,
                        "artifact_transport_authorized": False,
                        "artifact_transport_attempted": False,
                        "candidate_import_authorized": False,
                        "trust_registry_modified": False,
                        "provider_quota_consumption_allowed": False,
                    }
                try:
                    runtime_source_refresh_trust_decision = (
                        runtime_source_refresh_trust_decision_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_decision = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_decision_verification.v1",
                        "status": "blocked",
                        "review_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_decision_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "identity_verification_asserted": False,
                        "trust_enrollment_preview_authorized": False,
                        "trust_enrollment_authorized": False,
                        "trust_registry_modified": False,
                        "decision_confers_trust": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_trust_notification = (
                        runtime_source_refresh_trust_notification_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_notification = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_notification_verification.v1",
                        "status": "blocked",
                        "notification_status": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_notification_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "trust_enrollment_authorized": False,
                        "trust_registry_modified": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                        "delivery_attempted": False,
                        "sent": False,
                    }
                try:
                    runtime_source_refresh_trust_enrollment_preview = (
                        runtime_source_refresh_trust_enrollment_preview_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_trust_enrollment_preview = {
                        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_preview_verification.v1",
                        "status": "blocked",
                        "preview_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_trust_enrollment_preview_unavailable",
                        "action_required": False,
                        "interrupt_operator": False,
                        "preview_staged": False,
                        "trust_enrollment_preview_authorized": False,
                        "trust_enrollment_authorized": False,
                        "trust_registry_modified": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                try:
                    runtime_source_refresh_settlement = (
                        runtime_source_refresh_settlement_projection(
                            container_id
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ):
                    runtime_source_refresh_settlement = {
                        "schema": "propertyquarry.ooda_source_refresh_settlement_verification.v1",
                        "status": "blocked",
                        "settlement_state": "blocked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "blocking_reason": "runtime_source_refresh_settlement_unavailable",
                        "producer_completion_recorded": False,
                        "settlement_attributed": False,
                        "settlements": [],
                        "action_required": False,
                        "interrupt_operator": False,
                        "completion_confers_authority": False,
                        "claim_confers_authority": False,
                        "producer_dispatch_authorized": False,
                        "producer_refresh_authorized": False,
                        "deployment_or_restart_authorized": False,
                        "protected_operation_executed": False,
                        "provider_quota_consumption_allowed": False,
                        "delivery_authorized": False,
                    }
                summary["source"] = {
                    "type": "runtime_container",
                    "container_name": container_name,
                    "presentation_state": runtime_presentation_state,
                    "scheduler_witness": scheduler_witness,
                    "source_refresh_request": runtime_source_refresh_request,
                    "source_refresh_handoff": runtime_source_refresh_handoff,
                    "source_refresh_claims": runtime_source_refresh_claims,
                    "source_refresh_trust_intake": (
                        runtime_source_refresh_trust_intake
                    ),
                    "source_refresh_trust_candidate_import": (
                        runtime_source_refresh_trust_candidate_import
                    ),
                    "source_refresh_trust_candidate_artifact_request": (
                        runtime_source_refresh_trust_candidate_artifact_request
                    ),
                    "source_refresh_trust_candidate_manual_action": (
                        runtime_source_refresh_trust_candidate_manual_action
                    ),
                    "source_refresh_trust_candidate_artifact_notification": (
                        runtime_source_refresh_trust_candidate_artifact_notification
                    ),
                    "source_refresh_trust_decision": (
                        runtime_source_refresh_trust_decision
                    ),
                    "source_refresh_trust_notification": (
                        runtime_source_refresh_trust_notification
                    ),
                    "source_refresh_trust_enrollment_preview": (
                        runtime_source_refresh_trust_enrollment_preview
                    ),
                    "source_refresh_trust_enrollment_authorization": (
                        runtime_source_refresh_trust_enrollment_authorization
                    ),
                    "source_refresh_trust_enrollment_execution_readiness": (
                        runtime_source_refresh_trust_enrollment_execution_readiness
                    ),
                    "source_refresh_trust_enrollment_execution": (
                        runtime_source_refresh_trust_enrollment_execution
                    ),
                    "source_refresh_settlement": (
                        runtime_source_refresh_settlement
                    ),
                }
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
                summary = blocked("runtime_operator_projection_unavailable", source_type="runtime_container")
        else:
            receipt_path = root / "_completion/propertyquarry_ooda_notification_cycle/latest.json"
            signal_dir = root / "_completion/propertyquarry_ooda_signal_ingress"
            try:
                safe_tick = run_safe_tick(
                    refresh_public_origin_observation=True,
                    signal_dir=signal_dir,
                    cycle_receipt_path=receipt_path,
                    source_refresh_claim_trust_registry_path=(
                        source_refresh_claim_trust_registry_path
                    ),
                    source_refresh_claim_dir=source_refresh_claim_dir,
                    source_refresh_claim_receipt_path=(
                        source_refresh_claims_path
                    ),
                    source_refresh_claim_verification_path=(
                        source_refresh_claims_verification_path
                    ),
                    source_refresh_trust_intake_candidate_dir=(
                        source_refresh_trust_intake_candidate_dir
                    ),
                    source_refresh_trust_intake_receipt_path=(
                        source_refresh_trust_intake_path
                    ),
                    source_refresh_trust_intake_verification_path=(
                        source_refresh_trust_intake_verification_path
                    ),
                    source_refresh_trust_decision_dir=(
                        source_refresh_trust_decision_dir
                    ),
                    source_refresh_completion_dir=(
                        source_refresh_completion_dir
                    ),
                    source_refresh_settlement_receipt_path=(
                        source_refresh_settlement_path
                    ),
                    source_refresh_settlement_verification_path=(
                        source_refresh_settlement_verification_path
                    ),
                )
            except (OSError, TypeError, ValueError):
                safe_tick = {
                    "status": "blocked",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "blocking_reason": "safe_tick_unavailable",
                    "action_required": False,
                    "interrupt_operator": False,
                    "automatic_execution_allowed": False,
                    "deployment_or_restart_authorized": False,
                    "provider_quota_consumption_allowed": False,
                    "delivery_authorized": False,
                }
            if safe_tick.get("status") == "ready":
                summary = load_operator_status(
                    cycle_receipt_path=receipt_path,
                    signal_dir=signal_dir,
                )
            else:
                summary = blocked(
                    str(safe_tick.get("blocking_reason") or "safe_tick_unavailable"),
                    source_type="operator_filesystem_no_runtime_container",
                )
            summary["source"] = {
                "type": "operator_filesystem_no_runtime_container",
                "cycle_receipt": str(receipt_path),
                "signal_dir": str(signal_dir),
                "presentation_state": str(presentation_state_path),
                "safe_tick": {
                    "status": str(safe_tick.get("status") or "blocked"),
                    "updated_at": str(safe_tick.get("updated_at") or ""),
                    "blocking_reason": str(
                        safe_tick.get("blocking_reason") or ""
                    ),
                    "current_evidence_verified": dict(
                        safe_tick.get("progress") or {}
                    ).get("current_evidence_verified")
                    is True,
                    "receipt_persisted": safe_tick.get("receipt_persisted") is True,
                },
            }

source = dict(summary.get("source") or {})
try:
    if source.get("type") == "runtime_container":
        source_refresh_settlement = dict(
            source.get("source_refresh_settlement") or {}
        )
        if source_refresh_settlement.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_settlement_not_admissible"
            )
    elif source.get("type") == "operator_filesystem_no_runtime_container":
        source_refresh_settlement = inspect_settlement_bundle(
            summary,
            signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
            trust_registry_path=source_refresh_claim_trust_registry_path,
            completion_dir=source_refresh_completion_dir,
            receipt_path=source_refresh_settlement_path,
            verification_path=source_refresh_settlement_verification_path,
        )
    elif source.get("type") == "configured_operator_filesystem":
        source_refresh_settlement = materialize_settlement_bundle(
            summary,
            signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
            handoff_path=source_refresh_handoff_path,
            handoff_verification_path=(
                source_refresh_handoff_verification_path
            ),
            claim_lifecycle_path=source_refresh_claims_path,
            claim_verification_path=(
                source_refresh_claims_verification_path
            ),
            trust_registry_path=source_refresh_claim_trust_registry_path,
            claim_dir=source_refresh_claim_dir,
            completion_dir=source_refresh_completion_dir,
            receipt_path=source_refresh_settlement_path,
            verification_path=source_refresh_settlement_verification_path,
        )
    else:
        raise ValueError("source_refresh_settlement_source_not_admissible")
except (OSError, TypeError, ValueError):
    source_refresh_settlement = {
        "status": "blocked",
        "settlement_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_settlement_unavailable",
        "next_action": (
            "repair signed completion and current source bindings before attribution"
        ),
        "producer_completion_recorded": False,
        "settlement_attributed": False,
        "settlements": [],
        "progress": {
            "completion_signature_count": 0,
            "settled_count": 0,
            "expected_work_item_count": 0,
            "current_evidence_verified": False,
        },
        "action_required": False,
        "interrupt_operator": False,
        "completion_confers_authority": False,
        "claim_confers_authority": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_source_refresh_allowed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_request = dict(
            source.get("source_refresh_request") or {}
        )
        if source_refresh_request.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_request_not_admissible"
            )
    elif source.get("type") == "operator_filesystem_no_runtime_container":
        source_refresh_request = inspect_source_refresh_request_bundle(
            summary,
            request_path=source_refresh_request_path,
            verification_path=source_refresh_verification_path,
        )
    elif source.get("type") == "configured_operator_filesystem":
        if source_refresh_settlement.get("status") != "verified":
            raise ValueError("source_refresh_settlement_not_verified")
        source_refresh_request = materialize_source_refresh_request_bundle(
            summary,
            request_path=source_refresh_request_path,
            verification_path=source_refresh_verification_path,
        )
    else:
        raise ValueError("source_refresh_request_source_not_admissible")
except (OSError, TypeError, ValueError):
    source_refresh_request = {
        "status": "blocked",
        "request_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_request_unavailable",
        "next_action": (
            "regenerate a fresh approved OODA cycle before staging producer work"
        ),
        "request_id": "",
        "request_staged": False,
        "requested_lanes": [],
        "action_required": False,
        "interrupt_operator": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_handoff = dict(
            source.get("source_refresh_handoff") or {}
        )
        if source_refresh_handoff.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_handoff_not_admissible"
            )
    elif source.get("type") == "operator_filesystem_no_runtime_container":
        source_refresh_handoff = inspect_source_refresh_handoff_bundle(
            summary,
            request_path=source_refresh_request_path,
            request_verification_path=source_refresh_verification_path,
            handoff_path=source_refresh_handoff_path,
            verification_path=source_refresh_handoff_verification_path,
        )
    elif source.get("type") == "configured_operator_filesystem":
        if source_refresh_settlement.get("status") != "verified":
            raise ValueError("source_refresh_settlement_not_verified")
        source_refresh_handoff = materialize_source_refresh_handoff_bundle(
            summary,
            request_path=source_refresh_request_path,
            request_verification_path=source_refresh_verification_path,
            handoff_path=source_refresh_handoff_path,
            verification_path=source_refresh_handoff_verification_path,
        )
    else:
        raise ValueError("source_refresh_handoff_source_not_admissible")
except (OSError, TypeError, ValueError):
    source_refresh_handoff = {
        "status": "blocked",
        "handoff_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_handoff_unavailable",
        "next_action": (
            "repair the exact request-bound producer handoff before pickup"
        ),
        "handoff_id": "",
        "handoff_available": False,
        "work_items": [],
        "action_required": False,
        "interrupt_operator": False,
        "handoff_confers_authority": False,
        "producer_claim_recorded": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_claims = dict(
            source.get("source_refresh_claims") or {}
        )
        if source_refresh_claims.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_claims_not_admissible"
            )
    elif source.get("type") == "operator_filesystem_no_runtime_container":
        source_refresh_claims = inspect_claim_lifecycle_bundle(
            summary,
            request_path=source_refresh_request_path,
            request_verification_path=source_refresh_verification_path,
            handoff_path=source_refresh_handoff_path,
            handoff_verification_path=(
                source_refresh_handoff_verification_path
            ),
            trust_registry_path=(
                source_refresh_claim_trust_registry_path
            ),
            claim_dir=source_refresh_claim_dir,
            receipt_path=source_refresh_claims_path,
            verification_path=source_refresh_claims_verification_path,
        )
    elif source.get("type") == "configured_operator_filesystem":
        if source_refresh_settlement.get("status") != "verified":
            raise ValueError("source_refresh_settlement_not_verified")
        source_refresh_claims = materialize_claim_lifecycle_bundle(
            summary,
            request_path=source_refresh_request_path,
            request_verification_path=source_refresh_verification_path,
            handoff_path=source_refresh_handoff_path,
            handoff_verification_path=(
                source_refresh_handoff_verification_path
            ),
            trust_registry_path=(
                source_refresh_claim_trust_registry_path
            ),
            claim_dir=source_refresh_claim_dir,
            receipt_path=source_refresh_claims_path,
            verification_path=source_refresh_claims_verification_path,
        )
    else:
        raise ValueError("source_refresh_claims_source_not_admissible")
except (OSError, TypeError, ValueError):
    source_refresh_claims = {
        "status": "blocked",
        "claim_state": "blocked",
        "settlement_state": "unverified",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_claims_unavailable",
        "next_action": (
            "repair signed producer claim evidence before inferring pickup"
        ),
        "handoff_id": "",
        "producer_claim_recorded": False,
        "claims": [],
        "progress": {
            "expected_claim_count": 0,
            "claim_count": 0,
            "current_evidence_verified": False,
        },
        "action_required": False,
        "interrupt_operator": False,
        "claim_confers_authority": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_source_refresh_allowed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_trust_intake = dict(
            source.get("source_refresh_trust_intake") or {}
        )
        if source_refresh_trust_intake.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_trust_intake_not_admissible"
            )
    elif source.get("type") == "operator_filesystem_no_runtime_container":
        if source_refresh_claims.get("status") != "verified":
            raise ValueError("source_refresh_claims_not_verified")
        source_refresh_trust_intake = inspect_trust_intake_bundle(
            claim_verification_path=(
                source_refresh_claims_verification_path
            ),
            trust_registry_path=source_refresh_claim_trust_registry_path,
            candidate_dir=source_refresh_trust_intake_candidate_dir,
            receipt_path=source_refresh_trust_intake_path,
            verification_path=(
                source_refresh_trust_intake_verification_path
            ),
        )
    elif source.get("type") == "configured_operator_filesystem":
        if source_refresh_claims.get("status") != "verified":
            raise ValueError("source_refresh_claims_not_verified")
        source_refresh_trust_intake = materialize_trust_intake_bundle(
            claim_verification_path=(
                source_refresh_claims_verification_path
            ),
            trust_registry_path=source_refresh_claim_trust_registry_path,
            candidate_dir=source_refresh_trust_intake_candidate_dir,
            receipt_path=source_refresh_trust_intake_path,
            verification_path=(
                source_refresh_trust_intake_verification_path
            ),
        )
    else:
        raise ValueError("source_refresh_trust_intake_source_not_admissible")
except (OSError, TypeError, ValueError):
    source_refresh_trust_intake = {
        "status": "blocked",
        "intake_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_intake_unavailable",
        "next_action": (
            "repair the claim-bound public-key intake receipt without editing trust"
        ),
        "request_id": "",
        "request_staged": False,
        "requested_lanes": [],
        "progress": {
            "current_evidence_verified": False,
            "candidate_evidence_count": 0,
        },
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "producer_dispatch_authorized": False,
        "producer_refresh_authorized": False,
        "automatic_source_refresh_allowed": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_trust_candidate_import = dict(
            source.get("source_refresh_trust_candidate_import") or {}
        )
        if (
            source_refresh_trust_candidate_import.get("status")
            not in {"verified", "blocked"}
            or source_refresh_trust_candidate_import.get("import_state")
            not in {
                "awaiting_external_artifact",
                "external_artifact_not_admissible",
                "ready_for_manual_import",
                "not_required",
                "succeeded",
                "recovery_required",
                "blocked",
            }
        ):
            raise ValueError(
                "runtime_source_refresh_trust_candidate_import_not_admissible"
            )
    else:
        source_refresh_trust_candidate_import = (
            inspect_candidate_import_readiness(
                source_refresh_trust_intake,
                candidate_dir=source_refresh_trust_intake_candidate_dir,
                import_dir=source_refresh_trust_candidate_import_dir,
                source_discovery_dir=(
                    source_refresh_trust_candidate_source_dir
                ),
            )
        )
        if (
            source_refresh_trust_candidate_import.get("status")
            not in {"verified", "blocked"}
            or source_refresh_trust_candidate_import.get("import_state")
            not in {
                "awaiting_external_artifact",
                "external_artifact_not_admissible",
                "ready_for_manual_import",
                "not_required",
                "succeeded",
                "recovery_required",
                "blocked",
            }
        ):
            raise ValueError(
                "source_refresh_trust_candidate_import_not_admissible"
            )
        source_refresh_trust_candidate_import = (
            apply_candidate_import_presentation_state(
                source_refresh_trust_candidate_import,
                state_path=(
                    source_refresh_trust_candidate_import_presentation_state_path
                ),
            )
        )
except (OSError, TypeError, ValueError):
    source_refresh_trust_candidate_import = {
        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_import_verification.v1",
        "status": "blocked",
        "import_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_candidate_import_unavailable",
        "next_action": (
            "repair the exact current intake and import-history binding; "
            "do not copy private keys or edit trust"
        ),
        "request_id": "",
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_trust_candidate_artifact_request = dict(
            source.get("source_refresh_trust_candidate_artifact_request")
            or {}
        )
    else:
        source_refresh_trust_candidate_artifact_request = (
            materialize_candidate_artifact_request(
                source_refresh_trust_candidate_import,
                receipt_path=(
                    source_refresh_trust_candidate_artifact_request_path
                ),
            )
        )
    if not (
        source_refresh_trust_candidate_artifact_request.get("status")
        in {"verified", "blocked"}
        and source_refresh_trust_candidate_artifact_request.get(
            "interrupt_operator"
        )
        is False
        and source_refresh_trust_candidate_artifact_request.get(
            "producer_contacted"
        )
        is False
        and source_refresh_trust_candidate_artifact_request.get(
            "transport_delivery_attempted"
        )
        is False
        and source_refresh_trust_candidate_artifact_request.get(
            "candidate_import_authorized"
        )
        is False
        and source_refresh_trust_candidate_artifact_request.get(
            "trust_registry_modified"
        )
        is False
        and source_refresh_trust_candidate_artifact_request.get(
            "provider_quota_consumption_allowed"
        )
        is False
        and source_refresh_trust_candidate_artifact_request.get(
            "delivery_authorized"
        )
        is False
    ):
        raise ValueError(
            "source_refresh_trust_candidate_artifact_request_not_admissible"
        )
except (OSError, TypeError, ValueError):
    source_refresh_trust_candidate_artifact_request = {
        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_artifact_request_verification.v1",
        "status": "blocked",
        "request_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_candidate_artifact_request_unavailable",
        "artifact_request_staged": False,
        "action_required": False,
        "interrupt_operator": False,
        "receipt_persisted": False,
        "producer_contacted": False,
        "transport_delivery_attempted": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_trust_candidate_manual_action = dict(
            source.get("source_refresh_trust_candidate_manual_action") or {}
        )
    else:
        source_refresh_trust_candidate_manual_action = (
            materialize_candidate_manual_action(
                source_refresh_trust_candidate_import,
                source_refresh_trust_candidate_artifact_request,
                receipt_path=(
                    source_refresh_trust_candidate_manual_action_path
                ),
            )
        )
    if not (
        source_refresh_trust_candidate_manual_action.get("status")
        in {"verified", "blocked"}
        and source_refresh_trust_candidate_manual_action.get(
            "transport_delivery_authorized"
        )
        is False
        and source_refresh_trust_candidate_manual_action.get(
            "transport_delivery_attempted"
        )
        is False
        and source_refresh_trust_candidate_manual_action.get(
            "candidate_import_authorized"
        )
        is False
        and source_refresh_trust_candidate_manual_action.get(
            "trust_registry_modified"
        )
        is False
        and source_refresh_trust_candidate_manual_action.get(
            "provider_quota_consumption_allowed"
        )
        is False
        and source_refresh_trust_candidate_manual_action.get(
            "delivery_authorized"
        )
        is False
    ):
        raise ValueError(
            "source_refresh_trust_candidate_manual_action_not_admissible"
        )
except (OSError, TypeError, ValueError):
    source_refresh_trust_candidate_manual_action = {
        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_manual_action_verification.v1",
        "status": "blocked",
        "action_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_candidate_manual_action_unavailable",
        "operator_action_receipt_staged": False,
        "action_required": False,
        "interrupt_operator": False,
        "receipt_persisted": False,
        "producer_contacted": False,
        "transport_delivery_authorized": False,
        "transport_delivery_attempted": False,
        "notification_sent": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_trust_candidate_artifact_notification = dict(
            source.get(
                "source_refresh_trust_candidate_artifact_notification"
            )
            or {}
        )
    else:
        source_refresh_trust_candidate_artifact_notification = (
            run_candidate_artifact_notification(
                source_refresh_trust_candidate_import,
                source_refresh_trust_candidate_artifact_request,
                source_refresh_trust_candidate_manual_action,
                presentation_state_path=(
                    source_refresh_trust_candidate_import_presentation_state_path
                ),
                manual_action_receipt_path=(
                    source_refresh_trust_candidate_manual_action_path
                ),
                receipt_path=(
                    source_refresh_trust_candidate_artifact_notification_path
                ),
                send=False,
            )
        )
    if not (
        source_refresh_trust_candidate_artifact_notification.get("status")
        in {
            "not_required",
            "action_required",
            "deduplicated",
            "completed",
            "blocked",
        }
        and source_refresh_trust_candidate_artifact_notification.get(
            "producer_contacted"
        )
        is False
        and source_refresh_trust_candidate_artifact_notification.get(
            "artifact_transport_authorized"
        )
        is False
        and source_refresh_trust_candidate_artifact_notification.get(
            "artifact_transport_attempted"
        )
        is False
        and source_refresh_trust_candidate_artifact_notification.get(
            "candidate_import_authorized"
        )
        is False
        and source_refresh_trust_candidate_artifact_notification.get(
            "trust_registry_modified"
        )
        is False
        and source_refresh_trust_candidate_artifact_notification.get(
            "provider_quota_consumption_allowed"
        )
        is False
    ):
        raise ValueError(
            "source_refresh_trust_candidate_artifact_notification_not_admissible"
        )
except (OSError, TypeError, ValueError):
    source_refresh_trust_candidate_artifact_notification = {
        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_artifact_notification_verification.v1",
        "status": "blocked",
        "notification_status": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_candidate_artifact_notification_unavailable",
        "action_required": False,
        "interrupt_operator": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "presentation_recorded": False,
        "receipt_persisted": False,
        "producer_contacted": False,
        "artifact_transport_authorized": False,
        "artifact_transport_attempted": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
    }
try:
    if source.get("type") == "runtime_container":
        source_refresh_trust_decision = dict(
            source.get("source_refresh_trust_decision") or {}
        )
        source_refresh_trust_notification = dict(
            source.get("source_refresh_trust_notification") or {}
        )
        source_refresh_trust_enrollment_preview = dict(
            source.get("source_refresh_trust_enrollment_preview") or {}
        )
        source_refresh_trust_enrollment_authorization = dict(
            source.get("source_refresh_trust_enrollment_authorization")
            or {}
        )
        source_refresh_trust_enrollment_execution_readiness = dict(
            source.get(
                "source_refresh_trust_enrollment_execution_readiness"
            )
            or {}
        )
        source_refresh_trust_enrollment_execution = dict(
            source.get("source_refresh_trust_enrollment_execution") or {}
        )
        if source_refresh_trust_decision.get("status") not in {
            "not_required",
            "pending",
            "verified",
        }:
            raise ValueError(
                "runtime_source_refresh_trust_decision_not_admissible"
            )
        if source_refresh_trust_notification.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_trust_notification_not_admissible"
            )
        if source_refresh_trust_enrollment_preview.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_trust_enrollment_preview_not_admissible"
            )
        if source_refresh_trust_enrollment_authorization.get("status") not in {
            "not_required",
            "pending",
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_trust_enrollment_authorization_not_admissible"
            )
        if source_refresh_trust_enrollment_execution_readiness.get(
            "status"
        ) not in {"verified", "blocked"}:
            raise ValueError(
                "runtime_source_refresh_trust_enrollment_execution_readiness_not_admissible"
            )
        if source_refresh_trust_enrollment_execution.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "runtime_source_refresh_trust_enrollment_execution_not_admissible"
            )
    else:
        source_refresh_trust_decision = (
            verify_candidate_review_decision_for_report(
                source_refresh_trust_intake,
                decision_dir=source_refresh_trust_decision_dir,
            )
        )
        if source_refresh_trust_decision.get("status") not in {
            "not_required",
            "pending",
            "verified",
        }:
            raise ValueError(
                "source_refresh_trust_decision_not_admissible"
            )
        source_refresh_trust_notification = run_candidate_notification(
            source_refresh_trust_intake,
            source_refresh_trust_decision,
            presentation_state_path=(
                source_refresh_trust_candidate_presentation_state_path
            ),
            receipt_path=source_refresh_trust_notification_path,
            send=False,
        )
        if not (
            source_refresh_trust_notification.get("status")
            in {
                "not_required",
                "resolved",
                "action_required",
                "deduplicated",
                "completed",
            }
            and source_refresh_trust_notification.get(
                "delivery_authorized"
            )
            is False
            and source_refresh_trust_notification.get(
                "delivery_attempted"
            )
            is False
            and source_refresh_trust_notification.get("sent") is False
            and source_refresh_trust_notification.get(
                "trust_enrollment_authorized"
            )
            is False
            and source_refresh_trust_notification.get(
                "trust_registry_modified"
            )
            is False
        ):
            raise ValueError(
                "source_refresh_trust_notification_not_admissible"
            )
        source_refresh_trust_enrollment_preview = (
            materialize_enrollment_preview(
                source_refresh_trust_intake,
                source_refresh_trust_decision,
                trust_registry_path=(
                    source_refresh_claim_trust_registry_path
                ),
                receipt_path=(
                    source_refresh_trust_enrollment_preview_path
                ),
                verification_path=(
                    source_refresh_trust_enrollment_preview_verification_path
                ),
            )
        )
        if source_refresh_trust_enrollment_preview.get("status") != "verified":
            raise ValueError(
                "source_refresh_trust_enrollment_preview_not_admissible"
            )
        source_refresh_trust_enrollment_authorization = (
            verify_authorization_for_preview(
                source_refresh_trust_enrollment_preview,
                decision_dir=(
                    source_refresh_trust_enrollment_authorization_dir
                ),
            )
        )
        if source_refresh_trust_enrollment_authorization.get("status") not in {
            "not_required",
            "pending",
            "verified",
        }:
            raise ValueError(
                "source_refresh_trust_enrollment_authorization_not_admissible"
            )
        source_refresh_trust_enrollment_execution_readiness = (
            materialize_execution_readiness(
                source_refresh_trust_enrollment_preview,
                source_refresh_trust_enrollment_authorization,
                receipt_path=(
                    source_refresh_trust_enrollment_execution_readiness_path
                ),
                verification_path=(
                    source_refresh_trust_enrollment_execution_readiness_verification_path
                ),
            )
        )
        if source_refresh_trust_enrollment_execution_readiness.get(
            "status"
        ) != "verified":
            raise ValueError(
                "source_refresh_trust_enrollment_execution_readiness_not_admissible"
            )
        source_refresh_trust_enrollment_execution = (
            inspect_execution_for_readiness(
                source_refresh_trust_enrollment_execution_readiness,
                execution_dir=source_refresh_trust_enrollment_execution_dir,
                backup_dir=source_refresh_trust_enrollment_backup_dir,
            )
        )
        if source_refresh_trust_enrollment_execution.get("status") not in {
            "verified",
            "blocked",
        }:
            raise ValueError(
                "source_refresh_trust_enrollment_execution_not_admissible"
            )
    source_refresh_trust_intake = project_candidate_review_decision(
        source_refresh_trust_intake,
        source_refresh_trust_decision,
    )
except (OSError, TypeError, ValueError):
    source_refresh_trust_decision = {
        "status": "blocked",
        "review_state": "blocked",
        "blocking_reason": "source_refresh_trust_decision_unavailable",
        "action_required": False,
        "interrupt_operator": False,
        "identity_verification_asserted": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "decision_confers_trust": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    source_refresh_trust_intake = {
        **source_refresh_trust_intake,
        "status": "blocked",
        "intake_state": "blocked",
        "blocking_reason": "source_refresh_trust_decision_unavailable",
        "next_action": (
            "repair the exact candidate-review decision evidence; do not enroll trust"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    source_refresh_trust_notification = {
        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_notification_verification.v1",
        "status": "blocked",
        "notification_status": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_notification_unavailable",
        "action_required": False,
        "interrupt_operator": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "presentation_recorded": False,
        "receipt_persisted": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
    }
    source_refresh_trust_enrollment_preview = {
        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_preview_verification.v1",
        "status": "blocked",
        "preview_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_enrollment_preview_unavailable",
        "next_action": (
            "repair the exact intake, decision, registry, and preview binding; do not enroll trust"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "preview_staged": False,
        "trust_enrollment_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    source_refresh_trust_enrollment_authorization = {
        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_authorization_verification.v1",
        "status": "blocked",
        "authorization_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_enrollment_authorization_unavailable",
        "next_action": (
            "repair the exact preview and immutable authorization evidence; do not edit trust"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "explicit_authorization_recorded": False,
        "exact_preview_authorized": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    source_refresh_trust_enrollment_execution_readiness = {
        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_execution_readiness_verification.v1",
        "status": "blocked",
        "readiness_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_enrollment_execution_readiness_unavailable",
        "next_action": (
            "repair the exact preview, authorization, and registry bindings; do not edit trust"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "execution_request_staged": False,
        "execution_readiness_verified": False,
        "governed_execution_available": False,
        "trust_registry_modified": False,
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    source_refresh_trust_enrollment_execution = {
        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_execution_verification.v1",
        "status": "blocked",
        "execution_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "source_refresh_trust_enrollment_execution_unavailable",
        "next_action": (
            "repair exact readiness and immutable execution history before any retry"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "authorization_consumed": False,
        "manual_execution_authorized": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "trust_registry_write_attempted": False,
        "trust_registry_modified": False,
        "rollback_available": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
try:
    source_refresh_trust_intake = apply_candidate_presentation_state(
        source_refresh_trust_intake,
        state_path=source_refresh_trust_candidate_presentation_state_path,
    )
except (OSError, TypeError, ValueError):
    source_refresh_trust_intake = {
        **source_refresh_trust_intake,
        "status": "blocked",
        "intake_state": "blocked",
        "blocking_reason": (
            "source_refresh_trust_candidate_presentation_not_admissible"
        ),
        "next_action": (
            "repair the private candidate-presentation ledger; do not edit trust"
        ),
        "action_required": False,
        "interrupt_operator": False,
        "operator_review_required": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
runtime_observation_path = Path(
    str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_RUNTIME_OBSERVATION") or "").strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-observation.json"
).expanduser()
runtime_observation_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_RUNTIME_OBSERVATION_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-observation-verification.json"
).expanduser()
runtime_continuity_path = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_CONTINUITY") or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-continuity.json"
).expanduser()
runtime_continuity_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_CONTINUITY_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-continuity-verification.json"
).expanduser()
runtime_activation_readiness_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_READINESS"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-readiness.json"
).expanduser()
runtime_activation_readiness_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_READINESS_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-readiness-verification.json"
).expanduser()
runtime_activation_authorization_request_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_AUTHORIZATION_REQUEST"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-authorization-request.json"
).expanduser()
runtime_activation_authorization_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_AUTHORIZATION_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-authorization-request-verification.json"
).expanduser()
runtime_activation_authorization_decision_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_DECISION_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-authorization-decisions"
).expanduser()
runtime_activation_execution_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_EXECUTION_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-executions"
).expanduser()
runtime_activation_settlement_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_SETTLEMENT"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-settlement.json"
).expanduser()
runtime_activation_settlement_verification_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_SETTLEMENT_VERIFICATION"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-settlement-verification.json"
).expanduser()
runtime_activation_settlement_presentation_state_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_SETTLEMENT_PRESENTATION_STATE"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-settlement-presentation-state.json"
).expanduser()
runtime_activation_deployment_receipt_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_DEPLOYMENT_RECEIPT"
        )
        or ""
    ).strip()
    or root / "state/release/propertyquarry-local-deployment.v1.json"
).expanduser()
runtime_activation_presentation_state_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_SCHEDULER_ACTIVATION_PRESENTATION_STATE"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/scheduler-activation-presentation-state.json"
).expanduser()
try:
    runtime_observation = materialize_current_runtime_observation_bundle(
        observation_path=runtime_observation_path,
        verification_path=runtime_observation_verification_path,
        project=str(os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"),
    )
except (OSError, TypeError, ValueError, subprocess.TimeoutExpired, SecureFileIOError):
    runtime_observation = {
        "status": "blocked",
        "blocking_reason": "current_runtime_observation_unavailable",
        "action_required": False,
        "interrupt_operator": False,
        "automatic_execution_allowed": False,
        "deployment_or_restart_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
try:
    scheduler_continuity = materialize_current_scheduler_continuity_bundle(
        runtime_observation=runtime_observation,
        scheduler_iteration_witness=dict(source.get("scheduler_witness") or {}),
        source_type=str(source.get("type") or "operator_filesystem"),
        receipt_path=runtime_continuity_path,
        verification_path=runtime_continuity_verification_path,
    )
except (OSError, TypeError, ValueError):
    scheduler_continuity = {
        "status": "blocked",
        "continuity_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "scheduler_continuity_materialization_failed",
        "action_required": False,
        "interrupt_operator": False,
        "progress": {"current_evidence_verified": False},
        "persistent_reevaluation_verified": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "verification_receipt_persisted": False,
    }
try:
    scheduler_activation_readiness = (
        materialize_current_scheduler_activation_readiness_bundle(
            scheduler_continuity=scheduler_continuity,
            project=str(
                os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
            ),
            receipt_path=runtime_activation_readiness_path,
            verification_path=(
                runtime_activation_readiness_verification_path
            ),
        )
    )
except (OSError, TypeError, ValueError):
    scheduler_activation_readiness = {
        "status": "blocked",
        "readiness_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "scheduler_activation_readiness_materialization_failed",
        "action_required": False,
        "interrupt_operator": False,
        "progress": {"current_evidence_verified": False},
        "authorization_required": False,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "verification_receipt_persisted": False,
    }
activation_settlement_presentation_admissible = True
try:
    scheduler_activation_settlement = (
        materialize_current_scheduler_activation_settlement_bundle(
            scheduler_continuity=scheduler_continuity,
            execution_dir=runtime_activation_execution_dir,
            receipt_path=runtime_activation_settlement_path,
            verification_path=(
                runtime_activation_settlement_verification_path
            ),
            project=str(
                os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
            ),
        )
    )
    scheduler_activation_settlement_handoff = (
        project_scheduler_activation_settlement_handoff(
            scheduler_activation_settlement
        )
    )
    if scheduler_activation_settlement_handoff.get("status") != "blocked":
        scheduler_activation_settlement_handoff = (
            apply_operator_presentation_state(
                scheduler_activation_settlement_handoff,
                state_path=(
                    runtime_activation_settlement_presentation_state_path
                ),
            )
        )
        if dict(
            scheduler_activation_settlement_handoff.get("presentation") or {}
        ).get("state_status") == "invalid":
            raise ValueError(
                "scheduler_activation_settlement_presentation_not_admissible"
            )
except (OSError, TypeError, ValueError, subprocess.TimeoutExpired):
    activation_settlement_presentation_admissible = False
    scheduler_activation_settlement = {
        "status": "blocked",
        "settlement_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "scheduler_activation_settlement_unavailable",
        "action_required": False,
        "interrupt_operator": False,
        "actions": [],
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "historical_protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    scheduler_activation_settlement_handoff = dict(
        scheduler_activation_settlement
    )
activation_authorization_presentation_admissible = True
activation_authorization_decision: dict[str, object] | None = None
activation_execution_status: dict[str, object] | None = None
try:
    if (
        scheduler_activation_settlement.get("status") != "verified"
        or scheduler_activation_settlement.get("action_required") is True
    ):
        activation_authorization_handoff = {
            "status": "blocked",
            "handoff_state": "blocked",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "blocking_reason": (
                str(
                    scheduler_activation_settlement.get("blocking_reason")
                    or "scheduler_activation_settlement_required_before_authorization"
                )
            ),
            "next_action": str(
                scheduler_activation_settlement.get("next_action")
                or "settle prior activation execution before requesting new authority"
            ),
            "source_cycle_receipt_sha256": str(
                scheduler_activation_settlement.get(
                    "source_cycle_receipt_sha256"
                )
                or scheduler_activation_readiness.get(
                    "readiness_receipt_sha256"
                )
                or ""
            ),
            "action_required": False,
            "interrupt_operator": False,
            "actions": [],
            "automatic_execution_allowed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "deployment_or_restart_performed": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
        }
    else:
        activation_authorization_handoff = (
            stage_current_scheduler_activation_authorization_handoff(
                activation_readiness=scheduler_activation_readiness,
                request_path=runtime_activation_authorization_request_path,
                verification_path=(
                    runtime_activation_authorization_verification_path
                ),
            )
        )
    if (
        activation_authorization_handoff.get("handoff_state")
        == "authorization_request_staged"
    ):
        activation_authorization_decision = (
            verify_current_scheduler_activation_authorization_decision(
                request_path=runtime_activation_authorization_request_path,
                readiness_path=runtime_activation_readiness_verification_path,
                decision_dir=runtime_activation_authorization_decision_dir,
            )
        )
        decision_status = str(
            activation_authorization_decision.get("status") or "blocked"
        )
        if decision_status == "verified":
            if activation_authorization_decision.get(
                "exact_scope_authorized"
            ) is True:
                activation_execution_status = (
                    inspect_current_scheduler_activation_execution(
                        request_path=runtime_activation_authorization_request_path,
                        readiness_path=runtime_activation_readiness_verification_path,
                        decision_dir=runtime_activation_authorization_decision_dir,
                        execution_dir=runtime_activation_execution_dir,
                        deployment_receipt_path=(
                            runtime_activation_deployment_receipt_path
                        ),
                    )
                )
                execution_state = str(
                    activation_execution_status.get("execution_state")
                    or "blocked"
                )
                if execution_state == "authorized_pending_manual_execution":
                    activation_authorization_handoff.update(
                        {
                            **activation_execution_status,
                            "handoff_state": "activation_execution_pending",
                        }
                    )
                elif (
                    activation_execution_status.get("status") == "verified"
                    and execution_state == "succeeded"
                ):
                    activation_authorization_handoff.update(
                        {
                            "status": "ready",
                            "handoff_state": "activation_execution_succeeded",
                            "blocking_reason": "",
                            "next_action": (
                                "verify fresh scheduler continuity from the runtime"
                            ),
                            "action_required": False,
                            "interrupt_operator": False,
                            "actions": [],
                            "execution_outcome": activation_execution_status,
                        }
                    )
                elif activation_execution_status.get("action_required") is True:
                    activation_authorization_handoff.update(
                        {
                            "status": "action_required",
                            "handoff_state": "activation_execution_recovery_required",
                            "blocking_reason": str(
                                activation_execution_status.get(
                                    "blocking_reason"
                                )
                                or "scheduler_activation_execution_recovery_required"
                            ),
                            "next_action": str(
                                activation_execution_status.get("next_action")
                                or "inspect current runtime and execution receipts; do not replay"
                            ),
                            "source_cycle_receipt_sha256": str(
                                activation_execution_status.get(
                                    "source_cycle_receipt_sha256"
                                )
                                or activation_authorization_handoff.get(
                                    "source_cycle_receipt_sha256"
                                )
                                or ""
                            ),
                            "action_required": True,
                            "interrupt_operator": True,
                            "actions": list(
                                activation_execution_status.get("actions")
                                or []
                            ),
                            "execution_outcome": activation_execution_status,
                            "automatic_execution_allowed": False,
                            "protected_operation_executed": False,
                            "provider_quota_consumption_allowed": False,
                            "delivery_authorized": False,
                        }
                    )
                else:
                    activation_authorization_handoff.update(
                        {
                            "status": "blocked",
                            "handoff_state": "blocked",
                            "blocking_reason": str(
                                activation_execution_status.get(
                                    "blocking_reason"
                                )
                                or "scheduler_activation_execution_not_admissible"
                            ),
                            "action_required": False,
                            "interrupt_operator": False,
                            "actions": [],
                        }
                    )
            else:
                activation_authorization_handoff.update(
                    {
                        "status": "ready",
                        "handoff_state": "authorization_decision_recorded",
                        "blocking_reason": "",
                        "next_action": (
                            "retain the recorded negative decision; do not deploy"
                        ),
                        "action_required": False,
                        "interrupt_operator": False,
                        "actions": [],
                    }
                )
        elif decision_status != "pending":
            activation_authorization_handoff.update(
                {
                    "status": "blocked",
                    "handoff_state": "blocked",
                    "blocking_reason": str(
                        activation_authorization_decision.get(
                            "blocking_reason"
                        )
                        or "scheduler_activation_decision_not_admissible"
                    ),
                    "action_required": False,
                    "interrupt_operator": False,
                    "actions": [],
                }
            )
    if activation_authorization_handoff.get("status") != "blocked":
        activation_authorization_handoff = apply_operator_presentation_state(
            activation_authorization_handoff,
            state_path=runtime_activation_presentation_state_path,
        )
        if dict(
            activation_authorization_handoff.get("presentation") or {}
        ).get("state_status") == "invalid":
            raise ValueError(
                "scheduler_activation_presentation_state_not_admissible"
            )
except (OSError, TypeError, ValueError):
    activation_authorization_presentation_admissible = False
    activation_authorization_handoff = {
        "status": "blocked",
        "handoff_state": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "scheduler_activation_authorization_handoff_failed",
        "source_cycle_receipt_sha256": str(
            scheduler_activation_readiness.get("readiness_receipt_sha256") or ""
        ),
        "action_required": False,
        "interrupt_operator": False,
        "actions": [],
        "progress": {"current_evidence_verified": False},
        "authorization_required": False,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }
review_path = Path(
    str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_REVIEW_PACKET") or "").strip()
    or root / "_completion/propertyquarry_ooda_notification_cycle/runtime-review-packet.json"
).expanduser()
authorization_request_path = Path(
    str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_AUTHORIZATION_REQUEST") or "").strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-authorization-request.json"
).expanduser()
authorization_decision_dir = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_AUTHORIZATION_DECISION_DIR") or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-authorization-decisions"
).expanduser()
configuration_plan_path = Path(
    str(os.getenv("PROPERTYQUARRY_OPERATOR_OODA_CONFIGURATION_PLAN") or "").strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-plan.json"
).expanduser()
configuration_change_preview_dir = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_CONFIGURATION_PREVIEW_DIR") or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-change-previews"
).expanduser()
configuration_manual_action_dir = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_CONFIGURATION_MANUAL_ACTION_DIR")
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-manual-actions"
).expanduser()
configuration_manual_action_receipt_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_CONFIGURATION_MANUAL_ACTION_RECEIPT_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-manual-action-receipts"
).expanduser()
configuration_evidence_refresh_receipt_dir = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_CONFIGURATION_EVIDENCE_REFRESH_RECEIPT_DIR"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-evidence-refresh-receipts"
).expanduser()
configuration_action_presentation_state_path = Path(
    str(
        os.getenv(
            "PROPERTYQUARRY_OPERATOR_OODA_CONFIGURATION_ACTION_PRESENTATION_STATE"
        )
        or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/runtime-configuration-action-presentation-state.json"
).expanduser()
authority_posture_path = Path(
    str(
        os.getenv("PROPERTYQUARRY_OPERATOR_OODA_AUTHORITY_POSTURE") or ""
    ).strip()
    or root
    / "_completion/propertyquarry_ooda_notification_cycle/current-authority-posture.json"
).expanduser()
if source.get("type") != "runtime_container" and summary.get("status") != "blocked":
    try:
        authority_posture = refresh_current_authority_posture(
            posture_path=authority_posture_path,
            packet_path=review_path,
            request_path=authorization_request_path,
            decision_dir=authorization_decision_dir,
            plan_path=configuration_plan_path,
            cycle_receipt_path=Path(
                str(source.get("cycle_receipt") or receipt_path)
            ),
            signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
            project=str(
                os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
            ),
            root=root,
        )
    except (OSError, TypeError, ValueError):
        authority_posture = {
            "status": "blocked",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "blocking_reason": "current_authority_posture_refresh_failed",
            "next_action": "reverify the non-executing authority chain without applying or deploying anything",
            "exact_scope_authorized": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }
else:
    authority_posture = {
        "status": "blocked",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "blocking_reason": "current_authority_posture_source_unavailable",
        "next_action": "obtain a current operator-owned cycle projection before revalidating authority",
        "exact_scope_authorized": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
configuration_action_status = inspect_manual_action_status(
    receipt_dir=configuration_manual_action_receipt_dir,
    handoff_dir=configuration_manual_action_dir,
    refresh_receipt_dir=configuration_evidence_refresh_receipt_dir,
    root=root,
)
configuration_action_presentation_admissible = True
try:
    configuration_action_status = apply_manual_action_presentation_state(
        configuration_action_status,
        state_path=configuration_action_presentation_state_path,
    )
except ValueError:
    configuration_action_presentation_admissible = False
    configuration_action_status = {
        **configuration_action_status,
        "status": "blocked",
        "state": "presentation_evidence_blocked",
        "blocking_reason": "manual_action_presentation_state_not_admissible",
        "next_action": "inspect the private presentation ledger without overwriting it",
        "action_required": True,
        "interrupt_operator": True,
        "current_evidence_verified": False,
    }
review: dict[str, object] | None = None
authorization_handoff: dict[str, object] | None = None
configuration_handoff: dict[str, object] | None = None
configuration_change_preview: dict[str, object] | None = None
configuration_manual_action: dict[str, object] | None = None
presentation_contexts: dict[str, dict[str, object]] = {}
if source.get("type") != "runtime_container" and summary.get("status") != "blocked":
    pending_lanes = {
        str(action.get("lane") or "")
        for action in list(summary.get("actions") or [])
        if isinstance(action, dict)
    }
    if "gold_live_runtime" in pending_lanes:
        try:
            materialize_current_review_packet(
                cycle_receipt_path=Path(
                    str(source.get("cycle_receipt") or receipt_path)
                ),
                signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
                write_path=review_path,
                project=str(
                    os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
                ),
            )
        except (OSError, TypeError, ValueError, SecureFileIOError):
            pass
        try:
            review = verify_current_review_packet(
                packet_path=review_path,
                cycle_receipt_path=Path(str(source.get("cycle_receipt") or receipt_path)),
                signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
                project=str(os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"),
            )
            if review.get("status") == "verified":
                presentation_contexts["gold_live_runtime"] = operator_presentation_context(
                    review
                )
        except (OSError, TypeError, ValueError, SecureFileIOError):
            summary = blocked(
                "runtime_review_presentation_context_not_admissible",
                source_type=str(source.get("type") or "operator_filesystem"),
            )
        if review is not None and review.get("status") == "verified":
            try:
                authorization_handoff = stage_current_authorization_handoff(
                    request_path=authorization_request_path,
                    packet_path=review_path,
                    cycle_receipt_path=Path(
                        str(source.get("cycle_receipt") or receipt_path)
                    ),
                    signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
                    project=str(
                        os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
                    ),
                )
            except (OSError, TypeError, ValueError):
                authorization_handoff = {
                    "status": "blocked",
                    "blocking_reason": "authorization_handoff_staging_failed",
                    "authorization_recorded": False,
                    "execution_authorized": False,
                    "deployment_or_restart_authorized": False,
                    "provider_quota_consumption_allowed": False,
                    "delivery_authorized": False,
                }
        elif review is not None:
            review_reason = str(
                review.get("blocking_reason") or "runtime_review_not_current"
            )
            authorization_handoff = {
                "status": "blocked",
                "blocking_reason": review_reason,
                "authorization_recorded": False,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
            }
            configuration_handoff = {
                "status": "blocked",
                "blocking_reason": review_reason,
                "manual_apply_authorized": False,
                "automatic_apply_allowed": False,
                "apply_performed": False,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
            }
authorization_decision: dict[str, object] | None = None
if (
    authorization_handoff is not None
    and authorization_handoff.get("status") == "ready"
):
    try:
        authorization_decision = verify_current_authorization_decision(
            decision_dir=authorization_decision_dir,
            request_path=authorization_request_path,
            packet_path=review_path,
            cycle_receipt_path=Path(str(source.get("cycle_receipt") or receipt_path)),
            signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
            project=str(
                os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
            ),
        )
    except (OSError, TypeError, ValueError):
        authorization_decision = {
            "status": "blocked",
            "blocking_reason": "authorization_decision_verification_failed",
            "authorization_recorded": False,
            "exact_scope_authorized": False,
            "execution_authorized": False,
        }
    if str((authorization_decision or {}).get("status") or "") in {
        "pending",
        "verified",
    }:
        try:
            configuration_handoff = stage_current_configuration_handoff(
                plan_path=configuration_plan_path,
                request_path=authorization_request_path,
                packet_path=review_path,
                cycle_receipt_path=Path(
                    str(source.get("cycle_receipt") or receipt_path)
                ),
                signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
                decision_dir=authorization_decision_dir,
                project=str(
                    os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
                ),
                root=root,
            )
        except (OSError, TypeError, ValueError):
            configuration_handoff = {
                "status": "blocked",
                "blocking_reason": "configuration_handoff_staging_failed",
                "manual_apply_authorized": False,
                "automatic_apply_allowed": False,
                "apply_performed": False,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
            }

if (
    source.get("type") != "runtime_container"
    and summary.get("status") != "blocked"
    and configuration_handoff is not None
    and configuration_handoff.get("status") == "ready"
):
    try:
        configuration_change_preview = stage_current_configuration_change_preview(
            preview_dir=configuration_change_preview_dir,
            plan_path=configuration_plan_path,
            request_path=authorization_request_path,
            packet_path=review_path,
            cycle_receipt_path=Path(
                str(source.get("cycle_receipt") or receipt_path)
            ),
            signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
            decision_dir=authorization_decision_dir,
            project=str(
                os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
            ),
            root=root,
        )
    except (OSError, TypeError, ValueError):
        configuration_change_preview = {
            "status": "blocked",
            "blocking_reason": "configuration_change_preview_staging_failed",
            "manual_apply_authorized": False,
            "automatic_apply_allowed": False,
            "source_edit_performed": False,
            "execution_authorized": False,
            "deployment_or_restart_authorized": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }

if (
    source.get("type") != "runtime_container"
    and summary.get("status") != "blocked"
    and configuration_change_preview is not None
):
    if configuration_change_preview.get("status") == "ready":
        try:
            configuration_manual_action = stage_current_manual_action_handoff(
                handoff_dir=configuration_manual_action_dir,
                preview_dir=configuration_change_preview_dir,
                plan_path=configuration_plan_path,
                request_path=authorization_request_path,
                packet_path=review_path,
                cycle_receipt_path=Path(
                    str(source.get("cycle_receipt") or receipt_path)
                ),
                signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
                decision_dir=authorization_decision_dir,
                project=str(
                    os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"
                ),
                root=root,
            )
        except (OSError, TypeError, ValueError):
            configuration_manual_action = {
                "status": "blocked",
                "blocking_reason": "configuration_manual_action_staging_failed",
                "manual_apply_authorized": False,
                "manual_rollback_authorized": False,
                "automatic_execution_allowed": False,
                "source_edit_performed": False,
                "deployment_or_restart_authorized": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
            }
    elif configuration_change_preview.get("status") == "not_authorized":
        configuration_manual_action = {
            "status": "not_authorized",
            "blocking_reason": "explicit_exact_scope_approval_required",
            "manual_apply_authorized": False,
            "manual_rollback_authorized": False,
            "automatic_execution_allowed": False,
            "source_edit_performed": False,
            "deployment_or_restart_authorized": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }
    else:
        configuration_manual_action = {
            "status": "blocked",
            "blocking_reason": str(
                configuration_change_preview.get("blocking_reason")
                or "configuration_change_preview_not_ready"
            ),
            "manual_apply_authorized": False,
            "manual_rollback_authorized": False,
            "automatic_execution_allowed": False,
            "source_edit_performed": False,
            "deployment_or_restart_authorized": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        }

if source.get("type") != "runtime_container" and summary.get("status") != "blocked":
    consent_outcomes: dict[str, dict[str, object]] = {}
    if authorization_decision is not None:
        consent_outcomes["gold_live_runtime"] = {
            "decision": authorization_decision,
            "configuration": configuration_handoff or {},
            "configuration_preview": configuration_change_preview or {},
            "manual_action_handoff": configuration_manual_action or {},
        }
    try:
        summary = project_operator_consent(
            summary,
            state_path=consent_state_path,
            presentation_context_by_lane=presentation_contexts,
            consent_outcome_by_lane=consent_outcomes,
        )
        summary = apply_operator_presentation_state(
            summary,
            state_path=presentation_state_path,
            presentation_context_by_lane=presentation_contexts,
        )
    except (TypeError, ValueError):
        summary = blocked(
            "operator_consent_or_presentation_context_not_admissible",
            source_type=str(source.get("type") or "operator_filesystem"),
        )


def print_configuration_handoff(
    handoff: dict[str, object] | None,
) -> None:
    if handoff is not None and handoff.get("status") == "ready":
        planned_change = dict(handoff.get("change") or {})
        print(
            "configuration handoff: READY "
            f"({handoff.get('plan_state') or 'missing'})"
        )
        print(f"configuration path:    {configuration_plan_path}")
        print(
            "configuration plan id: "
            f"{handoff.get('plan_id') or 'missing'}"
        )
        print(
            "configuration status:  "
            f"{handoff.get('plan_status') or 'missing'}"
        )
        print(
            "source target:          "
            f"{planned_change.get('path') or 'missing'} "
            f"{planned_change.get('selector') or 'missing'}"
        )
        print(
            "source expression:      "
            f"{planned_change.get('current_expression') or 'missing'} -> "
            f"{planned_change.get('proposed_expression') or 'missing'}"
        )
        print(
            "manual apply authority: "
            + (
                "exact scope only"
                if handoff.get("manual_apply_authorized") is True
                else "none"
            )
        )
        print("configuration applied: none; automatic apply disabled")
    else:
        print("configuration handoff: unavailable")
        print(
            "configuration signal:  "
            f"{(handoff or {}).get('blocking_reason') or 'not_admissible'}"
        )
        print("manual apply authority: none")
        print("configuration applied: none; automatic apply disabled")


def print_configuration_change_preview(
    handoff: dict[str, object] | None,
) -> None:
    if handoff is not None and handoff.get("status") == "ready":
        preview = dict(handoff.get("preview") or {})
        print(
            "change preview:        READY "
            f"({handoff.get('preview_state') or 'missing'})"
        )
        print(f"change preview path:   {handoff.get('preview_path') or 'missing'}")
        print(f"change preview id:     {handoff.get('preview_id') or 'missing'}")
        print(
            "source hashes:         "
            f"{preview.get('before_sha256') or 'missing'} -> "
            f"{preview.get('after_sha256') or 'missing'}"
        )
        print(
            "rollback diff hash:    "
            f"{preview.get('rollback_unified_diff_sha256') or 'missing'}"
        )
        print("source edit performed: none; preview is non-applying")
    elif handoff is not None and handoff.get("status") == "not_authorized":
        print("change preview:        unavailable; exact approval required")
        print("preview authority:     none")
        print("source edit performed: none")
    else:
        print("change preview:        unavailable")
        print(
            "preview signal:        "
            f"{(handoff or {}).get('blocking_reason') or 'not_admissible'}"
        )
        print("preview authority:     none")
        print("source edit performed: none")


def print_configuration_manual_action(
    handoff: dict[str, object] | None,
) -> None:
    if handoff is not None and handoff.get("status") == "ready":
        target = dict(handoff.get("target") or {})
        print(
            "manual action:        READY "
            f"({handoff.get('handoff_state') or 'missing'})"
        )
        print(f"manual action path:   {handoff.get('handoff_path') or 'missing'}")
        print(f"manual action id:     {handoff.get('handoff_id') or 'missing'}")
        print(
            "exact apply hashes:   "
            f"{target.get('expected_before_sha256') or 'missing'} -> "
            f"{target.get('expected_after_sha256') or 'missing'}"
        )
        print(
            "manual apply command: "
            f"{handoff.get('apply_command') or 'missing'}"
        )
        print(
            "rollback source:      "
            f"{handoff.get('rollback_command_source') or 'missing'}"
        )
        print("automatic execution: none; explicit manual invocation required")
        print("deployment/restart:  excluded")
        print("source edit performed: none")
    elif handoff is not None and handoff.get("status") == "not_authorized":
        print("manual action:        unavailable; exact approval required")
        print("manual action authority: none")
        print("source edit performed: none")
    else:
        print("manual action:        unavailable")
        print(
            "manual action signal: "
            f"{(handoff or {}).get('blocking_reason') or 'not_admissible'}"
        )
        print("manual action authority: none")
        print("source edit performed: none")


def print_configuration_action_status(
    status: dict[str, object],
) -> None:
    state = str(status.get("state") or "unknown")
    if status.get("action_required") is True:
        label = (
            "REQUIRED"
            if status.get("interrupt_operator") is True
            else "PENDING (already presented)"
        )
        print(f"configuration follow-up: {label}")
        print(f"follow-up state:       {state}")
        if status.get("status") == "blocked":
            print(
                "follow-up signal:      "
                f"{status.get('blocking_reason') or 'not_admissible'}"
            )
        if status.get("receipt_id"):
            print(f"action receipt id:     {status.get('receipt_id')}")
            print(f"action receipt path:   {status.get('receipt_path')}")
        if status.get("source_observed_sha256"):
            print(f"current source hash:   {status.get('source_observed_sha256')}")
        print(
            "follow-up next action: "
            f"{status.get('next_action') or 'inspect the verified receipt chain'}"
        )
        if status.get("action_command"):
            print(f"follow-up command:     {status.get('action_command')}")
        if status.get("rollback_command"):
            print(f"exact rollback command: {status.get('rollback_command')}")
        evidence_refresh = dict(status.get("evidence_refresh") or {})
        if evidence_refresh.get("refresh_id"):
            print(
                "evidence refresh id:   "
                f"{evidence_refresh.get('refresh_id')}"
            )
            print(
                "evidence refresh state: "
                f"{evidence_refresh.get('status') or 'unknown'}"
            )
        if status.get("required_operator_id"):
            print(f"required operator id:  {status.get('required_operator_id')}")
        print("automatic execution:   none")
        print("deployment/restart:    not authorized; not performed")
        print("provider/delivery:     not used")
    elif state == "rolled_back" or state.endswith("_evidence_refreshed"):
        print(f"configuration follow-up: VERIFIED ({state})")
        print(f"action receipt id:     {status.get('receipt_id') or 'missing'}")
        print(f"current source hash:   {status.get('source_observed_sha256') or 'missing'}")
        print(f"follow-up next action: {status.get('next_action') or 'none'}")
        if status.get("action_command"):
            print(f"evidence refresh command: {status.get('action_command')}")
        if status.get("rollback_command"):
            print(f"exact rollback command: {status.get('rollback_command')}")
        evidence_refresh = dict(status.get("evidence_refresh") or {})
        if evidence_refresh.get("refresh_id"):
            print(f"evidence refresh id:   {evidence_refresh.get('refresh_id')}")
            print(
                "evidence refresh state: "
                f"{evidence_refresh.get('status') or 'unknown'}"
            )
        print("deployment/restart:    not authorized; not performed")
        print("provider/delivery:     not used")
    else:
        print("configuration follow-up: none")
        print("follow-up signal:      no manual action receipt")

print(f"ooda source:         {source.get('type') or 'unknown'}")
if source.get("container_name"):
    print(f"runtime container:   {source['container_name']}")
safe_tick = dict(source.get("safe_tick") or {})
if safe_tick:
    if (
        safe_tick.get("status") == "ready"
        and safe_tick.get("current_evidence_verified") is True
        and safe_tick.get("receipt_persisted") is True
    ):
        print(
            "ooda fallback tick: VERIFIED EVALUATE-ONLY "
            f"updated={safe_tick.get('updated_at') or 'missing'}"
        )
    else:
        print(
            "ooda fallback tick: BLOCKED "
            f"reason={safe_tick.get('blocking_reason') or 'not_admissible'}"
        )
    print(
        "fallback authority:  no delivery/provider/deployment/restart authority"
    )
if runtime_observation.get("status") == "verified":
    runtime_progress = dict(runtime_observation.get("progress") or {})
    scheduler_witness = dict(source.get("scheduler_witness") or {})
    scheduler_witness_progress = dict(
        scheduler_witness.get("progress") or {}
    )
    scheduler_condition = str(
        runtime_observation.get("scheduler_condition") or "unknown"
    )
    scheduler_container_healthy = (
        runtime_observation.get("scheduler_container_healthy") is True
        and scheduler_condition == "running"
        and runtime_progress.get("scheduler_container_count") == 1
        and runtime_progress.get("running_scheduler_container_count") == 1
        and runtime_progress.get("healthy_scheduler_container_count") == 1
    )
    continuity_progress = dict(scheduler_continuity.get("progress") or {})
    scheduler_running = (
        scheduler_continuity.get("status") == "verified"
        and scheduler_continuity.get("continuity_state") == "active"
        and scheduler_continuity.get("persistent_reevaluation_verified") is True
        and continuity_progress.get("current_evidence_verified") is True
        and continuity_progress.get("receipt_integrity_verified") is True
        and scheduler_continuity.get("verification_receipt_persisted") is True
        and scheduler_continuity.get("execution_authorized") is False
        and scheduler_continuity.get("deployment_or_restart_authorized") is False
        and scheduler_continuity.get("protected_operation_executed") is False
        and scheduler_continuity.get("provider_quota_consumption_allowed") is False
        and scheduler_continuity.get("delivery_authorized") is False
    )
    print(
        "runtime evidence:    VERIFIED "
        f"{str(runtime_observation.get('runtime_condition') or 'unknown').upper()}"
    )
    print(
        "runtime observed:    "
        f"{runtime_observation.get('runtime_observed_at') or 'missing'}; "
        f"current check={runtime_observation.get('current_observed_at') or 'missing'}"
    )
    print(
        "runtime containers:  "
        f"total={runtime_progress.get('container_count', 'missing')} "
        f"running={runtime_progress.get('running_container_count', 'missing')} "
        f"non-running={runtime_progress.get('non_running_container_count', 'missing')}"
    )
    print(
        "ooda scheduler:      VERIFIED "
        f"{scheduler_condition.upper()} "
        f"service={runtime_observation.get('scheduler_service') or 'missing'} "
        f"state={runtime_observation.get('scheduler_state') or 'absent'} "
        f"health={runtime_observation.get('scheduler_health') or 'none'}"
    )
    print(
        "scheduler containers: "
        f"total={runtime_progress.get('scheduler_container_count', 'missing')} "
        f"running={runtime_progress.get('running_scheduler_container_count', 'missing')} "
        f"healthy={runtime_progress.get('healthy_scheduler_container_count', 'missing')}"
    )
    if scheduler_witness.get("status") == "verified":
        print(
            "scheduler witness:  VERIFIED "
            f"{str(scheduler_witness.get('iteration_status') or 'unknown').upper()} "
            f"updated={scheduler_witness.get('iteration_updated_at') or 'missing'}"
        )
        print(
            "cycle witness:      "
            + (
                "VERIFIED CURRENT"
                if scheduler_witness_progress.get("cycle_binding_verified") is True
                else "UNAVAILABLE"
            )
        )
        if scheduler_witness.get("persistent_reevaluation_verified") is not True:
            print(
                "witness signal:    "
                f"{scheduler_witness.get('iteration_blocking_reason') or 'iteration_not_completed'}"
            )
    elif source.get("type") == "runtime_container":
        print(
            "scheduler witness:  UNAVAILABLE "
            f"reason={scheduler_witness.get('blocking_reason') or 'not_admissible'}"
        )
        print("cycle witness:      UNAVAILABLE")
    else:
        print("scheduler witness:  ABSENT (no scheduler container)")
        print("cycle witness:      ABSENT")
    if scheduler_continuity.get("status") == "verified":
        print(
            "continuity evidence: VERIFIED "
            f"{str(scheduler_continuity.get('continuity_state') or 'unknown').upper()} "
            f"updated={scheduler_continuity.get('updated_at') or 'missing'}"
        )
        print(
            "continuity receipt:  "
            f"{scheduler_continuity.get('receipt_path') or runtime_continuity_path} "
            f"sha256={scheduler_continuity.get('continuity_receipt_sha256') or 'missing'}"
        )
    else:
        print(
            "continuity evidence: BLOCKED "
            f"reason={scheduler_continuity.get('blocking_reason') or 'not_admissible'}"
        )
    if scheduler_activation_settlement.get("status") == "verified":
        settlement_state = str(
            scheduler_activation_settlement.get("settlement_state")
            or "unknown"
        )
        settlement_sources = dict(
            scheduler_activation_settlement.get("source_evidence") or {}
        )
        settlement_history = dict(
            settlement_sources.get("execution_history") or {}
        )
        settlement_release = dict(
            settlement_sources.get("scheduler_release_identity") or {}
        )
        print(
            "activation settlement: VERIFIED "
            f"{settlement_state.upper()} "
            f"updated={scheduler_activation_settlement.get('updated_at') or 'missing'}"
        )
        print(
            "settlement receipt:  "
            f"{scheduler_activation_settlement.get('receipt_path') or runtime_activation_settlement_path} "
            f"sha256={scheduler_activation_settlement.get('settlement_receipt_sha256') or 'missing'}"
        )
        print(
            "execution history:  "
            f"{str(settlement_history.get('history_state') or 'unknown').upper()}"
        )
        print(
            "scheduler release:  "
            f"{str(settlement_release.get('status') or 'unknown').upper()} "
            f"commit={settlement_release.get('release_commit_sha') or 'none'} "
            f"image={settlement_release.get('image_digest') or 'none'}"
        )
        if scheduler_activation_settlement.get("action_required") is True:
            settlement_label = (
                "REQUIRED"
                if scheduler_activation_settlement_handoff.get(
                    "interrupt_operator"
                )
                is True
                else "PENDING (already presented)"
            )
            print(f"activation settlement recovery: {settlement_label}")
            print(
                "settlement signal:   "
                f"{scheduler_activation_settlement.get('blocking_reason') or 'inspect_receipts'}"
            )
            print(
                "settlement next:     "
                f"{scheduler_activation_settlement.get('next_action') or 'inspect current runtime; do not replay'}"
            )
            print("activation replay: prohibited")
        elif settlement_state == "settled_success":
            print(
                "activation postcondition: exact governed release is active with a current cycle witness"
            )
        elif settlement_state == "externally_active":
            print(
                "activation attribution: active runtime has no governed execution history"
            )
    else:
        print(
            "activation settlement: BLOCKED "
            f"reason={scheduler_activation_settlement.get('blocking_reason') or 'not_admissible'}"
        )
    if scheduler_activation_readiness.get("status") == "verified":
        activation_source = dict(
            dict(
                scheduler_activation_readiness.get("source_evidence") or {}
            ).get("activation_preflight")
            or {}
        )
        print(
            "activation readiness: VERIFIED "
            f"{str(scheduler_activation_readiness.get('readiness_state') or 'unknown').upper()} "
            f"updated={scheduler_activation_readiness.get('updated_at') or 'missing'}"
        )
        print(
            "activation preflight: "
            f"{str(activation_source.get('status') or 'unknown').upper()} "
            f"observed={activation_source.get('observed_at') or 'missing'}"
        )
        activation_bindings = dict(
            scheduler_activation_readiness.get("source_bindings") or {}
        )
        activation_candidate_drop = dict(
            activation_source.get("candidate_drop") or {}
        )
        candidate_drop_digest = (
            hashlib.sha256(
                scheduler_activation_readiness_canonical(
                    activation_candidate_drop
                )
            ).hexdigest()
            if activation_candidate_drop
            else ""
        )
        candidate_drop_binding_verified = bool(
            activation_candidate_drop
            and activation_candidate_drop.get("source_path_recorded") is False
            and activation_candidate_drop.get("entry_names_recorded") is False
            and activation_bindings.get(
                "candidate_drop_observation_sha256"
            )
            == candidate_drop_digest
            and activation_bindings.get(
                "candidate_drop_source_path_sha256"
            )
            == activation_candidate_drop.get("source_path_sha256")
            and activation_bindings.get("candidate_drop_compose_sha256")
            == activation_candidate_drop.get("compose_sha256")
            and activation_bindings.get(
                "candidate_drop_runtime_image_spec_sha256"
            )
            == activation_candidate_drop.get(
                "runtime_image_spec_sha256"
            )
        )
        release_head_commit = str(
            activation_source.get("release_head_commit_sha") or ""
        )
        release_worktree_digest = str(
            activation_source.get("release_worktree_status_sha256") or ""
        )
        release_tree_identity_verified = (
            activation_source.get("release_tree_identity_verified") is True
            and activation_source.get("changed_paths_recorded") is False
            and activation_bindings.get("release_head_commit_sha")
            == release_head_commit
            and activation_bindings.get("release_worktree_status_sha256")
            == release_worktree_digest
        )
        if release_tree_identity_verified:
            print(
                "activation release:  VERIFIED "
                f"head={release_head_commit} "
                "worktree="
                + (
                    "CLEAN "
                    if activation_source.get("release_worktree_clean") is True
                    else "DIRTY "
                )
                + f"status_sha256={release_worktree_digest} "
                + "status_bytes="
                + str(
                    activation_source.get("release_worktree_status_bytes")
                    if activation_source.get("release_worktree_status_bytes")
                    is not None
                    else "missing"
                )
                + " paths=not-recorded"
            )
        elif activation_source.get("status") != "not_run":
            print("activation release:  UNAVAILABLE (no verified tree identity)")
        if candidate_drop_binding_verified:
            print(
                "activation candidate drop: "
                f"{str(activation_candidate_drop.get('status') or 'unknown').upper()} "
                f"mode={activation_candidate_drop.get('directory_mode') or 'none'} "
                f"uid={activation_candidate_drop.get('directory_uid', 'missing')} "
                f"gid={activation_candidate_drop.get('directory_gid', 'missing')} "
                "scheduler="
                f"{activation_candidate_drop.get('scheduler_runtime_uid', 'missing')}:"
                f"{activation_candidate_drop.get('scheduler_runtime_gid', 'missing')}"
            )
            print(
                "activation candidate bind: "
                "read_only="
                f"{str(activation_candidate_drop.get('compose_read_only_bind_verified') is True).lower()} "
                "create_host_path_disabled="
                f"{str(activation_candidate_drop.get('compose_create_host_path_disabled') is True).lower()} "
                "supplemental_group="
                f"{activation_candidate_drop.get('configured_group_gid') or 'none'} "
                f"path_sha256={activation_candidate_drop.get('source_path_sha256') or 'none'} "
                "paths=not-recorded entries=not-recorded"
            )
            if activation_candidate_drop.get("status") == "blocked":
                print(
                    "activation candidate signal: "
                    f"{activation_candidate_drop.get('blocking_reason') or 'not_admissible'}"
                )
        else:
            print(
                "activation candidate drop: UNAVAILABLE (no verified path-free binding)"
            )
        print(
            "activation signal:   "
            f"{scheduler_activation_readiness.get('blocking_reason') or 'ready_for_authorization'}"
        )
        print(
            "activation receipt:  "
            f"{scheduler_activation_readiness.get('receipt_path') or runtime_activation_readiness_path} "
            f"sha256={scheduler_activation_readiness.get('readiness_receipt_sha256') or 'missing'}"
        )
        activation_handoff_state = str(
            activation_authorization_handoff.get("handoff_state") or "blocked"
        )
        if activation_authorization_handoff.get("status") == "blocked":
            print(
                "activation authorization: BLOCKED "
                f"reason={activation_authorization_handoff.get('blocking_reason') or 'not_admissible'}"
            )
        elif activation_handoff_state == "activation_execution_pending":
            activation_label = (
                "REQUIRED"
                if activation_authorization_handoff.get("interrupt_operator")
                is True
                else "PENDING (already presented)"
            )
            print(
                "activation authorization: RESOLVED "
                "decision=APPROVE_EXACT_SCOPE"
            )
            print(f"activation execution: {activation_label}")
            print(
                "activation decision id: "
                f"{(activation_execution_status or {}).get('decision_id') or 'missing'}"
            )
            print(
                "activation decision expires: "
                f"{(activation_execution_status or {}).get('expires_at') or 'missing'}"
            )
            print(
                "activation execution command: "
                f"{(activation_execution_status or {}).get('execution_command') or 'missing'}"
            )
            print(
                "activation execution guard: one-shot claim plus immediate exact-release preflight"
            )
            print(
                "automatic execution: none; explicit manual invocation required"
            )
        elif activation_handoff_state == "activation_execution_recovery_required":
            activation_label = (
                "REQUIRED"
                if activation_authorization_handoff.get("interrupt_operator")
                is True
                else "PENDING (already presented)"
            )
            execution_outcome = dict(
                activation_authorization_handoff.get("execution_outcome") or {}
            )
            print(f"activation execution recovery: {activation_label}")
            print(
                "activation execution state: "
                f"{execution_outcome.get('execution_state') or 'unknown'}"
            )
            print(
                "activation execution signal: "
                f"{execution_outcome.get('blocking_reason') or 'inspect_receipts'}"
            )
            print(
                "activation claim path: "
                f"{execution_outcome.get('claim_path') or 'missing'}"
            )
            if execution_outcome.get("result_path"):
                print(
                    "activation result path: "
                    f"{execution_outcome.get('result_path')}"
                )
            print("activation replay: prohibited; inspect current runtime first")
        elif activation_handoff_state == "activation_execution_succeeded":
            execution_outcome = dict(
                activation_authorization_handoff.get("execution_outcome") or {}
            )
            runtime_receipt = dict(
                execution_outcome.get("runtime_receipt") or {}
            )
            print("activation authorization: CONSUMED")
            print("activation execution: VERIFIED SUCCEEDED")
            print(
                "activation result path: "
                f"{execution_outcome.get('result_path') or 'missing'}"
            )
            print(
                "activation runtime receipt: "
                f"{runtime_receipt.get('path') or 'missing'} "
                f"sha256={runtime_receipt.get('sha256') or 'missing'}"
            )
        elif activation_handoff_state == "authorization_decision_recorded":
            activation_decision_value = str(
                (activation_authorization_decision or {}).get("decision")
                or "missing"
            )
            print(
                "activation authorization: RESOLVED "
                f"decision={activation_decision_value.upper()}"
            )
            print(
                "activation decision id: "
                f"{(activation_authorization_decision or {}).get('decision_id') or 'missing'}"
            )
            print(
                "activation operation: not performed; automatic execution remains disabled"
            )
        elif activation_handoff_state == "authorization_request_staged":
            activation_label = (
                "REQUIRED"
                if activation_authorization_handoff.get("interrupt_operator")
                is True
                else "PENDING (already presented)"
            )
            activation_scope = dict(
                activation_authorization_handoff.get("scope") or {}
            )
            print(f"activation authorization: {activation_label}")
            print(
                "activation request id: "
                f"{activation_authorization_handoff.get('request_id') or 'missing'}"
            )
            print(
                "activation request path: "
                f"{activation_authorization_handoff.get('request_path') or 'missing'}"
            )
            print(
                "activation request expires: "
                f"{activation_authorization_handoff.get('expires_at') or 'missing'}"
            )
            print(
                "activation exact scope: "
                f"{activation_scope.get('operation') or 'missing'} "
                f"project={activation_scope.get('compose_project') or 'missing'}"
            )
            print(
                "activation decision options: approve_exact_scope | reject | defer"
            )
            print(
                "activation decision template: python3 "
                "scripts/propertyquarry_ooda_scheduler_activation_decision.py "
                "--decision DECISION --decider-id \"$PROPERTYQUARRY_OPERATOR_ID\" "
                f"--request-id {shlex.quote(str(activation_authorization_handoff.get('request_id') or ''))} "
                f"--request-sha256 {shlex.quote(str(activation_authorization_handoff.get('request_sha256') or ''))} "
                f"--request {shlex.quote(str(runtime_activation_authorization_request_path))} "
                f"--readiness {shlex.quote(str(runtime_activation_readiness_verification_path))} "
                f"--decision-dir {shlex.quote(str(runtime_activation_authorization_decision_dir))}"
            )
            print(
                "activation request authority: none until an explicit decision is recorded"
            )
        else:
            print(
                "activation authorization: NOT STAGED "
                f"reason={activation_authorization_handoff.get('blocking_reason') or 'activation_not_ready'}"
            )
    else:
        print(
            "activation readiness: BLOCKED "
            f"reason={scheduler_activation_readiness.get('blocking_reason') or 'not_admissible'}"
        )
    if scheduler_running:
        print("scheduler continuity: persistent reevaluation is running")
    else:
        print(
            "scheduler continuity: persistent reevaluation is not running; "
            "deployment/restart remains separately consent-gated"
        )
    print(
        "runtime receipt:     "
        f"{runtime_observation.get('observation_path') or runtime_observation_path} "
        f"sha256={runtime_observation.get('runtime_observation_sha256') or 'missing'}"
    )
    print(
        "runtime authority:   observation only; deployment/restart not authorized"
    )
    print(
        "continuity authority: observation only; no delivery/provider/deployment/restart authority"
    )
    if (
        (activation_authorization_decision or {}).get("status") == "verified"
        and (activation_authorization_decision or {}).get(
            "exact_scope_authorized"
        )
        is True
    ):
        if (
            (activation_execution_status or {}).get("execution_state")
            == "succeeded"
        ):
            print(
                "activation authority: consumed by one verified deployment; automatic/provider/delivery authority disabled"
            )
        elif (activation_execution_status or {}).get(
            "authorization_consumed"
        ) is True:
            print(
                "activation authority: consumed by one execution attempt; replay prohibited; provider/delivery authority disabled"
            )
        else:
            print(
                "activation authority: exact manual deployment scope approved; not performed; automatic/provider/delivery authority disabled"
            )
    else:
        print(
            "activation authority: preflight/request only; no build/delivery/provider/deployment/restart authority"
        )
else:
    print("runtime evidence:    UNAVAILABLE (not treated as a runtime action)")
    print(
        "runtime signal:      "
        f"{runtime_observation.get('blocking_reason') or 'not_admissible'}"
    )
print(
    "authority evidence:  "
    f"{str(authority_posture.get('status') or 'blocked').upper()} "
    f"updated={authority_posture.get('updated_at') or 'missing'}"
)
print(
    "authority signal:    "
    f"{authority_posture.get('blocking_reason') or 'exact scope verified; execution still disabled'}"
)
if (
    (activation_execution_status or {}).get(
        "protected_operation_executed"
    )
    is True
    or scheduler_activation_settlement.get(
        "historical_protected_operation_executed"
    )
    is True
):
    print(
        "authority execution: activation receipt records a protected deployment attempt; replay disabled"
    )
else:
    print(
        "authority execution: disabled; deployment/restart not authorized; protected operations not performed"
    )
print(f"ooda status:         {summary.get('status') or 'blocked'}")
print(f"status updated:      {summary.get('updated_at') or 'missing'}")
source_evidence = dict(summary.get("source_evidence") or {})
source_evidence_status = str(source_evidence.get("status") or "")
evidence_next_action = str(source_evidence.get("next_action") or "").strip()
if source_evidence_status == "waiting_for_fresh_sources":
    if source_refresh_claims.get("status") == "verified":
        evidence_next_action = str(
            (
                source_refresh_trust_intake.get("next_action")
                if source_refresh_trust_intake.get("status") == "verified"
                and source_refresh_trust_intake.get("request_staged") is True
                else ""
            )
            or source_refresh_claims.get("next_action")
            or "await signed producer claims or refreshed source evidence"
        )
        if source.get("type") == "operator_filesystem_no_runtime_container":
            evidence_next_action += (
                "; this operator read completed one safe host evaluation tick, "
                "while persistent reevaluation requires the governed OODA "
                "scheduler runtime"
            )
    elif source.get("type") == "operator_filesystem_no_runtime_container":
        evidence_next_action = (
            "repair the request-bound source handoff and signed producer claim "
            "lifecycle before inferring pickup; deployment/restart remains "
            "separately consent-gated"
        )
if source_evidence_status == "verified_current":
    print("source evidence:     VERIFIED CURRENT")
elif source_evidence_status == "waiting_for_fresh_sources":
    print("source evidence:     WAITING (not verified clear)")
    for lane in list(source_evidence.get("lanes") or []):
        if not isinstance(lane, dict) or lane.get("status") == "current":
            continue
        print(
            "evidence lane:       "
            f"{lane.get('lane') or 'unknown'} "
            f"{str(lane.get('status') or 'unavailable').upper()} "
            f"reason={lane.get('reason') or 'missing'} "
            f"source={lane.get('source_generated_at') or 'missing'}"
        )
        print(
            "evidence producer:   "
            f"{lane.get('producer_authority') or 'unknown'} "
            f"artifacts={','.join(str(value) for value in list(lane.get('source_artifacts') or [])) or 'missing'}"
        )
    print(
        "evidence next action: "
        f"{evidence_next_action or 'await producer-owned source receipts'}"
    )
    print("source auto-refresh: disabled; external producer authority retained")
    if source.get("type") == "operator_filesystem_no_runtime_container":
        print("safe fallback tick ran for this read; it is not a persistent scheduler")
    print("provider/delivery:   not used")
if source_refresh_request.get("status") == "verified":
    if source_refresh_request.get("request_staged") is True:
        refresh_lanes = [
            str(row.get("lane") or "unknown")
            for row in list(
                source_refresh_request.get("requested_lanes") or []
            )
            if isinstance(row, dict)
        ]
        print(
            "source refresh request: STAGED "
            f"id={source_refresh_request.get('request_id') or 'missing'} "
            f"updated={source_refresh_request.get('updated_at') or 'missing'}"
        )
        print(
            "refresh request lanes: "
            f"{','.join(refresh_lanes) or 'missing'}"
        )
    else:
        print(
            "source refresh request: VERIFIED NOT_REQUIRED "
            f"updated={source_refresh_request.get('updated_at') or 'missing'}"
        )
    print(
        "refresh request receipt: "
        f"{source_refresh_request.get('request_path') or source_refresh_request_path} "
        f"sha256={source_refresh_request.get('request_receipt_sha256') or 'missing'}"
    )
else:
    print(
        "source refresh request: BLOCKED "
        f"reason={source_refresh_request.get('blocking_reason') or 'not_admissible'}"
    )
print(
    "refresh request authority: staged work item only; producer dispatch, "
    "provider access, delivery, deployment, and protected execution not authorized"
)
if source_refresh_handoff.get("status") == "verified":
    if source_refresh_handoff.get("handoff_available") is True:
        handoff_items = [
            row
            for row in list(source_refresh_handoff.get("work_items") or [])
            if isinstance(row, dict)
        ]
        print(
            "producer handoff:   AVAILABLE "
            f"id={source_refresh_handoff.get('handoff_id') or 'missing'} "
            f"items={len(handoff_items)} "
            f"updated={source_refresh_handoff.get('updated_at') or 'missing'}"
        )
        for item in handoff_items:
            print(
                "handoff work item: "
                f"lane={item.get('lane') or 'unknown'} "
                f"id={item.get('work_item_id') or 'missing'} "
                f"artifacts={','.join(str(value) for value in list(item.get('required_artifacts') or [])) or 'missing'}"
            )
    else:
        print(
            "producer handoff:   VERIFIED NOT_REQUIRED "
            f"updated={source_refresh_handoff.get('updated_at') or 'missing'}"
        )
    print(
        "handoff receipt:      "
        f"{source_refresh_handoff.get('handoff_path') or source_refresh_handoff_path} "
        f"sha256={source_refresh_handoff.get('handoff_receipt_sha256') or 'missing'}"
    )
else:
    print(
        "producer handoff:   BLOCKED "
        f"reason={source_refresh_handoff.get('blocking_reason') or 'not_admissible'}"
    )
print(
    "handoff authority:   read-only pickup signal only; it does not establish "
    "a producer claim or grant provider, dispatch, delivery, deployment, or execution authority"
)
if source_refresh_claims.get("status") == "verified":
    claim_state = str(
        source_refresh_claims.get("claim_state") or "unclaimed"
    ).upper()
    claim_progress = dict(source_refresh_claims.get("progress") or {})
    producer_trust = dict(source_refresh_claims.get("trust") or {})
    print(
        "producer claim:     "
        f"{claim_state} "
        f"claims={int(claim_progress.get('claim_count') or 0)}/"
        f"{int(claim_progress.get('expected_claim_count') or 0)} "
        f"updated={source_refresh_claims.get('updated_at') or 'missing'}"
    )
    print(
        "claim settlement:  "
        f"{str(source_refresh_claims.get('settlement_state') or 'unverified').upper()}"
    )
    print(
        "producer trust:    "
        f"{str(producer_trust.get('status') or 'unknown').upper()} "
        f"active={int(producer_trust.get('active_producer_count') or 0)} "
        f"ready={str(producer_trust.get('producer_trust_ready') is True).lower()} "
        f"missing={','.join(str(lane) for lane in list(producer_trust.get('missing_lanes') or [])) or 'none'}"
    )
    for producer_claim in list(source_refresh_claims.get("claims") or []):
        if not isinstance(producer_claim, dict):
            continue
        print(
            "signed claim evidence: "
            f"producer={producer_claim.get('producer_id') or 'unknown'} "
            f"lane={producer_claim.get('lane') or 'unknown'} "
            f"work_item={producer_claim.get('work_item_id') or 'missing'} "
            f"claim_id={producer_claim.get('claim_id') or 'missing'}"
        )
    print(
        "claim receipt:      "
        f"{source_refresh_claims.get('receipt_path') or source_refresh_claims_path} "
        f"sha256={source_refresh_claims.get('lifecycle_receipt_sha256') or 'missing'}"
    )
else:
    print(
        "producer claim:     BLOCKED "
        f"reason={source_refresh_claims.get('blocking_reason') or 'not_admissible'}"
    )
print(
    "claim authority:     observation only; a signed claim never grants provider, "
    "dispatch, delivery, deployment, or execution authority"
)
if source_refresh_trust_intake.get("status") == "verified":
    trust_intake_progress = dict(
        source_refresh_trust_intake.get("progress") or {}
    )
    print(
        "trust intake:      "
        f"{str(source_refresh_trust_intake.get('request_status') or 'unknown').upper()} "
        f"state={source_refresh_trust_intake.get('intake_state') or 'unknown'} "
        f"lanes={','.join(str(lane) for lane in list(source_refresh_trust_intake.get('requested_lanes') or [])) or 'none'} "
        f"candidates={int(trust_intake_progress.get('candidate_evidence_count') or 0)} "
        f"updated={source_refresh_trust_intake.get('updated_at') or 'missing'}"
    )
    print(
        "trust intake receipt: "
        f"{source_refresh_trust_intake.get('receipt_path') or source_refresh_trust_intake_path} "
        f"sha256={source_refresh_trust_intake.get('intake_receipt_sha256') or 'missing'}"
    )
    if int(trust_intake_progress.get("candidate_evidence_count") or 0) == 0:
        print(
            "trust intake interrupt: suppressed; no public-key candidate exists for operator review"
        )
    else:
        for trust_candidate in list(
            source_refresh_trust_intake.get("candidates") or []
        ):
            if not isinstance(trust_candidate, dict):
                continue
            print(
                "trust candidate:   "
                f"producer={trust_candidate.get('producer_id') or 'unknown'} "
                f"key={trust_candidate.get('key_id') or 'unknown'} "
                f"fingerprint=sha256:{trust_candidate.get('public_key_sha256') or 'missing'} "
                f"lanes={','.join(str(lane) for lane in list(trust_candidate.get('lanes') or [])) or 'none'} "
                "proof=verified"
            )
        print(
            "trust candidate review: "
            f"id={source_refresh_trust_intake.get('candidate_review_id') or 'missing'} "
            f"options={','.join(str(option) for option in list(source_refresh_trust_intake.get('review_decision_options') or [])) or 'missing'}"
        )
        trust_decision_status = str(
            source_refresh_trust_decision.get("status") or "blocked"
        )
        print(
            "trust candidate decision: "
            f"{trust_decision_status.upper()} "
            f"state={source_refresh_trust_decision.get('review_state') or 'unknown'}"
        )
        if trust_decision_status == "verified":
            print(
                "trust decision evidence: "
                f"decision={source_refresh_trust_decision.get('decision') or 'missing'} "
                f"decider={source_refresh_trust_decision.get('decider_id') or 'missing'} "
                f"method={source_refresh_trust_decision.get('identity_verification_method') or 'none'} "
                f"reference={source_refresh_trust_decision.get('identity_evidence_ref') or 'none'}"
            )
            print(
                "trust intake interrupt: resolved by exact immutable review decision"
            )
            print(
                "trust decision authority: reversible enrollment preview only="
                f"{str(source_refresh_trust_decision.get('trust_enrollment_preview_authorized') is True).lower()}; "
                "trust enrollment and registry edits remain unauthorized"
            )
        elif source_refresh_trust_intake.get("interrupt_operator") is True:
            print(
                "trust intake interrupt: REQUIRED; novel verified public-key candidate review"
            )
        else:
            print(
                "trust intake interrupt: PENDING already presented; repeat interrupt suppressed"
            )
        if trust_decision_status == "pending":
            if source.get("type") == "runtime_container":
                trust_decision_base = [
                    "/usr/bin/docker",
                    "exec",
                    runtime_container_id,
                    "/usr/local/bin/python",
                    "/app/scripts/propertyquarry_ooda_source_refresh_trust_decision.py",
                    "--decision-dir",
                    runtime_source_refresh_trust_decision_dir,
                    "--verification-write",
                    runtime_source_refresh_trust_decision_verification_path,
                    "--claim-verification",
                    runtime_source_refresh_claims_verification_path,
                    "--trust-registry",
                    runtime_source_refresh_claim_trust_registry_path,
                    "--candidate-dir",
                    runtime_source_refresh_trust_intake_candidate_dir,
                    "--intake-receipt",
                    runtime_source_refresh_trust_intake_path,
                    "--intake-verification",
                    runtime_source_refresh_trust_intake_verification_path,
                ]
            else:
                trust_decision_base = [
                    "python3",
                    "scripts/propertyquarry_ooda_source_refresh_trust_decision.py",
                    "--decision-dir",
                    str(source_refresh_trust_decision_dir),
                ]
            trust_decision_binding = [
                "--decider-id",
                "OPERATOR_ID",
                "--candidate-review-id",
                str(
                    source_refresh_trust_intake.get("candidate_review_id")
                    or ""
                ),
                "--trust-intake-verification-sha256",
                str(
                    source_refresh_trust_intake.get(
                        "verification_receipt_sha256"
                    )
                    or ""
                ),
            ]
            fingerprint_args = [
                value
                for candidate in list(
                    source_refresh_trust_intake.get("candidates") or []
                )
                if isinstance(candidate, dict)
                for value in (
                    "--expected-public-key-sha256",
                    str(candidate.get("public_key_sha256") or ""),
                )
            ]
            print(
                "confirm template:    "
                + shlex.join(
                    trust_decision_base
                    + ["--decision", "confirm_identity_verified"]
                    + trust_decision_binding
                    + fingerprint_args
                    + [
                        "--identity-verification-method",
                        "METHOD",
                        "--identity-evidence-ref",
                        "EVIDENCE_REF",
                    ]
                )
            )
            print(
                "identity methods:    contract_record | in_person | trusted_channel | video_call | voice_call"
            )
            for candidate_decision in ("reject_candidate", "defer"):
                print(
                    f"{candidate_decision} template: "
                    + shlex.join(
                        trust_decision_base
                        + ["--decision", candidate_decision]
                        + trust_decision_binding
                    )
                )
else:
    print(
        "trust intake:      BLOCKED "
        f"reason={source_refresh_trust_intake.get('blocking_reason') or 'not_admissible'}"
    )
candidate_import_state = str(
    source_refresh_trust_candidate_import.get("import_state") or "blocked"
)
if source_refresh_trust_candidate_import.get("status") == "verified":
    print(
        "trust candidate import: "
        f"{candidate_import_state.upper()} "
        f"request={source_refresh_trust_candidate_import.get('request_id') or 'none'} "
        f"updated={source_refresh_trust_candidate_import.get('updated_at') or 'missing'}"
    )
    candidate_import_presentation = dict(
        source_refresh_trust_candidate_import.get("presentation") or {}
    )
    if candidate_import_presentation:
        print(
            "candidate request presentation: "
            f"{str(candidate_import_presentation.get('state') or 'unknown').upper()} "
            f"digest={candidate_import_presentation.get('presentation_digest') or 'none'} "
            f"reminder_after={candidate_import_presentation.get('reminder_after_seconds') or 'none'}s"
        )
    if source_refresh_trust_candidate_artifact_request.get("status") == "verified":
        artifact_request_receipt_path = (
            runtime_source_refresh_trust_candidate_artifact_request_path
            if source.get("type") == "runtime_container"
            else source_refresh_trust_candidate_artifact_request_path
        )
        print(
            "candidate public request: "
            + (
                "STAGED"
                if source_refresh_trust_candidate_artifact_request.get(
                    "artifact_request_staged"
                )
                is True
                else "NOT_REQUIRED"
            )
            + " state="
            + str(
                source_refresh_trust_candidate_artifact_request.get(
                    "request_state"
                )
                or "unknown"
            )
        )
        print(
            "candidate public request receipt: "
            f"{artifact_request_receipt_path} "
            "sha256="
            f"{source_refresh_trust_candidate_artifact_request.get('artifact_request_receipt_sha256') or 'missing'}"
        )
        print(
            "candidate public request contract: path-free public metadata only; "
            "not sent; no private key, import, trust, provider, delivery, or deployment authority"
        )
    else:
        print(
            "candidate public request: BLOCKED reason="
            f"{source_refresh_trust_candidate_artifact_request.get('blocking_reason') or 'not_admissible'}"
        )
    if source_refresh_trust_candidate_manual_action.get("status") == "verified":
        manual_action_receipt_path = (
            runtime_source_refresh_trust_candidate_manual_action_path
            if source.get("type") == "runtime_container"
            else source_refresh_trust_candidate_manual_action_path
        )
        manual_action_label = (
            "INTERRUPT_REQUIRED"
            if source_refresh_trust_candidate_manual_action.get(
                "interrupt_operator"
            )
            is True
            else "PENDING_ALREADY_PRESENTED"
            if source_refresh_trust_candidate_manual_action.get(
                "operator_action_receipt_staged"
            )
            is True
            else "NOT_REQUIRED"
        )
        print(
            "candidate manual action: "
            f"{manual_action_label} state="
            f"{source_refresh_trust_candidate_manual_action.get('action_state') or 'unknown'}"
        )
        print(
            "candidate manual action receipt: "
            f"{manual_action_receipt_path} sha256="
            f"{source_refresh_trust_candidate_manual_action.get('operator_action_receipt_sha256') or 'missing'}"
        )
        print(
            "candidate manual action authority: staged metadata only; no "
            "send, private key, import, trust, provider, delivery, deployment, "
            "or execution authority"
        )
    else:
        print(
            "candidate manual action: BLOCKED reason="
            f"{source_refresh_trust_candidate_manual_action.get('blocking_reason') or 'not_admissible'}"
        )
    artifact_notification_state = str(
        source_refresh_trust_candidate_artifact_notification.get(
            "notification_status"
        )
        or source_refresh_trust_candidate_artifact_notification.get("status")
        or "blocked"
    )
    if (
        source_refresh_trust_candidate_artifact_notification.get("status")
        in {
            "verified",
            "not_required",
            "action_required",
            "deduplicated",
            "completed",
        }
    ):
        artifact_notification_receipt_path = (
            runtime_source_refresh_trust_candidate_artifact_notification_path
            if source.get("type") == "runtime_container"
            else source_refresh_trust_candidate_artifact_notification_path
        )
        artifact_notification_label = {
            "not_required": "NOT_REQUIRED",
            "action_required": "WOULD_SEND",
            "deduplicated": "DEDUPLICATED",
            "completed": "SENT",
        }.get(artifact_notification_state, "UNKNOWN")
        print(
            "candidate artifact alert: "
            f"{artifact_notification_label} state={artifact_notification_state}"
        )
        print(
            "candidate artifact alert receipt: "
            f"{artifact_notification_receipt_path} sha256="
            f"{source_refresh_trust_candidate_artifact_notification.get('notification_receipt_sha256') or 'missing'}"
        )
        print(
            "candidate artifact alert authority: evaluate-only unless the "
            "scheduler has both explicit send enablement and an operator "
            "principal; no artifact transport, import, trust, provider, "
            "deployment, restart, or execution authority"
        )
    else:
        print(
            "candidate artifact alert: BLOCKED reason="
            f"{source_refresh_trust_candidate_artifact_notification.get('blocking_reason') or 'not_admissible'}"
        )
    artifact_request = dict(
        source_refresh_trust_candidate_import.get("artifact_request") or {}
    )
    candidate_source_discovery = dict(
        source_refresh_trust_candidate_import.get("source_discovery") or {}
    )
    if candidate_source_discovery:
        discovery_progress = dict(
            candidate_source_discovery.get("progress") or {}
        )
        print(
            "candidate watch:     "
            f"{str(candidate_source_discovery.get('discovery_state') or 'unknown').upper()} "
            f"directory={candidate_source_discovery.get('incoming_directory') or 'missing'} "
            f"scanned={discovery_progress.get('scanned_file_count', 'missing')} "
            f"valid={discovery_progress.get('valid_candidate_count', 'missing')} "
            f"invalid={discovery_progress.get('invalid_candidate_count', 'missing')}"
        )
        print(
            "candidate watch authority: read-only; no filenames for rejected entries, copy, import, trust, provider, or delivery authority"
        )
    if candidate_import_state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
        "ready_for_manual_import",
    }:
        print(
            "candidate artifact:  producer-owned Ed25519 public candidate JSON; "
            f"schema={artifact_request.get('candidate_schema') or 'missing'}"
        )
        print(
            "candidate lanes:     "
            f"{','.join(str(lane) for lane in list(artifact_request.get('requested_lanes') or [])) or 'missing'}"
        )
        print(
            "candidate destination: "
            f"{artifact_request.get('import_destination_directory') or 'missing'}/<public_key_sha256>.json"
        )
        print(
            "candidate secret rule: public material only; never provide or copy a private key"
        )
        print(
            "candidate drop permissions: directory=0750 source=0640; scheduler group read-only; group write and access for others forbidden"
        )
        if candidate_import_state == "external_artifact_not_admissible":
            print(
                "candidate watch next: "
                f"{source_refresh_trust_candidate_import.get('next_action') or 'repair the private drop folder'}"
            )
        elif source.get("type") == "runtime_container":
            print(
                "candidate import context: scheduler inspection is read-only; use an authorized isolated context with a writable candidate inbox"
            )
        else:
            candidate_import_common = [
                "--claim-verification",
                str(source_refresh_claims_verification_path),
                "--trust-registry",
                str(source_refresh_claim_trust_registry_path),
                "--candidate-dir",
                str(source_refresh_trust_intake_candidate_dir),
                "--intake-receipt",
                str(source_refresh_trust_intake_path),
                "--intake-verification",
                str(source_refresh_trust_intake_verification_path),
                "--import-dir",
                str(source_refresh_trust_candidate_import_dir),
                "--source-discovery-dir",
                str(source_refresh_trust_candidate_source_dir),
            ]
            candidate_import_script = str(
                root
                / "scripts/propertyquarry_ooda_source_refresh_trust_candidate_import.py"
            )
            inspect_source = (
                str(source_refresh_trust_candidate_import.get("source_path") or "")
                or "/ABS/PATH/producer-public-candidate.json"
            )
            print(
                "candidate inspect template: "
                + shlex.join(
                    [
                        sys.executable,
                        candidate_import_script,
                        "--inspect-source",
                        inspect_source,
                    ]
                    + candidate_import_common
                )
            )
            print(
                "candidate import template: "
                + shlex.join(
                    [
                        sys.executable,
                        candidate_import_script,
                        "--import-source",
                        inspect_source,
                        "--expected-request-id",
                        str(
                            source_refresh_trust_candidate_import.get(
                                "request_id"
                            )
                            or ""
                        ),
                        "--expected-semantic-request-sha256",
                        str(
                            source_refresh_trust_candidate_import.get(
                                "semantic_request_sha256"
                            )
                            or ""
                        ),
                        "--expected-intake-verification-sha256",
                        str(
                            source_refresh_trust_candidate_import.get(
                                "intake_verification_sha256"
                            )
                            or ""
                        ),
                        "--expected-source-sha256",
                        str(
                            source_refresh_trust_candidate_import.get(
                                "source_sha256"
                            )
                            or "SOURCE_SHA256_FROM_INSPECT"
                        ),
                        "--expected-public-key-sha256",
                        str(
                            source_refresh_trust_candidate_import.get(
                                "public_key_sha256"
                            )
                            or "PUBLIC_KEY_SHA256_FROM_INSPECT"
                        ),
                        "--operator-id",
                        "OPERATOR_ID",
                        "--import-method",
                        "authenticated_operator_session",
                        "--evidence-ref",
                        "EVIDENCE_REF",
                        "--confirm",
                        "IMPORT_VERIFIED_PUBLIC_KEY_CANDIDATE",
                    ]
                    + candidate_import_common
                )
            )
            print(
                "candidate import guard: inspect first, substitute both reported hashes, then invoke once with operator evidence"
            )
    elif candidate_import_state == "succeeded":
        print(
            "candidate import receipt: immutable success; run one safe tick to stage identity review"
        )
    elif candidate_import_state == "recovery_required":
        print(
            "candidate import recovery: inspect the immutable claim/result and destination; do not replay"
        )
else:
    print(
        "trust candidate import: BLOCKED "
        f"reason={source_refresh_trust_candidate_import.get('blocking_reason') or 'not_admissible'}"
    )
print(
    "candidate import authority: public candidate copy only after explicit exact invocation; no trust, provider, delivery, deployment, or execution authority"
)
trust_notification = dict(source_refresh_trust_notification or {})
trust_notification_state = str(
    trust_notification.get("notification_status")
    or trust_notification.get("status")
    or "blocked"
)
if trust_notification.get("status") in {
    "verified",
    "not_required",
    "resolved",
    "action_required",
    "deduplicated",
    "completed",
}:
    trust_notification_receipt_path = (
        runtime_source_refresh_trust_notification_path
        if source.get("type") == "runtime_container"
        else source_refresh_trust_notification_path
    )
    print(
        "trust candidate alert: "
        f"{trust_notification_state.upper()} "
        f"review={trust_notification.get('candidate_review_id') or 'none'} "
        f"updated={trust_notification.get('updated_at') or trust_notification.get('generated_at') or 'missing'}"
    )
    print(
        "trust alert delivery: "
        f"authorized={str(trust_notification.get('delivery_authorized') is True).lower()} "
        f"attempted={str(trust_notification.get('delivery_attempted') is True).lower()} "
        f"sent={str(trust_notification.get('sent') is True).lower()} "
        f"presentation_recorded={str(trust_notification.get('presentation_recorded') is True).lower()}"
    )
    print(
        "trust alert receipt:  "
        f"{trust_notification.get('receipt_path') or trust_notification_receipt_path} "
        f"sha256={trust_notification.get('notification_receipt_sha256') or 'missing'}"
    )
    print(
        "trust alert authority: evaluate-only for the operator fallback; no "
        "trust enrollment, registry edit, provider, deployment, restart, or "
        "execution authority"
    )
else:
    print(
        "trust candidate alert: BLOCKED "
        f"reason={trust_notification.get('blocking_reason') or 'not_admissible'}"
    )
if source_refresh_trust_enrollment_preview.get("status") == "verified":
    preview_state = str(
        source_refresh_trust_enrollment_preview.get("preview_state")
        or "unknown"
    )
    print(
        "trust enrollment preview: "
        f"{preview_state.upper()} "
        f"id={source_refresh_trust_enrollment_preview.get('preview_id') or 'none'} "
        f"updated={source_refresh_trust_enrollment_preview.get('updated_at') or 'missing'}"
    )
    if preview_state == "preview_staged":
        preview_scope = dict(
            source_refresh_trust_enrollment_preview.get(
                "authorization_scope"
            )
            or {}
        )
        preview_diff = dict(
            source_refresh_trust_enrollment_preview.get("registry_diff")
            or {}
        )
        print(
            "trust registry preview: "
            f"current=sha256:{source_refresh_trust_enrollment_preview.get('current_trust_registry_sha256') or 'missing'} "
            f"proposed=sha256:{source_refresh_trust_enrollment_preview.get('proposed_trust_registry_sha256') or 'missing'} "
            f"additions={len(list(preview_diff.get('additions') or []))}"
        )
        print(
            "trust preview consent: REQUIRED separate exact decision; "
            f"options={' | '.join(str(value) for value in list(preview_scope.get('decision_options') or [])) or 'missing'}"
        )
        print(
            "trust preview authority: preview receipt only; enrollment=false registry_modified=false"
        )
    print(
        "trust preview receipt: "
        f"{source_refresh_trust_enrollment_preview.get('receipt_path') or source_refresh_trust_enrollment_preview_path} "
        f"sha256={source_refresh_trust_enrollment_preview.get('preview_receipt_sha256') or 'missing'}"
    )
else:
    print(
        "trust enrollment preview: BLOCKED "
        f"reason={source_refresh_trust_enrollment_preview.get('blocking_reason') or 'not_admissible'}"
    )
trust_authorization_status = str(
    source_refresh_trust_enrollment_authorization.get("status") or "blocked"
)
trust_authorization_state = str(
    source_refresh_trust_enrollment_authorization.get("authorization_state")
    or "blocked"
)
if trust_authorization_status in {"not_required", "pending", "verified"}:
    print(
        "trust enrollment authorization: "
        f"{trust_authorization_status.upper()} "
        f"state={trust_authorization_state} "
        f"id={source_refresh_trust_enrollment_authorization.get('authorization_id') or 'none'}"
    )
    if trust_authorization_status == "pending":
        if source.get("type") == "runtime_container":
            trust_authorization_base = [
                "/usr/bin/docker",
                "exec",
                runtime_container_id,
                "/usr/local/bin/python",
                "/app/scripts/propertyquarry_ooda_source_refresh_trust_enrollment_authorization.py",
                "--decision-dir",
                runtime_source_refresh_trust_enrollment_authorization_dir,
                "--verification-write",
                runtime_source_refresh_trust_enrollment_authorization_verification_path,
                "--preview-decision-dir",
                runtime_source_refresh_trust_decision_dir,
                "--claim-verification",
                runtime_source_refresh_claims_verification_path,
                "--trust-registry",
                runtime_source_refresh_claim_trust_registry_path,
                "--candidate-dir",
                runtime_source_refresh_trust_intake_candidate_dir,
                "--intake-receipt",
                runtime_source_refresh_trust_intake_path,
                "--intake-verification",
                runtime_source_refresh_trust_intake_verification_path,
                "--preview-receipt",
                runtime_source_refresh_trust_enrollment_preview_path,
                "--preview-verification",
                runtime_source_refresh_trust_enrollment_preview_verification_path,
            ]
        else:
            trust_authorization_base = [
                "python3",
                "scripts/propertyquarry_ooda_source_refresh_trust_enrollment_authorization.py",
                "--decision-dir",
                str(source_refresh_trust_enrollment_authorization_dir),
                "--verification-write",
                str(
                    source_refresh_trust_enrollment_authorization_verification_path
                ),
                "--preview-decision-dir",
                str(source_refresh_trust_decision_dir),
                "--claim-verification",
                str(source_refresh_claims_verification_path),
                "--trust-registry",
                str(source_refresh_claim_trust_registry_path),
                "--candidate-dir",
                str(source_refresh_trust_intake_candidate_dir),
                "--intake-receipt",
                str(source_refresh_trust_intake_path),
                "--intake-verification",
                str(source_refresh_trust_intake_verification_path),
                "--preview-receipt",
                str(source_refresh_trust_enrollment_preview_path),
                "--preview-verification",
                str(source_refresh_trust_enrollment_preview_verification_path),
            ]
        trust_authorization_binding = [
            "--authorizer-id",
            "OPERATOR_ID",
            "--preview-id",
            str(source_refresh_trust_enrollment_preview.get("preview_id") or ""),
            "--preview-verification-sha256",
            str(
                source_refresh_trust_enrollment_preview.get(
                    "verification_receipt_sha256"
                )
                or ""
            ),
            "--current-trust-registry-sha256",
            str(
                source_refresh_trust_enrollment_preview.get(
                    "current_trust_registry_sha256"
                )
                or ""
            ),
            "--proposed-trust-registry-sha256",
            str(
                source_refresh_trust_enrollment_preview.get(
                    "proposed_trust_registry_sha256"
                )
                or ""
            ),
        ]
        print(
            "authorize exact preview template: "
            + shlex.join(
                trust_authorization_base
                + ["--decision", "authorize_exact_preview"]
                + trust_authorization_binding
                + [
                    "--authorization-method",
                    "METHOD",
                    "--authorization-evidence-ref",
                    "EVIDENCE_REF",
                ]
            )
        )
        print(
            "authorization methods: authenticated_operator_session | contract_record | in_person | trusted_channel"
        )
        for trust_authorization_decision in ("reject", "defer"):
            print(
                f"trust preview {trust_authorization_decision} template: "
                + shlex.join(
                    trust_authorization_base
                    + ["--decision", trust_authorization_decision]
                    + trust_authorization_binding
                )
            )
    elif trust_authorization_status == "verified":
        print(
            "trust authorization evidence: "
            f"decision={source_refresh_trust_enrollment_authorization.get('decision') or 'missing'} "
            f"authorizer={source_refresh_trust_enrollment_authorization.get('authorizer_id') or 'missing'} "
            f"reference={source_refresh_trust_enrollment_authorization.get('authorization_evidence_ref') or 'none'}"
        )
        print(
            "trust authorization execution: delegated only to the exact one-shot executor; automatic execution remains disabled"
        )
else:
    print(
        "trust enrollment authorization: BLOCKED "
        f"reason={source_refresh_trust_enrollment_authorization.get('blocking_reason') or 'not_admissible'}"
    )
if source_refresh_trust_enrollment_execution_readiness.get("status") == "verified":
    readiness_state = str(
        source_refresh_trust_enrollment_execution_readiness.get(
            "readiness_state"
        )
        or "unknown"
    )
    print(
        "trust enrollment execution readiness: "
        f"{readiness_state.upper()} "
        f"id={source_refresh_trust_enrollment_execution_readiness.get('readiness_id') or 'none'} "
        f"updated={source_refresh_trust_enrollment_execution_readiness.get('updated_at') or 'missing'}"
    )
    if readiness_state == "ready_for_governed_execution":
        print(
            "trust execution request: STAGED "
            f"authorization={source_refresh_trust_enrollment_execution_readiness.get('authorization_id') or 'missing'} "
            f"current=sha256:{source_refresh_trust_enrollment_execution_readiness.get('current_trust_registry_sha256') or 'missing'} "
            f"proposed=sha256:{source_refresh_trust_enrollment_execution_readiness.get('proposed_trust_registry_sha256') or 'missing'}"
        )
        print(
            "trust execution availability: governed one-shot executor available; execution=false registry_modified=false automatic_execution=false"
        )
    print(
        "trust execution readiness receipt: "
        f"{source_refresh_trust_enrollment_execution_readiness.get('receipt_path') or source_refresh_trust_enrollment_execution_readiness_path} "
        f"sha256={source_refresh_trust_enrollment_execution_readiness.get('readiness_receipt_sha256') or 'missing'}"
    )
else:
    print(
        "trust enrollment execution readiness: BLOCKED "
        f"reason={source_refresh_trust_enrollment_execution_readiness.get('blocking_reason') or 'not_admissible'}"
    )
trust_execution_state = str(
    source_refresh_trust_enrollment_execution.get("execution_state") or "blocked"
)
if source_refresh_trust_enrollment_execution.get("status") == "verified":
    print(
        "trust enrollment execution: "
        f"{trust_execution_state.upper()} "
        f"claim={source_refresh_trust_enrollment_execution.get('claim_id') or 'none'} "
        f"write_attempted={str(source_refresh_trust_enrollment_execution.get('trust_registry_write_attempted') is True).lower()} "
        f"registry_modified={str(source_refresh_trust_enrollment_execution.get('trust_registry_modified') is True).lower()} "
        f"rollback={str(source_refresh_trust_enrollment_execution.get('rollback_available') is True).lower()}"
    )
    if (
        trust_execution_state == "ready_for_manual_execution"
        and source.get("type") != "runtime_container"
    ):
        trust_execution_command = [
            sys.executable,
            str(
                root
                / "scripts/propertyquarry_ooda_source_refresh_trust_enrollment_execution.py"
            ),
            "--execute-exact-readiness",
            "--readiness-id",
            str(
                source_refresh_trust_enrollment_execution.get("readiness_id")
                or ""
            ),
            "--readiness-verification-sha256",
            str(
                source_refresh_trust_enrollment_execution.get(
                    "readiness_verification_sha256"
                )
                or ""
            ),
            "--authorization-id",
            str(
                source_refresh_trust_enrollment_execution.get(
                    "authorization_id"
                )
                or ""
            ),
            "--authorization-receipt-sha256",
            str(
                source_refresh_trust_enrollment_execution.get(
                    "authorization_receipt_sha256"
                )
                or ""
            ),
            "--current-trust-registry-sha256",
            str(
                source_refresh_trust_enrollment_execution.get(
                    "current_trust_registry_sha256"
                )
                or ""
            ),
            "--proposed-trust-registry-sha256",
            str(
                source_refresh_trust_enrollment_execution.get(
                    "proposed_trust_registry_sha256"
                )
                or ""
            ),
            "--executor-id",
            "EXECUTOR_ID",
            "--execution-method",
            "METHOD",
            "--execution-evidence-ref",
            "EVIDENCE_REF",
            "--authorization-dir",
            str(source_refresh_trust_enrollment_authorization_dir),
            "--preview-decision-dir",
            str(source_refresh_trust_decision_dir),
            "--claim-verification",
            str(source_refresh_claims_verification_path),
            "--trust-registry",
            str(source_refresh_claim_trust_registry_path),
            "--candidate-dir",
            str(source_refresh_trust_intake_candidate_dir),
            "--intake-receipt",
            str(source_refresh_trust_intake_path),
            "--intake-verification",
            str(source_refresh_trust_intake_verification_path),
            "--preview-receipt",
            str(source_refresh_trust_enrollment_preview_path),
            "--preview-verification",
            str(source_refresh_trust_enrollment_preview_verification_path),
            "--readiness-receipt",
            str(source_refresh_trust_enrollment_execution_readiness_path),
            "--readiness-verification",
            str(
                source_refresh_trust_enrollment_execution_readiness_verification_path
            ),
            "--execution-dir",
            str(source_refresh_trust_enrollment_execution_dir),
            "--backup-dir",
            str(source_refresh_trust_enrollment_backup_dir),
        ]
        print("trust execution template: " + shlex.join(trust_execution_command))
        print(
            "execution methods: authenticated_operator_session | contract_record | in_person | trusted_channel"
        )
    elif trust_execution_state == "ready_for_manual_execution":
        print(
            "trust execution template: invoke the packaged one-shot executor from an authorized isolated context; the scheduler mount remains read-only"
        )
elif source_refresh_trust_enrollment_execution.get("status") == "blocked":
    print(
        "trust enrollment execution: BLOCKED "
        f"reason={source_refresh_trust_enrollment_execution.get('blocking_reason') or 'not_admissible'}"
    )
print(
    "trust intake authority: public-key metadata only; exact registry replacement is limited to the explicit one-shot executor; "
    "private keys, automatic enrollment, provider access, delivery, and deployment remain disabled"
)
if source_refresh_settlement.get("status") == "verified":
    settlement_progress = dict(
        source_refresh_settlement.get("progress") or {}
    )
    print(
        "producer completion: "
        f"{str(source_refresh_settlement.get('settlement_state') or 'unknown').upper()} "
        f"signed={int(settlement_progress.get('completion_signature_count') or 0)} "
        f"settled={int(settlement_progress.get('settled_count') or 0)}/"
        f"{int(settlement_progress.get('expected_work_item_count') or 0)} "
        f"updated={source_refresh_settlement.get('updated_at') or 'missing'}"
    )
    for settled_item in list(
        source_refresh_settlement.get("settlements") or []
    ):
        if not isinstance(settled_item, dict):
            continue
        completion = dict(settled_item.get("completion") or {})
        print(
            "completion evidence: "
            f"state={settled_item.get('settlement_state') or 'unknown'} "
            f"producer={completion.get('producer_id') or settled_item.get('producer_id') or 'unattributed'} "
            f"lane={settled_item.get('lane') or 'unknown'} "
            f"work_item={settled_item.get('work_item_id') or 'missing'} "
            f"completion_id={completion.get('completion_id') or 'none'}"
        )
    print(
        "settlement receipt:  "
        f"{source_refresh_settlement.get('receipt_path') or source_refresh_settlement_path} "
        f"sha256={source_refresh_settlement.get('settlement_receipt_sha256') or 'missing'}"
    )
else:
    print(
        "producer completion: BLOCKED "
        f"reason={source_refresh_settlement.get('blocking_reason') or 'not_admissible'}"
    )
print(
    "completion authority: evidence and attribution only; no provider, dispatch, "
    "delivery, deployment, or execution authority"
)
retained_context_lanes = list(
    dict(summary.get("presentation") or {}).get("retained_context_lanes") or []
)
if retained_context_lanes:
    print(
        "presentation context: retained prior verified digest while current review is unavailable"
    )
print_configuration_action_status(configuration_action_status)
if summary.get("status") == "blocked":
    print("propertyquarry action: unavailable")
    print(f"action signal:       {summary.get('blocking_reason') or 'not_admissible'}")
    print(f"safe next action:    {summary.get('next_action') or 'regenerate the approved OODA cycle'}")
elif (
    summary.get("action_required") is not True
    and source_refresh_trust_candidate_import.get("status") == "verified"
    and candidate_import_state
    in {"awaiting_external_artifact", "ready_for_manual_import"}
    and source_refresh_trust_candidate_import.get("action_required") is True
):
    if source_refresh_trust_candidate_import.get("interrupt_operator") is True:
        print("propertyquarry action: REQUIRED")
    else:
        print("propertyquarry action: PENDING (already presented)")
    print("action signal:       producer public-key candidate artifact required")
    print(
        "candidate request id: "
        f"{source_refresh_trust_candidate_import.get('request_id') or 'missing'}"
    )
    print(
        "safe next action:    "
        f"{source_refresh_trust_candidate_import.get('next_action') or 'obtain and inspect the public candidate JSON'}"
    )
    print(
        "required artifact:   public candidate JSON matching the displayed request, lanes, registry hash, and proof-of-possession schema"
    )
    print(
        "consent gate:        explicit exact import invocation with operator identity and evidence; subsequent identity review and enrollment remain separate"
    )
    print(
        "operation performed: none; private keys, trust registry, provider access, delivery, deployment, and execution unchanged"
    )
elif (
    source_refresh_trust_enrollment_execution.get("status") == "verified"
    and trust_execution_state == "ready_for_manual_execution"
):
    print("propertyquarry action: REQUIRED")
    print("action signal:       exact trust-registry enrollment ready")
    print(
        "readiness id:        "
        f"{source_refresh_trust_enrollment_execution.get('readiness_id') or 'missing'}"
    )
    print(
        "safe next action:    run the displayed exact one-shot executor or defer; automatic execution is disabled"
    )
    print(
        "authorized operation: replace only the exact current registry bytes with the exact authorized preview"
    )
    print(
        "operation performed: none by this summary; no provider, delivery, deployment, or restart action"
    )
elif (
    source_refresh_trust_enrollment_preview.get("status") == "verified"
    and source_refresh_trust_enrollment_preview.get("preview_state")
    == "preview_staged"
    and source_refresh_trust_enrollment_authorization.get("status")
    == "pending"
):
    print("propertyquarry action: REQUIRED")
    print("action signal:       exact producer trust enrollment preview review")
    print(
        "preview id:          "
        f"{source_refresh_trust_enrollment_preview.get('preview_id') or 'missing'}"
    )
    print(
        "safe next action:    "
        f"{source_refresh_trust_enrollment_preview.get('next_action') or 'review the exact preview'}"
    )
    print(
        "authorized operation: none; use one exact authorization template above"
    )
    print(
        "operation performed: none; trust registry, provider access, delivery, deployment, and execution unchanged"
    )
elif (
    source_refresh_trust_intake.get("status") == "verified"
    and source_refresh_trust_intake.get("action_required") is True
    and summary.get("action_required") is not True
):
    if source_refresh_trust_intake.get("interrupt_operator") is True:
        print("propertyquarry action: REQUIRED")
        print("action signal:       novel producer public-key candidate review")
    else:
        print("propertyquarry action: PENDING (already presented)")
        print("action signal:       candidate review unchanged; no repeated operator interrupt")
    print(
        "candidate review id: "
        f"{source_refresh_trust_intake.get('candidate_review_id') or 'missing'}"
    )
    for trust_candidate in list(
        source_refresh_trust_intake.get("candidates") or []
    ):
        if not isinstance(trust_candidate, dict):
            continue
        print(
            "candidate scope:     "
            f"producer={trust_candidate.get('producer_id') or 'unknown'} "
            f"key={trust_candidate.get('key_id') or 'unknown'} "
            f"fingerprint=sha256:{trust_candidate.get('public_key_sha256') or 'missing'} "
            f"lanes={','.join(str(lane) for lane in list(trust_candidate.get('lanes') or [])) or 'none'}"
        )
    print(
        "decision options:    "
        f"{' | '.join(str(option) for option in list(source_refresh_trust_intake.get('review_decision_options') or [])) or 'missing'}"
    )
    print(
        "safe next action:    "
        f"{source_refresh_trust_intake.get('next_action') or 'verify candidate identity and fingerprint out of band'}"
    )
    print(
        "consent gate:        identity and fingerprint review only; enrollment requires a separate explicit decision"
    )
    print(
        "authorized operation: none; trust registry, provider access, delivery, deployment, and execution unchanged"
    )
elif summary.get("status") == "waiting_for_evidence":
    print("propertyquarry action: deferred (waiting for fresh evidence)")
    if retained_context_lanes:
        print(
            "action signal:       no current approved operator action; "
            "prior presentation identity retained"
        )
    else:
        print(
            "action signal:       no current approved operator action; "
            "presentation ledger left unchanged"
        )
    print(
        "safe next action:    "
        f"{evidence_next_action or summary.get('next_action') or 'await producer-owned source receipts'}"
    )
elif summary.get("action_required") is not True:
    print("propertyquarry action: none")
    resolved_outcomes = list(
        dict(summary.get("consent") or {}).get("resolved_outcomes") or []
    )
    if resolved_outcomes:
        decisions = ", ".join(
            f"{row.get('lane') or 'unknown'}={str(row.get('decision') or 'resolved').upper()}"
            for row in resolved_outcomes
            if isinstance(row, dict)
        )
        print(f"action signal:       exact semantic action resolved ({decisions})")
        print("authorization status: no execution authority; negative outcome retained")
        print("configuration applied: none; automatic apply disabled")
    else:
        print("action signal:       no novel approved operator action")
elif summary.get("interrupt_operator") is not True:
    print("propertyquarry action: PENDING (already presented)")
    print("action signal:       unchanged; no repeated operator interrupt")
    print(f"safe next action:    {summary.get('next_action') or 'await an explicit decision or a verified action change'}")
    if review is not None and review.get("status") == "verified":
        pending_proposal = dict(review.get("configuration_proposal") or {})
        pending_incident = dict(pending_proposal.get("incident") or {})
        print("recovery review:     VERIFIED CURRENT")
        print(f"recovery review hash: {review.get('packet_sha256') or 'missing'}")
        if pending_incident.get("kind") == "edge_connector_unavailable":
            print(
                "edge incident:       "
                f"provider={pending_incident.get('provider') or 'missing'} "
                f"code={pending_incident.get('code') or 'missing'} "
                f"http={pending_incident.get('http_status', 'missing')} "
                f"reason={pending_incident.get('reason') or 'missing'}"
            )
            pending_recovery = dict(
                pending_proposal.get("recovery_preview") or {}
            )
            pending_targets = list(pending_recovery.get("service_targets") or [])
            pending_missing = sum(
                1
                for row in pending_targets
                if isinstance(row, dict) and row.get("observed_state") == "absent"
            )
            print(
                "recovery target:     "
                f"project={pending_recovery.get('compose_project') or 'missing'} "
                f"connector={pending_recovery.get('connector_service') or 'missing'} "
                f"app={pending_recovery.get('application_service') or 'missing'} "
                f"missing={pending_missing}/{len(pending_targets)}"
            )
            print(
                "recovery compose:    "
                f"{','.join(str(value) for value in list(pending_recovery.get('compose_files') or [])) or 'missing'}"
            )
            print(
                "recovery preview:    non-executing; no command or authorization recorded"
            )
        print(
            "recovery authority:    review only; no runtime, provider, delivery, or configuration mutation authorized"
        )
    if authorization_handoff is not None and authorization_handoff.get("status") == "ready":
        authorization_scope = dict(authorization_handoff.get("scope") or {})
        print(
            "authorization handoff: READY "
            f"({authorization_handoff.get('request_state') or 'missing'})"
        )
        print(
            "authorization id:      "
            f"{authorization_handoff.get('request_id') or 'missing'}"
        )
        print(
            "authorization expires: "
            f"{authorization_handoff.get('expires_at') or 'missing'}"
        )
        print(
            "requested scope:       "
            f"{authorization_scope.get('operation') or 'missing'} "
            f"{authorization_scope.get('change_id') or 'missing'} "
            f"{authorization_scope.get('current_value') or 'missing'} -> "
            f"{authorization_scope.get('proposed_value') or 'missing'}"
        )
        decision_status = str((authorization_decision or {}).get("status") or "blocked")
        if decision_status == "verified":
            decision_value = str(
                (authorization_decision or {}).get("decision") or "missing"
            )
            print(f"authorization decision: VERIFIED ({decision_value})")
            if (authorization_decision or {}).get("exact_scope_authorized") is True:
                print("authorization status:  APPROVED exact configuration scope only")
                print(
                    "authorized operation:  runtime_configuration_change; deployment/restart excluded"
                )
            else:
                print(
                    "authorization status:  "
                    f"{decision_value.upper()}; exact scope not authorized"
                )
                print("authorized operation:  none")
            print("operation performed:    none; automatic execution disabled")
        elif decision_status == "pending":
            print("authorization decision: PENDING operator input")
            print("decision options:       approve_exact_scope | reject | defer")
            print("authorization status:  PENDING explicit decision; no authority recorded")
            print("authorized operation:  none")
            print(
                "decision template:      python3 "
                "scripts/propertyquarry_ooda_authorization_decision.py "
                "--decision DECISION --decider-id \"$PROPERTYQUARRY_OPERATOR_ID\" "
                f"--request-id {shlex.quote(str(authorization_handoff.get('request_id') or ''))} "
                f"--request-sha256 {shlex.quote(str(authorization_handoff.get('request_sha256') or ''))}"
            )
        else:
            print("authorization decision: not verified")
            print(
                "decision signal:        "
                f"{(authorization_decision or {}).get('blocking_reason') or 'not_admissible'}"
            )
            print("authorization status:  BLOCKED; no authority recorded")
            print("authorized operation:  none")
    elif (
        (authorization_handoff or {}).get("blocking_reason")
        == "authorization_handoff_not_applicable_to_edge_recovery_review"
    ):
        print("authorization handoff: NOT APPLICABLE")
        print(
            "authorization signal:  edge recovery is restart/deploy class, not a probe-configuration change"
        )
        print(
            "authorization status:  no deployment/restart authority staged"
        )
        print("authorized operation:  none")
    else:
        print("authorization handoff: unavailable")
        print(
            "authorization signal:  "
            f"{(authorization_handoff or {}).get('blocking_reason') or 'not_admissible'}"
        )
        print("authorization status:  BLOCKED; no authority recorded")
        print("authorized operation:  none")
    print_configuration_handoff(configuration_handoff)
    print_configuration_change_preview(configuration_change_preview)
    print_configuration_manual_action(configuration_manual_action)
else:
    print("propertyquarry action: REQUIRED")
    for index, action in enumerate(list(summary.get("actions") or []), start=1):
        protected = ", ".join(
            str(value) for value in list(action.get("protected_operations") or [])
        )
        print(f"action {index} lane:      {action.get('lane') or 'missing'}")
        print(f"action {index} reason:    {action.get('reason') or 'missing'}")
        print(f"source generated:    {action.get('source_generated_at') or 'missing'}")
        print(f"safe next action:    {action.get('safe_next_action') or 'missing'}")
        if action.get("manual_apply_authorized") is True:
            print(
                "consent gate:        exact configuration scope approved; manual apply only; "
                "deployment/restart excluded; protected="
                + (protected or "missing")
            )
        else:
            print(
                "consent gate:        required; automatic execution disabled; protected="
                + (protected or "missing")
            )
    print("provider quota:      disabled")
    if source.get("type") == "runtime_container":
        review = blocked(
            "runtime_review_packet_requires_current_host_projection",
            source_type="runtime_container",
        )
    elif review is None:
        review = verify_current_review_packet(
            packet_path=review_path,
            cycle_receipt_path=Path(str(source.get("cycle_receipt") or receipt_path)),
            signal_dir=Path(str(source.get("signal_dir") or signal_dir)),
            project=str(os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or "property"),
        )
    if review.get("status") == "verified":
        print("review packet:       VERIFIED")
        print(f"review packet path:  {review_path}")
        print(f"review packet hash:  {review.get('packet_sha256') or 'missing'}")
        proposal = dict(review.get("configuration_proposal") or {})
        if proposal:
            incident = dict(proposal.get("incident") or {})
            if incident.get("kind") == "edge_connector_unavailable":
                print(
                    "edge incident:       "
                    f"provider={incident.get('provider') or 'missing'} "
                    f"code={incident.get('code') or 'missing'} "
                    f"http={incident.get('http_status', 'missing')} "
                    f"reason={incident.get('reason') or 'missing'}"
                )
                recovery_preview = dict(proposal.get("recovery_preview") or {})
                recovery_targets = list(
                    recovery_preview.get("service_targets") or []
                )
                recovery_missing = sum(
                    1
                    for row in recovery_targets
                    if isinstance(row, dict) and row.get("observed_state") == "absent"
                )
                print(
                    "recovery target:     "
                    f"project={recovery_preview.get('compose_project') or 'missing'} "
                    f"connector={recovery_preview.get('connector_service') or 'missing'} "
                    f"app={recovery_preview.get('application_service') or 'missing'} "
                    f"missing={recovery_missing}/{len(recovery_targets)}"
                )
                print(
                    "recovery compose:    "
                    f"{','.join(str(value) for value in list(recovery_preview.get('compose_files') or [])) or 'missing'}"
                )
                print(
                    "recovery preview:    non-executing; no command or authorization recorded"
                )
            print(
                "probe target:        "
                f"{proposal.get('current_probe_origin') or 'missing'} -> "
                f"{proposal.get('proposed_probe_origin') or 'missing'}"
            )
            print(
                "public host binding: "
                f"{proposal.get('current_probe_host') or 'missing'} -> "
                f"{proposal.get('proposed_probe_host') or 'missing'}"
            )
            print(
                "dedicated runtime:   "
                f"{proposal.get('compose_project') or 'missing'}; "
                f"missing services={proposal.get('missing_service_count', 'missing')}"
            )
            print(
                "proposal authority:  review only; explicit authorization required; execution disabled"
            )
        authorization = authorization_handoff or {
            "status": "blocked",
            "blocking_reason": "authorization_handoff_not_staged",
        }
        if authorization.get("status") == "ready":
            authorization_scope = dict(authorization.get("scope") or {})
            print(
                "authorization request: READY "
                f"({authorization.get('request_state') or 'missing'})"
            )
            print(f"authorization path:    {authorization_request_path}")
            print(
                "authorization id:      "
                f"{authorization.get('request_id') or 'missing'}"
            )
            print(
                "authorization expires: "
                f"{authorization.get('expires_at') or 'missing'}"
            )
            print(
                "requested scope:       "
                f"{authorization_scope.get('operation') or 'missing'} "
                f"{authorization_scope.get('change_id') or 'missing'} "
                f"{authorization_scope.get('current_value') or 'missing'} -> "
                f"{authorization_scope.get('proposed_value') or 'missing'}"
            )
            print(
                "request authority:      none until an explicit decision is recorded"
            )
            authorization_decision = authorization_decision or {
                "status": "blocked",
                "blocking_reason": "authorization_decision_not_checked",
            }
            decision_status = str(authorization_decision.get("status") or "blocked")
            if decision_status == "verified":
                decision_value = str(
                    authorization_decision.get("decision") or "missing"
                )
                exact_scope_authorized = (
                    authorization_decision.get("exact_scope_authorized") is True
                )
                print("authorization decision: VERIFIED")
                print(
                    "decision path:         "
                    f"{authorization_decision.get('decision_path') or 'missing'}"
                )
                print(f"decision value:        {decision_value}")
                print(
                    "decision recorded by:  "
                    f"{authorization_decision.get('decider_id') or 'missing'}"
                )
                if exact_scope_authorized:
                    print(
                        "authorization status:  APPROVED exact configuration scope only"
                    )
                    print(
                        "authorized operation:  runtime_configuration_change; deployment/restart excluded"
                    )
                else:
                    print(
                        "authorization status:  "
                        f"{decision_value.upper()}; exact scope not authorized"
                    )
                    print("authorized operation:  none")
                print("operation performed:    none; automatic execution disabled")
            elif decision_status == "pending":
                print("authorization decision: PENDING operator input")
                print(
                    "decision options:       approve_exact_scope | reject | defer"
                )
                print(
                    "authorization status:  PENDING explicit decision; no authority recorded"
                )
                print("authorized operation:  none")
                print(
                    "decision template:      python3 "
                    "scripts/propertyquarry_ooda_authorization_decision.py "
                    "--decision DECISION --decider-id \"$PROPERTYQUARRY_OPERATOR_ID\" "
                    f"--request-id {shlex.quote(str(authorization.get('request_id') or ''))} "
                    f"--request-sha256 {shlex.quote(str(authorization.get('request_sha256') or ''))}"
                )
            else:
                print("authorization decision: not verified")
                print(
                    "decision signal:        "
                    f"{authorization_decision.get('blocking_reason') or 'not_admissible'}"
                )
                print("authorization status:  BLOCKED; no authority recorded")
                print("authorized operation:  none")
            print_configuration_handoff(configuration_handoff)
            print_configuration_change_preview(configuration_change_preview)
            print_configuration_manual_action(configuration_manual_action)
        elif (
            authorization.get("blocking_reason")
            == "authorization_handoff_not_applicable_to_edge_recovery_review"
        ):
            print("authorization request: NOT APPLICABLE")
            print(
                "authorization signal:  edge recovery is restart/deploy class, not a probe-configuration change"
            )
            print(
                "authorization next:    use a separate exact deployment/restart authorization only after clean release evidence"
            )
            print(
                "recovery authority:    none staged; no runtime, provider, delivery, or configuration mutation performed"
            )
        else:
            print("authorization request: not verified")
            print(
                "authorization signal:  "
                f"{authorization.get('blocking_reason') or 'not_admissible'}"
            )
            print(
                "authorization next:    inspect and repair the private request or "
                "refresh the current runtime evidence"
            )
    else:
        print("review packet:       not verified")
        print(f"review signal:       {review.get('blocking_reason') or 'not_admissible'}")
        if source.get("type") == "runtime_container":
            print(
                "review next action:  stage a current operator-owned runtime projection before materializing a review packet"
            )
        else:
            print(
                "review next action:  python3 scripts/propertyquarry_ooda_runtime_review.py "
                f"--cycle-receipt {shlex.quote(str(source.get('cycle_receipt') or receipt_path))} "
                f"--signal-dir {shlex.quote(str(source.get('signal_dir') or signal_dir))} "
                f"--packet {shlex.quote(str(review_path))}"
            )

sys.stdout.flush()
if (
    source_refresh_trust_candidate_import.get("status") == "verified"
    and source_refresh_trust_candidate_import.get("action_required") is True
    and source_refresh_trust_candidate_import.get("interrupt_operator") is True
    and candidate_import_state
    in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
        "ready_for_manual_import",
    }
):
    expected_candidate_import_presentation_digest = str(
        dict(
            source_refresh_trust_candidate_import.get("presentation") or {}
        ).get("presentation_digest")
        or ""
    )
    try:
        if source.get("type") == "runtime_container":
            recorded_candidate_import_projection = (
                runtime_source_refresh_trust_candidate_import_projection(
                    runtime_container_id,
                    record_presentation=True,
                    expected_presentation_digest=(
                        expected_candidate_import_presentation_digest
                    ),
                )
            )
            candidate_import_presentation_receipt = dict(
                recorded_candidate_import_projection.get(
                    "presentation_receipt"
                )
                or {}
            )
        else:
            candidate_import_presentation_receipt = (
                record_candidate_import_presentation(
                    source_refresh_trust_candidate_import,
                    state_path=(
                        source_refresh_trust_candidate_import_presentation_state_path
                    ),
                    expected_presentation_digest=(
                        expected_candidate_import_presentation_digest
                    ),
                )
            )
        if not (
            candidate_import_presentation_receipt.get("status")
            == "recorded"
            and candidate_import_presentation_receipt.get(
                "presentation_digest"
            )
            == expected_candidate_import_presentation_digest
            and candidate_import_presentation_receipt.get(
                "delivery_state_updated"
            )
            is False
            and candidate_import_presentation_receipt.get(
                "provider_quota_consumed"
            )
            is False
            and candidate_import_presentation_receipt.get(
                "trust_registry_modified"
            )
            is False
            and candidate_import_presentation_receipt.get(
                "protected_operation_executed"
            )
            is False
        ):
            raise ValueError(
                "trust_candidate_import_presentation_receipt_not_admissible"
            )
        if source.get("type") == "runtime_container":
            refreshed_candidate_manual_action = (
                runtime_source_refresh_trust_candidate_manual_action_projection(
                    runtime_container_id,
                    materialize=True,
                )
            )
        else:
            refreshed_candidate_import = (
                apply_candidate_import_presentation_state(
                    source_refresh_trust_candidate_import,
                    state_path=(
                        source_refresh_trust_candidate_import_presentation_state_path
                    ),
                )
            )
            refreshed_candidate_manual_action = (
                materialize_candidate_manual_action(
                    refreshed_candidate_import,
                    source_refresh_trust_candidate_artifact_request,
                    receipt_path=(
                        source_refresh_trust_candidate_manual_action_path
                    ),
                )
            )
        if not (
            refreshed_candidate_manual_action.get("status") == "verified"
            and refreshed_candidate_manual_action.get(
                "operator_action_receipt_staged"
            )
            is True
            and refreshed_candidate_manual_action.get("action_required")
            is True
            and refreshed_candidate_manual_action.get("interrupt_operator")
            is False
            and refreshed_candidate_manual_action.get("receipt_persisted")
            is True
            and refreshed_candidate_manual_action.get(
                "transport_delivery_attempted"
            )
            is False
            and refreshed_candidate_manual_action.get(
                "candidate_import_authorized"
            )
            is False
            and refreshed_candidate_manual_action.get(
                "trust_registry_modified"
            )
            is False
        ):
            raise ValueError(
                "trust_candidate_manual_action_refresh_not_admissible"
            )
        if source.get("type") == "runtime_container":
            refreshed_candidate_artifact_notification = (
                runtime_source_refresh_trust_candidate_artifact_notification_projection(
                    runtime_container_id,
                    evaluate=True,
                )
            )
            refreshed_artifact_notification_state = str(
                refreshed_candidate_artifact_notification.get(
                    "notification_status"
                )
                or ""
            )
        else:
            refreshed_candidate_artifact_notification = (
                run_candidate_artifact_notification(
                    refreshed_candidate_import,
                    source_refresh_trust_candidate_artifact_request,
                    refreshed_candidate_manual_action,
                    presentation_state_path=(
                        source_refresh_trust_candidate_import_presentation_state_path
                    ),
                    manual_action_receipt_path=(
                        source_refresh_trust_candidate_manual_action_path
                    ),
                    receipt_path=(
                        source_refresh_trust_candidate_artifact_notification_path
                    ),
                    send=False,
                )
            )
            refreshed_artifact_notification_state = str(
                refreshed_candidate_artifact_notification.get("status") or ""
            )
        if not (
            refreshed_artifact_notification_state == "deduplicated"
            and refreshed_candidate_artifact_notification.get(
                "action_required"
            )
            is True
            and refreshed_candidate_artifact_notification.get(
                "interrupt_operator"
            )
            is False
            and refreshed_candidate_artifact_notification.get(
                "delivery_authorized"
            )
            is False
            and refreshed_candidate_artifact_notification.get(
                "delivery_attempted"
            )
            is False
            and refreshed_candidate_artifact_notification.get("sent")
            is False
            and refreshed_candidate_artifact_notification.get(
                "receipt_persisted"
            )
            is True
        ):
            raise ValueError(
                "trust_candidate_artifact_notification_refresh_not_admissible"
            )
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        candidate_import_presentation_reason = "record_not_verified"
        if "candidate_import_presentation_receipt" in locals():
            candidate_import_presentation_reason = str(
                candidate_import_presentation_receipt.get(
                    "blocking_reason"
                )
                or candidate_import_presentation_reason
            )
        print(
            "candidate import presentation ledger: BLOCKED "
            f"({candidate_import_presentation_reason})"
        )
if (
    source_refresh_trust_intake.get("status") == "verified"
    and source_refresh_trust_intake.get("action_required") is True
    and source_refresh_trust_intake.get("interrupt_operator") is True
):
    trust_candidate_presentation_receipt = record_candidate_presentation(
        source_refresh_trust_intake,
        state_path=source_refresh_trust_candidate_presentation_state_path,
        expected_candidate_review_id=str(
            source_refresh_trust_intake.get("candidate_review_id") or ""
        ),
    )
    if trust_candidate_presentation_receipt.get("status") == "blocked":
        print(
            "trust candidate presentation ledger: BLOCKED "
            f"({trust_candidate_presentation_receipt.get('blocking_reason') or 'not_admissible'})"
        )
    elif source.get("type") == "runtime_container":
        try:
            refreshed_runtime_trust_notification = (
                runtime_source_refresh_trust_notification_projection(
                    runtime_container_id,
                    record_presentation=True,
                    expected_candidate_review_id=str(
                        source_refresh_trust_intake.get(
                            "candidate_review_id"
                        )
                        or ""
                    ),
                )
            )
            if not (
                refreshed_runtime_trust_notification.get("status")
                == "verified"
                and refreshed_runtime_trust_notification.get(
                    "notification_status"
                )
                == "deduplicated"
                and refreshed_runtime_trust_notification.get(
                    "interrupt_operator"
                )
                is False
                and refreshed_runtime_trust_notification.get(
                    "presentation_transition_recorded"
                )
                is True
            ):
                raise ValueError(
                    "runtime_trust_candidate_notification_refresh_not_admissible"
                )
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            subprocess.TimeoutExpired,
        ):
            print(
                "runtime trust candidate notification receipt: BLOCKED "
                "(post_presentation_refresh_not_verified)"
            )
    elif source.get("type") != "runtime_container":
        try:
            refreshed_source_refresh_trust_notification = (
                run_candidate_notification(
                    source_refresh_trust_intake,
                    source_refresh_trust_decision,
                    presentation_state_path=(
                        source_refresh_trust_candidate_presentation_state_path
                    ),
                    receipt_path=source_refresh_trust_notification_path,
                    send=False,
                )
            )
            if not (
                refreshed_source_refresh_trust_notification.get("status")
                == "deduplicated"
                and refreshed_source_refresh_trust_notification.get(
                    "action_required"
                )
                is True
                and refreshed_source_refresh_trust_notification.get(
                    "operator_review_required"
                )
                is True
                and refreshed_source_refresh_trust_notification.get(
                    "interrupt_operator"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "would_send"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "delivery_authorized"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "delivery_attempted"
                )
                is False
                and refreshed_source_refresh_trust_notification.get("sent")
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "receipt_persisted"
                )
                is True
                and refreshed_source_refresh_trust_notification.get(
                    "trust_enrollment_authorized"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "trust_registry_modified"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "producer_dispatch_authorized"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "producer_refresh_authorized"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "automatic_source_refresh_allowed"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "automatic_execution_allowed"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "execution_authorized"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "deployment_or_restart_authorized"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "protected_operation_executed"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "provider_quota_consumption_allowed"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "private_key_material_requested"
                )
                is False
                and refreshed_source_refresh_trust_notification.get(
                    "private_key_material_recorded"
                )
                is False
            ):
                raise ValueError(
                    "trust_candidate_notification_refresh_not_admissible"
                )
        except (OSError, TypeError, ValueError):
            print(
                "trust candidate notification receipt: BLOCKED "
                "(post_presentation_refresh_not_verified)"
            )
if (
    activation_settlement_presentation_admissible
    and scheduler_activation_settlement_handoff.get("status") != "blocked"
):
    settlement_presentation_receipt = record_operator_presentation(
        scheduler_activation_settlement_handoff,
        state_path=runtime_activation_settlement_presentation_state_path,
        expected_source_cycle_receipt_sha256=str(
            scheduler_activation_settlement_handoff.get(
                "source_cycle_receipt_sha256"
            )
            or ""
        ),
    )
    if settlement_presentation_receipt.get("status") == "blocked":
        print(
            "activation settlement presentation ledger: BLOCKED "
            f"({settlement_presentation_receipt.get('blocking_reason') or 'not_admissible'})"
        )
if (
    activation_authorization_presentation_admissible
    and activation_authorization_handoff.get("status") != "blocked"
):
    activation_presentation_receipt = record_operator_presentation(
        activation_authorization_handoff,
        state_path=runtime_activation_presentation_state_path,
        expected_source_cycle_receipt_sha256=str(
            activation_authorization_handoff.get(
                "source_cycle_receipt_sha256"
            )
            or ""
        ),
    )
    if activation_presentation_receipt.get("status") == "blocked":
        print(
            "activation presentation ledger: BLOCKED "
            f"({activation_presentation_receipt.get('blocking_reason') or 'not_admissible'})"
        )
if configuration_action_presentation_admissible:
    configuration_action_presentation_receipt = record_manual_action_presentation(
        configuration_action_status,
        state_path=configuration_action_presentation_state_path,
        expected_semantic_digest=str(
            configuration_action_status.get("semantic_digest") or ""
        ),
    )
    if configuration_action_presentation_receipt.get("status") == "blocked":
        print(
            "configuration follow-up ledger: BLOCKED "
            f"({configuration_action_presentation_receipt.get('blocking_reason') or 'not_admissible'})"
        )
if summary.get("status") != "blocked":
    if source.get("type") != "runtime_container":
        consent_receipt = record_operator_consent(
            summary,
            state_path=consent_state_path,
            expected_source_cycle_receipt_sha256=str(
                summary.get("source_cycle_receipt_sha256") or ""
            ),
        )
        if consent_receipt.get("status") == "blocked":
            print(
                "consent ledger: BLOCKED "
                f"({consent_receipt.get('blocking_reason') or 'not_admissible'})"
            )
    if source.get("type") == "runtime_container":
        try:
            expected_cycle_digest = str(
                summary.get("source_cycle_receipt_sha256") or ""
            ).strip()
            recorded_projection = runtime_operator_projection(
                runtime_container_id,
                record_presentation=True,
                expected_cycle_receipt_sha256=expected_cycle_digest,
            )
            presentation_receipt = dict(
                recorded_projection.get("presentation_receipt") or {}
            )
            if not (
                recorded_projection.get("source_cycle_receipt_sha256")
                == expected_cycle_digest
                and presentation_receipt.get("status")
                in {"recorded", "unchanged"}
                and presentation_receipt.get("source_cycle_receipt_sha256")
                == expected_cycle_digest
                and presentation_receipt.get("delivery_state_updated") is False
                and presentation_receipt.get("provider_quota_consumed") is False
                and presentation_receipt.get("protected_operation_executed") is False
            ):
                raise ValueError("runtime_presentation_receipt_not_admissible")
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
            print("presentation ledger: BLOCKED (runtime_record_not_verified)")
    else:
        presentation_receipt = record_operator_presentation(
            summary,
            state_path=presentation_state_path,
        )
        if presentation_receipt.get("status") == "blocked":
            print(
                "presentation ledger: BLOCKED "
                f"({presentation_receipt.get('blocking_reason') or 'not_admissible'})"
            )
PY
}

print_product_control_summary() {
  run_operator_python - <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root / "ea"))
pulse_path = root / ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json"
default_journey_path = Path("/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json")

from app.product.service import _public_guide_freshness_projection


def load_json(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


pulse = load_json(pulse_path) if pulse_path.exists() else None
signals = dict((pulse or {}).get("supporting_signals") or {})
configured_journey = str(signals.get("journey_gate_source") or "").strip()
journey_path = (root / configured_journey).resolve() if configured_journey else default_journey_path
journey = load_json(journey_path) if journey_path.exists() else None
journey_summary = dict((journey or {}).get("summary") or {})
journies = [dict(row) for row in list((journey or {}).get("journeys") or []) if isinstance(row, dict)]
pulse_gate = dict((pulse or {}).get("journey_gate_health") or {})
route = dict(signals.get("provider_route_stewardship") or {})
public_guide = _public_guide_freshness_projection()
support_closures_waiting = sum(int(dict(row.get("signals") or {}).get("support_closure_waiting_count") or 0) for row in journies)
support_human_responses = sum(int(dict(row.get("signals") or {}).get("support_needs_human_response_count") or 0) for row in journies)

journey_state = str(pulse_gate.get("state") or journey_summary.get("overall_state") or "missing").strip() or "missing"
journey_action = str(journey_summary.get("recommended_action") or pulse_gate.get("reason") or "No published journey action.").strip()
support_fallout = "clear"
if support_closures_waiting or support_human_responses:
    parts = []
    if support_closures_waiting:
        parts.append(f"{support_closures_waiting} closures waiting")
    if support_human_responses:
        parts.append(f"{support_human_responses} human responses needed")
    support_fallout = " · ".join(parts)

print(f"weekly pulse:      {pulse_path if pulse_path.exists() else 'missing'}")
print(f"pulse generated:   {str((pulse or {}).get('generated_at') or 'missing').strip() or 'missing'}")
print(f"active wave:       {str((pulse or {}).get('active_wave') or 'missing').strip() or 'missing'}")
print(f"wave status:       {str((pulse or {}).get('active_wave_status') or 'missing').strip() or 'missing'}")
print(f"launch readiness:  {str(signals.get('launch_readiness') or 'missing').strip() or 'missing'}")
print(f"journey gates:     {journey_path if journey_path.exists() else 'missing'}")
print(f"journey generated: {str((journey or {}).get('generated_at') or 'missing').strip() or 'missing'}")
print(f"journey gate:      {journey_state}")
print(f"journey action:    {journey_action}")
print(f"support fallout:   {support_fallout}")
print(f"route review due:  {str(route.get('review_due') or 'not published').strip() or 'not published'}")
print(f"public guide:      {str(public_guide.get('path') or 'missing').strip() or 'missing'}")
print(f"guide updated:     {str(public_guide.get('generated_at') or 'missing').strip() or 'missing'}")
print(f"guide freshness:   {str(public_guide.get('detail') or 'No public-guide freshness is mirrored.').strip() or 'No public-guide freshness is mirrored.'}")
PY
}

print_grounding_summary() {
  run_operator_python - <<'PY'
from __future__ import annotations

from pathlib import Path

import yaml

root = Path.cwd()
design_root = root / ".codex-design" / "product"


def load_yaml(path: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(path.read_text())
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip() or "missing"


trust = load_yaml(design_root / "PUBLIC_TRUST_CONTENT.yaml")
release = load_yaml(design_root / "PUBLIC_RELEASE_EXPERIENCE.yaml")
scorecard = load_yaml(design_root / "PRODUCT_HEALTH_SCORECARD.yaml")

help_page = next(
    (dict(row) for row in list(trust.get("trust_pages") or []) if isinstance(row, dict) and str(row.get("id") or "").strip() == "help"),
    {},
)
support_scorecard = next(
    (dict(row) for row in list(scorecard.get("scorecards") or []) if isinstance(row, dict) and str(row.get("id") or "").strip() == "support_and_feedback_closure"),
    {},
)
first_action = next((dict(row) for row in list(help_page.get("actions") or []) if isinstance(row, dict)), {})
first_metric = next((dict(row) for row in list(support_scorecard.get("metrics") or []) if isinstance(row, dict)), {})
cadence = dict(scorecard.get("cadence") or {})

print(f"public help:       {compact(help_page.get('heading') or 'Get help without guessing')}")
print(f"help summary:      {compact(help_page.get('intro') or release.get('release_notes_summary'))}")
if first_action:
    print(f"help first action: {compact(first_action.get('label'))} -> {compact(first_action.get('href'))}")
print(f"support question:  {compact(support_scorecard.get('question'))}")
if first_metric:
    print(f"support target:    {compact(first_metric.get('name'))} target {compact(first_metric.get('target'))}")
print(f"operator cadence:  {compact(cadence.get('review') or 'weekly')}")
print(f"snapshot owner:    {compact(cadence.get('snapshot_owner') or 'product_governor')}")
PY
}

print_codex_governance_summary() {
  run_operator_python - <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root / "ea"))

from app.api.routes.responses import _codex_governance_payload, _codex_profiles


def compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip() or "missing"


profiles = {
    str(item.get("profile") or "").strip(): dict(item)
    for item in _codex_profiles()
    if isinstance(item, dict)
}
governance = _codex_governance_payload()
cadence = dict(governance.get("review_cadence") or {})
support = dict(governance.get("support_help_boundary") or {})

for key, label in (
    ("easy", "easy"),
    ("core", "hard coder"),
    ("groundwork", "groundwork"),
    ("audit", "audit/jury"),
):
    row = profiles.get(key, {})
    print(f"{label}:           {compact(row.get('expectation_summary'))}")
print(f"review cadence:  {compact(cadence.get('review') or 'weekly')} / {compact(cadence.get('snapshot_owner') or 'product_governor')}")
print(f"support/help:    {compact(support.get('summary'))}")
PY
}

if [[ "${1:-}" == "--propertyquarry-action-only" ]]; then
  print_propertyquarry_action_summary
  exit 0
fi

echo "== Operator Summary =="
echo

echo "-- version --"
bash scripts/version_info.sh
echo

echo "-- key commands --"
echo "deploy:            make deploy"
echo "deploy (memory):   make deploy-memory"
echo "deploy + bootstrap: EA_BOOTSTRAP_DB=1 make deploy"
echo "bootstrap only:    make bootstrap"
echo "db status:         make db-status"
echo "db size:           make db-size"
echo "db retention:      make db-retention"
echo "smoke api:         make smoke-api"
echo "smoke postgres:    make smoke-postgres"
echo "smoke pg legacy:   make smoke-postgres-legacy"
echo "pg contracts:      make test-postgres-contracts"
echo "release smoke:     make release-smoke"
echo "ci gates:          make ci-gates"
echo "ci gates auth:     ./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ci-gates-authenticated"
echo "ci gates pg:       make ci-gates-postgres"
echo "ci gates pg leg:   make ci-gates-postgres-legacy"
echo "runtime hard gate: make runtime-hard-exit-gates"
echo "full hard gates:   make hard-exit-gates"
echo "ltd gates auth:    ./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ltd-release-gates"
echo "ltd critical auth: ./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py verify-ltd-critical-entries-authenticated"
echo "ltd flagship auth: ./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py verify-ltd-flagship-subset-authenticated"
echo "all local:         make all-local"
echo "verify assets:     make verify-release-assets"
echo "flagship ready:    make verify-flagship-release-readiness"
echo "release docs:      make release-docs"
echo "release prefl auth: ./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight"
echo "operator help:     make operator-help"
echo "provider ready:    make provider-readiness"
echo "overlay vision:    make overlay-vision-check"
echo "overlay vision+dl: make overlay-vision-pull"
echo "support bundle:    make support-bundle"
echo "tasks archive:     make tasks-archive"
echo "tasks archive dry: make tasks-archive-dry-run"
echo "tasks archive prn: make tasks-archive-prune"
echo "endpoints:         make endpoints"
echo "openapi export:    make openapi-export"
echo "openapi diff:      make openapi-diff"
echo "openapi prune:     make openapi-prune"
echo

echo "-- docs --"
echo "runbook:           RUNBOOK.md"
echo "architecture:      ARCHITECTURE_MAP.md"
echo "http examples:     HTTP_EXAMPLES.http"
echo "changelog:         CHANGELOG.md"
echo "env matrix:        ENVIRONMENT_MATRIX.md"
echo "release checklist: RELEASE_CHECKLIST.md"
echo

echo "-- action required --"
print_propertyquarry_action_summary
echo

echo "-- product control --"
print_product_control_summary
echo

echo "-- grounded packets --"
print_grounding_summary
echo

echo "-- codex governance --"
print_codex_governance_summary
echo

echo "-- queued task --"
if [[ -f TASKS_WORK_LOG.md ]]; then
  awk '/^## Queue/{flag=1;next}/^## In Progress/{flag=0}flag' TASKS_WORK_LOG.md | sed -n '1,8p'
else
  echo "local task log not present"
fi
