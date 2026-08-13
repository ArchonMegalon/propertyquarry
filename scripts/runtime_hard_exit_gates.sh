#!/bin/bash -p
set -euo pipefail
set +x

# Retain the client-side probe credential only in this shell.  Authenticated
# read-only probes receive it through bounded stdin; no child process inherits
# either the client credential or the server-side verifier authority.
release_probe_secret="${PROPERTYQUARRY_LIVE_PROBE_SECRET:-${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET:-}}"
unset PROPERTYQUARRY_LIVE_PROBE_SECRET PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET
unset PROPERTYQUARRY_RELEASE_PROBE_SECRET PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID
unset EA_API_TOKEN PROPERTYQUARRY_LIVE_API_TOKEN

if [[ ${#release_probe_secret} -gt 4096 || "${release_probe_secret}" == *$'\n'* || "${release_probe_secret}" == *$'\r'* ]]; then
  unset release_probe_secret
  echo "error: release-probe credential must be one line of at most 4096 bytes" >&2
  exit 2
fi

# Resolve the repository with shell builtins so the retained, non-exported
# credential cannot reach a command-substitution child.
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

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/runtime_hard_exit_gates.sh

Runs the runtime-only hard exit bundle that a live deploy must pass:
  - smoke_help
  - verify_pocket_audio_archive

Generic runtime mode also runs smoke_api. PropertyQuarry runtime mode replaces
that generic route/auth contract with its dedicated public, authenticated,
mobile, and provider smokes.

`smoke_api_tibor.sh` stays in the full hard-exit bundle because it mutates
deeper task-contract state and is not a live-deploy-safe probe.

Optional PropertyQuarry runtime lane:
  Set PROPERTYQUARRY_RUNTIME_GATES=1 to additionally run the deployed runtime
  public/authenticated/mobile/provider smokes against PROPERTYQUARRY_LIVE_SMOKE_BASE_URL
  (default https://propertyquarry.com). PROPERTYQUARRY_LIVE_PROBE_SECRET is required
  for the signed, read-only authenticated, mobile, and provider-catalog probes;
  it is captured before child processes start and passed only through bounded
  stdin. The credential is not passed to the anonymous public smoke. All three
  protected probes use the fixed synthetic release-probe identity; customer UI
  checks use PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL (default Free). In this mode,
  verify_pocket_audio_archive remains informative but will warn instead of
  failing the PropertyQuarry-specific runtime lane.
EOF
  exit 0
fi

propertyquarry_runtime_gates_enabled=0
case "$(printf '%s' "${PROPERTYQUARRY_RUNTIME_GATES:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on|enabled|propertyquarry)
    propertyquarry_runtime_gates_enabled=1
    ;;
esac

propertyquarry_base_url="${PROPERTYQUARRY_LIVE_SMOKE_BASE_URL:-https://propertyquarry.com}"
propertyquarry_probe_principal_id="propertyquarry-release-probe"
propertyquarry_probe_plan_label="${PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL:-Free}"

cd "${EA_ROOT}"
bash scripts/smoke_help.sh
if [[ "${propertyquarry_runtime_gates_enabled}" == "0" ]]; then
  env -u EA_API_TOKEN bash scripts/smoke_api.sh
  "${PYTHON_BIN}" scripts/verify_pocket_audio_archive.py
else
  if [[ -z "${release_probe_secret}" ]]; then
    echo "PROPERTYQUARRY_RUNTIME_GATES=1 requires PROPERTYQUARRY_LIVE_PROBE_SECRET for signed read-only runtime smokes." >&2
    exit 2
  fi
  if ! "${PYTHON_BIN}" scripts/verify_pocket_audio_archive.py; then
    echo "PROPERTYQUARRY_RUNTIME_GATES=1 active: verify_pocket_audio_archive.py failed, continuing because Pocket archive backfill is outside the PropertyQuarry runtime lane." >&2
  fi
  mkdir -p _completion/smoke

  env -u EA_API_TOKEN PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_public_smoke.py \
    --base-url "${propertyquarry_base_url}" \
    --write _completion/smoke/property-live-public-latest.json \
    --timeout-seconds 8

  env -u EA_API_TOKEN -u PROPERTYQUARRY_LIVE_API_TOKEN \
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_authenticated_smoke.py \
    --release-probe-secret-stdin \
    --base-url "${propertyquarry_base_url}" \
    --principal-id "${propertyquarry_probe_principal_id}" \
    --expected-plan-label "${propertyquarry_probe_plan_label}" \
    --country-code "${PROPERTYQUARRY_LIVE_SMOKE_COUNTRY_CODE:-AT}" \
    --write _completion/smoke/property-live-authenticated-latest.json \
    --timeout-seconds 8 \
    <<<"${release_probe_secret}"

  env -u EA_API_TOKEN -u PROPERTYQUARRY_LIVE_API_TOKEN \
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_live_mobile_surface_smoke.py \
    --release-probe-secret-stdin \
    --base-url "${propertyquarry_base_url}" \
    --host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}" \
    --principal-id "${propertyquarry_probe_principal_id}" \
    --expected-plan-label "${propertyquarry_probe_plan_label}" \
    --write _completion/smoke/property-live-mobile-surface-latest.json \
    <<<"${release_probe_secret}"

  env -u EA_API_TOKEN -u PROPERTYQUARRY_LIVE_API_TOKEN \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE=1 \
    PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN=0 \
    PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_live_provider_smoke.py \
    --release-probe-secret-stdin \
    --base-url "${propertyquarry_base_url}" \
    --principal-id "${propertyquarry_probe_principal_id}" \
    --no-execute-search-matrix \
    --no-cross-country-sanitization \
    --write _completion/smoke/property-live-provider-latest.json \
    --timeout-seconds 8 \
    <<<"${release_probe_secret}"

  unset release_probe_secret
fi
