#!/bin/bash -p
set -euo pipefail

PATH="/usr/bin:/bin"
export PATH
readonly PATH
unset BASH_ENV ENV CDPATH GLOBIGNORE
IFS=$' \t\n'

script_source="${BASH_SOURCE[0]}"
[[ "${script_source}" == */* ]] || {
  printf '%s\n' "error: release asset verifier must be invoked with an explicit path" >&2
  exit 2
}
EA_ROOT="$(cd -P -- "${script_source%/*}/.." && pwd -P)"
readonly EA_ROOT
cd "${EA_ROOT}"

verification_mode="authority"
if [[ "${1:-}" == "--developer" ]]; then
  verification_mode="developer"
  shift
fi
if [[ "${verification_mode}" == "authority" ]] && \
  { [[ -v PYTHON_BIN ]] || [[ -v PYTEST_PYTHON_BIN ]]; }; then
  echo "error: release interpreter override forbidden" >&2
  exit 2
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  ./scripts/verify_release_assets.sh
  PYTHON_BIN=python3 ./scripts/verify_release_assets.sh --developer

Validates presence of required runtime docs, scripts, and schema files.
The default mode uses the authenticated release interpreter. The explicit
developer mode is a non-authoritative local parity check.
Exits non-zero when any required asset is missing.
EOF
  exit 0
fi

release_python_candidate="${PYTHON_BIN:-python3}"
unset \
  LD_AUDIT \
  LD_LIBRARY_PATH \
  LD_PRELOAD \
  PYTHONBREAKPOINT \
  PYTHONCASEOK \
  PYTHONDEBUG \
  PYTHONDEVMODE \
  PYTHONFAULTHANDLER \
  PYTHONHOME \
  PYTHONINSPECT \
  PYTHONMALLOC \
  PYTHONMALLOCSTATS \
  PYTHONOPTIMIZE \
  PYTHONPATH \
  PYTHONPLATLIBDIR \
  PYTHONPROFILEIMPORTTIME \
  PYTHONSTARTUP \
  PYTHONTRACEMALLOC \
  PYTHONUSERBASE \
  PYTHONWARNDEFAULTENCODING \
  PYTHONWARNINGS \
  PYTEST_ADDOPTS \
  PYTEST_PLUGINS
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=0
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1

if [[ "${verification_mode}" == "developer" ]]; then
  if [[ "${release_python_candidate}" != */* ]]; then
    release_python_candidate="$(command -v -- "${release_python_candidate}")" || {
      echo "error: developer Python interpreter is unavailable" >&2
      exit 2
    }
  fi
  [[ -x "${release_python_candidate}" && ! -d "${release_python_candidate}" ]] || {
    echo "error: developer Python interpreter is not executable" >&2
    exit 2
  }
  developer_probe="$(
    "${release_python_candidate}" -I -c \
      'import sys; print("propertyquarry-developer-python-v1" if sys.version_info >= (3, 12) else "")'
  )" || {
    echo "error: developer Python interpreter probe failed" >&2
    exit 2
  }
  [[ "${developer_probe}" == "propertyquarry-developer-python-v1" ]] || {
    echo "error: developer Python 3.12 or newer is required" >&2
    exit 2
  }
  RELEASE_PYTHON="${release_python_candidate}"
  echo "warning: developer release-asset verification is non-authoritative" >&2
else
  release_python_launcher="${EA_ROOT}/scripts/propertyquarry_release_python.sh"
  RELEASE_PYTHON="$("${release_python_launcher}" --print-interpreter)" || exit $?
  [[ "${RELEASE_PYTHON}" == \
    "${EA_ROOT}/.propertyquarry_release_tools/release-venv/bin/python" ]] || {
    echo "error: authenticated release interpreter path is invalid" >&2
    exit 2
  }
fi
unset PYTHON_BIN PYTEST_PYTHON_BIN
export PYTHONPATH="${EA_ROOT}/scripts:${EA_ROOT}/ea:${EA_ROOT}"
readonly RELEASE_PYTHON

missing=0
RELEASE_ASSETS_TMP_DIR=""
RELEASE_ASSETS_TMP_IDENTITY=""

create_release_assets_tmp_dir() {
  local candidate=""
  local candidate_identity=""
  local candidate_metadata=""
  umask 077
  candidate="$(
    /usr/bin/mktemp -d -- \
      "/tmp/propertyquarry-release-assets.${BASHPID}.XXXXXXXX"
  )" || {
    echo "error: release asset temporary directory cannot be created" >&2
    return 1
  }
  candidate_metadata="$(
    /usr/bin/stat -c '%d:%i:%u:%a' -- "${candidate}" 2>/dev/null
  )" || {
    echo "error: release asset temporary directory cannot be inspected" >&2
    return 1
  }
  candidate_identity="${candidate_metadata%:*:*}"
  if [[ ! -d "${candidate}" || -L "${candidate}" ||
        "${candidate_metadata}" != \
          "${candidate_identity}:$(/usr/bin/id -u):700" ]]; then
    echo "error: release asset temporary directory is not private" >&2
    return 1
  fi
  RELEASE_ASSETS_TMP_DIR="${candidate}"
  RELEASE_ASSETS_TMP_IDENTITY="${candidate_identity}"
}

cleanup_release_assets_tmp() {
  local current_identity=""
  case "${RELEASE_ASSETS_TMP_DIR:-}" in
    /tmp/propertyquarry-release-assets.*)
      if [[ ! -e "${RELEASE_ASSETS_TMP_DIR}" &&
            ! -L "${RELEASE_ASSETS_TMP_DIR}" ]]; then
        RELEASE_ASSETS_TMP_DIR=""
        RELEASE_ASSETS_TMP_IDENTITY=""
        return 0
      fi
      if [[ ! -d "${RELEASE_ASSETS_TMP_DIR}" ||
            -L "${RELEASE_ASSETS_TMP_DIR}" ]]; then
        echo \
          "error: refusing cleanup of replaced release asset temporary directory" \
          >&2
        return 1
      fi
      current_identity="$(
        /usr/bin/stat -c '%d:%i' -- "${RELEASE_ASSETS_TMP_DIR}" 2>/dev/null
      )" || {
        echo \
          "error: release asset temporary directory cannot be identified for cleanup" \
          >&2
        return 1
      }
      if [[ -z "${RELEASE_ASSETS_TMP_IDENTITY}" ||
            "${current_identity}" != "${RELEASE_ASSETS_TMP_IDENTITY}" ]]; then
        echo \
          "error: refusing cleanup of replaced release asset temporary directory" \
          >&2
        return 1
      fi
      /usr/bin/rm -rf --one-file-system -- "${RELEASE_ASSETS_TMP_DIR}" || {
        echo "error: release asset temporary directory cleanup failed" >&2
        return 1
      }
      RELEASE_ASSETS_TMP_DIR=""
      RELEASE_ASSETS_TMP_IDENTITY=""
      ;;
    "")
      ;;
    *)
      echo "error: refusing unsafe release asset temporary cleanup target" >&2
      return 1
      ;;
  esac
}

release_assets_cleanup_on_exit() {
  local status="$1"
  trap '' HUP INT TERM
  trap - EXIT
  if ! cleanup_release_assets_tmp && [[ "${status}" -eq 0 ]]; then
    status=1
  fi
  exit "${status}"
}

release_assets_terminate_from_signal() {
  trap '' HUP INT TERM
  local status="$1"
  trap - EXIT
  cleanup_release_assets_tmp || true
  exit "${status}"
}

trap 'release_assets_cleanup_on_exit "$?"' EXIT
trap 'release_assets_terminate_from_signal 129' HUP
trap 'release_assets_terminate_from_signal 130' INT
trap 'release_assets_terminate_from_signal 143' TERM
create_release_assets_tmp_dir

SMOKE_RUNTIME_GUARD_FILES=(
  "tests/smoke_runtime_api.py"
  "tests/smoke_runtime_api_suite_1.py"
  "tests/smoke_runtime_api_suite_2.py"
  "tests/smoke_runtime_api_suite_3.py"
  "tests/smoke_runtime_api_suite_4.py"
)
SMOKE_RUNTIME_GUARD_TARGET="${RELEASE_ASSETS_TMP_DIR}/smoke-runtime-guard.py"
cat "${SMOKE_RUNTIME_GUARD_FILES[@]}" > "${SMOKE_RUNTIME_GUARD_TARGET}"
DESIGN_MIRROR_STDOUT="${RELEASE_ASSETS_TMP_DIR}/design-mirror.stdout"
DESIGN_MIRROR_STDERR="${RELEASE_ASSETS_TMP_DIR}/design-mirror.stderr"
FULL_MIRROR_STDOUT="${RELEASE_ASSETS_TMP_DIR}/full-mirror.stdout"
FULL_MIRROR_STDERR="${RELEASE_ASSETS_TMP_DIR}/full-mirror.stderr"

required_files=(
  "README.md"
  "RUNBOOK.md"
  "SKILLS.md"
  "ARCHITECTURE_MAP.md"
  "HTTP_EXAMPLES.http"
  "CHANGELOG.md"
  "ENVIRONMENT_MATRIX.md"
  "MILESTONE.json"
  "RELEASE_CHECKLIST.md"
  "docs/PROPERTYQUARRY_IMAGE_PUBLICATION.md"
  "docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md"
  "docs/PROPERTYQUARRY_GLOBAL_LAUNCH_TERMINAL_INSTALL.md"
  "docs/propertyquarry_global_market_envelope.v1.json"
  "docs/PROPERTYQUARRY_INCIDENT_AND_SUPPORT_OPERATIONS.md"
  "config/monitoring/propertyquarry_incident_support.v1.json"
  "docs/PROPERTYQUARRY_GLOBAL_EXPERIENCE_EVIDENCE.md"
  "config/monitoring/propertyquarry_global_experience.v1.json"
  "config/monitoring/propertyquarry_flagship_operations.v1.json"
  "docs/PROPERTYQUARRY_JURISDICTION_PRIVACY_AND_PROVIDER_RIGHTS.md"
  "config/compliance/propertyquarry_jurisdiction_privacy_rights.v1.json"
  ".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md"
  ".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json"
  ".codex-design/repo/IMPLEMENTATION_SCOPE.md"
  ".codex-design/ea/START_HERE.md"
  ".codex-design/ea/SURFACE_DESIGN_SYSTEM.md"
  ".codex-design/ea/LTD_INTEGRATION_MAP.md"
  ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json"
  ".github/workflows/propertyquarry-publish-runtime-images.yml"
  ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json"
  ".codex-design/product/PUBLIC_GUIDE_IMAGE_CURATION.yaml"
  ".codex-design/product/TELEGRAM_FLAGSHIP_RUNTIME_DESIGN.md"
  ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json"
  "scripts/deploy.sh"
  "scripts/db_bootstrap.sh"
  "scripts/db_status.sh"
  "scripts/db_size.sh"
  "scripts/db_retention.sh"
  "scripts/smoke_api.sh"
  "scripts/smoke_postgres.sh"
  "scripts/test_postgres_contracts.sh"
  "scripts/smoke_help.sh"
  "scripts/export_openapi.sh"
  "scripts/diff_openapi.sh"
  "scripts/prune_openapi.sh"
  "scripts/list_endpoints.sh"
  "scripts/version_info.sh"
  "scripts/operator_summary.sh"
  "scripts/support_bundle.sh"
  "scripts/archive_tasks.sh"
  "scripts/resolve_onemin_ai_key.sh"
  "scripts/resolve_browseract_key.sh"
  "scripts/provision_propertyquarry_admission_database.py"
  "scripts/provision_propertyquarry_auth_env.py"
  "scripts/provision_propertyquarry_runtime_database.py"
  "scripts/materialize_ea_flagship_release_gate.py"
  "scripts/propertyquarry_global_market_envelope.py"
  "scripts/propertyquarry_incident_support_gate.py"
  "scripts/propertyquarry_global_experience_gate.py"
  "scripts/propertyquarry_jurisdiction_privacy_rights_gate.py"
  "scripts/build_propertyquarry_global_launch_terminal_bundle.py"
  "scripts/propertyquarry_global_launch_terminal.py"
  "scripts/propertyquarry_gold_status.py"
  "scripts/verify_propertyquarry_global_governance_assets.py"
  "packaging/propertyquarry-global-launch-terminal/global-launch-terminal-bundle.v1.schema.json"
  "scripts/materialize_ea_browser_workflow_proof.py"
  "scripts/materialize_weekly_product_pulse.py"
  "scripts/bootstrap_propertyquarry_release_python.sh"
  "scripts/propertyquarry_release_python.sh"
  "scripts/propertyquarry_release_python_verify.py"
  "scripts/verify_generated_release_artifacts_clean.py"
  "scripts/verify_propertyquarry_python_wheelhouse.py"
  "scripts/verify_flagship_release_readiness.py"
  "scripts/verify_design_mirror_bundle.py"
  "scripts/repair_design_mirror_bundle.sh"
  "scripts/refresh_ltds_from_inventory.py"
  "scripts/refresh_ltds_from_inventory.sh"
  "scripts/refresh_ltds_via_api.py"
  "scripts/refresh_ltds_via_api.sh"
  "tests/test_propertyquarry_auth_env_provisioner.py"
  "tests/test_propertyquarry_database_provisioners.py"
  "ea/schema/20260305_v0_2_execution_ledger_kernel.sql"
  "ea/schema/20260305_v0_3_channel_runtime_kernel.sql"
  "ea/schema/20260305_v0_4_policy_decisions_kernel.sql"
  "ea/schema/20260305_v0_5_artifacts_kernel.sql"
  "ea/schema/20260305_v0_6_execution_ledger_v2.sql"
  "ea/schema/20260305_v0_7_approvals_kernel.sql"
  "ea/schema/20260305_v0_8_channel_runtime_reliability.sql"
  "ea/schema/20260305_v0_9_tool_connector_kernel.sql"
  "ea/schema/20260305_v0_10_task_contracts_kernel.sql"
  "ea/schema/20260305_v0_11_memory_kernel.sql"
  "ea/schema/20260305_v0_12_entities_relationships_kernel.sql"
  "ea/schema/20260305_v0_13_commitments_kernel.sql"
  "ea/schema/20260305_v0_14_authority_bindings_kernel.sql"
  "ea/schema/20260305_v0_15_delivery_preferences_kernel.sql"
  "ea/schema/20260305_v0_16_follow_ups_kernel.sql"
  "ea/schema/20260305_v0_17_deadline_windows_kernel.sql"
  "ea/schema/20260305_v0_18_stakeholders_kernel.sql"
  "ea/schema/20260305_v0_19_decision_windows_kernel.sql"
  "ea/schema/20260305_v0_20_communication_policies_kernel.sql"
  "ea/schema/20260305_v0_21_follow_up_rules_kernel.sql"
  "ea/schema/20260305_v0_22_interruption_budgets_kernel.sql"
  "ea/schema/20260305_v0_23_execution_queue_kernel.sql"
  "ea/schema/20260305_v0_24_human_tasks_kernel.sql"
  "ea/schema/20260305_v0_25_human_task_resume_kernel.sql"
  "ea/schema/20260305_v0_26_human_task_assignment_state.sql"
  "ea/schema/20260305_v0_27_human_task_review_contract.sql"
  "ea/schema/20260305_v0_28_operator_profiles_kernel.sql"
  "ea/schema/20260305_v0_29_human_task_assignment_source.sql"
  "ea/schema/20260305_v0_30_human_task_assignment_provenance.sql"
  "ea/schema/20260305_v0_31_artifact_principal_scope.sql"
  "ea/schema/20260305_v0_32_provider_bindings_kernel.sql"
  "ea/schema/20260305_v0_33_task_contract_runtime_policy.sql"
  "ea/schema/20260305_v0_34_assistant_onboarding_canonical_schema.sql"
  "ea/schema/20260305_v0_35_execution_ledger_legacy_compat.sql"
  "ea/schema/20260305_v0_36_propertyquarry_property_passport.sql"
  "ea/schema/20260305_v0_37_runtime_repository_contract.sql"
  "ea/schema/20260305_v0_38_operator_profile_principal_scope.sql"
  "ea/requirements.wheelhouse.lock"
)

echo "== verify release assets =="
for f in "${required_files[@]}"; do
if [[ -f "${f}" ]]; then
    echo "ok: ${f}"
  else
    echo "missing: ${f}" >&2
    missing=1
  fi
done

if python3 scripts/verify_propertyquarry_python_wheelhouse.py \
  --requirements-lock ea/requirements.lock \
  --hash-lock ea/requirements.wheelhouse.lock \
  --wheelhouse vendor/propertyquarry-python-wheels; then
  echo "ok: PropertyQuarry offline Python wheelhouse is exact and hash locked"
else
  echo "missing: PropertyQuarry offline Python wheelhouse is invalid" >&2
  missing=1
fi

if python3 scripts/verify_propertyquarry_global_governance_assets.py; then
  echo "ok: PropertyQuarry global-governance release assets and fail-closed semantics"
else
  echo "missing: PropertyQuarry global-governance release assets or fail-closed semantics" >&2
  missing=1
fi

if python3 scripts/verify_design_mirror_bundle.py >/tmp/ea_design_mirror_verify.out 2>/tmp/ea_design_mirror_verify.err; then
  echo "ok: bounded design mirror bundle parity"
else
  cat "${DESIGN_MIRROR_STDOUT}"
  cat "${DESIGN_MIRROR_STDERR}" >&2
  echo "missing: bounded design mirror bundle parity" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" scripts/verify_full_design_mirror_parity.py \
  >"${FULL_MIRROR_STDOUT}" 2>"${FULL_MIRROR_STDERR}"; then
  echo "ok: full design mirror parity"
else
  cat "${FULL_MIRROR_STDOUT}"
  cat "${FULL_MIRROR_STDERR}" >&2
  echo "missing: full design mirror parity" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
import subprocess
from pathlib import Path

from scripts.materialize_ea_flagship_release_gate import browser_receipt_pass_blockers
from scripts.propertyquarry_release_proof_baseline import (
    GLOBAL_LAUNCH_MARKET_ENVELOPE_AUTHORITY,
    GLOBAL_LAUNCH_TERMINAL_COMMAND,
    approved_baseline_binding,
    approved_global_launch_contract_blockers,
    approved_seed_baseline_blockers,
)

gate = json.loads(Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json").read_text(encoding="utf-8"))
assert gate["product"] == "propertyquarry"
assert gate["surface"] == "propertyquarry_flagship_release_control"
assert gate["version"] == 2
assert not approved_seed_baseline_blockers(gate)
assert not approved_global_launch_contract_blockers(gate["global_launch_contract"])
evidence_sources = gate["browser_workflow_proof"]["evidence_sources"]
browser_sources = {entry["file"]: set(entry["cases"]) for entry in evidence_sources}
assert len(browser_sources) == len(evidence_sources)
assert any("/e2e/" not in entry["file"] for entry in evidence_sources)
assert sum("/e2e/" in entry["file"] for entry in evidence_sources) == 1
assert all(entry["cases"] and len(set(entry["cases"])) == len(entry["cases"]) for entry in evidence_sources)
assert gate["browser_workflow_proof"]["proof_target"] == "propertyquarry"
assert "tests/test_propertyquarry_workspace_redesign.py" in browser_sources
assert "tests/e2e/test_propertyquarry_greenfield_browser.py" in browser_sources
assert "tests/test_property_evidence_overlays.py" in browser_sources
assert "test_propertyquarry_workspace_routes_render_greenfield_surfaces" in browser_sources["tests/test_propertyquarry_workspace_redesign.py"]
assert "test_propertyquarry_failed_run_stays_on_activity_surface" in browser_sources["tests/test_propertyquarry_workspace_redesign.py"]
assert "test_propertyquarry_greenfield_workspace_in_real_browser" in browser_sources["tests/e2e/test_propertyquarry_greenfield_browser.py"]
assert "test_propertyquarry_greenfield_workspace_is_mobile_usable" in browser_sources["tests/e2e/test_propertyquarry_greenfield_browser.py"]
assert "test_propertyquarry_research_evidence_states_and_links_render_in_real_browser" in browser_sources["tests/e2e/test_propertyquarry_greenfield_browser.py"]
assert "test_property_research_rows_preserve_evidence_states_and_original_article_link" in browser_sources["tests/test_property_evidence_overlays.py"]
assert "EA_FLAGSHIP_TRUTH_PLANE.md" == gate["truth_plane"]["source"].split("/")[-1]

browser_receipt = json.loads(Path(".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json").read_text(encoding="utf-8"))
assert browser_receipt["contract_name"] == "ea.browser_workflow_proof"
assert browser_receipt["status"] in {"blocked", "preview_only", "pass"}
assert browser_receipt["product"] == gate["product"]
assert browser_receipt["proof_target"] == gate["browser_workflow_proof"]["proof_target"]
assert browser_receipt["approved_baseline"] == approved_baseline_binding()
if browser_receipt["status"] == "pass":
    unsupported_pass_reasons = browser_receipt_pass_blockers(browser_receipt, gate)
    assert not unsupported_pass_reasons, (
        "browser workflow proof claims pass without completed required lanes: "
        f"{unsupported_pass_reasons}"
    )
assert browser_receipt["expected_browser_signals"] == gate["browser_workflow_proof"]["expected_browser_signals"]
expected_source_backed = [
    entry
    for entry in gate["browser_workflow_proof"]["evidence_sources"]
    if "/e2e/" not in entry["file"]
]
source_backed_proofs = browser_receipt["source_backed_journey_proofs"]
assert len(source_backed_proofs) == len(expected_source_backed)
for lane, expected in zip(source_backed_proofs, expected_source_backed):
    assert lane["test_file"] == expected["file"]
    assert lane["cases"] == expected["cases"]
assert browser_receipt["source_backed_journey_proof"] == source_backed_proofs[0]
assert browser_receipt["real_browser_e2e_proof"]["test_file"] == "tests/e2e/test_propertyquarry_greenfield_browser.py"

flagship_receipt = json.loads(Path(".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json").read_text(encoding="utf-8"))
assert flagship_receipt["status"] in {"blocked", "preview_only", "pass"}
assert flagship_receipt["product"] == gate["product"]
assert flagship_receipt["surface"] == gate["surface"]
assert flagship_receipt["approved_baseline"] == approved_baseline_binding()
assert flagship_receipt["readiness_scope"] == "source_and_browser_proof"
assert flagship_receipt["live_readiness"] == {
    "status": "not_evaluated",
    "authority": "_completion/property_gold_status/release-gate.json",
    "required_profile": "launch",
}
assert flagship_receipt["global_launch_contract"] == gate["global_launch_contract"]
assert flagship_receipt["global_launch_readiness"] == {
    "status": "not_evaluated",
    "market_envelope_authority": GLOBAL_LAUNCH_MARKET_ENVELOPE_AUTHORITY,
    "terminal_command": GLOBAL_LAUNCH_TERMINAL_COMMAND,
    "source_browser_checkpoint_is_sufficient": False,
}
assert "final live readiness is not evaluated" in flagship_receipt["operator_summary"].lower()
assert "does not establish global launch authority" in flagship_receipt["operator_summary"].lower()
assert flagship_receipt["browser_workflow_proof"]["proof_target"] == gate["browser_workflow_proof"]["proof_target"]
if flagship_receipt["status"] == "pass":
    assert browser_receipt["status"] == "pass", (
        "flagship release receipt claims pass while browser workflow proof is "
        f"{browser_receipt['status']}"
    )

pulse = json.loads(Path(".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json").read_text(encoding="utf-8"))
assert pulse["contract_name"] == "ea.weekly_product_pulse"
supporting = pulse.get("supporting_signals") or {}
release_truth_source = pulse.get("release_truth_source") or supporting.get("flagship_release_receipt_source")
journey_gate_source = pulse.get("journey_gate_source") or supporting.get("journey_gate_source")
release_truth_provenance = pulse.get("release_truth_provenance") or {}
journey_gate_provenance = pulse.get("journey_gate_provenance") or {}
assert release_truth_source == ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json"
assert journey_gate_source == "/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json"
assert supporting.get("journey_gate_source") == "/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json"
assert supporting.get("flagship_release_receipt_source") == ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json"
assert release_truth_provenance.get("present") is True
assert release_truth_provenance.get("sha256")
assert release_truth_provenance.get("git_head")
assert journey_gate_provenance.get("present") is True
assert journey_gate_provenance.get("sha256")
assert journey_gate_provenance.get("git_head")
assert supporting.get("journey_gate_git_head") == journey_gate_provenance.get("git_head")
assert supporting.get("launch_readiness")
assert pulse["governor_decisions"]

current_head = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
release_truth_head = str(release_truth_provenance.get("git_head") or "").strip()
if release_truth_head and current_head and release_truth_head != current_head:
    changed_since_receipt = [
        line.strip()
        for line in subprocess.run(
            ["git", "diff", "--name-only", f"{release_truth_head}..{current_head}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if line.strip()
    ]
    allowed_prefixes = (
        ".codex-design/product/",
        ".codex-studio/published/",
        ".codex-design/repo/",
    )
    allowed_exact = {
        "README.md",
        "RUNBOOK.md",
        "RELEASE_CHECKLIST.md",
        "PRODUCT_RELEASE_CHECKLIST.md",
        "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
        "Makefile",
        "CHANGELOG.md",
        "LTDs.md",
        ".github/workflows/smoke-runtime.yml",
        "ea/app/api/routes/plans.py",
        "ea/app/services/execution_approval_pause_service.py",
        "scripts/materialize_ea_browser_workflow_proof.py",
        "scripts/materialize_weekly_product_pulse.py",
        "scripts/operator_summary.sh",
        "scripts/smoke_api.sh",
        "scripts/smoke_postgres.sh",
        "scripts/test_postgres_contracts.sh",
        "scripts/verify_generated_release_artifacts_clean.py",
        "scripts/verify_flagship_release_readiness.py",
        "scripts/verify_release_assets.sh",
        "tests/e2e/visual_baselines/admin-community-page.png",
        "tests/test_chummer5a_parity_lab_pack.py",
        "tests/test_ea_browser_workflow_proof_materializer.py",
        "tests/e2e/test_product_workflows.py",
        "tests/test_execution_runtime_services.py",
        "tests/test_flagship_release_readiness_gate.py",
        "tests/test_migration_contracts.py",
        "tests/test_operator_contracts.py",
        "tests/test_providers_api_contracts.py",
        "tests/test_skills.py",
        "tests/smoke_runtime_api_suite_3.py",
        "tests/test_weekly_product_pulse_materializer.py",
    }
    disallowed = [
        path for path in changed_since_receipt
        if path not in allowed_exact and not any(path.startswith(prefix) for prefix in allowed_prefixes)
    ]
    assert not disallowed, (
        "weekly pulse release provenance is stale relative to current HEAD and "
        f"non-doc/runtime changes landed after receipt materialization: {disallowed}"
    )
PY
then
  echo "ok: PropertyQuarry flagship truth plane gate seed"
else
  echo "missing: PropertyQuarry flagship truth plane gate seed, generated receipt, or weekly pulse" >&2
  missing=1
fi

if python3 - <<'PY'
import subprocess
import sys
from pathlib import Path


def _head_bytes(path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        check=True,
        capture_output=True,
    ).stdout


paths = (
    ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
    ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
)
drift = []
for path in paths:
    assert _head_bytes(path) == Path(path).read_bytes(), path
PY
then
  echo "ok: generated release artifacts stay semantically aligned with HEAD"
else
  echo "missing: generated release artifacts drift semantically from HEAD" >&2
  git diff -- \
    .codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json \
    .codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json \
    .codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json >&2 || true
  missing=1
fi

echo "== verify release docs linkage =="
if grep -Fq "make operator-help" "README.md"; then
  echo "ok: README operator-help reference"
else
  echo "missing: README operator-help reference" >&2
  missing=1
fi

if grep -Fq "scripts/smoke_help.sh --help" "README.md"; then
  echo "ok: README smoke-help help note"
else
  echo "missing: README smoke-help help note" >&2
  missing=1
fi

if grep -Fq "make release-smoke" "README.md"; then
  echo "ok: README release-smoke reference"
else
  echo "missing: README release-smoke reference" >&2
  missing=1
fi

if grep -Fq "make smoke-postgres-legacy" "README.md"; then
  echo "ok: README legacy postgres smoke reference"
else
  echo "missing: README legacy postgres smoke reference" >&2
  missing=1
fi

if grep -Fq "make test-postgres-contracts" "README.md"; then
  echo "ok: README postgres contract test reference"
else
  echo "missing: README postgres contract test reference" >&2
  missing=1
fi

if grep -Fq "make ci-gates-postgres-legacy" "README.md"; then
  echo "ok: README legacy postgres parity reference"
else
  echo "missing: README legacy postgres parity reference" >&2
  missing=1
fi

if grep -Fq \
  "./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight" \
  "README.md"; then
  echo "ok: README release-preflight reference"
else
  echo "missing: README release-preflight reference" >&2
  missing=1
fi

if grep -Fq "lighter local readiness pass" "README.md"; then
  echo "ok: README all-local vs release-preflight note"
else
  echo "missing: README all-local vs release-preflight note" >&2
  missing=1
fi

if grep -Fq "make docs-verify" "README.md"; then
  echo "ok: README docs-verify alias reference"
else
  echo "missing: README docs-verify alias reference" >&2
  missing=1
fi

if grep -Fq "make release-docs" "README.md"; then
  echo "ok: README release-docs reference"
else
  echo "missing: README release-docs reference" >&2
  missing=1
fi

if grep -Fq "temporary backward-compatible alias" "README.md"; then
  echo "ok: README backend alias deprecation note"
else
  echo "missing: README backend alias deprecation note" >&2
  missing=1
fi

if grep -Fq "ea_pgdata" "README.md" && \
   grep -Fq "/var/lib/postgresql/data" "README.md" && \
   grep -Fq "not RAM" "README.md"; then
  echo "ok: README pgdata note"
else
  echo "missing: README pgdata note" >&2
  missing=1
fi

if grep -Fq "EA_RETENTION_PROFILE=aggressive|standard|conservative" "README.md" && \
   grep -Fq "EA_RETENTION_TABLES" "README.md" && \
   grep -Fq "EA_RETENTION_SKIP_TABLES" "README.md" && \
   grep -Fq "EA_DB_SIZE_SCHEMA=<schema>" "README.md" && \
   grep -Fq "EA_DB_SIZE_SORT_KEY=total|table|index" "README.md" && \
   grep -Fq "EA_DB_SIZE_TABLE_PREFIX=<prefix>" "README.md" && \
   grep -Fq "EA_DB_SIZE_MIN_MB=<n>" "README.md" && \
   grep -Fq "SUPPORT_INCLUDE_DB_SIZE=0" "README.md" && \
   grep -Fq "SUPPORT_DB_SIZE_LIMIT=<n>" "README.md"; then
  echo "ok: README db visibility and retention note"
else
  echo "missing: README db visibility and retention note" >&2
  missing=1
fi

if grep -Fq "policy_denied:tool_not_allowed" "README.md"; then
  echo "ok: README policy tool contract note"
else
  echo "missing: README policy tool contract note" >&2
  missing=1
fi

if grep -Fq "/v1/policy/evaluate" "README.md" && \
   grep -Fq "step_kind" "README.md" && \
   grep -Fq "/v1/policy/evaluate" "RUNBOOK.md" && \
   grep -Fq "step/authority/review metadata" "RUNBOOK.md" && \
   grep -Fq "/v1/policy/evaluate" "HTTP_EXAMPLES.http" && \
   grep -Fq '"step_kind": "connector_call"' "HTTP_EXAMPLES.http" && \
   grep -Fq "connector_call|execute|manager" "scripts/smoke_api.sh" && \
   grep -Fq "test_policy_requires_approval_for_connector_dispatch_step_even_without_explicit_send_action" "tests/test_policy.py" && \
   grep -Fq "/v1/policy/evaluate" "scripts/smoke_api.sh"; then
  if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "external_action_policy_api_exposure")
assert capability["status"] == "released"
PY
  then
    echo "ok: external-action policy evaluation route docs"
  else
    echo "missing: external-action policy evaluation milestone status" >&2
    missing=1
  fi
else
  echo "missing: external-action policy evaluation route docs" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "policy_plane_principal_scope_enforcement")
assert capability["status"] == "released"
PY
then
  if grep -Fq "Principal-scoped rewrite/session/artifact/receipt/run-cost, plan-compile, connector, human-task, and memory routes treat body/query \`principal_id\` as compatibility input only; mismatches against the request principal fail with \`403 principal_scope_mismatch\`." "README.md" && \
     grep -Fq "| GET | \`/v1/policy/decisions/recent\` | \`200\` | \`403 principal_scope_mismatch\`, \`404 session_not_found\` when \`session_id\` is scoped to another principal |" "RUNBOOK.md" && \
     grep -Fq "| POST | \`/v1/policy/evaluate\` | \`200\` | validation \`422\`, \`403 principal_scope_mismatch\` |" "RUNBOOK.md" && \
     grep -Fq "| GET | \`/v1/policy/approvals/history\` | \`200\` | \`403 principal_scope_mismatch\`, \`404 session_not_found\` when \`session_id\` is scoped to another principal" "RUNBOOK.md" && \
     grep -Fq "POLICY_EVAL_SCOPE_MISMATCH_REASON" "scripts/smoke_api.sh" && \
     grep -Fq 'foreign_approve.json()["error"]["code"] == "principal_scope_mismatch"' "tests/test_policy_scope_contracts.py" && \
     grep -Fq 'foreign_deny.json()["error"]["code"] == "principal_scope_mismatch"' "tests/test_policy_scope_contracts.py" && \
     grep -Fq 'foreign_expire.json()["error"]["code"] == "principal_scope_mismatch"' "tests/test_policy_scope_contracts.py" && \
     grep -Fq 'history_foreign.json()["error"]["code"] == "principal_scope_mismatch"' "tests/test_policy_scope_contracts.py" && \
     grep -Fq "decided_by_scope_mismatch" "tests/test_policy_scope_contracts.py" && \
     grep -Fq "policy_plane_principal_scope_enforcement" "CHANGELOG.md"; then
    echo "ok: policy plane principal-scope release coverage"
  else
    echo "missing: policy plane principal-scope release coverage" >&2
    missing=1
  fi
else
  echo "missing: policy plane principal-scope milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_lookup_api_exposure")
assert capability["status"] == "released"
PY
then
  if grep -Fq "/v1/rewrite/artifacts/{artifact_id}" "README.md" && \
     grep -Fq "/v1/rewrite/artifacts/{artifact_id}" "RUNBOOK.md" && \
     grep -Fq "/v1/rewrite/artifacts/{{artifact_id}}" "HTTP_EXAMPLES.http" && \
     grep -Fq '/v1/rewrite/artifacts/${ARTIFACT_ID}' "scripts/smoke_api.sh"; then
    echo "ok: artifact lookup route docs"
  else
    echo "missing: artifact lookup route docs" >&2
    missing=1
  fi
else
  echo "missing: artifact lookup milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "receipt_and_run_cost_lookup_api_exposure")
assert capability["status"] == "released"
PY
then
  if grep -Fq "/v1/rewrite/receipts/{receipt_id}" "README.md" && \
     grep -Fq "/v1/rewrite/run-costs/{cost_id}" "README.md" && \
     grep -Fq "/v1/rewrite/receipts/{receipt_id}" "RUNBOOK.md" && \
     grep -Fq "/v1/rewrite/run-costs/{cost_id}" "RUNBOOK.md" && \
     grep -Fq "/v1/rewrite/receipts/{{receipt_id}}" "HTTP_EXAMPLES.http" && \
     grep -Fq "/v1/rewrite/run-costs/{{cost_id}}" "HTTP_EXAMPLES.http" && \
     grep -Fq '/v1/rewrite/receipts/${RECEIPT_ID}' "scripts/smoke_api.sh" && \
     grep -Fq '/v1/rewrite/run-costs/${COST_ID}' "scripts/smoke_api.sh" && \
     grep -Fq 'TASK_EXECUTE_RECEIPT_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'TASK_EXECUTE_COST_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'fetched_receipt.json()["task_key"] == "stakeholder_briefing"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'fetched_cost.json()["task_key"] == "stakeholder_briefing"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "receipt_and_run_cost_lookup_api_exposure" "CHANGELOG.md"; then
    echo "ok: receipt and run-cost lookup release baseline"
  else
    echo "missing: receipt and run-cost lookup release baseline" >&2
    missing=1
  fi
else
  echo "missing: receipt and run-cost lookup milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "approval_resume_execution")
assert capability["status"] == "released"
PY
then
  if grep -Fq "resumes execution inline" "README.md" && \
     grep -Fq "resumes execution immediately" "RUNBOOK.md" && \
     grep -Fq "approve and resume execution" "HTTP_EXAMPLES.http" && \
     grep -Fq "approval resume path ok" "scripts/smoke_api.sh"; then
    echo "ok: approval resume execution docs"
  else
    echo "missing: approval resume execution docs" >&2
    missing=1
  fi
else
  echo "missing: approval resume execution milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "execution_queue_inline_worker")
assert capability["status"] == "released"
assert "ea/schema/20260305_v0_23_execution_queue_kernel.sql" in milestone["migrations"]
PY
then
  if grep -Fq 'rewrite execution now persists durable `execution_queue` rows and drains them inline for API requests before returning' "README.md" && \
     grep -Fq 'Allowed and approved rewrites now pass through durable `execution_queue` rows first; the current API path drains that queue inline, while non-API runner roles can drain it as workers.' "RUNBOOK.md" && \
     grep -Fq "v0_23 execution queue kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "execution_queue" "scripts/db_status.sh" && \
     grep -Fq "queue_items" "scripts/smoke_api.sh" && \
     grep -Fq "execution_queue" "scripts/smoke_postgres.sh" && \
     grep -Fq "test_postgres_execution_queue_enqueue_lease_complete_and_list" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq 'lease_next_queue_item(lease_owner="contract-worker"' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq 'Promoted milestone capability `execution_queue_inline_worker` to released' "CHANGELOG.md"; then
    echo "ok: execution queue inline worker release baseline"
  else
    echo "missing: execution queue inline worker release baseline" >&2
    missing=1
  fi
else
  echo "missing: execution queue inline worker milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "runtime_mode_fail_fast_storage")
assert capability["status"] == "released"
PY
then
  if grep -Fq "EA_RUNTIME_MODE=dev|test|prod" "README.md" && \
     grep -Fq "EA_RUNTIME_MODE=prod" "RUNBOOK.md" && \
     grep -Fq "EA_RUNTIME_MODE" "ENVIRONMENT_MATRIX.md" && \
     grep -Fq "prod fail-fast path ok" "scripts/smoke_postgres.sh"; then
    echo "ok: runtime mode fail-fast docs"
  else
    echo "missing: runtime mode fail-fast docs" >&2
    missing=1
  fi
else
  echo "missing: runtime mode fail-fast milestone status" >&2
  missing=1
fi

if grep -Fq 'Release preflight now keys off the EA flagship truth plane, gate seed, generated release receipt, and weekly pulse; `MILESTONE.json` remains supporting delivery history.' "README.md"; then
  echo "ok: README EA flagship gate pointer"
else
  echo "missing: README EA flagship gate pointer" >&2
  missing=1
fi

if grep -Fq "EA_FLAGSHIP_RELEASE_GATE.generated.json" "README.md" && \
   grep -Fq "scripts/materialize_ea_flagship_release_gate.py" "README.md"; then
  echo "ok: README EA flagship receipt pointer"
else
  echo "missing: README EA flagship receipt pointer" >&2
  missing=1
fi

if grep -Fq "WEEKLY_PRODUCT_PULSE.generated.json" "README.md" && \
   grep -Fq "scripts/materialize_weekly_product_pulse.py" "README.md"; then
  echo "ok: README EA weekly pulse pointer"
else
  echo "missing: README EA weekly pulse pointer" >&2
  missing=1
fi

if grep -Fq "WEEKLY_PRODUCT_PULSE.generated.json" ".codex-design/product/README.md" && \
   grep -Fq "EA flagship receipt, fleet journey gates, and scorecard" ".codex-design/product/README.md"; then
  echo "ok: design README EA weekly pulse note"
else
  echo "missing: design README EA weekly pulse note" >&2
  missing=1
fi

if grep -Fq 'Release preflight checklist includes the EA flagship truth-plane contract in `RELEASE_CHECKLIST.md`.' "README.md"; then
  echo "ok: README checklist EA truth-plane note"
else
  echo "missing: README checklist EA truth-plane note" >&2
  missing=1
fi

if grep -Fq \
  "Recommended sequencing: run \`make release-docs\` before dispatching the" \
  "README.md" && \
   grep -Fq "authenticated \`release-preflight\` target." "README.md"; then
  echo "ok: README release-docs sequencing note"
else
  echo "missing: README release-docs sequencing note" >&2
  missing=1
fi

if grep -Fq "smoke, readiness, CI parity, release/support, and task-archive shortcuts" "README.md"; then
  echo "ok: README operator summary shortcut note"
else
  echo "missing: README operator summary shortcut note" >&2
  missing=1
fi

if grep -Fq "operator_summary.sh --help" "README.md"; then
  echo "ok: README operator-summary help note"
else
  echo "missing: README operator-summary help note" >&2
  missing=1
fi

if grep -Fq 'Endpoint/version/OpenAPI helper scripts also expose `--help`' "README.md"; then
  echo "ok: README endpoint/version/openapi help note"
else
  echo "missing: README endpoint/version/openapi help note" >&2
  missing=1
fi

if grep -Fq '`scripts/version_info.sh` still prints milestone capability-status counts and release tags from `MILESTONE.json` as delivery history, but EA flagship release claims now come from `EA_FLAGSHIP_TRUTH_PLANE.md`, `EA_FLAGSHIP_RELEASE_GATE.json`, and `EA_FLAGSHIP_RELEASE_GATE.generated.json`.' "README.md"; then
  echo "ok: README version-info EA truth-plane note"
else
  echo "missing: README version-info EA truth-plane note" >&2
  missing=1
fi

if grep -Fq "SUPPORT_INCLUDE_DB_VOLUME=0" "README.md" && \
   grep -Fq "live \`ea-db\` mount inspection output" "README.md"; then
  echo "ok: README support bundle volume note"
else
  echo "missing: README support bundle volume note" >&2
  missing=1
fi

if grep -Fq "Operator Script Help Index" "RUNBOOK.md"; then
  echo "ok: RUNBOOK script help index"
else
  echo "missing: RUNBOOK script help index" >&2
  missing=1
fi

if grep -Fq "EA_STORAGE_BACKEND" "ENVIRONMENT_MATRIX.md" && \
   grep -Fq "deprecated compatibility alias" "ENVIRONMENT_MATRIX.md"; then
  echo "ok: ENVIRONMENT_MATRIX canonical backend env note"
else
  echo "missing: ENVIRONMENT_MATRIX canonical backend env note" >&2
  missing=1
fi

if grep -Fq "scripts/operator_summary.sh" "RUNBOOK.md"; then
  echo "ok: RUNBOOK operator-summary help reference"
else
  echo "missing: RUNBOOK operator-summary help reference" >&2
  missing=1
fi

if grep -Fq "ea_pgdata" "RUNBOOK.md" && \
   grep -Fq "/var/lib/postgresql/data" "RUNBOOK.md" && \
   grep -Fq "not RAM" "RUNBOOK.md"; then
  echo "ok: RUNBOOK pgdata note"
else
  echo "missing: RUNBOOK pgdata note" >&2
  missing=1
fi

if grep -Fq "EA_RETENTION_PROFILE=aggressive bash scripts/db_retention.sh" "RUNBOOK.md" && \
   grep -Fq "EA_RETENTION_TABLES=execution_events,delivery_outbox bash scripts/db_retention.sh" "RUNBOOK.md" && \
   grep -Fq "EA_RETENTION_SKIP_TABLES=observation_events,policy_decisions bash scripts/db_retention.sh" "RUNBOOK.md" && \
   grep -Fq "EA_DB_SIZE_SCHEMA=public bash scripts/db_size.sh" "RUNBOOK.md" && \
   grep -Fq "EA_DB_SIZE_SORT_KEY=index bash scripts/db_size.sh" "RUNBOOK.md" && \
   grep -Fq "EA_DB_SIZE_TABLE_PREFIX=execution_ bash scripts/db_size.sh" "RUNBOOK.md" && \
   grep -Fq "EA_DB_SIZE_MIN_MB=25 bash scripts/db_size.sh" "RUNBOOK.md" && \
   grep -Fq "SUPPORT_INCLUDE_DB_SIZE=0 bash scripts/support_bundle.sh" "RUNBOOK.md" && \
   grep -Fq "SUPPORT_DB_SIZE_LIMIT=15 bash scripts/support_bundle.sh" "RUNBOOK.md"; then
  echo "ok: RUNBOOK db visibility and retention note"
else
  echo "missing: RUNBOOK db visibility and retention note" >&2
  missing=1
fi

if grep -Fq "tool_not_allowed" "RUNBOOK.md" && \
   grep -Fq "high-risk/high-budget or external-send actions" "RUNBOOK.md"; then
  echo "ok: RUNBOOK policy metadata note"
else
  echo "missing: RUNBOOK policy metadata note" >&2
  missing=1
fi

if grep -Fq '"artifact_repository"' "HTTP_EXAMPLES.http" && \
   grep -Fq '"allowed_tools":["artifact_repository"]' "scripts/smoke_api.sh"; then
  echo "ok: task-contract examples align on artifact_repository"
else
  echo "missing: task-contract examples align on artifact_repository" >&2
  missing=1
fi

if grep -Fq "scripts/list_endpoints.sh" "RUNBOOK.md" && \
   grep -Fq "scripts/version_info.sh" "RUNBOOK.md" && \
   grep -Fq "scripts/export_openapi.sh" "RUNBOOK.md" && \
   grep -Fq "scripts/diff_openapi.sh" "RUNBOOK.md" && \
   grep -Fq "scripts/prune_openapi.sh" "RUNBOOK.md"; then
  echo "ok: RUNBOOK endpoint/version/openapi help references"
else
  echo "missing: RUNBOOK endpoint/version/openapi help references" >&2
  missing=1
fi

if grep -Fq "scripts/test_postgres_contracts.sh" "RUNBOOK.md" && \
   grep -Fq "make test-postgres-contracts" "RUNBOOK.md"; then
  echo "ok: RUNBOOK postgres contract test reference"
else
  echo "missing: RUNBOOK postgres contract test reference" >&2
  missing=1
fi

if grep -Fq '`bash scripts/version_info.sh` still prints milestone capability-status counts and release tags from `MILESTONE.json` as delivery history, but EA flagship release claims now come from `EA_FLAGSHIP_TRUTH_PLANE.md`, `EA_FLAGSHIP_RELEASE_GATE.json`, and `EA_FLAGSHIP_RELEASE_GATE.generated.json`.' "RUNBOOK.md"; then
  echo "ok: RUNBOOK version-info EA truth-plane note"
else
  echo "missing: RUNBOOK version-info EA truth-plane note" >&2
  missing=1
fi

if grep -Fq "EA_FLAGSHIP_RELEASE_GATE.generated.json" "RUNBOOK.md" && \
   grep -Fq "scripts/materialize_ea_flagship_release_gate.py" "RUNBOOK.md"; then
  echo "ok: RUNBOOK EA flagship receipt pointer"
else
  echo "missing: RUNBOOK EA flagship receipt pointer" >&2
  missing=1
fi

if grep -Fq "WEEKLY_PRODUCT_PULSE.generated.json" "RUNBOOK.md" && \
   grep -Fq "scripts/materialize_weekly_product_pulse.py" "RUNBOOK.md" && \
   grep -Fq "Refresh the weekly pulse" "RUNBOOK.md"; then
  echo "ok: RUNBOOK EA weekly pulse pointer"
else
  echo "missing: RUNBOOK EA weekly pulse pointer" >&2
  missing=1
fi

if grep -Fq "scripts/smoke_help.sh" "RUNBOOK.md"; then
  echo "ok: RUNBOOK smoke-help reference"
else
  echo "missing: RUNBOOK smoke-help reference" >&2
  missing=1
fi

if grep -Fq "SUPPORT_INCLUDE_DB_VOLUME=0 bash scripts/support_bundle.sh" "RUNBOOK.md" && \
   grep -Fq "live \`ea-db\` mount inspection" "RUNBOOK.md"; then
  echo "ok: RUNBOOK support bundle volume note"
else
  echo "missing: RUNBOOK support bundle volume note" >&2
  missing=1
fi

if grep -Fq "Release ops linkage" "RUNBOOK.md"; then
  echo "ok: RUNBOOK release ops linkage note"
else
  echo "missing: RUNBOOK release ops linkage note" >&2
  missing=1
fi

if grep -Fq \
  "./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight" \
  "RUNBOOK.md"; then
  echo "ok: RUNBOOK authenticated release-preflight reference"
else
  echo "missing: RUNBOOK authenticated release-preflight reference" >&2
  missing=1
fi

if grep -Fq "bash scripts/property_release_gates.sh" \
     "README.md" \
     "RUNBOOK.md" \
     "docs/PROPERTYQUARRY_SLO_RELEASE_EVIDENCE.md"; then
  echo "stale: release guidance bypasses the privileged PropertyQuarry gate shebang" >&2
  missing=1
else
  echo "ok: release guidance preserves the privileged PropertyQuarry gate shebang"
fi

if grep -Fq "bash scripts/verify_release_assets.sh" \
     "RUNBOOK.md" \
     "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"; then
  echo "stale: release guidance bypasses the privileged asset-verifier shebang" >&2
  missing=1
else
  echo "ok: release guidance preserves the privileged asset-verifier shebang"
fi

if grep -Fq "run: bash scripts/propertyquarry_live_release_gates.sh" \
     ".github/workflows/smoke-runtime.yml"; then
  echo "stale: live release workflow bypasses the privileged gate shebang" >&2
  missing=1
elif grep -Fq "run: ./scripts/propertyquarry_live_release_gates.sh" \
       ".github/workflows/smoke-runtime.yml"; then
  echo "ok: live release workflow preserves the privileged gate shebang"
else
  echo "missing: live release workflow direct privileged gate invocation" >&2
  missing=1
fi

if grep -Fq "run: bash scripts/property_release_gates.sh" \
     ".github/workflows/smoke-runtime.yml"; then
  echo "stale: release workflow bypasses the authenticated PropertyQuarry dispatcher" >&2
  missing=1
elif grep -Fq \
       "run: ./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py property-release-gates" \
       ".github/workflows/smoke-runtime.yml"; then
  echo "ok: release workflow uses the authenticated PropertyQuarry dispatcher"
else
  echo "missing: release workflow authenticated PropertyQuarry dispatcher" >&2
  missing=1
fi

if grep -Fq '  - `make verify-flagship-release-readiness`' "RUNBOOK.md" && \
   grep -Fq '  - `make verify-generated-release-artifacts-clean`' "RUNBOOK.md"; then
  echo "ok: RUNBOOK CI gate readiness and generated-clean bullets"
else
  echo "missing: RUNBOOK CI gate readiness or generated-clean bullets" >&2
  missing=1
fi

if grep -Fq "lightweight readiness pass" "RUNBOOK.md"; then
  echo "ok: RUNBOOK all-local vs release-preflight note"
else
  echo "missing: RUNBOOK all-local vs release-preflight note" >&2
  missing=1
fi

if grep -Fq "make docs-verify" "RUNBOOK.md"; then
  echo "ok: RUNBOOK docs-verify alias reference"
else
  echo "missing: RUNBOOK docs-verify alias reference" >&2
  missing=1
fi

if grep -Fq "make release-docs" "RUNBOOK.md"; then
  echo "ok: RUNBOOK release-docs reference"
else
  echo "missing: RUNBOOK release-docs reference" >&2
  missing=1
fi

if grep -Fq "make smoke-postgres-legacy" "RUNBOOK.md"; then
  echo "ok: RUNBOOK legacy postgres smoke reference"
else
  echo "missing: RUNBOOK legacy postgres smoke reference" >&2
  missing=1
fi

if grep -Fq "make ci-gates-postgres-legacy" "RUNBOOK.md"; then
  echo "ok: RUNBOOK legacy postgres parity reference"
else
  echo "missing: RUNBOOK legacy postgres parity reference" >&2
  missing=1
fi

if grep -Fq 'operator summary includes release smoke/readiness commands plus legacy smoke/parity shortcuts, release/support commands' "RUNBOOK.md"; then
  echo "ok: RUNBOOK operator summary shortcut note"
else
  echo "missing: RUNBOOK operator summary shortcut note" >&2
  missing=1
fi

if grep -Fq "pre-smoke documentation/usage pass" "RUNBOOK.md"; then
  echo "ok: RUNBOOK release-docs sequencing note"
else
  echo "missing: RUNBOOK release-docs sequencing note" >&2
  missing=1
fi

if grep -Fq 'RELEASE_CHECKLIST.md` now includes explicit EA flagship truth-plane and release-readiness preflight lines to validate the browser proof, release gate seed, weekly pulse, and Fleet journey gate.' "RUNBOOK.md"; then
  echo "ok: RUNBOOK EA truth-plane linkage note"
else
  echo "missing: RUNBOOK EA truth-plane linkage note" >&2
  missing=1
fi

if grep -Fq 'cannot establish Executive Assistant core eligibility' "FLAGSHIP_CLOSEOUT_PLAN.md"; then
  echo "ok: FLAGSHIP_CLOSEOUT_PLAN standalone PropertyQuarry claim boundary"
else
  echo "missing: FLAGSHIP_CLOSEOUT_PLAN standalone PropertyQuarry claim boundary" >&2
  missing=1
fi

if grep -Fq 'the full PropertyQuarry source, security, recovery, live, and provenance gates pass' "FLAGSHIP_CLOSEOUT_PLAN.md"; then
  echo "ok: FLAGSHIP_CLOSEOUT_PLAN full PropertyQuarry gate note"
else
  echo "missing: FLAGSHIP_CLOSEOUT_PLAN full PropertyQuarry gate note" >&2
  missing=1
fi

if grep -Fq "CI gate bundle" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST CI gate bundle line"
else
  echo "missing: RELEASE_CHECKLIST CI gate bundle line" >&2
  missing=1
fi

if grep -Fq "./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST authenticated release-preflight line"
else
  echo "missing: RELEASE_CHECKLIST authenticated release-preflight line" >&2
  missing=1
fi

if grep -Fq 'make release-preflight' "RELEASE_CHECKLIST.md"; then
  echo "stale: RELEASE_CHECKLIST direct release-preflight Make invocation" >&2
  missing=1
else
  echo "ok: RELEASE_CHECKLIST excludes direct release-preflight Make invocation"
fi

if grep -Fq "make ci-gates" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST ci-gates line"
else
  echo "missing: RELEASE_CHECKLIST ci-gates line" >&2
  missing=1
fi

if grep -Fq "make ci-gates-postgres" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST ci-gates-postgres line"
else
  echo "missing: RELEASE_CHECKLIST ci-gates-postgres line" >&2
  missing=1
fi

if grep -Fq "make ci-gates-postgres-legacy" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST ci-gates-postgres-legacy line"
else
  echo "missing: RELEASE_CHECKLIST ci-gates-postgres-legacy line" >&2
  missing=1
fi

if grep -Fq "make docs-verify" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST docs-verify line"
else
  echo "missing: RELEASE_CHECKLIST docs-verify line" >&2
  missing=1
fi

if grep -Fq "make release-docs" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST release-docs line"
else
  echo "missing: RELEASE_CHECKLIST release-docs line" >&2
  missing=1
fi

if grep -Fq 'Docs parity confirms the EA canon, flagship truth plane, gate seed, and generated receipt are present and the browser proof is still green.' "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST EA truth-plane line"
else
  echo "missing: RELEASE_CHECKLIST EA truth-plane line" >&2
  missing=1
fi

if grep -Fq "EA_FLAGSHIP_RELEASE_GATE.generated.json" "RELEASE_CHECKLIST.md"; then
  echo "ok: RELEASE_CHECKLIST EA flagship receipt line"
else
  echo "missing: RELEASE_CHECKLIST EA flagship receipt line" >&2
  missing=1
fi

if grep -Fq "EA_FLAGSHIP_RELEASE_GATE.generated.json" "PRODUCT_RELEASE_CHECKLIST.md"; then
  echo "ok: PRODUCT_RELEASE_CHECKLIST EA flagship receipt line"
else
  echo "missing: PRODUCT_RELEASE_CHECKLIST EA flagship receipt line" >&2
  missing=1
fi

if grep -Fq \
  "./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py property-release-gates" \
  "PRODUCT_RELEASE_CHECKLIST.md"; then
  echo "ok: PRODUCT_RELEASE_CHECKLIST authenticated PropertyQuarry release gate"
else
  echo "missing: PRODUCT_RELEASE_CHECKLIST authenticated PropertyQuarry release gate" >&2
  missing=1
fi

if grep -Fq "make ci-gates" "CHANGELOG.md"; then
  echo "ok: CHANGELOG ci-gates note"
else
  echo "missing: CHANGELOG ci-gates note" >&2
  missing=1
fi

if grep -Fq "make release-preflight" "CHANGELOG.md"; then
  echo "ok: CHANGELOG release-preflight note"
else
  echo "missing: CHANGELOG release-preflight note" >&2
  missing=1
fi

if grep -Fq "make docs-verify" "CHANGELOG.md"; then
  echo "ok: CHANGELOG docs-verify note"
else
  echo "missing: CHANGELOG docs-verify note" >&2
  missing=1
fi

if grep -Fq "make release-docs" "CHANGELOG.md"; then
  echo "ok: CHANGELOG release-docs note"
else
  echo "missing: CHANGELOG release-docs note" >&2
  missing=1
fi

if grep -Fq "make ci-gates-postgres-legacy" "CHANGELOG.md"; then
  echo "ok: CHANGELOG legacy postgres parity note"
else
  echo "missing: CHANGELOG legacy postgres parity note" >&2
  missing=1
fi

if grep -Fq "Operator summary output now includes legacy Postgres smoke and CI parity shortcuts." "CHANGELOG.md"; then
  echo "ok: CHANGELOG operator summary parity note"
else
  echo "missing: CHANGELOG operator summary parity note" >&2
  missing=1
fi

if grep -Fq "Operator summary output now also surfaces release/support commands" "CHANGELOG.md"; then
  echo "ok: CHANGELOG operator summary release/support note"
else
  echo "missing: CHANGELOG operator summary release/support note" >&2
  missing=1
fi

if grep -Fq "Operator summary output now also includes task-archive shortcuts" "CHANGELOG.md"; then
  echo "ok: CHANGELOG operator summary task-archive note"
else
  echo "missing: CHANGELOG operator summary task-archive note" >&2
  missing=1
fi

if grep -Fq "EA_STORAGE_BACKEND" "CHANGELOG.md" && \
   grep -Fq "deprecated compatibility alias" "CHANGELOG.md"; then
  echo "ok: CHANGELOG backend env deprecation note"
else
  echo "missing: CHANGELOG backend env deprecation note" >&2
  missing=1
fi

if grep -Fq 'Operator summary output now also includes `make release-smoke` and `make all-local`' "CHANGELOG.md"; then
  echo "ok: CHANGELOG operator summary readiness note"
else
  echo "missing: CHANGELOG operator summary readiness note" >&2
  missing=1
fi

if grep -Fq 'Operator summary now exposes a `--help` contract' "CHANGELOG.md"; then
  echo "ok: CHANGELOG operator-summary help-contract note"
else
  echo "missing: CHANGELOG operator-summary help-contract note" >&2
  missing=1
fi

if grep -Fq 'Endpoint, version, and OpenAPI helper scripts now expose `--help` contracts' "CHANGELOG.md"; then
  echo "ok: CHANGELOG endpoint/version/openapi help-contract note"
else
  echo "missing: CHANGELOG endpoint/version/openapi help-contract note" >&2
  missing=1
fi

if grep -Fq '`version_info.sh` now prints milestone capability-status counts and release tags' "CHANGELOG.md"; then
  echo "ok: CHANGELOG version-info milestone summary note"
else
  echo "missing: CHANGELOG version-info milestone summary note" >&2
  missing=1
fi

if grep -Fq 'scripts/smoke_help.sh` now exposes its own `--help` contract' "CHANGELOG.md"; then
  echo "ok: CHANGELOG smoke-help help-contract note"
else
  echo "missing: CHANGELOG smoke-help help-contract note" >&2
  missing=1
fi

if grep -Fq "Milestone metadata now uses \`planned|coded|wired|tested|released\` capability statuses plus CI/docs/release gate tags." "CHANGELOG.md"; then
  echo "ok: CHANGELOG milestone gate-tag note"
else
  echo "missing: CHANGELOG milestone gate-tag note" >&2
  missing=1
fi

if grep -Fq "Release checklist now includes explicit milestone release-tag parity verification." "CHANGELOG.md"; then
  echo "ok: CHANGELOG checklist milestone-tag note"
else
  echo "missing: CHANGELOG checklist milestone-tag note" >&2
  missing=1
fi

if grep -Fq "SUPPORT_INCLUDE_DB_VOLUME" "CHANGELOG.md"; then
  echo "ok: CHANGELOG support bundle volume note"
else
  echo "missing: CHANGELOG support bundle volume note" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "support_bundle_pgdata_attribution")
assert capability["status"] == "released"
PY
then
  if grep -Fq "SUPPORT_INCLUDE_DB_VOLUME=0" "README.md" && \
     grep -Fq "ea-db mount/volume attribution" "README.md" && \
     grep -Fq "ea_pgdata" "README.md" && \
     grep -Fq "/var/lib/postgresql/data" "README.md" && \
     grep -Fq "SUPPORT_INCLUDE_DB_VOLUME=0 bash scripts/support_bundle.sh" "RUNBOOK.md" && \
     grep -Fq "ea_pgdata" "RUNBOOK.md" && \
     grep -Fq "/var/lib/postgresql/data" "RUNBOOK.md" && \
     grep -Fq "support_bundle_pgdata_attribution" "CHANGELOG.md" && \
     grep -Fq "SUPPORT_INCLUDE_DB_VOLUME" "CHANGELOG.md" && \
     grep -Fq 'echo "expected_runtime_volume=ea_pgdata"' "scripts/support_bundle.sh" && \
     grep -Fq 'echo "expected_container_mount=/var/lib/postgresql/data"' "scripts/support_bundle.sh" && \
     grep -Fq 'docker inspect "${DB_CONTAINER}" --format' "scripts/support_bundle.sh"; then
    echo "ok: support bundle pgdata attribution release baseline"
  else
    echo "missing: support bundle pgdata attribution release baseline" >&2
    missing=1
  fi
else
  echo "missing: support bundle pgdata attribution milestone status" >&2
  missing=1
fi

if grep -Fq "Support bundle export now optionally includes DB size snapshots" "CHANGELOG.md" && \
   grep -Fq "Retention operator flow now supports profile presets" "CHANGELOG.md" && \
   grep -Fq "Retention operator flow now supports table allowlist/skip filters" "CHANGELOG.md" && \
   grep -Fq "DB size operator flow now supports schema scoping" "CHANGELOG.md" && \
   grep -Fq "DB size operator flow now supports sort-key selection" "CHANGELOG.md" && \
   grep -Fq "DB size operator flow now supports table-prefix scoping" "CHANGELOG.md" && \
   grep -Fq "DB size operator flow now supports minimum-size filtering" "CHANGELOG.md"; then
  echo "ok: CHANGELOG db visibility and retention note"
else
  echo "missing: CHANGELOG db visibility and retention note" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "operator_db_visibility_and_retention")
assert capability["status"] == "released"
PY
then
  if grep -Fq "EA_DB_SIZE_SCHEMA" "scripts/db_size.sh" && \
     grep -Fq "EA_DB_SIZE_SORT_KEY" "scripts/db_size.sh" && \
     grep -Fq "EA_DB_SIZE_TABLE_PREFIX" "scripts/db_size.sh" && \
     grep -Fq "EA_DB_SIZE_MIN_MB" "scripts/db_size.sh" && \
     grep -Fq "EA_RETENTION_PROFILE" "scripts/db_retention.sh" && \
     grep -Fq "EA_RETENTION_TABLES" "scripts/db_retention.sh" && \
     grep -Fq "EA_RETENTION_SKIP_TABLES" "scripts/db_retention.sh" && \
     grep -Fq "SUPPORT_INCLUDE_DB_SIZE=0|1" "scripts/support_bundle.sh" && \
     grep -Fq "SUPPORT_DB_SIZE_LIMIT=<n>" "scripts/support_bundle.sh" && \
     grep -Fq 'echo "-- db size snapshot --"' "scripts/support_bundle.sh" && \
     grep -Fq 'EA_DB_SIZE_LIMIT="${DB_SIZE_LIMIT}" bash scripts/db_size.sh' "scripts/support_bundle.sh"; then
    echo "ok: operator db visibility and retention release baseline"
  else
    echo "missing: operator db visibility and retention release baseline" >&2
    missing=1
  fi
else
  echo "missing: operator db visibility and retention milestone status" >&2
  missing=1
fi

if grep -Fq "./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ci-gates-authenticated" ".github/workflows/smoke-runtime.yml" && \
   grep -Fq "ci-gates-authenticated:" "Makefile"; then
  echo "ok: smoke-runtime workflow uses authenticated ci-gates"
else
  echo "missing: smoke-runtime workflow ci-gates usage" >&2
  missing=1
fi

if grep -Fq "python -m playwright install --with-deps chromium" ".github/workflows/smoke-runtime.yml"; then
  echo "ok: smoke-runtime workflow installs playwright browsers for real-browser gates"
else
  echo "missing: smoke-runtime workflow playwright browser install" >&2
  missing=1
fi

if grep -Fq "scripts/smoke_postgres.sh" ".github/workflows/smoke-runtime.yml"; then
  echo "ok: smoke-runtime workflow includes postgres smoke job"
else
  echo "missing: smoke-runtime workflow postgres smoke job" >&2
  missing=1
fi

if grep -Fq "scripts/test_postgres_contracts.sh" ".github/workflows/smoke-runtime.yml"; then
  echo "ok: smoke-runtime workflow includes postgres contract job"
else
  echo "missing: smoke-runtime workflow postgres contract job" >&2
  missing=1
fi

if grep -Fq -- "--legacy-fixture" ".github/workflows/smoke-runtime.yml"; then
  echo "ok: smoke-runtime workflow includes legacy migration smoke job"
else
  echo "missing: smoke-runtime workflow legacy migration smoke job" >&2
  missing=1
fi

if grep -Fq "make smoke-postgres-legacy" "scripts/operator_summary.sh" && \
   grep -Fq "Usage:" "scripts/operator_summary.sh" && \
   grep -Fq "make release-smoke" "scripts/operator_summary.sh" && \
   grep -Fq "make test-postgres-contracts" "scripts/operator_summary.sh" && \
   grep -Fq "make all-local" "scripts/operator_summary.sh" && \
   grep -Fq "make ci-gates-postgres-legacy" "scripts/operator_summary.sh" && \
   grep -Fq "make provider-readiness" "scripts/operator_summary.sh" && \
   grep -Fq "make verify-flagship-release-readiness" "scripts/operator_summary.sh" && \
   grep -Fq "./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight" "scripts/operator_summary.sh" && \
   grep -Fq "make support-bundle" "scripts/operator_summary.sh" && \
   grep -Fq "make tasks-archive" "scripts/operator_summary.sh" && \
   grep -Fq "make tasks-archive-dry-run" "scripts/operator_summary.sh" && \
   grep -Fq "make tasks-archive-prune" "scripts/operator_summary.sh"; then
  echo "ok: operator-summary includes help, readiness, legacy postgres, release/support, and task-archive shortcuts"
else
  echo "missing: operator-summary help, readiness, legacy postgres, release/support, and task-archive shortcuts" >&2
  missing=1
fi

if grep -Fq "scripts/operator_summary.sh" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/operator_summary.sh" "Makefile"; then
  echo "ok: operator-summary included in help-smoke and operator-help surfaces"
else
  echo "missing: operator-summary help-smoke/operator-help wiring" >&2
  missing=1
fi

if grep -Fq "Usage:" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/smoke_help.sh" "Makefile"; then
  echo "ok: smoke-help includes help contract and operator-help wiring"
else
  echo "missing: smoke-help help contract/operator-help wiring" >&2
  missing=1
fi

if grep -Fq "scripts/list_endpoints.sh" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/version_info.sh" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/test_postgres_contracts.sh" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/export_openapi.sh" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/diff_openapi.sh" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/prune_openapi.sh" "scripts/smoke_help.sh" && \
   grep -Fq "scripts/list_endpoints.sh" "Makefile" && \
   grep -Fq "scripts/version_info.sh" "Makefile" && \
   grep -Fq "scripts/test_postgres_contracts.sh" "Makefile" && \
   grep -Fq "scripts/export_openapi.sh" "Makefile" && \
   grep -Fq "scripts/diff_openapi.sh" "Makefile" && \
   grep -Fq "scripts/prune_openapi.sh" "Makefile"; then
  echo "ok: endpoint/version/openapi scripts included in help-smoke and operator-help surfaces"
else
  echo "missing: endpoint/version/openapi help-smoke/operator-help wiring" >&2
  missing=1
fi

if grep -Fq "tests/test_postgres_contract_matrix_integration.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_generic_async_dependency_projection_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_memory_router_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_openapi_async_acceptance_examples_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_openapi_dependency_examples_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_plan_scope_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_principal_fallback_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_rewrite_scope_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_rewrite_api_scope_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_rewrite_dependency_projection_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_step_parent_projection_contracts.py" "scripts/test_postgres_contracts.sh" && \
   grep -Fq "tests/test_tool_execution.py" "scripts/test_postgres_contracts.sh"; then
  echo "ok: postgres contract script covers focused router and rewrite scope invariants"
else
  echo "missing: postgres contract script focused invariant coverage" >&2
  missing=1
fi

if grep -Fq "exports OpenAPI and verifies paused session-step dependency examples" "scripts/smoke_postgres.sh" && \
   grep -Fq -- "--force-recreate ea-api" "scripts/smoke_postgres.sh" && \
   grep -Fq "bash scripts/export_openapi.sh" "scripts/smoke_postgres.sh" && \
   grep -Fq "step-artifact-save-waiting-approval" "scripts/smoke_postgres.sh" && \
   grep -Fq "step-artifact-save-blocked-human" "scripts/smoke_postgres.sh" && \
   grep -Fq "openapi export ok" "scripts/smoke_postgres.sh"; then
  echo "ok: postgres smoke exports openapi dependency examples"
else
  echo "missing: postgres smoke exports openapi dependency examples" >&2
  missing=1
fi

if grep -Fq "dependency_keys: list[str]" "ea/app/api/routes/rewrite.py" && \
   grep -Fq "dependency_states: dict[str, str]" "ea/app/api/routes/rewrite.py" && \
   grep -Fq "dependency_step_ids: dict[str, str]" "ea/app/api/routes/rewrite.py" && \
   grep -Fq "blocked_dependency_keys: list[str]" "ea/app/api/routes/rewrite.py" && \
   grep -Fq "dependencies_satisfied: bool" "ea/app/api/routes/rewrite.py" && \
   grep -Fq "Current state for each declared dependency key. Paused approval-backed sessions keep completed " "ea/app/api/routes/rewrite.py" && \
   grep -Fq 'This can still be true for a `waiting_approval` step, ' "ea/app/api/routes/rewrite.py" && \
   grep -Fq '"step_id": "step-artifact-save-waiting-approval"' "ea/app/api/routes/rewrite.py" && \
   grep -Fq '"step_id": "step-artifact-save-blocked-human"' "ea/app/api/routes/rewrite.py" && \
   grep -Fq "_step_dependency_projection(" "ea/app/api/routes/rewrite.py" && \
   grep -Fq "step_policy_evaluate" "tests/test_rewrite_dependency_projection_contracts.py" && \
   grep -Fq '["step_policy_evaluate"]' "tests/test_rewrite_dependency_projection_contracts.py" && \
   grep -Fq '"dependency_states"] == {"step_policy_evaluate": "completed"}' "tests/test_rewrite_dependency_projection_contracts.py" && \
   grep -Fq 'steps["step_artifact_save"]["state"] == "waiting_approval"' "tests/test_rewrite_dependency_projection_contracts.py" && \
   grep -Fq 'steps["step_artifact_save"]["blocked_dependency_keys"] == ["step_human_review"]' "tests/test_rewrite_dependency_projection_contracts.py" && \
   grep -Fq 'steps_by_key["step_policy_evaluate"]["dependency_states"] == {"step_input_prepare": "completed"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'steps_by_key["step_artifact_save"]["dependency_states"] == {"step_policy_evaluate": "completed"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'approval_steps["step_artifact_save"]["state"] == "waiting_approval"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'review_steps["step_artifact_save"]["blocked_dependency_keys"] == ["step_human_review"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'generic_approval_steps["step_artifact_save"]["state"] == "waiting_approval"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'generic_review_steps["step_artifact_save"]["blocked_dependency_keys"] == ["step_human_review"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'step-artifact-save-waiting-approval' "tests/test_openapi_dependency_examples_contracts.py" && \
   grep -Fq 'step-artifact-save-blocked-human' "tests/test_openapi_dependency_examples_contracts.py" && \
   grep -Fq 'schemas["RewriteAcceptedOut"]["examples"]' "tests/test_openapi_async_acceptance_examples_contracts.py" && \
   grep -Fq 'schemas["PlanExecuteAcceptedOut"]["examples"]' "tests/test_openapi_async_acceptance_examples_contracts.py" && \
   grep -Fq "/v1/plans/compile" "tests/test_plan_scope_contracts.py" && \
   grep -Fq "/v1/plans/execute" "tests/test_plan_scope_contracts.py" && \
   grep -Fq "/v1/rewrite/sessions/" "tests/test_plan_scope_contracts.py" && \
   grep -Fq "/v1/rewrite/artifacts/" "tests/test_plan_scope_contracts.py" && \
   grep -Fq "/v1/rewrite/receipts/" "tests/test_plan_scope_contracts.py" && \
   grep -Fq "/v1/rewrite/run-costs/" "tests/test_plan_scope_contracts.py" && \
   grep -Fq 'principal_scope_mismatch' "tests/test_plan_scope_contracts.py" && \
   grep -Fq 'principal_id_required' "tests/test_principal_fallback_contracts.py" && \
   grep -Fq 'planner.build_plan' "tests/test_principal_fallback_contracts.py" && \
   grep -Fq 'orchestrator.build_artifact' "tests/test_principal_fallback_contracts.py" && \
   grep -Fq 'orchestrator.execute_task_artifact' "tests/test_principal_fallback_contracts.py" && \
   grep -Fq 'service.compile_rewrite_intent' "tests/test_principal_fallback_contracts.py" && \
   grep -Fq 'principal_id' "ea/app/api/routes/rewrite.py" && \
   grep -Fq 'principal_id' "ea/app/api/routes/plans.py" && \
   grep -Fq 'principal_id' "ea/app/repositories/artifacts_postgres.py" && \
   grep -Fq 'principal_id' "tests/test_artifacts_postgres_integration.py" && \
   grep -Fq 'principal_id' "tests/test_rewrite_scope_contracts.py" && \
   grep -Fq 'principal_id' "tests/test_rewrite_api_scope_contracts.py" && \
   grep -Fq 'principal_id' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq "projection_ok=(" "scripts/smoke_api.sh" && \
   grep -Fq 'curl -fsS "${BASE}/openapi.json"' "scripts/smoke_api.sh" && \
   grep -Fq "rewrite_examples=(schemas.get('RewriteAcceptedOut') or {}).get('examples') or []" "scripts/smoke_api.sh" && \
   grep -Fq "plan_examples=(schemas.get('PlanExecuteAcceptedOut') or {}).get('examples') or []" "scripts/smoke_api.sh" && \
   grep -Fq "save_step.get('state',''), policy_step.get('dependency_states') == {'step_input_prepare': 'completed'}" "scripts/smoke_api.sh" && \
   grep -Fq "save_step.get('blocked_dependency_keys') == ['step_human_review']" "scripts/smoke_api.sh" && \
   grep -Fq "policy_step.get('parent_step_id') == input_id" "scripts/smoke_api.sh" && \
   grep -Fq "save_step.get('parent_step_id') == policy_id" "scripts/smoke_api.sh" && \
   grep -Fq 'steps_by_key["step_policy_evaluate"]["parent_step_id"] == steps_by_key["step_input_prepare"]["step_id"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'steps_by_key["step_artifact_save"]["parent_step_id"] == steps_by_key["step_policy_evaluate"]["step_id"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
   grep -Fq 'save_step.parent_step_id is None' "tests/test_step_parent_projection_contracts.py" && \
   grep -Fq 'sidecar_step.parent_step_id == input_step.step_id' "tests/test_step_parent_projection_contracts.py" && \
   grep -Fq "first.get('principal_id','')" "scripts/smoke_api.sh" && \
   grep -Fq "approval-123|human-task-123|poll_or_subscribe|poll_or_subscribe|poll_or_subscribe|decision_brief_approval|stakeholder_briefing_review|rewrite_retry_delayed" "scripts/smoke_api.sh" && \
   grep -Fq 'GENERIC_APPROVAL_TASK_KEY="decision_brief_approval_${SMOKE_RUN_TOKEN}"' "scripts/smoke_api.sh" && \
   grep -Fq '${GENERIC_APPROVAL_TASK_KEY}|awaiting_approval|waiting_approval|True|True|True|True|True' "scripts/smoke_api.sh" && \
   grep -Fq "stakeholder_briefing_review|awaiting_human|waiting_human|True|True|True|True|queued|True|True|True" "scripts/smoke_api.sh"; then
  echo "ok: session step dependency projection contract and smoke coverage"
else
  echo "missing: session step dependency projection contract and smoke coverage" >&2
  missing=1
fi

if grep -Fq "explicit \`principal_id\` ownership" "README.md" && \
   grep -Fq "explicit \`principal_id\` ownership" "RUNBOOK.md" && \
   grep -Fq "principal_id ownership" "HTTP_EXAMPLES.http" && \
   grep -Fq "artifact_principal_ownership_projection" "MILESTONE.json" && \
   grep -Fq "explicit \`principal_id\` ownership" "CHANGELOG.md"; then
  echo "ok: artifact principal ownership docs and milestone coverage"
else
  echo "missing: artifact principal ownership docs and milestone coverage" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "single_dependency_parent_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq "multi-prerequisite join steps stay parentless" "README.md" && \
     grep -Fq "multi-prerequisite join steps stay parentless" "RUNBOOK.md" && \
     grep -Fq "parent_step_id\` only from actual single-dependency edges" "CHANGELOG.md"; then
    echo "ok: single-dependency parent projection docs and milestone release coverage"
  else
    echo "missing: single-dependency parent projection docs and milestone release coverage" >&2
    missing=1
  fi
else
  echo "missing: single-dependency parent projection milestone release status" >&2
  missing=1
fi

if grep -Fq '"status_model"' "MILESTONE.json" && \
   grep -Fq '"release_tags"' "MILESTONE.json" && \
   grep -Fq '"planned"' "MILESTONE.json" && \
   grep -Fq '"coded"' "MILESTONE.json" && \
   grep -Fq '"wired"' "MILESTONE.json" && \
   grep -Fq '"tested"' "MILESTONE.json" && \
   grep -Fq '"released"' "MILESTONE.json" && \
   grep -Fq '"ci_gate_bundle"' "MILESTONE.json" && \
   grep -Fq '"release_preflight_bundle"' "MILESTONE.json" && \
   grep -Fq '"docs_verify_alias"' "MILESTONE.json" && \
   grep -Fq '"postgres_legacy_fixture_smoke"' "MILESTONE.json" && \
   grep -Fq '"ci_postgres_legacy_smoke_job"' "MILESTONE.json" && \
   grep -Fq '"ci_gates_postgres_legacy_local_target"' "MILESTONE.json"; then
  echo "ok: MILESTONE status model and release tags"
else
  echo "missing: MILESTONE status model and release tags" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "smoke_and_release_gate_bundle")
assert capability["status"] == "released"
PY
then
  echo "ok: smoke and release gate bundle milestone release status"
else
  echo "missing: smoke and release gate bundle milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "principal_scoped_memory_seed_apis")
assert capability["status"] == "released"
PY
then
  if grep -Fq "/v1/memory/candidates" "README.md" && \
     grep -Fq "/v1/memory/stakeholders" "README.md" && \
     grep -Fq "/v1/memory/interruption-budgets" "README.md" && \
     grep -Fq "/v1/memory/candidates" "RUNBOOK.md" && \
     grep -Fq "/v1/memory/stakeholders" "RUNBOOK.md" && \
     grep -Fq "/v1/memory/interruption-budgets" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `principal_scoped_memory_seed_apis` to released' "CHANGELOG.md" && \
     grep -Fq "/v1/memory/candidates" "scripts/smoke_api.sh" && \
     grep -Fq "/v1/memory/stakeholders" "scripts/smoke_api.sh" && \
     grep -Fq "/v1/memory/interruption-budgets" "scripts/smoke_api.sh"; then
    echo "ok: principal-scoped memory seed API coverage"
  else
    echo "missing: principal-scoped memory seed API coverage" >&2
    missing=1
  fi
else
  echo "missing: principal-scoped memory seed API milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "principal_request_context_guardrails")
assert capability["status"] == "released"
assert milestone["env_contract"]["EA_DEFAULT_PRINCIPAL_ID"]
PY
then
  if grep -Fq "X-EA-Principal-ID" "README.md" && \
     grep -Fq "EA_DEFAULT_PRINCIPAL_ID" "README.md" && \
     grep -Fq "principal_scope_mismatch" "README.md" && \
     grep -Fq "X-EA-Principal-ID" "RUNBOOK.md" && \
     grep -Fq "EA_DEFAULT_PRINCIPAL_ID" "RUNBOOK.md" && \
     grep -Fq "principal_scope_mismatch" "RUNBOOK.md" && \
     grep -Fq "EA_DEFAULT_PRINCIPAL_ID" "ENVIRONMENT_MATRIX.md" && \
     grep -Fq "X-EA-Principal-ID" "HTTP_EXAMPLES.http" && \
     grep -Fq "principal_scope_mismatch" "HTTP_EXAMPLES.http" && \
     grep -Fq "X-EA-Principal-ID" "scripts/smoke_api.sh" && \
     grep -Fq "principal_scope_mismatch" "scripts/smoke_api.sh" && \
     grep -Fq "principal_request_context_guardrails" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "X-EA-Principal-ID" "CHANGELOG.md"; then
    echo "ok: principal request-context guardrails release baseline"
  else
    echo "missing: principal request-context guardrails release baseline" >&2
    missing=1
  fi
else
  echo "missing: principal request-context guardrails milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "principal_scoped_rewrite_and_plan_routes")
assert capability["status"] == "released"
PY
then
  if grep -Fq "rewrite/session/artifact/receipt/run-cost, plan-compile/execute" "README.md" && \
     grep -Fq '/v1/rewrite/sessions/{session_id}' "RUNBOOK.md" && \
     grep -Fq '/v1/plans/compile' "RUNBOOK.md" && \
     grep -Fq '"principal_id": "exec-2"' "HTTP_EXAMPLES.http" && \
     grep -Fq "REWRITE_SESSION_MISMATCH_CODE" "scripts/smoke_api.sh" && \
     grep -Fq "PLAN_MISMATCH_CODE" "scripts/smoke_api.sh" && \
     grep -Fq "test_rewrite_routes_enforce_principal_scope" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_plan_compile_derives_request_principal_and_rejects_mismatch" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: principal-scoped rewrite and plan routes docs"
  else
    echo "missing: principal-scoped rewrite and plan routes docs" >&2
    missing=1
  fi
else
  echo "missing: principal-scoped rewrite and plan routes milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_principal_scoped_human_task_routes")
assert capability["status"] == "released"
PY
then
  if grep -Fq "session-bound human task create/list requests now also enforce the linked execution session principal" "README.md" && \
     grep -Fq "GET /v1/human/tasks?session_id=..." "RUNBOOK.md" && \
     grep -Fq "HUMAN_CREATE_MISMATCH_CODE" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_SESSION_LIST_MISMATCH_CODE" "scripts/smoke_api.sh" && \
     grep -Fq "test_human_task_session_routes_enforce_session_principal_scope" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: session principal-scoped human task routes docs"
  else
    echo "missing: session principal-scoped human task routes docs" >&2
    missing=1
  fi
else
  echo "missing: session principal-scoped human task routes milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "generic_task_execution_runtime")
assert capability["status"] == "released"
PY
then
  if grep -Fq '/v1/plans/execute' "README.md" && \
     grep -Fq 'non-`rewrite_text` artifact flows' "README.md" && \
     grep -Fq 'structured `input_json` plus `context_refs`' "README.md" && \
     grep -Fq '/v1/plans/execute' "RUNBOOK.md" && \
     grep -Fq 'stakeholder briefings' "RUNBOOK.md" && \
     grep -Fq 'structured `input_json` plus `context_refs`' "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `generic_task_execution_runtime` to released' "CHANGELOG.md" && \
     grep -Fq 'POST {{host}}/v1/plans/execute' "HTTP_EXAMPLES.http" && \
     grep -Fq '"input_json": {' "HTTP_EXAMPLES.http" && \
     grep -Fq '"context_refs": [' "HTTP_EXAMPLES.http" && \
     grep -Fq 'TASK_EXECUTE_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'context_refs' "scripts/smoke_api.sh" && \
     grep -Fq 'test_plan_execute_input_contracts.py' "scripts/test_postgres_contracts.sh" && \
     grep -Fq 'test_generic_task_execution_uses_compiled_contract_runtime' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'test_plan_execute_accepts_structured_input_json_and_context_refs' "tests/test_plan_execute_input_contracts.py" && \
     grep -Fq 'test_postgres_orchestrator_executes_non_rewrite_task_contract' "tests/test_postgres_contract_matrix_integration.py"; then
    echo "ok: generic task execution runtime docs"
  else
    echo "missing: generic task execution runtime docs" >&2
    missing=1
  fi
else
  echo "missing: generic task execution runtime milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "memory_reasoning_context_packs")
assert capability["status"] == "released"
PY
then
  if grep -Fq '/v1/memory/context-pack' "README.md" && \
     grep -Fq 'injects synthesized `context_pack` payloads from principal-scoped memory reasoning' "README.md" && \
     grep -Fq '/v1/memory/context-pack' "RUNBOOK.md" && \
     grep -Fq 'including promoted-memory signals, conflict rows, commitment-risk rows, and unresolved refs' "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `memory_reasoning_context_packs` to released' "CHANGELOG.md" && \
     grep -Fq 'test_memory_context_pack_route_returns_reasoned_pack' "tests/test_plan_execute_input_contracts.py" && \
     grep -Fq 'test_plan_execute_accepts_structured_input_json_and_context_refs' "tests/test_plan_execute_input_contracts.py" && \
     grep -Fq 'release/operator guards now pin that docs plus runtime contract baseline behavior' "MILESTONE.json"; then
    echo "ok: memory reasoning context-pack docs and contract coverage"
  else
    echo "missing: memory reasoning context-pack docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: memory reasoning context-pack milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "plan_graph_validation")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'validates duplicate step keys, unknown dependency keys, and dependency cycles before queue execution starts' "README.md" && \
     grep -Fq 'duplicate step keys, unknown dependency keys, and dependency cycles before any session rows are started' "RUNBOOK.md" && \
     grep -Fq 'duplicate step keys, unknown dependency keys, and dependency cycles before queue execution or session creation begins' "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `plan_graph_validation` to released' "CHANGELOG.md" && \
     grep -Fq 'tests/test_plan_graph_validation_contracts.py' "scripts/test_postgres_contracts.sh" && \
     grep -Fq 'test_validate_plan_spec_rejects_unknown_dependency_keys' "tests/test_plan_graph_validation_contracts.py" && \
     grep -Fq 'validate_plan_spec(plan)' "ea/app/services/planner.py" && \
     grep -Fq 'validate_plan_spec(plan)' "ea/app/services/execution_task_orchestration_service.py"; then
    echo "ok: plan graph validation docs"
  else
    echo "missing: plan graph validation docs" >&2
    missing=1
  fi
else
  echo "missing: plan graph validation milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "step_io_contract_enforcement")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'only merges declared dependency inputs and validates declared step outputs before completion' "README.md" && \
     grep -Fq 'only merges declared dependency inputs and fails missing declared outputs before a step can complete' "RUNBOOK.md" && \
     grep -Fq 'only merge declared dependency inputs and now fail fast when a completed step omits any declared output key' "CHANGELOG.md" && \
     grep -Fq 'tests/test_step_io_contracts.py' "scripts/test_postgres_contracts.sh" && \
     grep -Fq 'test_merged_step_input_json_filters_dependency_outputs_to_declared_input_keys' "tests/test_step_io_contracts.py" && \
     grep -Fq '_validate_step_input_contract' "ea/app/services/orchestrator.py" && \
     grep -Fq '_validate_step_output_contract' "ea/app/services/orchestrator.py" && \
     grep -Fq 'Promoted milestone capability `step_io_contract_enforcement` to released' "CHANGELOG.md" && \
     grep -Fq 'release/operator guards now pin those runtime IO contracts' "MILESTONE.json"; then
    echo "ok: step io contract release baseline"
  else
    echo "missing: step io contract release baseline" >&2
    missing=1
  fi
else
  echo "missing: step io contract milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "generic_task_execution_async_contracts")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'same first-class `202 awaiting_approval` and `202 awaiting_human` async contract' "README.md" && \
     grep -Fq 'step_artifact_save.state=waiting_approval' "README.md" && \
     grep -Fq 'blocked_dependency_keys=["step_human_review"]' "README.md" && \
     grep -Fq 'same first-class `202 awaiting_approval` and `202 awaiting_human` workflow contract' "RUNBOOK.md" && \
     grep -Fq 'step_artifact_save` in `waiting_approval`' "RUNBOOK.md" && \
     grep -Fq 'blocked_dependency_keys=["step_human_review"]' "RUNBOOK.md" && \
     grep -Fq '"task_key": "decision_brief_approval"' "HTTP_EXAMPLES.http" && \
     grep -Fq '"task_key": "stakeholder_briefing_review"' "HTTP_EXAMPLES.http" && \
     grep -Fq 'inspect paused approval-backed session dependency projection' "HTTP_EXAMPLES.http" && \
     grep -Fq 'inspect paused human-review-backed session dependency projection' "HTTP_EXAMPLES.http" && \
     grep -Fq 'GENERIC_APPROVAL_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'GENERIC_HUMAN_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'test_generic_task_execution_supports_async_approval_and_human_contracts' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'Promoted milestone capability `generic_task_execution_async_contracts` to released' "CHANGELOG.md" && \
     grep -Fq 'shared async workflow contract' "CHANGELOG.md" && \
     grep -Fq 'release/operator guards now pin' "CHANGELOG.md"; then
    echo "ok: generic task execution async contracts release baseline"
  else
    echo "missing: generic task execution async contracts release baseline" >&2
    missing=1
  fi
else
  echo "missing: generic task execution async contracts milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_lookup_task_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'originating task key and deliverable type' "README.md" && \
     grep -Fq 'originating `task_key`/`deliverable_type`' "RUNBOOK.md" && \
     grep -Fq 'includes originating task_key and deliverable_type' "HTTP_EXAMPLES.http" && \
     grep -Fq 'TASK_EXECUTE_ARTIFACT_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'TASK_EXECUTE_ARTIFACT_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'fetched_artifact.json()["task_key"] == "stakeholder_briefing"' "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: artifact lookup task identity projection docs"
  else
    echo "missing: artifact lookup task identity projection docs" >&2
    missing=1
  fi
else
  echo "missing: artifact lookup task identity projection milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_preview_handle_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'preview_text' "README.md" && \
     grep -Fq 'storage_handle' "README.md" && \
     grep -Fq 'preview_text' "RUNBOOK.md" && \
     grep -Fq 'storage_handle' "RUNBOOK.md" && \
     grep -Fq 'preview_text and storage_handle' "HTTP_EXAMPLES.http" && \
     grep -Fq 'Promoted milestone capability `artifact_preview_handle_projection` to released' "CHANGELOG.md" && \
     grep -Fq 'REWRITE_ARTIFACT_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'TASK_EXECUTE_ARTIFACT_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'fetched_artifact.json()["preview_text"] == "Board context and stakeholder sensitivities."' "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: artifact preview/handle projection docs"
  else
    echo "missing: artifact preview/handle projection docs" >&2
    missing=1
  fi
else
  echo "missing: artifact preview/handle projection milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "proof_lookup_task_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'direct execution proof records' "README.md" && \
     grep -Fq 'originating `task_key`/`deliverable_type`' "RUNBOOK.md" && \
     grep -Fq 'fetch receipt (includes originating task_key and deliverable_type)' "HTTP_EXAMPLES.http" && \
     grep -Fq 'fetch run cost (includes originating task_key and deliverable_type)' "HTTP_EXAMPLES.http" && \
     grep -Fq 'TASK_EXECUTE_RECEIPT_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'TASK_EXECUTE_COST_JSON' "scripts/smoke_api.sh" && \
     grep -Fq 'fetched_receipt.json()["task_key"] == "stakeholder_briefing"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'fetched_cost.json()["task_key"] == "stakeholder_briefing"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'Promoted milestone capability `proof_lookup_task_identity_projection` to released' "CHANGELOG.md" && \
     grep -Fq 'release/operator guards now pin' "CHANGELOG.md" && \
     grep -Fq 'direct receipt and run-cost `task_key` plus `deliverable_type` lookup projection' "CHANGELOG.md"; then
    echo "ok: proof lookup task identity projection release baseline"
  else
    echo "missing: proof lookup task identity projection release baseline" >&2
    missing=1
  fi
else
  echo "missing: proof lookup task identity projection milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_artifact_task_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'inline artifact/proof rows now carry originating task identity' "README.md" && \
     grep -Fq 'self-describing artifact/proof task identity' "RUNBOOK.md" && \
     grep -Fq 'TASK_EXECUTE_SESSION_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'stakeholder_briefing|stakeholder_briefing|stakeholder_briefing' "scripts/smoke_api.sh" && \
     grep -Fq 'session_body["artifacts"][0]["task_key"] == "stakeholder_briefing"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'Promoted milestone capability `session_artifact_task_identity_projection` to released' "CHANGELOG.md"; then
    echo "ok: session artifact task identity projection docs"
  else
    echo "missing: session artifact task identity projection docs" >&2
    missing=1
  fi
else
  echo "missing: session artifact task identity projection milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "async_queue_projection_task_identity")
assert "release/operator guards now pin that self-describing async queue identity contract" in capability["notes"]
assert capability["status"] == "released"
PY
then
  if grep -Fq 'approval projections now carry the originating task identity' "README.md" && \
     grep -Fq 'queue/detail payloads now also carry the originating task identity' "README.md" && \
     grep -Fq 'Approval and human-task queue/detail payloads now stay self-describing' "RUNBOOK.md" && \
     grep -Fq 'Approvals -> pending (includes originating task_key and deliverable_type)' "HTTP_EXAMPLES.http" && \
     grep -Fq 'Human tasks -> direct detail (includes originating task_key and deliverable_type)' "HTTP_EXAMPLES.http" && \
     grep -Fq 'GENERIC_APPROVAL_PENDING_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'GENERIC_APPROVAL_HISTORY_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'GENERIC_HUMAN_LIST_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'pending_row["task_key"] == "decision_brief_approval"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'review_detail.json()["task_key"] == "stakeholder_briefing_review"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "release/operator guards now pin that self-describing async queue identity contract" "CHANGELOG.md"; then
    echo "ok: async queue projection task identity docs"
  else
    echo "missing: async queue projection task identity docs" >&2
    missing=1
  fi
else
  echo "missing: async queue projection task identity milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_history_task_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'assignment-history` exposes task-scoped ownership transitions, now carries originating task identity too' "README.md" && \
     grep -Fq 'those direct history rows now also carry originating `task_key`/`deliverable_type`' "RUNBOOK.md" && \
     grep -Fq 'assignment history (includes originating task_key and deliverable_type)' "HTTP_EXAMPLES.http" && \
     grep -Fq 'GENERIC_HUMAN_HISTORY_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'review_history.json()[0]["task_key"] == "stakeholder_briefing_review"' "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: human task assignment-history task identity docs"
  else
    echo "missing: human task assignment-history task identity docs" >&2
    missing=1
  fi
else
  echo "missing: human task assignment-history task identity milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_human_task_assignment_history_task_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'inline human-task assignment-history rows now carry originating task identity' "README.md" && \
     grep -Fq 'assignment-history rows now also carry originating `task_key`/`deliverable_type`' "RUNBOOK.md" && \
     grep -Fq 'human-task assignment-history rows include originating task_key and deliverable_type' "HTTP_EXAMPLES.http" && \
     grep -Fq 'GENERIC_HUMAN_SESSION_HISTORY_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'review_session_body["human_task_assignment_history"][0]["task_key"] == "stakeholder_briefing_review"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'Promoted milestone capability `session_human_task_assignment_history_task_identity_projection` to released' "CHANGELOG.md" && \
     grep -Fq 'release/operator guards now pin' "CHANGELOG.md" && \
     grep -Fq 'inline session `human_task_assignment_history[]` `task_key` and `deliverable_type` projection' "CHANGELOG.md"; then
    echo "ok: session human task assignment-history task identity release baseline"
  else
    echo "missing: session human task assignment-history task identity release baseline" >&2
    missing=1
  fi
else
  echo "missing: session human task assignment-history task identity milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_human_task_packet_task_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'inline human-task packet rows now carry originating task identity' "README.md" && \
     grep -Fq 'inline `human_tasks` rows now also carry originating `task_key`/`deliverable_type`' "RUNBOOK.md" && \
     grep -Fq 'human-task packet, and human-task assignment-history rows include originating task_key and deliverable_type' "HTTP_EXAMPLES.http" && \
     grep -Fq 'GENERIC_HUMAN_SESSION_TASK_FIELDS' "scripts/smoke_api.sh" && \
     grep -Fq 'review_session_body["human_tasks"][0]["task_key"] == "stakeholder_briefing_review"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'Promoted milestone capability `session_human_task_packet_task_identity_projection` to released' "CHANGELOG.md"; then
    echo "ok: session human task packet task identity docs"
  else
    echo "missing: session human task packet task identity docs" >&2
    missing=1
  fi
else
  echo "missing: session human task packet task identity milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_principal_scoped_human_task_routes")
assert capability["status"] == "released"
PY
then
  if grep -Fq "session-bound human task create/list requests now also enforce the linked execution session principal" "README.md" && \
     grep -Fq 'GET /v1/human/tasks?session_id=...' "RUNBOOK.md" && \
     grep -Fq "HUMAN_CREATE_MISMATCH_CODE" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_SESSION_LIST_MISMATCH_CODE" "scripts/smoke_api.sh" && \
     grep -Fq "test_human_task_session_routes_enforce_session_principal_scope" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: session-principal-scoped human task routes docs"
  else
    echo "missing: session-principal-scoped human task routes docs" >&2
    missing=1
  fi
else
  echo "missing: session-principal-scoped human task routes milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "dependency_aware_execution_scheduler")
assert capability["status"] == "released"
PY
then
if grep -Fq "queue advancement now enqueues every currently ready step from satisfied dependency edges" "README.md" && \
   grep -Fq "queue advancement now enqueues every currently ready step from satisfied dependency edges" "RUNBOOK.md" && \
   grep -Fq 'Queue advancement now enqueues the full ready set from satisfied `depends_on` edges' "CHANGELOG.md" && \
   grep -Fq 'Promoted milestone capability `dependency_aware_execution_scheduler` to released' "CHANGELOG.md" && \
   grep -Fq "test_postgres_orchestrator_dependency_scheduler_waits_for_all_dependencies" "tests/test_postgres_contract_matrix_integration.py" && \
   grep -Fq "test_postgres_queue_leasing_skips_paused_sessions_even_with_ready_items" "tests/test_postgres_contract_matrix_integration.py"; then
  echo "ok: dependency-aware execution scheduler release baseline"
else
  echo "missing: dependency-aware execution scheduler release baseline" >&2
    missing=1
  fi
else
  echo "missing: dependency-aware execution scheduler milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "queued_policy_step_audit_truthfulness")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'policy_decision` is now recorded by the queued `step_policy_evaluate` handler after `input_prepared`' "README.md" && \
     grep -Fq 'policy_decision` is now emitted from the queued `step_policy_evaluate` handler after `input_prepared`' "RUNBOOK.md" && \
     grep -Fq 'Policy decisions are now recorded from the queued `step_policy_evaluate` handler after `input_prepared`' "CHANGELOG.md" && \
     grep -Fq "queued_policy_step_audit_truthfulness" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "order_ok" "scripts/smoke_api.sh" && \
     grep -Fq 'policy_decision' "scripts/smoke_api.sh" && \
     grep -Fq 'event_names.index("input_prepared") < event_names.index("policy_decision")' "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: queued policy-step audit truthfulness release baseline"
  else
    echo "missing: queued policy-step audit truthfulness release baseline" >&2
    missing=1
  fi
else
  echo "missing: queued policy-step audit truthfulness milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_dependency_input_merge")
assert capability["status"] == "released"
PY
then
  if grep -Fq "compiled human-review steps now merge dependency outputs into the created packet input" "README.md" && \
     grep -Fq "queued human-review step now also merges dependency outputs into the packet input" "RUNBOOK.md" && \
     grep -Fq "Human-review step execution now merges dependency outputs into the created packet input" "CHANGELOG.md" && \
     grep -Fq "test_postgres_human_task_step_merges_dependency_outputs" "tests/test_postgres_contract_matrix_integration.py"; then
    echo "ok: human task dependency input merge release docs"
  else
    echo "missing: human task dependency input merge release docs" >&2
    missing=1
  fi
else
  echo "missing: human task dependency input merge milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "typed_step_handler_gateway")
assert capability["status"] == "released"
planner_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "planner_dependency_graph_projection")
assert planner_capability["status"] == "released"
PY
then
  if grep -Fq "step_input_prepare" "README.md" && \
     grep -Fq "step_policy_evaluate" "README.md" && \
     grep -Fq "step_artifact_save" "README.md" && \
     grep -Fq "step_input_prepare" "RUNBOOK.md" && \
     grep -Fq "step_policy_evaluate" "RUNBOOK.md" && \
     grep -Fq "step_artifact_save" "RUNBOOK.md" && \
     grep -Fq "step_input_prepare" "scripts/smoke_api.sh" && \
     grep -Fq "step_policy_evaluate" "scripts/smoke_api.sh" && \
     grep -Fq "input_prepared" "scripts/smoke_api.sh" && \
     grep -Fq "policy_step_completed" "scripts/smoke_api.sh" && \
     grep -Fq "step_input_prepare" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "step_policy_evaluate" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "input_prepared" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "policy_step_completed" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "step_input_prepare" "tests/test_planner.py" && \
     grep -Fq "step_policy_evaluate" "tests/test_planner.py" && \
     grep -Fq "typed_step_handler_gateway" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "step_input_prepare" "CHANGELOG.md"; then
    echo "ok: typed step-handler gateway release baseline"
  else
    echo "missing: typed step-handler gateway release baseline" >&2
    missing=1
  fi
else
  echo "missing: typed step-handler gateway milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "planner_dependency_graph_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq '`POST /v1/plans/compile` now exposes explicit plan-step dependencies plus declared input/output keys' "README.md" && \
     grep -Fq '`POST /v1/plans/compile` exposes `depends_on`, `input_keys`, and `output_keys`' "RUNBOOK.md" && \
     grep -Fq "Promoted the dependency-aware planner graph projection into a released milestone capability" "CHANGELOG.md" && \
     grep -Fq 'compiled.json()["plan"]["steps"][1]["depends_on"] == ["step_input_prepare"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'plan.steps[1].depends_on == ("step_input_prepare",)' "tests/test_planner.py"; then
    echo "ok: planner dependency graph projection docs"
  else
    echo "missing: planner dependency graph projection docs" >&2
    missing=1
  fi
else
  echo "missing: planner dependency graph projection milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "plan_step_operational_semantics_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'owner`, `authority_class`, `review_class`, `failure_strategy`, `timeout_budget_seconds`, `max_attempts`, and `retry_backoff_seconds`' "README.md" && \
     grep -Fq '`owner`, `authority_class`, `review_class`, `failure_strategy`, `timeout_budget_seconds`, `max_attempts`, and `retry_backoff_seconds`' "RUNBOOK.md" && \
     grep -Fq 'Compiled plan steps now project explicit owner, authority_class, review_class, failure_strategy, timeout_budget_seconds, max_attempts, and retry_backoff_seconds semantics' "CHANGELOG.md" && \
     grep -Fq 'expected direct three-step plan compile response with explicit artifact-save semantics' "scripts/smoke_api.sh" && \
     grep -Fq 'compiled.json()["plan"]["steps"][0]["owner"] == "system"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'compiled.json()["plan"]["steps"][0]["timeout_budget_seconds"] == 30' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'compiled_review.json()["plan"]["steps"][2]["review_class"] == "operator"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'compiled_review.json()["plan"]["steps"][2]["timeout_budget_seconds"] == 3600' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'plan.steps[2].authority_class == "draft"' "tests/test_planner.py" && \
     grep -Fq 'plan.steps[2].owner == "human"' "tests/test_planner.py" && \
     grep -Fq 'plan.steps[2].timeout_budget_seconds == 3600' "tests/test_planner.py"; then
    echo "ok: plan step operational semantics docs"
  else
    echo "missing: plan step operational semantics docs" >&2
    missing=1
  fi
else
  echo "missing: plan step operational semantics milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "planner_human_task_branch_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq "human_review_role" "README.md" && \
     grep -Fq "step_human_review" "README.md" && \
     grep -Fq "human_review_role" "RUNBOOK.md" && \
     grep -Fq "step_human_review" "RUNBOOK.md" && \
     grep -Fq "rewrite_review" "scripts/smoke_api.sh" && \
     grep -Fq "communications_reviewer" "scripts/smoke_api.sh" && \
     grep -Fq "step_human_review" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "communications_review" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "human_review_role" "tests/test_planner.py" && \
     grep -Fq "step_human_review" "tests/test_planner.py"; then
    echo "ok: planner human-task branch docs"
  else
    echo "missing: planner human-task branch docs" >&2
    missing=1
  fi
else
  echo "missing: planner human-task branch milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "runtime_human_task_step_execution")
assert capability["status"] == "released"
PY
then
  if grep -Fq "awaiting_human" "README.md" && \
     grep -Fq "202 awaiting_human" "RUNBOOK.md" && \
     grep -Fq "Promoted the compiled human-review runtime execution slice into a released milestone capability" "CHANGELOG.md" && \
     grep -Fq "compiled human review runtime ok" "scripts/smoke_api.sh" && \
     grep -Fq "awaiting_human|poll_or_subscribe|True|" "scripts/smoke_api.sh" && \
     grep -Fq "test_rewrite_compiled_human_review_branch_pauses_and_resumes" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "human_task_step_started" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: runtime human-task step execution docs"
  else
    echo "missing: runtime human-task step execution docs" >&2
    missing=1
  fi
else
  echo "missing: runtime human-task step execution milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_review_payload_artifact_override")
assert capability["status"] == "released"
PY
then
  if grep -Fq "returned_payload_json.final_text" "README.md" && \
     grep -Fq "final_text" "RUNBOOK.md" && \
     grep -Fq "edited by reviewer" "scripts/smoke_api.sh" && \
     grep -Fq 'body_after["artifacts"][0]["content"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'returned_payload_json": {"final_text": reviewed_text}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "human_review_payload_artifact_override" "CHANGELOG.md" && \
     grep -Fq "reviewer-edited content" "CHANGELOG.md"; then
    echo "ok: human-review payload artifact override release baseline"
  else
    echo "missing: human-review payload artifact override release baseline" >&2
    missing=1
  fi
else
  echo "missing: human-review payload artifact override milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "planner_human_review_operational_metadata")
assert capability["status"] == "released"
PY
then
  if grep -Fq "human_review_priority" "README.md" && \
     grep -Fq "human_review_sla_minutes" "README.md" && \
     grep -Fq "human_review_desired_output_json" "README.md" && \
     grep -Fq "human_review_priority" "RUNBOOK.md" && \
     grep -Fq "human_review_sla_minutes" "RUNBOOK.md" && \
     grep -Fq "human_review_desired_output_json" "RUNBOOK.md" && \
     grep -Fq "Promoted the planner human-review operational metadata slice into a released milestone capability" "CHANGELOG.md" && \
     grep -Fq "manager_review" "scripts/smoke_api.sh" && \
     grep -Fq "high|45|3600|1|0|True|manager_review" "scripts/smoke_api.sh" && \
     grep -Fq 'review_task["priority"] == "high"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'review_task["desired_output_json"]["escalation_policy"] == "manager_review"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "human_review_sla_minutes" "tests/test_planner.py" && \
     grep -Fq 'timeout_budget_seconds == 3600' "tests/test_planner.py" && \
     grep -Fq 'desired_output_json["escalation_policy"] == "manager_review"' "tests/test_planner.py"; then
    echo "ok: planner human-review operational metadata docs"
  else
    echo "missing: planner human-review operational metadata docs" >&2
    missing=1
  fi
else
  echo "missing: planner human-review operational metadata milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "registry_backed_tool_execution_service")
assert capability["status"] == "released"
PY
then
  if grep -Fq "ToolExecutionService" "README.md" && \
     grep -Fq "tool.v1" "README.md" && \
     grep -Fq "self-heals missing built-in tool definitions" "README.md" && \
     grep -Fq "ToolExecutionService" "RUNBOOK.md" && \
     grep -Fq "tool.v1" "RUNBOOK.md" && \
     grep -Fq "self-heals its registry definition" "RUNBOOK.md" && \
     grep -Fq "Promoted the registry-backed tool execution service slice into a released milestone capability" "CHANGELOG.md" && \
     grep -Fq "artifact_repository|tool.v1" "scripts/smoke_api.sh" && \
     grep -Fq "tool_execution_completed" "scripts/smoke_api.sh" && \
     grep -Fq "tool_execution_completed" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "invocation_contract" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_tool_execution_service_self_heals_missing_builtin_artifact_definition" "tests/test_tool_execution.py" && \
     grep -Fq "test_tool_execution_service_self_heals_missing_builtin_connector_dispatch_definition" "tests/test_tool_execution.py" && \
     test -f "tests/test_tool_execution.py"; then
    echo "ok: registry-backed tool execution service docs"
  else
    echo "missing: registry-backed tool execution service docs" >&2
    missing=1
  fi
else
  echo "missing: registry-backed tool execution service milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "connector_dispatch_tool_execution_slice")
assert capability["status"] == "released"
PY
then
  if grep -Fq "/v1/tools/execute" "README.md" && \
     grep -Fq "connector.dispatch" "README.md" && \
     grep -Fq "/v1/tools/execute" "RUNBOOK.md" && \
     grep -Fq "connector.dispatch" "RUNBOOK.md" && \
     grep -Fq "/v1/tools/execute" "HTTP_EXAMPLES.http" && \
     grep -Fq "connector.dispatch" "HTTP_EXAMPLES.http" && \
     grep -Fq 'TOOL_EXEC_STATUS="$(python3 -c ' "scripts/smoke_api.sh" && \
     grep -Fq '"${TOOL_EXEC_STATUS}" != "queued" && "${TOOL_EXEC_STATUS}" != "retry"' "scripts/smoke_api.sh" && \
     grep -Fq "connector.dispatch|tool.v1" "scripts/smoke_api.sh" && \
     grep -Fq "/v1/tools/execute" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "connector.dispatch" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_tool_execution_service_executes_builtin_connector_dispatch_handler" "tests/test_tool_execution.py" && \
     grep -Fq "Promoted the connector dispatch tool execution slice into a released milestone capability" "CHANGELOG.md"; then
    echo "ok: connector dispatch tool execution slice release docs"
  else
    echo "missing: connector dispatch tool execution slice release docs" >&2
    missing=1
  fi
else
  echo "missing: connector dispatch tool execution slice milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "browseract_account_facts_tool_execution_slice")
assert capability["status"] == "released"
PY
then
  if grep -Fq "/v1/tools/execute" "README.md" && \
     grep -Fq "browseract.extract_account_facts" "README.md" && \
     grep -Fq "/v1/tools/execute" "RUNBOOK.md" && \
     grep -Fq "browseract.extract_account_facts" "RUNBOOK.md" && \
     grep -Fq "/v1/tools/execute" "HTTP_EXAMPLES.http" && \
     grep -Fq "browseract.extract_account_facts" "HTTP_EXAMPLES.http" && \
     grep -Fq "browseract.extract_account_facts|BrowserAct|Tier 3|ops@example.com" "scripts/smoke_api.sh" && \
     grep -Fq "browseract.extract_account_facts" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "browseract_ltd_discovery" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_tool_execution_service_executes_builtin_browseract_extract_handler" "tests/test_tool_execution.py" && \
     grep -Fq "test_tool_execution_service_self_heals_missing_builtin_browseract_definition" "tests/test_tool_execution.py" && \
     grep -Fq "Promoted the BrowserAct account-facts tool execution slice into a released milestone capability" "CHANGELOG.md"; then
    echo "ok: BrowserAct account-facts tool execution slice release docs"
  else
    echo "missing: BrowserAct account-facts tool execution slice release docs" >&2
    missing=1
  fi
else
  echo "missing: BrowserAct account-facts tool execution slice milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "connector_dispatch_binding_scope_guardrails")
assert capability["status"] == "released"
PY
then
  if grep -Fq "enabled connector binding" "README.md" && \
     grep -Fq "principal scope" "RUNBOOK.md" && \
     grep -Fq '"binding_id"' "HTTP_EXAMPLES.http" && \
     grep -Fq "principal_scope_mismatch" "scripts/smoke_api.sh" && \
     grep -Fq "binding_id" "scripts/smoke_api.sh" && \
     grep -Fq "execute_mismatch" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'execute_mismatch.json()["error"]["code"] == "principal_scope_mismatch"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_tool_execution_service_rejects_foreign_connector_binding_scope" "tests/test_tool_execution.py" && \
     grep -Fq "connector_dispatch_binding_scope_guardrails" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "delivery side effect is queued" "CHANGELOG.md"; then
    echo "ok: connector dispatch binding scope guardrails release baseline"
  else
    echo "missing: connector dispatch binding scope guardrails release baseline" >&2
    missing=1
  fi
else
  echo "missing: connector dispatch binding scope guardrails milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "approval_async_acceptance_contract")
assert capability["status"] == "released"
PY
then
  if grep -Fq "202 Accepted" "README.md" && \
     grep -Fq "awaiting_approval" "README.md" && \
     grep -Fq "202 awaiting_approval" "RUNBOOK.md" && \
     grep -Fq "poll_or_subscribe" "RUNBOOK.md" && \
     grep -Fq "approval_async_acceptance_contract" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "202 Accepted" "CHANGELOG.md" && \
     grep -Fq "approval-required acceptance contract" "HTTP_EXAMPLES.http" && \
     grep -Fq "expected 202 for approval-required path" "scripts/smoke_api.sh" && \
     grep -Fq "awaiting_approval|poll_or_subscribe" "scripts/smoke_api.sh" && \
     grep -Fq "assert create.status_code == 202" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "next_action" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: approval async acceptance contract docs"
  else
    echo "missing: approval async acceptance contract docs" >&2
    missing=1
  fi
else
  echo "missing: approval async acceptance contract milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_packets_kernel")
assert capability["status"] == "released"
resume_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_pause_resume_session_flow")
assert resume_capability["status"] == "released"
filter_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_queue_filters")
assert filter_capability["status"] == "released"
backlog_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_backlog_endpoints")
assert backlog_capability["status"] == "released"
assignment_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_assignment")
assert assignment_capability["status"] == "released"
visibility_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_state_visibility")
assert visibility_capability["status"] == "released"
assert "human_task_assignment_state_field" in visibility_capability["scope"]
assert "claimed_and_returned_assignment_projection" in visibility_capability["scope"]
assert "release/operator guards now pin that assignment-state visibility contract" in visibility_capability["notes"]
assert "ea/schema/20260305_v0_26_human_task_assignment_state.sql" in milestone["migrations"]
review_contract_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_review_contract_metadata")
assert review_contract_capability["status"] == "released"
assert "ea/schema/20260305_v0_27_human_task_review_contract.sql" in milestone["migrations"]
operator_capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "operator_profile_specialized_backlog_routing")
assert operator_capability["status"] == "released"
assert "ea/schema/20260305_v0_28_operator_profiles_kernel.sql" in milestone["migrations"]
PY
then
  if grep -Fq "/v1/human/tasks" "README.md" && \
     grep -Fq "human task packets" "README.md" && \
     grep -Fq "resume_session_on_return=true" "README.md" && \
     grep -Fq "assigned_operator_id" "README.md" && \
     grep -Fq "/v1/human/tasks/backlog" "README.md" && \
     grep -Fq "/v1/human/tasks/{human_task_id}/assign" "README.md" && \
     grep -Fq "/v1/human/tasks/unassigned" "README.md" && \
     grep -Fq "/v1/human/tasks" "RUNBOOK.md" && \
     grep -Fq "awaiting_human" "RUNBOOK.md" && \
     grep -Fq "overdue_only" "RUNBOOK.md" && \
     grep -Fq "/v1/human/tasks/mine" "RUNBOOK.md" && \
     grep -Fq "assignment_state=assigned|unassigned" "RUNBOOK.md" && \
     grep -Fq "human_task_assigned" "RUNBOOK.md" && \
     grep -Fq "human_task_returned" "RUNBOOK.md" && \
     grep -Fq "/v1/human/tasks/{{human_task_id}}/return" "HTTP_EXAMPLES.http" && \
     grep -Fq "role_required=communications_reviewer&overdue_only=true" "HTTP_EXAMPLES.http" && \
     grep -Fq "assigned_operator_id=operator&status=claimed" "HTTP_EXAMPLES.http" && \
     grep -Fq "/v1/human/tasks/backlog?role_required=communications_reviewer&overdue_only=true&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "/v1/human/tasks/unassigned?role_required=communications_reviewer&overdue_only=true&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "/v1/human/tasks/mine?operator_id=operator&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "/v1/human/tasks/{{human_task_id}}/assign" "HTTP_EXAMPLES.http" && \
     grep -Fq "assignment_state=assigned&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq '"resume_session_on_return": true' "HTTP_EXAMPLES.http" && \
     grep -Fq "v0_24 human tasks kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "v0_25 human task resume kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "v0_26 human task assignment-state kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "v0_27 human task review contract kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "v0_28 operator profiles kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "human_tasks" "scripts/db_status.sh" && \
     grep -Fq "human tasks ok" "scripts/smoke_api.sh" && \
     grep -Fq "assignment_state" "scripts/smoke_api.sh" && \
     grep -Fq "awaiting_human|True|True" "scripts/smoke_api.sh" && \
     grep -Fq "role/overdue human task queue filter" "scripts/smoke_api.sh" && \
     grep -Fq "assigned-operator human task queue filter" "scripts/smoke_api.sh" && \
     grep -Fq "human task backlog endpoint" "scripts/smoke_api.sh" && \
     grep -Fq "human task mine endpoint" "scripts/smoke_api.sh" && \
     grep -Fq 'Promoted milestone capability `human_task_operator_backlog_endpoints` to released' "CHANGELOG.md" && \
     grep -Fq "human_task_assignment_state_visibility" "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `human_task_assignment_state_visibility` to released' "CHANGELOG.md" && \
     grep -Fq "pre-assigned task" "scripts/smoke_api.sh" && \
     grep -Fq "human task unassigned endpoint" "scripts/smoke_api.sh" && \
     grep -Fq "assigned-only backlog endpoint" "scripts/smoke_api.sh" && \
     grep -Fq "/v1/human/tasks" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assignment_state="unassigned"' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "test_postgres_human_tasks_create_claim_return_and_list" "tests/test_postgres_contract_matrix_integration.py"; then
    echo "ok: human task packet kernel docs"
  else
    echo "missing: human task packet kernel docs" >&2
    missing=1
  fi
else
  echo "missing: human task packet kernel milestone status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_review_contract_metadata")
assert capability["status"] == "released"
assert "ea/schema/20260305_v0_27_human_task_review_contract.sql" in milestone["migrations"]
PY
then
  if grep -Fq "human_review_authority_required" "README.md" && \
     grep -Fq "human_review_why_human" "README.md" && \
     grep -Fq "human_review_quality_rubric_json" "README.md" && \
     grep -Fq "human_review_authority_required" "RUNBOOK.md" && \
     grep -Fq "human_review_why_human" "RUNBOOK.md" && \
     grep -Fq "human_review_quality_rubric_json" "RUNBOOK.md" && \
     grep -Fq "send_on_behalf_review" "scripts/smoke_api.sh" && \
     grep -Fq "External executive communication needs human tone review." "scripts/smoke_api.sh" && \
     grep -Fq 'review_task["authority_required"] == "send_on_behalf_review"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "quality_rubric_json" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "human_review_authority_required" "tests/test_planner.py" && \
     grep -Fq "human_review_quality_rubric_json" "tests/test_planner.py" && \
     grep -Fq 'authority_required="send_on_behalf_review"' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "v0_27 human task review contract kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq 'Promoted milestone capability `human_task_review_contract_metadata` to released' "CHANGELOG.md"; then
    echo "ok: human task review-contract metadata release baseline"
  else
    echo "missing: human task review-contract metadata release baseline" >&2
    missing=1
  fi
else
  echo "missing: human task review-contract metadata milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "operator_profile_specialized_backlog_routing")
assert capability["status"] == "released"
assert "ea/schema/20260305_v0_28_operator_profiles_kernel.sql" in milestone["migrations"]
PY
then
  if grep -Fq "/v1/human/tasks/operators" "README.md" && \
     grep -Fq "skill-tag" "README.md" && \
     grep -Fq "/v1/human/tasks/operators" "RUNBOOK.md" && \
     grep -Fq "operator_id=<id>" "RUNBOOK.md" && \
     grep -Fq "Promoted the operator-profile specialized backlog routing slice into a released milestone capability" "CHANGELOG.md" && \
     grep -Fq "operator-specialist" "scripts/smoke_api.sh" && \
     grep -Fq "operator-specialized backlog endpoint" "scripts/smoke_api.sh" && \
     grep -Fq "operator-specialized backlog endpoint to exclude" "scripts/smoke_api.sh" && \
     grep -Fq '"/v1/human/tasks/operators"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "operator-specialist" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_postgres_operator_profiles_upsert_get_and_list" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "v0_28 operator profiles kernel" "scripts/db_bootstrap.sh"; then
    echo "ok: operator-profile specialized backlog routing docs"
  else
    echo "missing: operator-profile specialized backlog routing docs" >&2
    missing=1
  fi
else
  echo "missing: operator-profile specialized backlog routing milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_assignment_hints")
assert capability["status"] == "released"
assert "suggested_operator_ids" in capability["scope"]
assert "auto_assign_operator_id" in capability["scope"]
PY
then
  if grep -Fq "routing_hints_json" "README.md" && \
     grep -Fq "auto_assign_operator_id" "README.md" && \
     grep -Fq "routing_hints_json" "RUNBOOK.md" && \
     grep -Fq "auto_assign_operator_id" "RUNBOOK.md" && \
     grep -Fq "operator auto-assignment hint" "scripts/smoke_api.sh" && \
     grep -Fq "routing_hints_json" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "auto_assign_operator_id" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_postgres_human_task_operator_assignment_hints" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "routing_hints_json: dict[str, object]" "ea/app/api/routes/rewrite.py" && \
     grep -Fq "routing_hints_json: dict[str, object]" "ea/app/api/routes/human.py" && \
     grep -Fq "human_task_operator_assignment_hints" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "recommended_operator_id" "CHANGELOG.md"; then
    echo "ok: human task operator assignment hints release baseline"
  else
    echo "missing: human task operator assignment hints release baseline" >&2
    missing=1
  fi
else
  echo "missing: human task operator assignment hints milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_recommended_assignment_action")
assert capability["status"] == "released"
assert "auto_assign_operator_id_consumption" in capability["scope"]
PY
then
  if grep -Fq "/v1/human/tasks/{human_task_id}/assign" "README.md" && \
     grep -Fq 'omits `operator_id`' "README.md" && \
     grep -Fq "auto_assign_operator_id" "RUNBOOK.md" && \
     grep -Fq 'omits `operator_id`' "RUNBOOK.md" && \
     grep -Fq -- "-d '{}'" "scripts/smoke_api.sh" && \
     grep -Fq "pending|assigned|operator-specialist" "scripts/smoke_api.sh" && \
     grep -Fq 'json={}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assigned.json()["assigned_operator_id"] == "operator-specialist"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "human_task_no_auto_assign_candidate" "ea/app/api/routes/human.py"; then
    echo "ok: human task recommended assignment action docs"
  else
    echo "missing: human task recommended assignment action docs" >&2
    missing=1
  fi
else
  echo "missing: human task recommended assignment action milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "planner_human_task_auto_preselection")
assert capability["status"] == "released"
assert "plan_step_auto_assign_projection" in capability["scope"]
assert "runtime_human_task_auto_assignment" in capability["scope"]
PY
then
  if grep -Fq "human_review_auto_assign_if_unique" "README.md" && \
     grep -Fq "human_review_auto_assign_if_unique" "RUNBOOK.md" && \
     grep -Fq "human_review_auto_assign_if_unique" "scripts/smoke_api.sh" && \
     grep -Fq "assigned|operator-specialist" "scripts/smoke_api.sh" && \
     grep -Fq "human_review_auto_assign_if_unique" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'review_task["assignment_state"] == "assigned"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'review_task["assigned_operator_id"] == "operator-specialist"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "human_review_auto_assign_if_unique" "tests/test_planner.py" && \
     grep -Fq "auto_assign_if_unique is True" "tests/test_planner.py"; then
    echo "ok: planner human task auto-preselection docs"
  else
    echo "missing: planner human task auto-preselection docs" >&2
    missing=1
  fi
else
  echo "missing: planner human task auto-preselection milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_source_visibility")
assert capability["status"] == "released"
assert "ea/schema/20260305_v0_29_human_task_assignment_source.sql" in milestone["migrations"]
PY
then
  if grep -Fq "assignment_source" "README.md" && \
     grep -Fq "assignment_source" "RUNBOOK.md" && \
     grep -Fq "assignment_source" "scripts/smoke_api.sh" && \
     grep -Fq "operator-specialist|recommended" "scripts/smoke_api.sh" && \
     grep -Fq "operator-junior|manual" "scripts/smoke_api.sh" && \
     grep -Fq "auto_preselected" "scripts/smoke_api.sh" && \
     grep -Fq 'task["assignment_source"] == ""' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assigned.json()["assignment_source"] == "recommended"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'review_task["assignment_source"] == "auto_preselected"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assignment_source="manual"' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "v0_29 human task assignment-source kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "Promoted the human-task assignment-source visibility slice into a released milestone capability" "CHANGELOG.md"; then
    echo "ok: human task assignment source visibility docs"
  else
    echo "missing: human task assignment source visibility docs" >&2
    missing=1
  fi
else
  echo "missing: human task assignment source visibility milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_provenance_fields")
assert capability["status"] == "released"
assert "ea/schema/20260305_v0_30_human_task_assignment_provenance.sql" in milestone["migrations"]
PY
then
  if grep -Fq "assigned_at" "README.md" && \
     grep -Fq "assigned_by_actor_id" "README.md" && \
     grep -Fq "assigned_at" "RUNBOOK.md" && \
     grep -Fq "assigned_by_actor_id" "RUNBOOK.md" && \
     grep -Fq "assigned_by_actor_id" "scripts/smoke_api.sh" && \
     grep -Fq "orchestrator:auto_preselected" "scripts/smoke_api.sh" && \
     grep -Fq 'task["assigned_by_actor_id"] == ""' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assigned.json()["assigned_by_actor_id"] == "exec-1"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'review_task["assigned_by_actor_id"] == "orchestrator:auto_preselected"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assigned_by_actor_id="principal-1"' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq 'assigned_by_actor_id == "operator-1"' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "v0_30 human task assignment provenance kernel" "scripts/db_bootstrap.sh" && \
     grep -Fq "human_task_assignment_provenance_fields" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "assigned_at" "CHANGELOG.md" && \
     grep -Fq "assigned_by_actor_id" "CHANGELOG.md"; then
    echo "ok: human task assignment provenance release baseline"
  else
    echo "missing: human task assignment provenance release baseline" >&2
    missing=1
  fi
else
  echo "missing: human task assignment provenance milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_history_api")
assert capability["status"] == "released"
PY
then
  if grep -Fq "/v1/human/tasks/{human_task_id}/assignment-history" "README.md" && \
     grep -Fq "/v1/human/tasks/{human_task_id}/assignment-history" "RUNBOOK.md" && \
     grep -Fq "/v1/human/tasks/{{human_task_id}}/assignment-history" "HTTP_EXAMPLES.http" && \
     grep -Fq "/v1/human/tasks/\${HUMAN_TASK_ID}/assignment-history" "scripts/smoke_api.sh" && \
     grep -Fq "human_task_created,human_task_assigned,human_task_assigned,human_task_claimed,human_task_returned" "scripts/smoke_api.sh" && \
     grep -Fq '/assignment-history", params={"limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "Promoted the human-task assignment-history API slice into a released milestone capability" "CHANGELOG.md"; then
    echo "ok: human task assignment history docs"
  else
    echo "missing: human task assignment history docs" >&2
    missing=1
  fi
else
  echo "missing: human task assignment history milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_human_task_assignment_history_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'Promoted milestone capability `session_human_task_assignment_history_projection` to released' "CHANGELOG.md" && \
     grep -Fq "human_task_assignment_history" "README.md" && \
     grep -Fq "human_task_assignment_history" "RUNBOOK.md" && \
     grep -Fq "human_task_assignment_history" "scripts/smoke_api.sh" && \
     grep -Fq 'body["human_task_assignment_history"] == []' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'session_body["human_task_assignment_history"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'body["human_task_assignment_history"][1]["assignment_source"] == "auto_preselected"' "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: session human task assignment history projection docs"
  else
    echo "missing: session human task assignment history projection docs" >&2
    missing=1
  fi
else
  echo "missing: session human task assignment history projection milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_history_filters")
assert capability["status"] == "released"
PY
then
  if grep -Fq "assigned_operator_id" "README.md" && \
     grep -Fq "assigned_by_actor_id" "README.md" && \
     grep -Fq "assigned_operator_id" "RUNBOOK.md" && \
     grep -Fq "assigned_by_actor_id" "RUNBOOK.md" && \
     grep -Fq "event_name=human_task_assigned&assigned_by_actor_id=exec-1" "scripts/smoke_api.sh" && \
     grep -Fq "event_name=human_task_returned&assigned_operator_id=operator-junior" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"limit": 10, "event_name": "human_task_assigned", "assigned_by_actor_id": "exec-1"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"limit": 10, "event_name": "human_task_returned", "assigned_operator_id": "operator-junior"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/{{human_task_id}}/assignment-history?limit=20&event_name=human_task_assigned&assigned_by_actor_id={{principal_id}}" "HTTP_EXAMPLES.http" && \
     grep -Fq "Promoted the human-task assignment-history filters slice into a released milestone capability" "CHANGELOG.md"; then
    echo "ok: human task assignment history filters docs"
  else
    echo "missing: human task assignment history filters docs" >&2
    missing=1
  fi
else
  echo "missing: human task assignment history filters milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_last_transition_summary_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq "last_transition_event_name" "README.md" && \
     grep -Fq "last_transition_operator_id" "README.md" && \
     grep -Fq "last_transition_by_actor_id" "README.md" && \
     grep -Fq "last_transition_event_name" "RUNBOOK.md" && \
     grep -Fq "last_transition_operator_id" "RUNBOOK.md" && \
     grep -Fq "last_transition_by_actor_id" "RUNBOOK.md" && \
     grep -Fq "HUMAN_CREATE_SUMMARY_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_REWRITE_SUMMARY_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq "human_task_returned|True|returned|operator-junior|manual|operator-junior" "scripts/smoke_api.sh" && \
     grep -Fq 'task["last_transition_event_name"] == "human_task_created"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assigned.json()["last_transition_event_name"] == "human_task_assigned"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'returned.json()["last_transition_event_name"] == "human_task_returned"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'review_task["last_transition_event_name"] == "human_task_assigned"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'last_transition_event_name: str' "ea/app/api/routes/human.py" && \
     grep -Fq 'last_transition_event_name: str' "ea/app/api/routes/rewrite.py" && \
     grep -Fq "Promoted the human-task last-transition summary projection slice into a released milestone capability" "CHANGELOG.md"; then
    echo "ok: human task last transition summary docs"
  else
    echo "missing: human task last transition summary docs" >&2
    missing=1
  fi
else
  echo "missing: human task last transition summary milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_last_transition_sorting")
assert capability["status"] == "released"
PY
then
  if grep -Fq "sort=last_transition_desc" "README.md" && \
     grep -Fq "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "human task last-transition sort ok" "scripts/smoke_api.sh" && \
     grep -Fq "SORT_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "SORT_BACKLOG_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "sort": "last_transition_desc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"sort": "last_transition_desc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?sort=last_transition_desc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "sla_due_at_asc_last_transition_desc" "ea/app/api/routes/human.py" && \
     grep -Fq 'Promoted milestone capability `human_task_last_transition_sorting` to released' "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "freshest-transition queue ordering" "CHANGELOG.md"; then
    echo "ok: human task last transition sorting release docs"
  else
    echo "missing: human task last transition sorting release docs" >&2
    missing=1
  fi
else
  echo "missing: human task last transition sorting release milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_sla_sorting")
assert capability["status"] == "released"
PY
then
  if grep -Fq "sort=sla_due_at_asc" "README.md" && \
     grep -Fq "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "human task SLA sort ok" "scripts/smoke_api.sh" && \
     grep -Fq "SLA_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "SLA_BACKLOG_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "sort": "sla_due_at_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"sort": "sla_due_at_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?sort=sla_due_at_asc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "sla_due_at_asc_last_transition_desc" "ea/app/api/routes/human.py"; then
    echo "ok: human task SLA sorting release docs"
  else
    echo "missing: human task SLA sorting release docs" >&2
    missing=1
  fi
else
  echo "missing: human task SLA sorting release milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_sla_transition_combined_sorting")
assert capability["status"] == "released"
PY
then
  if grep -Fq "sort=sla_due_at_asc_last_transition_desc" "README.md" && \
     grep -Fq "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "human task combined sort ok" "scripts/smoke_api.sh" && \
     grep -Fq "COMBINED_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "COMBINED_BACKLOG_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "sort": "sla_due_at_asc_last_transition_desc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"sort": "sla_due_at_asc_last_transition_desc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?sort=sla_due_at_asc_last_transition_desc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "sla_due_at_asc_last_transition_desc" "ea/app/api/routes/human.py" && \
     grep -Fq 'Promoted milestone capability `human_task_sla_transition_combined_sorting` to released' "CHANGELOG.md"; then
    echo "ok: human task combined sorting docs"
  else
    echo "missing: human task combined sorting docs" >&2
    missing=1
  fi
else
  echo "missing: human task combined sorting milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_unscheduled_fallback_sorting")
assert capability["status"] == "released"
PY
then
  if grep -Fq "fall back to oldest-created ordering for tasks without \`sla_due_at\`" "README.md" && \
     grep -Fq "fall back to oldest-created ordering for tasks without \`sla_due_at\`" "RUNBOOK.md" && \
     grep -Fq "human task unscheduled fallback sort ok" "scripts/smoke_api.sh" && \
     grep -Fq "UNSCHED_SLA_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "UNSCHED_COMBINED_BACKLOG_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "sort": "sla_due_at_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"status": "pending", "sort": "sla_due_at_asc_last_transition_desc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks?principal_id={{principal_id}}&status=pending&sort=sla_due_at_asc&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: human task unscheduled fallback sorting docs"
  else
    echo "missing: human task unscheduled fallback sorting docs" >&2
    missing=1
  fi
else
  echo "missing: human task unscheduled fallback sorting milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_created_asc_sorting")
assert capability["status"] == "released"
PY
then
  if grep -Fq "sort=created_asc" "README.md" && \
     grep -Fq "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `human_task_created_asc_sorting` to released' "CHANGELOG.md" && \
     grep -Fq "human task created-asc sort ok" "scripts/smoke_api.sh" && \
     grep -Fq "CREATED_ASC_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "CREATED_ASC_MINE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "sort": "created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"sort": "created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"operator_id": "operator-sorter", "status": "pending", "sort": "created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?sort=created_asc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "created_asc" "ea/app/api/routes/human.py"; then
    echo "ok: human task created asc sorting docs"
  else
    echo "missing: human task created asc sorting docs" >&2
    missing=1
  fi
else
  echo "missing: human task created asc sorting milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_priority_created_sorting")
assert capability["status"] == "released"
PY
then
  if grep -Fq "sort=priority_desc_created_asc" "README.md" && \
     grep -Fq "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "human task priority-desc-created-asc sort ok" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_SORT_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_SORT_MINE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "sort": "priority_desc_created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"sort": "priority_desc_created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"operator_id": "operator-sorter", "status": "pending", "sort": "priority_desc_created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?sort=priority_desc_created_asc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "priority_desc_created_asc" "ea/app/api/routes/human.py"; then
    echo "ok: human task priority created sorting docs"
  else
    echo "missing: human task priority created sorting docs" >&2
    missing=1
  fi
else
  echo "missing: human task priority created sorting milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_priority_filters")
assert capability["status"] == "released"
PY
then
  if grep -Fq "accept \`priority=<level>\` filters" "README.md" && \
     grep -Fq "supports \`priority\`" "RUNBOOK.md" && \
     grep -Fq "priority=urgent|high|normal|low" "RUNBOOK.md" && \
     grep -Fq "human task priority filter ok" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_FILTER_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_FILTER_MINE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "priority": "high", "sort": "created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"priority": "high", "sort": "created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"operator_id": "operator-sorter", "status": "pending", "priority": "urgent", "sort": "created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?priority=high&sort=created_asc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "priority: str | None = None" "ea/app/api/routes/human.py"; then
    echo "ok: human task priority filters docs"
  else
    echo "missing: human task priority filters docs" >&2
    missing=1
  fi
else
  echo "missing: human task priority filters milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_multi_priority_filters")
assert capability["status"] == "released"
PY
then
  if grep -Fq "comma-separated values like \`priority=urgent,high\`" "README.md" && \
     grep -Fq "priority=urgent,high" "RUNBOOK.md" && \
     grep -Fq "human task multi-priority filter ok" "scripts/smoke_api.sh" && \
     grep -Fq "MULTI_PRIORITY_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "MULTI_PRIORITY_MINE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"operator_id": "operator-sorter", "status": "pending", "priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?priority=urgent,high&sort=priority_desc_created_asc&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: human task multi priority filters docs"
  else
    echo "missing: human task multi priority filters docs" >&2
    missing=1
  fi
else
  echo "missing: human task multi priority filters milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_priority_summary")
assert capability["status"] == "released"
PY
then
  if grep -Fq "GET /v1/human/tasks/priority-summary" "README.md" && \
     grep -Fq "/v1/human/tasks/priority-summary" "RUNBOOK.md" && \
     grep -Fq "human task priority summary ok" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_SUMMARY_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_SUMMARY_UNASSIGNED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "role_required": role_required}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"status": "pending", "role_required": role_required, "assignment_state": "unassigned"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/priority-summary?status=pending&role_required=communications_reviewer" "HTTP_EXAMPLES.http" && \
     grep -Fq '@router.get("/priority-summary")' "ea/app/api/routes/human.py" && \
     grep -Fq 'human_task_priority_summary' "CHANGELOG.md" && \
     grep -Fq 'release/operator guards' "CHANGELOG.md" && \
     grep -Fq 'highest_priority' "CHANGELOG.md"; then
    echo "ok: human task priority summary release baseline"
  else
    echo "missing: human task priority summary release baseline" >&2
    missing=1
  fi
else
  echo "missing: human task priority summary milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assigned_priority_summary")
assert capability["status"] == "released"
PY
then
  if grep -Fq "also accepts \`assigned_operator_id\`" "README.md" && \
     grep -Fq "assigned_operator_id" "RUNBOOK.md" && \
     grep -Fq "PRIORITY_SUMMARY_ASSIGNED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_SUMMARY_ASSIGNED_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "role_required": role_required, "assigned_operator_id": operator_id}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/priority-summary?status=pending&role_required=communications_reviewer&assigned_operator_id=operator" "HTTP_EXAMPLES.http"; then
    echo "ok: human task assigned priority summary docs"
  else
    echo "missing: human task assigned priority summary docs" >&2
    missing=1
  fi
else
  echo "missing: human task assigned priority summary milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_matched_priority_summary")
assert capability["status"] == "released"
PY
then
  if grep -Fq "also accepts \`operator_id\`" "README.md" && \
     grep -Fq "operator_id" "RUNBOOK.md" && \
     grep -Fq "PRIORITY_SUMMARY_MATCHED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_SUMMARY_MATCHED_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq '"operator_id": "operator-specialist-summary"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&operator_id=operator-specialist" "HTTP_EXAMPLES.http" && \
     grep -Fq "operator_id: str" "ea/app/api/routes/human.py"; then
    echo "ok: human task operator-matched priority summary docs"
  else
    echo "missing: human task operator-matched priority summary docs" >&2
    missing=1
  fi
else
  echo "missing: human task operator-matched priority summary milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "human_task_priority_summary_assignment_source_filter"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "also accepts \`assignment_source\`" "README.md" && \
     grep -Fq "assignment_source" "RUNBOOK.md" && \
     grep -Fq "PRIORITY_SUMMARY_MANUAL_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_REWRITE_AUTO_SUMMARY_JSON" "scripts/smoke_api.sh" && \
     grep -Fq '"assignment_source": "auto_preselected"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/priority-summary?status=pending&assignment_source=manual" "HTTP_EXAMPLES.http" && \
     grep -Fq "assignment_source: str" "ea/app/api/routes/human.py"; then
    echo "ok: human task assignment-source priority summary docs"
  else
    echo "missing: human task assignment-source priority summary docs" >&2
    missing=1
  fi
else
  echo "missing: human task assignment-source priority summary milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_priority_summary_mixed_source_non_ownerless_isolation"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "rechecked after extra ownerless rows are added" "README.md" && \
     grep -Fq "rechecked after extra ownerless rows are added" "RUNBOOK.md" && \
     grep -Fq "PRIORITY_SUMMARY_MANUAL_MIXED_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_REWRITE_AUTO_SUMMARY_MIXED_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq "human_task_priority_summary_mixed_source_non_ownerless_isolation" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "mixed-source churn does not contaminate non-ownerless summary counts" "CHANGELOG.md"; then
    echo "ok: human task mixed-source non-ownerless priority summary release baseline"
  else
    echo "missing: human task mixed-source non-ownerless priority summary release baseline" >&2
    missing=1
  fi
else
  echo "missing: human task mixed-source non-ownerless priority summary milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_source_queue_filters")
assert capability["status"] == "released"
PY
then
  if grep -Fq "queue views now also accept \`assignment_source=<source>\`" "README.md" && \
     grep -Fq "assignment_source=manual|recommended|auto_preselected" "RUNBOOK.md" && \
     grep -Fq "PRIORITY_SUMMARY_MANUAL_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_REWRITE_AUTO_BACKLOG_JSON" "scripts/smoke_api.sh" && \
     grep -Fq '"assignment_source": "manual"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?assignment_source=auto_preselected&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: human task assignment-source queue filters docs"
  else
    echo "missing: human task assignment-source queue filters docs" >&2
    missing=1
  fi
else
  echo "missing: human task assignment-source queue filters milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_assignment_source_alias"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "assignment_source=none" "README.md" && \
     grep -Fq "assignment_source=none" "RUNBOOK.md" && \
     grep -Fq "HUMAN_UNASSIGNED_NONE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "PRIORITY_SUMMARY_NONE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"status": "pending", "assignment_state": "unassigned", "assignment_source": "none"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"assignment_source": "none"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'assignment_source="none"' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "/v1/human/tasks/unassigned?assignment_source=none&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "human_task_ownerless_assignment_source_alias" "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "ownerless queue and priority-summary alias" "CHANGELOG.md"; then
    echo "ok: human task ownerless assignment-source alias release baseline"
  else
    echo "missing: human task ownerless assignment-source alias release baseline" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless assignment-source alias milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_session_history_alias"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "human_task_assignment_source=none" "README.md" && \
     grep -Fq "human_task_assignment_source=none" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_NONE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_HISTORY_NONE_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"limit": 10, "assignment_source": "none"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'params={"human_task_assignment_source": "none"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/rewrite/sessions/{{session_id}}?human_task_assignment_source=none" "HTTP_EXAMPLES.http" && \
     grep -Fq "/v1/human/tasks/{{human_task_id}}/assignment-history?limit=20&assignment_source=none" "HTTP_EXAMPLES.http"; then
    echo "ok: human task ownerless session/history alias docs"
  else
    echo "missing: human task ownerless session/history alias docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless session/history alias milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_backlog_alias"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "assignment_state=unassigned&assignment_source=none" "README.md" && \
     grep -Fq "assignment_state=unassigned&assignment_source=none" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_BACKLOG_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"assignment_state": "unassigned", "assignment_source": "none"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq 'Promoted milestone capability `human_task_ownerless_backlog_alias` to released' "CHANGELOG.md"; then
    echo "ok: human task ownerless backlog alias docs"
  else
    echo "missing: human task ownerless backlog alias docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless backlog alias milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_backlog_created_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "assignment_state=unassigned&assignment_source=none&sort=created_asc" "README.md" && \
     grep -Fq "assignment_state=unassigned&assignment_source=none&sort=created_asc" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_BACKLOG_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"sort": "created_asc"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=created_asc&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: human task ownerless backlog created sort docs"
  else
    echo "missing: human task ownerless backlog created sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless backlog created sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_backlog_last_transition_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" "README.md" && \
     grep -Fq "assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_BACKLOG_TRANSITION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq '"sort": "last_transition_desc"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=last_transition_desc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq 'Promoted milestone capability `human_task_ownerless_backlog_last_transition_sort` to released' "CHANGELOG.md"; then
    echo "ok: human task ownerless backlog last-transition sort docs"
  else
    echo "missing: human task ownerless backlog last-transition sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless backlog last-transition sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_ownerless_unassigned_last_transition_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "assignment_source=none&sort=last_transition_desc" "README.md" && \
     grep -Fq "assignment_source=none&sort=last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"assignment_source": "none", "sort": "last_transition_desc"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/unassigned?assignment_source=none&sort=last_transition_desc&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: human task ownerless unassigned last-transition sort docs"
  else
    echo "missing: human task ownerless unassigned last-transition sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless unassigned last-transition sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_ownerless_unassigned_created_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "assignment_source=none&sort=created_asc" "README.md" && \
     grep -Fq "assignment_source=none&sort=created_asc" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_UNASSIGNED_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"assignment_source": "none", "sort": "created_asc"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/unassigned?assignment_source=none&sort=created_asc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq 'Promoted milestone capability `human_task_ownerless_unassigned_created_sort` to released' "CHANGELOG.md"; then
    echo "ok: human task ownerless unassigned created sort docs"
  else
    echo "missing: human task ownerless unassigned created sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless unassigned created sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_ownerless_list_created_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc" "README.md" && \
     grep -Fq "status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_LIST_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq '"status": "pending"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"assignment_state": "unassigned"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"assignment_source": "none"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"/v1/human/tasks"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq "Promoted milestone capability \`human_task_ownerless_list_created_sort\` to released" "CHANGELOG.md"; then
  echo "ok: human task ownerless list created sort docs"
  else
    echo "missing: human task ownerless list created sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless list created sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_ownerless_list_last_transition_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" "README.md" && \
     grep -Fq "status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_LIST_TRANSITION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq '"status": "pending"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"assignment_state": "unassigned"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"assignment_source": "none"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"sort": "last_transition_desc"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc&limit=20" "HTTP_EXAMPLES.http" && \
     grep -Fq 'Promoted milestone capability `human_task_ownerless_list_last_transition_sort` to released' "CHANGELOG.md"; then
    echo "ok: human task ownerless list last-transition sort docs"
  else
    echo "missing: human task ownerless list last-transition sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless list last-transition sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_session_ownerless_created_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "session_id=<id>&assignment_source=none&sort=created_asc" "README.md" && \
     grep -Fq "session_id=<id>&assignment_source=none&sort=created_asc" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `human_task_session_ownerless_created_sort` to released' "CHANGELOG.md" && \
     grep -Fq "SESSION_HUMAN_NONE_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"session_id": session_id, "assignment_source": "none", "sort": "created_asc"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks?session_id={{session_id}}&assignment_source=none&sort=created_asc&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: human task session ownerless created sort docs"
  else
    echo "missing: human task session ownerless created sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task session ownerless created sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_session_ownerless_last_transition_sort"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "session_id=<id>&assignment_source=none&sort=last_transition_desc" "README.md" && \
     grep -Fq "session_id=<id>&assignment_source=none&sort=last_transition_desc" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_NONE_TRANSITION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"session_id": session_id, "assignment_source": "none", "sort": "last_transition_desc"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks?session_id={{session_id}}&assignment_source=none&sort=last_transition_desc&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: human task session ownerless last-transition sort docs"
  else
    echo "missing: human task session ownerless last-transition sort docs" >&2
    missing=1
  fi
else
  echo "missing: human task session ownerless last-transition sort milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_session_ownerless_mixed_source_isolation"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "manual and auto-preselected neighbors too" "README.md" && \
     grep -Fq "manual and auto-preselected neighbors present" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_NONE_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "SESSION_HUMAN_NONE_TRANSITION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "keeping mixed-source neighbors out" "scripts/smoke_api.sh" && \
     grep -Fq "ownerless_session_created_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_session_transition_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'Promoted milestone capability `human_task_session_ownerless_mixed_source_isolation` to released' "CHANGELOG.md" && \
     grep -Fq "release/operator guards" "CHANGELOG.md" && \
     grep -Fq "session-list mixed-source isolation contract" "CHANGELOG.md"; then
    echo "ok: human task session ownerless mixed-source isolation release baseline"
  else
    echo "missing: human task session ownerless mixed-source isolation release baseline" >&2
    missing=1
  fi
else
  echo "missing: human task session ownerless mixed-source isolation milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_ownerless_sorted_queue_mixed_source_isolation"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "manual and auto-preselected neighbors" "README.md" && \
     grep -Fq "manual and auto-preselected neighbors present" "RUNBOOK.md" && \
     grep -Fq "HUMAN_OWNERLESS_BACKLOG_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_OWNERLESS_UNASSIGNED_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_OWNERLESS_LIST_CREATED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "keeping mixed-source neighbors out" "scripts/smoke_api.sh" && \
     grep -Fq "ownerless_backlog_created_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_unassigned_created_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_list_created_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_backlog_transition_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_unassigned_transition_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_list_transition_all_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: human task ownerless sorted queue mixed-source isolation docs"
  else
    echo "missing: human task ownerless sorted queue mixed-source isolation docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless sorted queue mixed-source isolation milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_ownerless_priority_summary_mixed_source_counts"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "ownerless \`priority-summary?assignment_state=unassigned&assignment_source=none\` slice is now explicitly covered after mixed-source churn" "README.md" && \
     grep -Fq "ownerless \`priority-summary?status=pending&assignment_state=unassigned&assignment_source=none\` slice is now also covered after mixed-source churn" "RUNBOOK.md" && \
     grep -Fq "PRIORITY_SUMMARY_NONE_MIXED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "stay ownerless-only after mixed-source churn" "scripts/smoke_api.sh" && \
     grep -Fq "ownerless_summary_after_churn" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'ownerless_summary_after_churn_body["total"] == 2' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'ownerless_summary_after_churn_body["counts_json"]["low"] == 2' "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: human task ownerless priority summary mixed-source counts docs"
  else
    echo "missing: human task ownerless priority summary mixed-source counts docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless priority summary mixed-source counts milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_ownerless_unsorted_queue_mixed_source_isolation"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "unsorted ownerless \`assignment_source=none\` list, backlog, and unassigned slices are now also explicitly covered after mixed-source churn" "README.md" && \
     grep -Fq "unsorted ownerless \`assignment_source=none\` list, backlog, and unassigned slices are now also covered after mixed-source churn" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `human_task_ownerless_unsorted_queue_mixed_source_isolation` to released' "CHANGELOG.md" && \
     grep -Fq "HUMAN_OWNERLESS_LIST_MIXED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_UNASSIGNED_NONE_MIXED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_OWNERLESS_BACKLOG_MIXED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "stay ownerless-only after mixed-source churn" "scripts/smoke_api.sh" && \
     grep -Fq "ownerless_list_after_churn_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_unassigned_after_churn_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_backlog_after_churn_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: human task ownerless unsorted queue mixed-source isolation docs"
  else
    echo "missing: human task ownerless unsorted queue mixed-source isolation docs" >&2
    missing=1
  fi
else
  echo "missing: human task ownerless unsorted queue mixed-source isolation milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "human_task_session_ownerless_unsorted_mixed_source_isolation"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "unsorted session-scoped \`session_id=<id>&assignment_source=none\` slice is now also explicitly covered after mixed-source churn" "README.md" && \
     grep -Fq "unsorted session-scoped \`session_id=<id>&assignment_source=none\` slice is now also covered after mixed-source churn" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_NONE_MIXED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "stay ownerless-only after mixed-source churn" "scripts/smoke_api.sh" && \
     grep -Fq "ownerless_session_list_after_churn_ids ==" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: human task session ownerless unsorted mixed-source isolation docs"
  else
    echo "missing: human task session ownerless unsorted mixed-source isolation docs" >&2
    missing=1
  fi
else
  echo "missing: human task session ownerless unsorted mixed-source isolation milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "session_ownerless_projection_mixed_source_counts"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "mixed-source session-detail ownerless slice is now also explicitly count-checked" "README.md" && \
     grep -Fq "mixed-source session-detail ownerless projection is now also count-checked" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_NONE_PROJECTION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "longer empty-source history trail" "scripts/smoke_api.sh" && \
     grep -Fq 'len(ownerless_session_projection_body["human_tasks"]) == 2' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'len(ownerless_session_projection_body["human_task_assignment_history"]) > len(' "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: session ownerless projection mixed-source counts docs"
  else
    echo "missing: session ownerless projection mixed-source counts docs" >&2
    missing=1
  fi
else
  echo "missing: session ownerless projection mixed-source counts milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "session_ownerless_projection_created_order"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "human_task_assignment_source=none" "README.md" && \
     grep -Fq "human_task_assignment_source=none" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_NONE_PROJECTION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"human_task_assignment_source": "none"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_session_projection_ids == [ownerless_task_id, ownerless_newer_task_id]" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_session_history_ids == [ownerless_task_id, ownerless_newer_task_id]" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/rewrite/sessions/{{session_id}}?human_task_assignment_source=none" "HTTP_EXAMPLES.http" && \
     grep -Fq 'Promoted milestone capability `session_ownerless_projection_created_order` to released' "CHANGELOG.md"; then
    echo "ok: session ownerless projection created order docs"
  else
    echo "missing: session ownerless projection created order docs" >&2
    missing=1
  fi
else
  echo "missing: session ownerless projection created order milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry
    for entry in milestone["capabilities"]
    if entry["name"] == "session_ownerless_projection_mixed_source_isolation"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq "manual and auto-preselected work" "README.md" && \
     grep -Fq "manual and auto-preselected neighbors" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_NONE_PROJECTION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "two-row current ownerless slice" "scripts/smoke_api.sh" && \
     grep -Fq 'row["human_task_id"] not in {manual_task_id, auto_task_id}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ownerless_session_projection_history_all_ids[:4]" "${SMOKE_RUNTIME_GUARD_TARGET}"; then
    echo "ok: session ownerless projection mixed-source isolation docs"
  else
    echo "missing: session ownerless projection mixed-source isolation docs" >&2
    missing=1
  fi
else
  echo "missing: session ownerless projection mixed-source isolation milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_history_source_filter")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'assignment-history` also accepts `event_name`, `assigned_operator_id`, `assigned_by_actor_id`, and `assignment_source`' "README.md" && \
     grep -Fq "assignment_source" "RUNBOOK.md" && \
     grep -Fq "HUMAN_HISTORY_RECOMMENDED_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"limit": 10, "assignment_source": "recommended"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks/{{human_task_id}}/assignment-history?limit=20&assignment_source=recommended" "HTTP_EXAMPLES.http" && \
     grep -Fq "Promoted the human-task assignment-history source-filter slice into a released milestone capability" "CHANGELOG.md"; then
    echo "ok: human task assignment-history source filter docs"
  else
    echo "missing: human task assignment-history source filter docs" >&2
    missing=1
  fi
else
  echo "missing: human task assignment-history source filter milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_human_task_assignment_source_filter")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'also accepts `human_task_assignment_source`' "README.md" && \
     grep -Fq "human_task_assignment_source" "RUNBOOK.md" && \
     grep -Fq "SESSION_HUMAN_MANUAL_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_REWRITE_AUTO_SESSION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"human_task_assignment_source": "manual"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/rewrite/sessions/{{session_id}}?human_task_assignment_source=manual" "HTTP_EXAMPLES.http"; then
    echo "ok: session human-task assignment-source filter docs"
  else
    echo "missing: session human-task assignment-source filter docs" >&2
    missing=1
  fi
else
  echo "missing: session human-task assignment-source filter milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(
    entry for entry in milestone["capabilities"] if entry["name"] == "session_scoped_human_task_assignment_source_filters"
)
assert capability["status"] == "released"
PY
then
  if grep -Fq 'session_id=<id>&assignment_source=<source>' "README.md" && \
     grep -Fq 'session_id=<id>&assignment_source=<source>' "RUNBOOK.md" && \
     grep -Fq "PRIORITY_SUMMARY_MANUAL_SESSION_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "HUMAN_REWRITE_AUTO_LIST_JSON" "scripts/smoke_api.sh" && \
     grep -Fq 'params={"session_id": session_id, "assignment_source": "manual"}' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "/v1/human/tasks?principal_id={{principal_id}}&session_id={{session_id}}&assignment_source=manual&limit=20" "HTTP_EXAMPLES.http"; then
    echo "ok: session-scoped human task assignment-source queue docs"
  else
    echo "missing: session-scoped human task assignment-source queue docs" >&2
    missing=1
  fi
else
  echo "missing: session-scoped human task assignment-source queue milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "task_contract_workflow_templates")
assert capability["status"] == "released"
PY
then
  if grep -Fq "workflow_template" "README.md" && \
     grep -Fq "artifact_then_dispatch" "README.md" && \
     grep -Fq "workflow_template" "RUNBOOK.md" && \
     grep -Fq "artifact_then_dispatch" "RUNBOOK.md" && \
     grep -Fq "stakeholder_dispatch" "HTTP_EXAMPLES.http" && \
     grep -Fq "artifact_then_dispatch" "HTTP_EXAMPLES.http" && \
     grep -Fq "stakeholder_dispatch" "scripts/smoke_api.sh" && \
     grep -Fq "step_connector_dispatch" "scripts/smoke_api.sh" && \
     grep -Fq "stakeholder_dispatch" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "step_connector_dispatch" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "tests/test_task_contract_step_templates.py" "scripts/test_postgres_contracts.sh" && \
     grep -Fq 'Promoted milestone capability `task_contract_workflow_templates` to released' "CHANGELOG.md" && \
     grep -Fq "baseline dispatch-template contract" "CHANGELOG.md" && \
     grep -Fq "release/operator guards now pin" "CHANGELOG.md"; then
    echo "ok: task contract workflow template release baseline"
  else
    echo "missing: task contract workflow template release baseline" >&2
    missing=1
  fi
else
  echo "missing: task contract workflow template milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "composable_post_artifact_workflow_packs")
assert capability["status"] == "released"
PY
then
  if grep -Fq "artifact_then_packs" "README.md" && \
     grep -Fq "post_artifact_packs" "README.md" && \
     grep -Fq "artifact_then_packs" "RUNBOOK.md" && \
     grep -Fq "post_artifact_packs" "RUNBOOK.md" && \
     grep -Fq "stakeholder_pack_template" "HTTP_EXAMPLES.http" && \
     grep -Fq "artifact_then_packs" "HTTP_EXAMPLES.http" && \
     grep -Fq "artifact_then_packs" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "unknown_post_artifact_pack:unknown_pack" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "tests/test_task_contract_step_templates.py" "scripts/test_postgres_contracts.sh" && \
     grep -Fq 'Promoted milestone capability `composable_post_artifact_workflow_packs` to released' "CHANGELOG.md" && \
     grep -Fq "post_artifact_packs=[dispatch,memory_candidate]" "CHANGELOG.md" && \
     grep -Fq "release/operator guards now pin" "CHANGELOG.md"; then
    echo "ok: composable post-artifact workflow packs release baseline"
  else
    echo "missing: composable post-artifact workflow packs release baseline" >&2
    missing=1
  fi
else
  echo "missing: composable post-artifact workflow packs milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "workflow_template_registry_validation")
assert capability["status"] == "released"
PY
then
  if grep -Fq "unknown_workflow_template:<value>" "README.md" && \
     grep -Fq "unknown_workflow_template:<value>" "RUNBOOK.md" && \
     grep -Fq "unknown_workflow_template:<value>" "CHANGELOG.md" && \
     grep -Fq "unknown_workflow_template:not_real" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "_workflow_template_builders" "ea/app/services/planner.py" && \
     grep -Fq "except PlanValidationError as exc" "ea/app/api/routes/plans.py" && \
     grep -Fq "except PlanValidationError as exc" "ea/app/api/routes/rewrite.py" && \
     grep -Fq 'Promoted milestone capability `workflow_template_registry_validation` to released' "CHANGELOG.md"; then
    echo "ok: workflow template registry validation docs"
  else
    echo "missing: workflow template registry validation docs" >&2
    missing=1
  fi
else
  echo "missing: workflow template registry validation milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "postgres_contract_matrix")
assert capability["status"] == "released"
PY
then
  if grep -Fq "current matrix covers artifacts, channel runtime, approvals, policy decisions, and task contracts" "README.md" && \
     grep -Fq 'Current `scripts/test_postgres_contracts.sh` coverage includes artifacts, channel runtime, approvals, policy decisions, and task contracts.' "RUNBOOK.md" && \
     grep -Fq "bash scripts/test_postgres_contracts.sh" ".github/workflows/smoke-runtime.yml" && \
     grep -Fq "tests/test_postgres_contract_matrix_integration.py" "scripts/test_postgres_contracts.sh" && \
     grep -Fq "test_postgres_approvals_create_decide_and_list_history" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "test_postgres_policy_decisions_append_and_filter_recent" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "test_postgres_task_contracts_upsert_get_and_list" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "test_postgres_evidence_object_repo_materializes_queries_and_merges_evidence_pack_rows" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq 'Promoted milestone capability `postgres_contract_matrix` to released' "CHANGELOG.md"; then
    echo "ok: postgres contract matrix release baseline"
  else
    echo "missing: postgres contract matrix release baseline" >&2
    missing=1
  fi
else
  echo "missing: postgres contract matrix milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "review_then_dispatch_workflow_template")
assert capability["status"] == "released"
PY
then
  if grep -Fq "stakeholder_review_dispatch" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "stakeholder_review_dispatch" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "hybrid@example.com" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "step_human_review" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "review and send a stakeholder briefing" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "stakeholder_review_dispatch" "scripts/smoke_api.sh" && \
     grep -Fq "hybrid@example.com" "scripts/smoke_api.sh" && \
     grep -Fq "step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch" "README.md" && \
     grep -Fq "step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch" "RUNBOOK.md" && \
     grep -Fq "combined human-review case" "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `review_then_dispatch_workflow_template` to released' "CHANGELOG.md" && \
     grep -Fq "release/operator guards now pin" "CHANGELOG.md" && \
     grep -Fq "review-then-dispatch workflow" "CHANGELOG.md"; then
    echo "ok: review-then-dispatch workflow template release baseline"
  else
    echo "missing: review-then-dispatch workflow template release baseline" >&2
    missing=1
  fi
else
  echo "missing: review-then-dispatch workflow template milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "tool_then_artifact_workflow_template")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'workflow_template": "tool_then_artifact"' "tests/test_task_contract_step_templates.py" && \
     grep -Fq "browseract_ltd_discovery_generic" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "unsupported_tool_then_artifact" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "browseract_ltd_discovery_generic" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "browseract_ltd_discovery_generic" "scripts/smoke_api.sh" && \
     grep -Fq "workflow_template=tool_then_artifact" "README.md" && \
     grep -Fq "workflow_template=tool_then_artifact" "RUNBOOK.md" && \
     grep -Fq "workflow_template=tool_then_artifact" "CHANGELOG.md" && \
     grep -Fq "browseract_ltd_discovery_generic" "HTTP_EXAMPLES.http"; then
    echo "ok: tool-then-artifact workflow template docs and smoke coverage"
  else
    echo "missing: tool-then-artifact workflow template docs or smoke coverage" >&2
    missing=1
  fi
else
  echo "missing: tool-then-artifact workflow template milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "browseract_account_inventory_tool_execution_slice")
assert capability["status"] == "released"
assert "release/operator guards" in capability["notes"]
PY
then
  if grep -Fq "browseract.extract_account_inventory" "tests/test_tool_execution.py" && \
     grep -Fq "step_browseract_inventory_extract" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "browseract_ltd_inventory_refresh" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "browseract.extract_account_inventory" "scripts/smoke_api.sh" && \
     grep -Fq "browseract.extract_account_inventory" "README.md" && \
     grep -Fq "browseract.extract_account_inventory" "RUNBOOK.md" && \
     grep -Fq "browseract.extract_account_inventory" "CHANGELOG.md" && \
     grep -Fq "browseract_ltd_inventory_refresh" "HTTP_EXAMPLES.http" && \
     grep -Fq "browseract.extract_account_inventory" "LTDs.md"; then
    echo "ok: browseract inventory tool execution docs and smoke coverage"
  else
    echo "missing: browseract inventory tool execution docs or smoke coverage" >&2
    missing=1
  fi
else
  echo "missing: browseract inventory tool execution milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "browseract_live_discovery_input_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq '"account_hints_json"' "ea/app/services/skills.py" && \
     grep -Fq '"run_url"' "ea/app/services/skills.py" && \
     grep -Fq "requested_run_url" "tests/test_tool_execution.py" && \
     grep -Fq "account_hints_json" "tests/test_skills.py" && \
     grep -Fq "requested_run_url" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "account_hints_json" "scripts/smoke_api.sh" && \
     grep -Fq "account_hints_json" "README.md" && \
     grep -Fq "account_hints_json" "RUNBOOK.md" && \
     grep -Fq "account_hints_json" "CHANGELOG.md" && \
     grep -Fq "account_hints_json" "HTTP_EXAMPLES.http"; then
    echo "ok: browseract live discovery input projection docs and smoke coverage"
  else
    echo "missing: browseract live discovery input projection docs or smoke coverage" >&2
    missing=1
  fi
else
  echo "missing: browseract live discovery input projection milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_then_memory_candidate_workflow_template")
assert capability["status"] == "released"
PY
then
  if grep -Fq "artifact_then_memory_candidate" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "step_memory_candidate_stage" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "stakeholder_memory_candidate" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "step_memory_candidate_stage" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq '"memory_write_allowed",' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "stakeholder_memory_candidate" "scripts/smoke_api.sh" && \
     grep -Fq "step_memory_candidate_stage" "scripts/smoke_api.sh" && \
     grep -Fq "artifact_then_memory_candidate" "README.md" && \
     grep -Fq "step_input_prepare -> step_policy_evaluate -> step_artifact_save -> step_memory_candidate_stage" "README.md" && \
     grep -Fq "artifact_then_memory_candidate" "RUNBOOK.md" && \
     grep -Fq "step_input_prepare -> step_policy_evaluate -> step_artifact_save -> step_memory_candidate_stage" "RUNBOOK.md" && \
     grep -Fq "artifact_then_memory_candidate" "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `artifact_then_memory_candidate_workflow_template` to released' "CHANGELOG.md" && \
     grep -Fq "stakeholder_memory_candidate" "HTTP_EXAMPLES.http"; then
    echo "ok: artifact-then-memory-candidate workflow template docs and smoke coverage"
  else
    echo "missing: artifact-then-memory-candidate workflow template docs or smoke coverage" >&2
    missing=1
  fi
else
  echo "missing: artifact-then-memory-candidate workflow template milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "dispatch_then_memory_candidate_workflow_template")
assert capability["status"] == "released"
PY
then
  if grep -Fq "artifact_then_dispatch_then_memory_candidate" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "stakeholder_dispatch_memory_candidate" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "dispatch-memory@example.com" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "stakeholder_dispatch_memory_candidate" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "dispatch-memory@example.com" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "stakeholder_dispatch_memory_candidate" "scripts/smoke_api.sh" && \
     grep -Fq "dispatch-memory@example.com" "scripts/smoke_api.sh" && \
     grep -Fq "artifact_then_dispatch_then_memory_candidate" "README.md" && \
     grep -Fq "step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" "README.md" && \
     grep -Fq "artifact_then_dispatch_then_memory_candidate" "RUNBOOK.md" && \
     grep -Fq "step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" "RUNBOOK.md" && \
     grep -Fq "artifact_then_dispatch_then_memory_candidate" "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `dispatch_then_memory_candidate_workflow_template` to released' "CHANGELOG.md" && \
     grep -Fq "stakeholder_dispatch_memory_candidate" "HTTP_EXAMPLES.http"; then
    echo "ok: dispatch-then-memory-candidate workflow template docs and smoke coverage"
  else
    echo "missing: dispatch-then-memory-candidate workflow template docs or smoke coverage" >&2
    missing=1
  fi
else
  echo "missing: dispatch-then-memory-candidate workflow template milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "review_dispatch_then_memory_candidate_workflow_template")
assert capability["status"] == "released"
PY
then
  if grep -Fq "stakeholder_review_dispatch_memory_candidate" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "reviewed-memory@example.com" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "stakeholder_review_dispatch_memory_candidate" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "reviewed-memory@example.com" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "stakeholder_review_dispatch_memory_candidate" "scripts/smoke_api.sh" && \
     grep -Fq "reviewed-memory@example.com" "scripts/smoke_api.sh" && \
     grep -Fq "step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" "README.md" && \
     grep -Fq "step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `review_dispatch_then_memory_candidate_workflow_template` to released' "CHANGELOG.md" && \
     grep -Fq "hybrid human-review case" "CHANGELOG.md" && \
     grep -Fq "stakeholder_review_dispatch_memory_candidate" "HTTP_EXAMPLES.http"; then
    echo "ok: review-dispatch-then-memory-candidate workflow template docs and smoke coverage"
  else
    echo "missing: review-dispatch-then-memory-candidate workflow template docs or smoke coverage" >&2
    missing=1
  fi
else
  echo "missing: review-dispatch-then-memory-candidate workflow template milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "execution_queue_retry_runtime")
assert capability["status"] == "released"
PY
then
  if grep -Fq "test_retry_failure_strategy_requeues_a_failed_step_until_it_succeeds" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "test_retry_failure_strategy_exhausts_into_terminal_session_failure" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "step_retry_scheduled" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "test_postgres_execution_queue_retry_requeues_the_same_row" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "tests/test_queue_retry_contracts.py" "scripts/test_postgres_contracts.sh" && \
     grep -Fq "failure_strategy=retry" "README.md" && \
     grep -Fq "failure_strategy=retry" "RUNBOOK.md" && \
     grep -Fq 'Queued step failures can now actually honor `failure_strategy=retry`' "CHANGELOG.md"; then
    echo "ok: execution queue retry runtime docs and contract coverage"
  else
    echo "missing: execution queue retry runtime docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: execution queue retry runtime milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "inline_retry_drain_runtime")
assert capability["status"] == "released"
PY
then
  if grep -Fq "test_execute_task_artifact_drains_zero_backoff_retries_inline_to_completion" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "test_approval_resume_drains_zero_backoff_retries_inline_to_completion" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "drain_session_inline(" "ea/app/services/execution_queue_service.py" && \
     grep -Fq "_next_eligible_queue_item_for_session" "ea/app/services/execution_queue_service.py" && \
     grep -Fq "zero-backoff retries now keep draining same-session queue work inline" "README.md" && \
     grep -Fq "retry_backoff_seconds=0" "RUNBOOK.md" && \
     grep -Fq "Zero-backoff retries now keep draining the same session inline" "CHANGELOG.md"; then
    echo "ok: inline retry drain runtime docs and contract coverage"
  else
    echo "missing: inline retry drain runtime docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: inline retry drain runtime milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "contract_retry_policy_metadata")
assert capability["status"] == "released"
PY
then
  if grep -Fq "test_planner_can_compile_artifact_retry_policy_from_task_contract_metadata" "tests/test_planner.py" && \
     grep -Fq "test_planner_can_compile_dispatch_retry_policy_from_task_contract_metadata" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "test_execute_task_artifact_uses_compiled_artifact_retry_policy_from_contract_metadata" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "_step_retry_policy" "ea/app/services/planner.py" && \
     grep -Fq 'prefix="artifact"' "ea/app/services/planner.py" && \
     grep -Fq 'prefix="dispatch"' "ea/app/services/planner.py" && \
     grep -Fq "budget_policy_json.artifact_failure_strategy|artifact_max_attempts|artifact_retry_backoff_seconds" "README.md" && \
     grep -Fq "artifact_failure_strategy|artifact_max_attempts|artifact_retry_backoff_seconds" "RUNBOOK.md" && \
     grep -Fq "Task-contract metadata can now tune the built-in artifact and dispatch retry posture" "CHANGELOG.md"; then
    echo "ok: contract retry policy metadata docs and contract coverage"
  else
    echo "missing: contract retry policy metadata docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: contract retry policy metadata milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "delayed_retry_async_acceptance")
assert capability["status"] == "released"
PY
then
  if grep -Fq "test_execute_task_artifact_returns_queued_async_state_for_delayed_retry" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "test_approval_resume_keeps_delayed_retry_sessions_async_instead_of_erroring" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "test_plan_execute_surfaces_delayed_retry_as_queued_async_acceptance" "tests/test_plan_execute_input_contracts.py" && \
     grep -Fq "test_rewrite_artifact_surfaces_delayed_retry_as_queued_async_acceptance" "tests/test_rewrite_api_scope_contracts.py" && \
     grep -Fq 'example["status"] == "queued"' "tests/test_openapi_async_acceptance_examples_contracts.py" && \
     grep -Fq "AsyncExecutionQueuedError" "ea/app/services/orchestrator.py" && \
     grep -Fq "except AsyncExecutionQueuedError as exc" "ea/app/api/routes/plans.py" && \
     grep -Fq "except AsyncExecutionQueuedError as exc" "ea/app/api/routes/rewrite.py" && \
     grep -Fq 'first-class `202 queued` async acceptance' "README.md" && \
     grep -Fq '`202 queued`' "RUNBOOK.md" && \
     grep -Fq 'Nonzero-backoff retries now surface as a first-class `202 queued` async acceptance' "CHANGELOG.md"; then
    echo "ok: delayed retry async acceptance docs and contract coverage"
  else
    echo "missing: delayed retry async acceptance docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: delayed retry async acceptance milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "review_dispatch_delayed_retry_runtime")
assert capability["status"] == "released"
PY
then
  if grep -Fq "test_planner_can_compile_review_then_dispatch_retry_policy_from_task_contract_metadata" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "test_review_then_dispatch_workflow_template_keeps_delayed_dispatch_retry_async_after_approval" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "test_review_then_dispatch_delayed_retry_stays_queued_after_http_approval" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "stakeholder_review_dispatch_retry" "scripts/smoke_api.sh" && \
     grep -Fq "hybrid-retry@example.com" "scripts/smoke_api.sh" && \
     grep -Fq "expected delayed review-then-dispatch approval flow to leave dispatch queued behind next_attempt_at" "scripts/smoke_api.sh" && \
     grep -Fq "dispatch_failure_strategy|max_attempts|retry_backoff_seconds" "README.md" && \
     grep -Fq "dispatch_failure_strategy|dispatch_max_attempts|dispatch_retry_backoff_seconds" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `review_dispatch_delayed_retry_runtime` to released' "CHANGELOG.md"; then
    echo "ok: review-then-dispatch delayed retry runtime docs and contract coverage"
  else
    echo "missing: review-then-dispatch delayed retry runtime docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: review-then-dispatch delayed retry runtime milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_catalog_layer")
assert capability["status"] == "released"
PY
then
  if grep -Fq "tests/test_skills.py" "scripts/test_postgres_contracts.sh" && \
     grep -Fq "test_skill_catalog_flow_and_meeting_prep_compilation" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "meeting_prep" "tests/test_skills.py" && \
     grep -Fq 'POST /v1/skills' "SKILLS.md" && \
     grep -Fq '`meeting_prep`' "SKILLS.md" && \
     grep -Fq "/v1/skills*" "README.md" && \
     grep -Fq "SKILLS.md" "README.md" && \
     grep -Fq "/v1/skills" "RUNBOOK.md" && \
     grep -Fq "/v1/skills" "HTTP_EXAMPLES.http" && \
     grep -Fq "meeting_prep" "scripts/smoke_api.sh" && \
     grep -Fq "skills ok" "scripts/smoke_api.sh" && \
     grep -Fq 'first-class `/v1/skills` catalog' "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `skill_catalog_layer` to released' "CHANGELOG.md"; then
    echo "ok: skill catalog layer docs and contract coverage"
  else
    echo "missing: skill catalog layer docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: skill catalog layer milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "ltd_inventory_refresh_skill_catalog_slice")
assert capability["status"] == "released"
PY
then
  if grep -Fq "test_skill_catalog_can_execute_ltd_inventory_refresh_skill" "tests/test_skills.py" && \
     grep -Fq "test_skill_catalog_can_project_ltd_inventory_refresh_runtime" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "ltd_inventory_refresh" "scripts/smoke_api.sh" && \
     grep -Fq "ltd_inventory_refresh" "README.md" && \
     grep -Fq "ltd_inventory_refresh" "RUNBOOK.md" && \
     grep -Fq "ltd_inventory_refresh" "CHANGELOG.md" && \
     grep -Fq "ltd_inventory_refresh" "HTTP_EXAMPLES.http" && \
     grep -Fq '`ltd_inventory_refresh`' "SKILLS.md"; then
    echo "ok: ltd inventory refresh skill docs and smoke coverage"
  else
    echo "missing: ltd inventory refresh skill docs or smoke coverage" >&2
    missing=1
  fi
else
  echo "missing: ltd inventory refresh skill milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_status_transition_api")
assert capability["status"] == "released"
PY
then
  if grep -Fq "test_retry_scheduling_uses_explicit_session_status_transition_api" "tests/test_queue_retry_contracts.py" && \
     grep -Fq "_RecordingLedger" "tests/test_queue_retry_contracts.py" && \
     grep -Fq 'ledger.status_updates == ["running", "queued"]' "tests/test_queue_retry_contracts.py" && \
     grep -Fq 'ledger.completion_updates == []' "tests/test_queue_retry_contracts.py" && \
     grep -Fq 'ledger.set_session_status(session.session_id, "awaiting_approval")' "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "set_session_status" "ea/app/repositories/ledger.py" && \
     grep -Fq "set_session_status" "ea/app/repositories/ledger_postgres.py" && \
     grep -Fq '_set_session_status(session_id, "awaiting_approval")' "ea/app/services/execution_approval_pause_service.py" && \
     grep -Fq "set_session_status(...)" "README.md" && \
     grep -Fq "set_session_status(...)" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `session_status_transition_api` to released' "CHANGELOG.md" && \
     grep -Fq "release/operator guards now pin that explicit nonterminal session-status transition contract" "MILESTONE.json" && \
     grep -Fq "set_session_status(...)" "CHANGELOG.md"; then
    echo "ok: session-status transition api release baseline"
  else
    echo "missing: session-status transition api release baseline" >&2
    missing=1
  fi
else
  echo "missing: session-status transition api milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_provider_hints_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq "provider_hints_json" "ea/app/domain/models.py" && \
     grep -Fq "provider_hints_json" "ea/app/services/skills.py" && \
     grep -Fq "provider_hints_json" "ea/app/api/routes/skills.py" && \
     grep -Fq 'body["provider_hints_json"]["primary"] == ["1min.AI"]' "tests/test_skills.py" && \
     grep -Fq 'created.json()["provider_hints_json"]["primary"] == ["1min.AI"]' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "provider_hints_json" "scripts/smoke_api.sh" && \
     grep -Fq "provider-hint" "README.md" && \
     grep -Fq "provider policy" "RUNBOOK.md" && \
     grep -Fq "provider_hints_json" "CHANGELOG.md" && \
     grep -Fq "provider_hints_json" "HTTP_EXAMPLES.http" && \
     grep -Fq "provider_hints_json" "SKILLS.md"; then
    echo "ok: skill provider hints projection docs and contract coverage"
  else
    echo "missing: skill provider hints projection docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: skill provider hints projection milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_provider_hint_filtering")
assert capability["status"] == "released"
PY
then
  if grep -Fq "provider_hint: str = Query" "ea/app/api/routes/skills.py" && \
     grep -Fq "provider_hint=provider_hint" "ea/app/api/routes/skills.py" && \
     grep -Fq "def list_skills(self, limit: int = 100, provider_hint: str = \"\")" "ea/app/services/skills.py" && \
     grep -Fq "_collect_string_values" "ea/app/services/skills.py" && \
     grep -Fq 'client.get("/v1/skills", params={"limit": 10, "provider_hint": "browseract"})' "tests/test_skills.py" && \
     grep -Fq 'client.get("/v1/skills", params={"limit": 10, "provider_hint": "browseract"})' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "provider_hint=browseract" "scripts/smoke_api.sh" && \
     grep -Fq "provider_hint=BrowserAct" "README.md" && \
     grep -Fq "provider_hint=<value>" "RUNBOOK.md" && \
     grep -Fq "provider_hint=<value>" "CHANGELOG.md" && \
     grep -Fq "provider_hint=BrowserAct" "HTTP_EXAMPLES.http" && \
     grep -Fq "provider_hint=BrowserAct" "SKILLS.md"; then
    echo "ok: skill provider hint filtering docs and contract coverage"
  else
    echo "missing: skill provider hint filtering docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: skill provider hint filtering milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq "_resolve_skill_key(" "ea/app/api/routes/plans.py" && \
     grep -Fq "skill_key: str" "ea/app/api/routes/plans.py" && \
     grep -Fq 'compiled.json()["skill_key"] == "meeting_prep"' "tests/test_skills.py" && \
     grep -Fq 'executed.json()["skill_key"] == "meeting_prep"' "tests/test_skills.py" && \
     grep -Fq 'body["skill_key"] == "rewrite_text"' "tests/test_plan_execute_input_contracts.py" && \
     grep -Fq 'plan_approval["skill_key"] == "decision_briefing"' "tests/test_openapi_async_acceptance_examples_contracts.py" && \
     grep -Fq "compiled.get('skill_key','')" "scripts/smoke_api.sh" && \
     grep -Fq "body.get('skill_key','')" "scripts/smoke_api.sh" && \
     grep -Fq 'resolved `skill_key`' "README.md" && \
     grep -Fq 'resolved `skill_key`' "RUNBOOK.md" && \
     grep -Fq 'resolved `skill_key`' "CHANGELOG.md"; then
    echo "ok: skill identity projection docs and contract coverage"
  else
    echo "missing: skill identity projection docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: skill identity projection milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "plan_skill_key_entrypoint_alias")
assert capability["status"] == "released"
PY
then
  if grep -Fq "_resolve_task_key(" "ea/app/api/routes/plans.py" && \
     grep -Fq 'skill_key: str = Field(default="", max_length=200)' "ea/app/api/routes/plans.py" && \
     grep -Fq "task_or_skill_key_required" "ea/app/api/routes/plans.py" && \
     grep -Fq "task_skill_key_mismatch" "ea/app/api/routes/plans.py" && \
     grep -Fq "compiled_via_skill = client.post(" "tests/test_skills.py" && \
     grep -Fq "executed_via_skill = client.post(" "tests/test_skills.py" && \
     grep -Fq "task_or_skill_key_required" "tests/test_plan_execute_input_contracts.py" && \
     grep -Fq "compiled_via_skill = client.post(" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "LTD_SKILL_PLAN_BY_SKILL_JSON" "scripts/smoke_api.sh" && \
     grep -Fq "accepts either \`task_key\` or \`skill_key\`" "README.md" && \
     grep -Fq "accepts either \`task_key\` or \`skill_key\`" "RUNBOOK.md" && \
     grep -Fq "accept either \`task_key\` or \`skill_key\`" "CHANGELOG.md" && \
     grep -Fq '"skill_key": "meeting_prep"' "HTTP_EXAMPLES.http" && \
     grep -Fq '"skill_key": "ltd_inventory_refresh"' "HTTP_EXAMPLES.http"; then
    echo "ok: plan skill_key entrypoint alias docs and contract coverage"
  else
    echo "missing: plan skill_key entrypoint alias docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: plan skill_key entrypoint alias milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "ltd_discovery_markdown_refresh")
assert capability["status"] == "released"
PY
then
  if grep -Fq "DISCOVERY_TRACKING_HEADING" "ea/app/services/ltd_inventory_markdown.py" && \
     grep -Fq "build_discovery_updates" "ea/app/services/ltd_inventory_markdown.py" && \
     grep -Fq "refresh_ltds_from_inventory.py" "scripts/refresh_ltds_from_inventory.sh" && \
     grep -Fq "update_discovery_tracking_table" "scripts/refresh_ltds_from_inventory.py" && \
     grep -Fq "test_update_discovery_tracking_table_rewrites_matching_services_only" "tests/test_ltd_inventory_markdown.py" && \
     grep -Fq "test_refresh_ltds_script_can_write_updated_markdown" "tests/test_ltd_inventory_markdown.py" && \
     grep -Fq "refresh_ltds_from_inventory.sh" "README.md" && \
     grep -Fq "refresh_ltds_from_inventory.sh" "RUNBOOK.md" && \
     grep -Fq "refresh_ltds_from_inventory.sh" "CHANGELOG.md" && \
     grep -Fq "refresh_ltds_from_inventory.sh" "LTDs.md"; then
    echo "ok: ltd discovery markdown refresh docs and contract coverage"
  else
    echo "missing: ltd discovery markdown refresh docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: ltd discovery markdown refresh milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "ltd_discovery_api_refresh_runner")
assert capability["status"] == "released"
PY
then
  if grep -Fq "build_inventory_execute_payload" "ea/app/services/ltd_inventory_api.py" && \
     grep -Fq "extract_inventory_output_json" "ea/app/services/ltd_inventory_api.py" && \
     grep -Fq "refresh_ltds_via_api.py" "scripts/refresh_ltds_via_api.sh" && \
     grep -Fq "/v1/plans/execute" "scripts/refresh_ltds_via_api.py" && \
     grep -Fq "update_discovery_tracking_table" "scripts/refresh_ltds_via_api.py" && \
     grep -Fq "test_refresh_ltds_via_api_script_executes_skill_and_updates_markdown" "tests/test_ltd_inventory_api.py" && \
     grep -Fq "refresh_ltds_via_api.sh" "scripts/smoke_api.sh" && \
     grep -Fq "refresh_ltds_via_api.sh" "README.md" && \
     grep -Fq "refresh_ltds_via_api.sh" "RUNBOOK.md" && \
     grep -Fq "refresh_ltds_via_api.sh" "CHANGELOG.md" && \
     grep -Fq "refresh_ltds_via_api.sh" "LTDs.md"; then
    echo "ok: ltd discovery api refresh runner docs and contract coverage"
  else
    echo "missing: ltd discovery api refresh runner docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: ltd discovery api refresh runner milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_evidence_pack_output_template")
assert capability["status"] == "released"
PY
then
  if grep -Fq "_artifact_output_template_key" "ea/app/services/planner.py" && \
     grep -Fq "artifact_output_template" "ea/app/services/planner.py" && \
     grep -Fq '"format": "evidence_pack"' "ea/app/services/execution_step_runtime_service.py" && \
     grep -Fq "test_planner_can_project_evidence_pack_artifact_output_template" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "test_artifact_then_memory_candidate_evidence_pack_persists_structured_output" "tests/test_task_contract_step_templates.py" && \
     grep -Fq "plan_execute_artifact_json" "scripts/smoke_api.sh" && \
     grep -Fq 'artifact_output_template":"evidence_pack' "scripts/smoke_api.sh" && \
     grep -Fq "artifact_output_template=evidence_pack" "README.md" && \
     grep -Fq "artifact_output_template=evidence_pack" "RUNBOOK.md" && \
     grep -Fq "artifact_output_template=evidence_pack" "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `artifact_evidence_pack_output_template` to released' "CHANGELOG.md" && \
     grep -Fq "release/operator guards now pin that evidence-pack output-template contract" "MILESTONE.json"; then
    echo "ok: artifact evidence-pack output template release baseline"
  else
    echo "missing: artifact evidence-pack output template release baseline" >&2
    missing=1
  fi
else
  echo "missing: artifact evidence-pack output template milestone release status" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "evidence_pack_memory_candidate_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq '"evidence_pack": artifact_structured_output_json' "ea/app/services/execution_step_runtime_service.py" && \
     grep -Fq 'fact_json["claims"]' "tests/test_task_contract_step_templates.py" && \
     grep -Fq 'fact_json["evidence_refs"]' "tests/test_task_contract_step_templates.py" && \
     grep -Fq "EVIDENCE_CANDIDATE_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq "memory-candidate staging" "README.md" && \
     grep -Fq "memory-candidate staging" "RUNBOOK.md" && \
     grep -Fq "memory-candidate staging" "CHANGELOG.md" && \
     grep -Fq 'Promoted milestone capability `evidence_pack_memory_candidate_projection` to released' "CHANGELOG.md"; then
    echo "ok: evidence-pack memory candidate projection docs and contract coverage"
  else
    echo "missing: evidence-pack memory candidate projection docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: evidence-pack memory candidate projection milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "evidence_object_ledger_api")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'prefix="/v1/evidence"' "ea/app/api/routes/evidence.py" && \
     grep -Fq "merge_objects(" "ea/app/services/evidence_runtime.py" && \
     grep -Fq '"evidence_object_id"' "ea/app/services/tool_execution_artifact_adapter.py" && \
     grep -Fq "test_tool_execution_service_materializes_evidence_objects_for_evidence_pack_artifacts" "tests/test_tool_execution.py" && \
     grep -Fq "test_postgres_evidence_object_repo_materializes_queries_and_merges_evidence_pack_rows" "tests/test_postgres_contract_matrix_integration.py" && \
     grep -Fq "test_evidence_object_routes_materialize_and_merge_evidence_pack_artifacts" "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "EVIDENCE_OBJECT_FIELDS" "scripts/smoke_api.sh" && \
     grep -Fq "/v1/evidence/objects" "README.md" && \
     grep -Fq "/v1/evidence/objects" "RUNBOOK.md" && \
     grep -Fq "/v1/evidence/objects" "CHANGELOG.md" && \
     grep -Fq "/v1/evidence/objects" "HTTP_EXAMPLES.http" && \
     grep -Fq 'Promoted milestone capability `evidence_object_ledger_api` to released' "CHANGELOG.md"; then
    echo "ok: evidence object ledger docs and contract coverage"
  else
    echo "missing: evidence object ledger docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: evidence object ledger milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "runtime_skill_identity_projection")
assert capability["status"] == "released"
PY
then
  if grep -Fq "intent_skill_key: str" "ea/app/api/routes/rewrite.py" && \
     grep -Fq "_resolve_skill_key(" "ea/app/api/routes/rewrite.py" && \
     grep -Fq 'session_body["intent_skill_key"] == "meeting_prep"' "tests/test_skills.py" && \
     grep -Fq 'fetched_artifact.json()["skill_key"] == "meeting_prep"' "tests/test_skills.py" && \
     grep -Fq 'session_body["intent_skill_key"] == "stakeholder_briefing"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "body.get('intent_skill_key','')" "scripts/smoke_api.sh" && \
     grep -Fq "body.get('skill_key','')" "scripts/smoke_api.sh" && \
     grep -Fq "intent_skill_key" "README.md" && \
     grep -Fq "intent_skill_key" "RUNBOOK.md" && \
     grep -Fq "intent_skill_key" "CHANGELOG.md"; then
    echo "ok: runtime skill identity projection docs and contract coverage"
  else
    echo "missing: runtime skill identity projection docs or contract coverage" >&2
    missing=1
  fi
else
  echo "missing: runtime skill identity projection milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "typed_task_and_skill_policy_models")
assert capability["status"] == "released"
PY
then
  if grep -Fq "typed runtime policy models" "README.md" && \
     grep -Fq "artifact_retry" "README.md" && \
     grep -Fq "skill_catalog" "README.md" && \
     grep -Fq "typed runtime policy models" "RUNBOOK.md" && \
     grep -Fq "artifact_retry" "RUNBOOK.md" && \
     grep -Fq "skill_catalog" "RUNBOOK.md" && \
     grep -Fq 'Promoted milestone capability `typed_task_and_skill_policy_models` to released' "CHANGELOG.md" && \
     grep -Fq "typed runtime policy projection" "CHANGELOG.md" && \
     grep -Fq "artifact_failure_strategy" "scripts/smoke_api.sh" && \
     grep -Fq "human_review_role" "scripts/smoke_api.sh" && \
     grep -Fq "artifact_output_template" "scripts/smoke_api.sh" && \
     grep -Fq "pre_artifact_tool_name" "scripts/smoke_api.sh" && \
     grep -Fq "test_task_contract_runtime_policy_parses_typed_metadata" "tests/test_task_contract_runtime_policy.py" && \
     grep -Fq "policy.skill_catalog.skill_key" "tests/test_task_contract_runtime_policy.py" && \
     grep -Fq "policy.artifact_retry.failure_strategy" "tests/test_task_contract_runtime_policy.py"; then
    echo "ok: typed task and skill policy models release baseline"
  else
    echo "missing: typed task and skill policy models release baseline" >&2
    missing=1
  fi
else
  echo "missing: typed task and skill policy models milestone" >&2
  missing=1
fi

if "${RELEASE_PYTHON}" - <<'PY'
import json
from pathlib import Path

milestone = json.loads(Path("MILESTONE.json").read_text(encoding="utf-8"))
capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "provider_registry_capability_routing")
assert capability["status"] == "released"
PY
then
  if grep -Fq 'Promoted milestone capability `provider_registry_capability_routing` to released' "CHANGELOG.md" && \
     grep -Fq "dynamically registered runtime tools" "CHANGELOG.md" && \
     grep -Fq 'execute_unregistered.json()["error"]["code"] == "tool_not_registered:provider.not_registered"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq 'email_handler_missing.json()["error"]["code"] == "tool_handler_missing:email.send"' "${SMOKE_RUNTIME_GUARD_TARGET}" && \
     grep -Fq "test_tool_execution_service_executes_registered_tool_not_in_provider_catalog" "tests/test_tool_execution.py" && \
     grep -Fq "release/operator guards now pin that capability-addressed routing baseline" "MILESTONE.json"; then
    echo "ok: provider registry capability routing release baseline"
  else
    echo "missing: provider registry capability routing release baseline" >&2
    missing=1
  fi
else
  echo "missing: provider registry capability routing milestone" >&2
  missing=1
fi

if [[ "${missing}" -ne 0 ]]; then
  echo "release asset verification failed" >&2
  exit 1
fi

echo "all required release assets present"
