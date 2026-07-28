#!/usr/bin/env bash
set -euo pipefail

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${EA_ROOT}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/operator_summary.sh

Print a compact operator command summary including deploy, smoke, readiness,
release, support, and documentation shortcuts plus current version metadata,
the current mirrored product-control pulse, and grounded help/support/operator
packet guidance plus codex lane governance from the local design mirror.
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
      echo "operator summary: PROPERTYQUARRY_OPERATOR_PYTHON is not a usable application interpreter: ${explicit}" >&2
      return 1
    fi
    printf '%s\n' "${explicit}"
    return 0
  fi

  for candidate in \
    "${EA_ROOT}/.venv/bin/python" \
    "${EA_ROOT}/scripts/propertyquarry_release_python.sh" \
    python3
  do
    if operator_python_usable "${candidate}"; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

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
