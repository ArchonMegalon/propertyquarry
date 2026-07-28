#!/usr/bin/env bash
set -euo pipefail

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${EA_ROOT}"
API_SERVICE="${PROPERTYQUARRY_API_SERVICE:-${EA_API_SERVICE:-ea-api}}"
DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"
# Legacy operator contract reference:
# "${DC[@]}" logs --tail "${TAIL_LINES}" "${API_SERVICE}"
# "${DC[@]}" logs --tail "${TAIL_LINES}" "${DB_SERVICE}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/support_bundle.sh

Environment:
  SUPPORT_BUNDLE_PREFIX=<name>          Bundle filename prefix (default: support_bundle)
  SUPPORT_BUNDLE_TIMESTAMP_FMT=<fmt>    UTC timestamp format for filename (date format)
  SUPPORT_LOG_TAIL_LINES=<n>            Number of log lines to capture (default: 300)
  SUPPORT_INCLUDE_API=0|1               Include compose API service logs (default: 1)
  SUPPORT_INCLUDE_DB=0|1                Include compose DB service logs (default: 1)
  SUPPORT_INCLUDE_DB_VOLUME=0|1         Include compose DB service mount/volume attribution (default: 1)
  SUPPORT_INCLUDE_DB_SIZE=0|1           Include DB size snapshot via db_size.sh (default: 1)
  SUPPORT_INCLUDE_PRODUCT_CONTROL=0|1   Include mirrored weekly pulse and journey-gate summary (default: 1)
  SUPPORT_INCLUDE_GROUNDING=0|1         Include mirrored help/support/operator grounding summary (default: 1)
                                        and codex governance guidance (default: 1)
  SUPPORT_DB_SIZE_LIMIT=<n>             Top table count for DB size snapshot (default: 10)
  SUPPORT_INCLUDE_QUEUE=0|1             Include queued task snapshot (default: 1)
EOF
  exit 0
fi

OPERATOR_PYTHONPATH="${EA_ROOT}/ea:${EA_ROOT}"

operator_python_usable() {
  local candidate="$1"

  if [[ "${candidate}" == */* ]]; then
    [[ -x "${candidate}" ]] || return 1
  else
    command -v "${candidate}" >/dev/null 2>&1 || return 1
  fi

  PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONPATH="${OPERATOR_PYTHONPATH}" \
    "${candidate}" -B -c \
      'import yaml; from app.product.service import _public_guide_freshness_projection; from app.api.routes.responses import _codex_governance_payload, _codex_profiles' \
      >/dev/null 2>&1
}

select_operator_python() {
  local explicit="${PROPERTYQUARRY_OPERATOR_PYTHON:-}"
  local candidate

  if [[ -n "${explicit}" ]]; then
    if ! operator_python_usable "${explicit}"; then
      echo "support bundle: PROPERTYQUARRY_OPERATOR_PYTHON is not a usable application interpreter: ${explicit}" >&2
      return 1
    fi
    printf '%s\n' "${explicit}"
    return 0
  fi

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

  echo "support bundle: no usable application Python interpreter found" >&2
  return 1
}

run_operator_python() {
  PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONPATH="${OPERATOR_PYTHONPATH}" \
    "${OPERATOR_PYTHON}" -B "$@"
}

if command -v timeout >/dev/null 2>&1; then
  COMPOSE_PROBE=(timeout 5s)
else
  COMPOSE_PROBE=()
fi

DC=()
if "${COMPOSE_PROBE[@]}" docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif "${COMPOSE_PROBE[@]}" docker-compose version >/dev/null 2>&1; then
  DC=(docker-compose)
fi

OUT_DIR="${EA_ROOT}/artifacts"
mkdir -p "${OUT_DIR}"
STAMP_FMT="${SUPPORT_BUNDLE_TIMESTAMP_FMT:-%Y%m%dT%H%M%SZ}"
STAMP="$(date -u +"${STAMP_FMT}")"
PREFIX="${SUPPORT_BUNDLE_PREFIX:-support_bundle}"
UNIQUE_SUFFIX="${SUPPORT_BUNDLE_UNIQUE_SUFFIX:-$$}"
if [[ -n "${UNIQUE_SUFFIX}" ]]; then
  OUT_FILE="${OUT_DIR}/${PREFIX}_${STAMP}_${UNIQUE_SUFFIX}.txt"
else
  OUT_FILE="${OUT_DIR}/${PREFIX}_${STAMP}.txt"
fi
TAIL_LINES="${SUPPORT_LOG_TAIL_LINES:-300}"
INCLUDE_DB="${SUPPORT_INCLUDE_DB:-1}"
INCLUDE_API="${SUPPORT_INCLUDE_API:-1}"
INCLUDE_DB_VOLUME="${SUPPORT_INCLUDE_DB_VOLUME:-1}"
INCLUDE_DB_SIZE="${SUPPORT_INCLUDE_DB_SIZE:-1}"
INCLUDE_PRODUCT_CONTROL="${SUPPORT_INCLUDE_PRODUCT_CONTROL:-1}"
INCLUDE_GROUNDING="${SUPPORT_INCLUDE_GROUNDING:-1}"
DB_SIZE_LIMIT="${SUPPORT_DB_SIZE_LIMIT:-10}"
INCLUDE_QUEUE="${SUPPORT_INCLUDE_QUEUE:-1}"
DB_CONTAINER="${EA_DB_CONTAINER:-${DB_SERVICE}}"
OPERATOR_PYTHON=""
if [[ "${INCLUDE_PRODUCT_CONTROL}" == "1" || "${INCLUDE_GROUNDING}" == "1" ]]; then
  OPERATOR_PYTHON="$(select_operator_python)"
fi

redact() {
  sed -E \
    -e 's#(postgresql://[^:]+:)[^@]+@#\1REDACTED@#g' \
    -e 's#([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd][^=:\n]{0,40}[=:])[^\n ]+#\1REDACTED#g' \
    -e 's#([Pp][Aa][Ss][Ss][Ww][Dd][^=:\n]{0,40}[=:])[^\n ]+#\1REDACTED#g' \
    -e 's#([Tt][Oo][Kk][Ee][Nn][^=:\n]{0,40}[=:])[^\n ]+#\1REDACTED#g' \
    -e 's#([Ss][Ee][Cc][Rr][Ee][Tt][^=:\n]{0,40}[=:])[^\n ]+#\1REDACTED#g' \
    -e 's#([Aa][Pp][Ii][_-]?[Kk][Ee][Yy][^=:\n]{0,40}[=:])[^\n ]+#\1REDACTED#g'
}

compose_available() {
  [[ ${#DC[@]} -gt 0 ]]
}

run_compose() {
  if ! compose_available; then
    return 1
  fi
  if command -v timeout >/dev/null 2>&1; then
    timeout 20s "${DC[@]}" "$@"
  else
    "${DC[@]}" "$@"
  fi
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
support_fallout_state = "watch" if (support_closures_waiting or support_human_responses) else "clear"

print(f"pulse_path={pulse_path if pulse_path.exists() else 'missing'}")
print(f"pulse_generated_at={str((pulse or {}).get('generated_at') or 'missing').strip() or 'missing'}")
print(f"active_wave={str((pulse or {}).get('active_wave') or 'missing').strip() or 'missing'}")
print(f"active_wave_status={str((pulse or {}).get('active_wave_status') or 'missing').strip() or 'missing'}")
print(f"launch_readiness={str(signals.get('launch_readiness') or 'missing').strip() or 'missing'}")
print(f"journey_gates_path={journey_path if journey_path.exists() else 'missing'}")
print(f"journey_generated_at={str((journey or {}).get('generated_at') or 'missing').strip() or 'missing'}")
print(f"journey_gate_state={journey_state}")
print(f"journey_gate_action={journey_action}")
print(f"support_fallout_state={support_fallout_state}")
print(f"support_closures_waiting={support_closures_waiting}")
print(f"support_human_responses_needed={support_human_responses}")
print(f"route_review_due={str(route.get('review_due') or 'not published').strip() or 'not published'}")
print(f"public_guide_path={str(public_guide.get('path') or 'missing').strip() or 'missing'}")
print(f"public_guide_generated_at={str(public_guide.get('generated_at') or 'missing').strip() or 'missing'}")
print(f"public_guide_freshness={str(public_guide.get('state') or 'missing').strip() or 'missing'}")
print(f"public_guide_detail={str(public_guide.get('detail') or 'No public-guide freshness is mirrored.').strip() or 'No public-guide freshness is mirrored.'}")
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

print(f"public_help_heading={compact(help_page.get('heading') or 'Get help without guessing')}")
print(f"public_help_summary={compact(help_page.get('intro') or release.get('release_notes_summary'))}")
if first_action:
    print(f"public_help_primary_action={compact(first_action.get('label'))} -> {compact(first_action.get('href'))}")
print(f"support_scorecard_question={compact(support_scorecard.get('question'))}")
if first_metric:
    print(f"support_scorecard_target={compact(first_metric.get('name'))} target {compact(first_metric.get('target'))}")
print(f"operator_review_cadence={compact(cadence.get('review') or 'weekly')}")
print(f"operator_snapshot_owner={compact(cadence.get('snapshot_owner') or 'product_governor')}")
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

print(f"codex_review_cadence={compact(cadence.get('review') or 'weekly')}")
print(f"codex_snapshot_owner={compact(cadence.get('snapshot_owner') or 'product_governor')}")
print(f"codex_easy_expectation={compact(dict(profiles.get('easy') or {}).get('expectation_summary'))}")
print(f"codex_core_expectation={compact(dict(profiles.get('core') or {}).get('expectation_summary'))}")
print(f"codex_groundwork_expectation={compact(dict(profiles.get('groundwork') or {}).get('expectation_summary'))}")
print(f"codex_audit_expectation={compact(dict(profiles.get('audit') or {}).get('expectation_summary'))}")
print(f"codex_support_help_boundary={compact(support.get('summary'))}")
PY
}

{
  echo "== Support Bundle =="
  echo "generated_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo

  echo "-- version info --"
  bash scripts/version_info.sh || true
  echo

  if [[ "${INCLUDE_PRODUCT_CONTROL}" == "1" ]]; then
    echo "-- product control --"
    print_product_control_summary | redact || true
    echo
  else
    echo "-- product control --"
    echo "skipped (SUPPORT_INCLUDE_PRODUCT_CONTROL=${INCLUDE_PRODUCT_CONTROL})"
    echo
  fi

  if [[ "${INCLUDE_GROUNDING}" == "1" ]]; then
    echo "-- grounding --"
    print_grounding_summary | redact || true
    echo
    echo "-- codex governance --"
    print_codex_governance_summary | redact || true
    echo
  else
    echo "-- grounding --"
    echo "skipped (SUPPORT_INCLUDE_GROUNDING=${INCLUDE_GROUNDING})"
    echo
    echo "-- codex governance --"
    echo "skipped (SUPPORT_INCLUDE_GROUNDING=${INCLUDE_GROUNDING})"
    echo
  fi

  echo "-- compose ps --"
  if compose_available; then
    run_compose ps || true
  else
    echo "skipped (compose unavailable)"
  fi
  echo

  if [[ "${INCLUDE_API}" == "1" ]]; then
    echo "-- ${API_SERVICE} logs (tail ${TAIL_LINES}) --"
    if compose_available; then
      run_compose logs --tail "${TAIL_LINES}" "${API_SERVICE}" 2>&1 | redact || true
    else
      echo "skipped (compose unavailable)"
    fi
    echo
  else
    echo "-- ${API_SERVICE} logs --"
    echo "skipped (SUPPORT_INCLUDE_API=${INCLUDE_API})"
    echo
  fi

  if [[ "${INCLUDE_DB}" == "1" ]]; then
    echo "-- ${DB_SERVICE} logs (tail ${TAIL_LINES}) --"
    if compose_available; then
      run_compose logs --tail "${TAIL_LINES}" "${DB_SERVICE}" 2>&1 | redact || true
    else
      echo "skipped (compose unavailable)"
    fi
    echo
  else
    echo "-- ${DB_SERVICE} logs --"
    echo "skipped (SUPPORT_INCLUDE_DB=${INCLUDE_DB})"
    echo
  fi

  if [[ "${INCLUDE_DB_VOLUME}" == "1" ]]; then
    echo "-- ${DB_SERVICE} volume attribution --"
    echo "expected_runtime_volume=ea_pgdata"
    echo "expected_container_mount=/var/lib/postgresql/data"
    if compose_available; then
      echo "compose_declared_volumes=$(run_compose config --volumes 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | sed 's/^ *//; s/ *$//')"
    else
      echo "compose_declared_volumes=unavailable"
    fi
    if command -v timeout >/dev/null 2>&1 && timeout 20s docker inspect "${DB_CONTAINER}" >/dev/null 2>&1; then
      timeout 20s docker inspect "${DB_CONTAINER}" --format '{{range .Mounts}}{{println .Name "|" .Source "|" .Destination "|" .Type}}{{end}}' 2>/dev/null | redact || true
    elif docker inspect "${DB_CONTAINER}" >/dev/null 2>&1; then
      docker inspect "${DB_CONTAINER}" --format '{{range .Mounts}}{{println .Name "|" .Source "|" .Destination "|" .Type}}{{end}}' 2>/dev/null | redact || true
    else
      echo "${DB_SERVICE} mount inspection unavailable"
    fi
    echo
  else
    echo "-- ${DB_SERVICE} volume attribution --"
    echo "skipped (SUPPORT_INCLUDE_DB_VOLUME=${INCLUDE_DB_VOLUME})"
    echo
  fi

  if [[ "${INCLUDE_DB_SIZE}" == "1" ]]; then
    echo "-- db size snapshot --"
    EA_DB_SIZE_LIMIT="${DB_SIZE_LIMIT}" bash scripts/db_size.sh 2>&1 | redact || true
    echo
  else
    echo "-- db size snapshot --"
    echo "skipped (SUPPORT_INCLUDE_DB_SIZE=${INCLUDE_DB_SIZE})"
    echo
  fi

  if [[ "${INCLUDE_QUEUE}" == "1" ]]; then
    echo "-- queued task snapshot --"
    if [[ -f TASKS_WORK_LOG.md ]]; then
      awk '/^## Queue/{flag=1;next}/^## In Progress/{flag=0}flag' TASKS_WORK_LOG.md || true
    else
      echo "local task log not present"
    fi
  else
    echo "-- queued task snapshot --"
    echo "skipped (SUPPORT_INCLUDE_QUEUE=${INCLUDE_QUEUE})"
  fi
} > "${OUT_FILE}"

echo "support bundle written: ${OUT_FILE}"
