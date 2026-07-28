#!/usr/bin/env bash
set -euo pipefail

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/smoke_help.sh
  /bin/bash -p scripts/smoke_help.sh --authenticated

Run the script-help smoke contract by checking that key operator scripts return
a Usage header for their --help output. The authenticated form uses the
hash-locked release interpreter and privileged Bash.
EOF
  exit 0
fi

authenticated=0
if [[ "${1:-}" == "--authenticated" ]]; then
  authenticated=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "error: smoke_help.sh accepts only --authenticated" >&2
  exit 2
fi

if [[ "${authenticated}" -eq 1 ]]; then
  if [[ -v PYTHON_BIN || -v PYTEST_PYTHON_BIN ]]; then
    echo "error: authenticated help smoke forbids interpreter overrides" >&2
    exit 2
  fi
  PYTHON_COMMAND=("${EA_ROOT}/scripts/propertyquarry_release_python.sh")
  BASH_COMMAND=(/bin/bash -p)
else
  PYTHON_BIN="${PYTHON_BIN:-}"
  if [[ -z "${PYTHON_BIN}" ]]; then
    if [[ -x "${EA_ROOT}/.venv/bin/python" ]]; then
      PYTHON_BIN="${EA_ROOT}/.venv/bin/python"
    else
      PYTHON_BIN="python3"
    fi
  fi
  PYTHON_COMMAND=("${PYTHON_BIN}")
  BASH_COMMAND=(bash)
fi

SCRIPTS=(
  scripts/deploy.sh
  scripts/db_bootstrap.sh
  scripts/db_status.sh
  scripts/db_size.sh
  scripts/db_retention.sh
  scripts/smoke_api.sh
  scripts/smoke_help.sh
  scripts/smoke_postgres.sh
  scripts/test_postgres_contracts.sh
  scripts/hard_exit_gates.sh
  scripts/runtime_hard_exit_gates.sh
  scripts/propertyquarry_native_release_control_gates.py
  scripts/propertyquarry_release_make_dispatch.py
  scripts/verify_ltd_critical_entries.py
  scripts/verify_ltd_flagship_subset.py
  scripts/list_endpoints.sh
  scripts/version_info.sh
  scripts/export_openapi.sh
  scripts/diff_openapi.sh
  scripts/prune_openapi.sh
  scripts/operator_summary.sh
  scripts/support_bundle.sh
  scripts/archive_tasks.sh
  scripts/deploy_propertyquarry.sh
  scripts/propertyquarry_live_public_smoke.py
  scripts/propertyquarry_live_authenticated_smoke.py
  scripts/property_live_provider_smoke.py
  scripts/bootstrap_payfunnels_propertyquarry.py
  scripts/bootstrap_emailit_propertyquarry.py
  scripts/verify_release_assets.sh
)

for s in "${SCRIPTS[@]}"; do
  echo "== help smoke: ${s} =="
  case "${s}" in
    *.py)
      out="$("${PYTHON_COMMAND[@]}" "${EA_ROOT}/${s}" --help)"
      ;;
    *)
      out="$("${BASH_COMMAND[@]}" "${EA_ROOT}/${s}" --help)"
      ;;
  esac
  if [[ "${out}" != *"Usage:"* ]]; then
    echo "missing Usage header in ${s} --help output" >&2
    exit 21
  fi
done

echo "help smoke complete"
