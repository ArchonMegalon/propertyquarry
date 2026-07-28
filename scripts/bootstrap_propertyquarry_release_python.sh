#!/bin/bash -p
set -euo pipefail

PATH=/usr/bin:/bin
export PATH
readonly PATH
unset BASH_ENV ENV CDPATH GLOBIGNORE
IFS=$' \t\n'

script_source="${BASH_SOURCE[0]}"
[[ "${script_source}" == */* ]] || {
  printf '%s\n' "error: release verifier bootstrap must be invoked with an explicit path" >&2
  exit 2
}
ROOT="$(cd -P -- "${script_source%/*}/.." && pwd -P)"
SYSTEM_PYTHON=/usr/bin/python3.12
SYSTEM_PYTHON_SHA256=1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118
INPUT="${ROOT}/config/propertyquarry_release_verifier_requirements.in"
INPUT_SHA256=8ab4dc0083c09281b5790155a6efc920eb459fe0c0b1d9c3c2e8c41c3d6d0375
LOCK="${ROOT}/config/propertyquarry_release_verifier_requirements.lock"
LOCK_SHA256=eeccb87676cd5ec10857fe04dabae2aa461abd48c6c90a788004fab9483f4ab8
VENV="${ROOT}/.propertyquarry_release_tools/release-venv"
LAUNCHER="${ROOT}/scripts/propertyquarry_release_python.sh"
VERIFY_SCRIPT="${ROOT}/scripts/propertyquarry_release_python_verify.py"
CREATE_ROOT_HELPER="${ROOT}/scripts/propertyquarry_release_python_create_root.py"
BOOTSTRAP_LOCK_FD=""
created=0
created_venv_identity=""
complete=0

fail() {
  printf '%s\n' "error: $1" >&2
  exit 2
}

acquire_bootstrap_lock() {
  exec {BOOTSTRAP_LOCK_FD}<"${ROOT}" ||
    fail "release verifier repository root cannot be opened for locking"
  /usr/bin/flock --exclusive "${BOOTSTRAP_LOCK_FD}" ||
    fail "release verifier bootstrap lock cannot be acquired"
}

cleanup() {
  local current_venv_identity=""
  local quarantine=""
  local quarantined_venv_identity=""
  if [[ "${complete}" -eq 0 &&
        ( "${created}" -eq 1 || -n "${created_venv_identity}" ) ]]; then
    case "${VENV}" in
      "/docker/property/.propertyquarry_release_tools/release-venv")
        if [[ ! -e "${VENV}" && ! -L "${VENV}" ]]; then
          created=0
          return 0
        fi
        if [[ ! -d "${VENV}" || -L "${VENV}" ]]; then
          printf '%s\n' \
            "error: refusing cleanup of replaced release verifier environment" >&2
          return 1
        fi
        if [[ ! "${created_venv_identity}" =~ ^[0-9]+:[0-9]+$ ]]; then
          printf '%s\n' \
            "error: refusing cleanup without a created environment identity" >&2
          return 1
        fi
        current_venv_identity="$(
          /usr/bin/stat -c '%d:%i' -- "${VENV}" 2>/dev/null
        )" || {
          printf '%s\n' \
            "error: release verifier environment cannot be identified for cleanup" >&2
          return 1
        }
        if [[ "${current_venv_identity}" != "${created_venv_identity}" ]]; then
          printf '%s\n' \
            "error: refusing cleanup of replaced release verifier environment" >&2
          return 1
        fi
        quarantine="$(
          /usr/bin/mktemp -d -- \
            "${parent}/release-venv.incomplete.XXXXXXXX"
        )" || {
          printf '%s\n' \
            "error: incomplete release verifier quarantine cannot be created" >&2
          return 1
        }
        if ! /usr/bin/mv -T -- "${VENV}" "${quarantine}"; then
          printf '%s\n' \
            "error: incomplete release verifier cannot be quarantined" >&2
          return 1
        fi
        quarantined_venv_identity="$(
          /usr/bin/stat -c '%d:%i' -- "${quarantine}" 2>/dev/null
        )" || {
          printf '%s\n' \
            "error: quarantined release verifier cannot be identified" >&2
          created=0
          return 1
        }
        created=0
        if [[ "${quarantined_venv_identity}" != "${current_venv_identity}" ]]; then
          printf '%s\n' \
            "error: release verifier changed while it was quarantined" >&2
          return 1
        fi
        printf 'warning: incomplete release verifier retained at %s\n' \
          "${quarantine}" >&2
        ;;
      *)
        printf '%s\n' "error: refusing unsafe verifier cleanup target" >&2
        return 1
        ;;
    esac
  fi
}

cleanup_on_exit() {
  local status="$1"
  trap '' HUP INT TERM
  trap - EXIT
  cleanup || true
  exit "${status}"
}

terminate_from_signal() {
  trap '' HUP INT TERM
  local status="$1"
  trap - EXIT
  cleanup || true
  exit "${status}"
}

[[ "$#" -eq 0 ]] || fail "bootstrap_propertyquarry_release_python.sh accepts no arguments"
[[ "${ROOT}" == "/docker/property" ]] ||
  fail "release verifier bootstrap requires the canonical /docker/property checkout"
[[ -f "${SYSTEM_PYTHON}" && ! -L "${SYSTEM_PYTHON}" && -x "${SYSTEM_PYTHON}" ]] ||
  fail "pinned CPython 3.12.3 binary is unavailable"
source_sha="$(/usr/bin/sha256sum -- "${SYSTEM_PYTHON}")"
[[ "${source_sha%% *}" == "${SYSTEM_PYTHON_SHA256}" ]] ||
  fail "pinned CPython binary digest mismatch"
input_sha="$(/usr/bin/sha256sum -- "${INPUT}")"
[[ "${input_sha%% *}" == "${INPUT_SHA256}" ]] ||
  fail "release verifier requirements input digest mismatch"
lock_sha="$(/usr/bin/sha256sum -- "${LOCK}")"
[[ "${lock_sha%% *}" == "${LOCK_SHA256}" ]] ||
  fail "release verifier requirements lock digest mismatch"
/usr/bin/env -i \
  HOME=/tmp \
  LANG=C \
  LC_ALL=C \
  PATH=/usr/bin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED=0 \
  PYTHONNOUSERSITE=1 \
  "${SYSTEM_PYTHON}" -I -S -B \
  "${VERIFY_SCRIPT}" --check-requirements-parity

acquire_bootstrap_lock
parent="${VENV%/*}"
if [[ ! -e "${parent}" && ! -L "${parent}" ]]; then
  umask 077
  /usr/bin/mkdir -m 700 -- "${parent}"
fi
[[ -d "${parent}" && ! -L "${parent}" ]] ||
  fail "release verifier parent is not a real directory"
parent_metadata="$(/usr/bin/stat -c '%u %a' -- "${parent}")" ||
  fail "release verifier parent cannot be inspected"
parent_uid="${parent_metadata%% *}"
parent_mode="${parent_metadata##* }"
[[ "${parent_uid}" == "0" || "${parent_uid}" == "$(/usr/bin/id -u)" ]] ||
  fail "release verifier parent has an untrusted owner"
(( (8#${parent_mode} & 8#022) == 0 )) ||
  fail "release verifier parent is peer-writable"

if [[ -e "${VENV}" || -L "${VENV}" ]]; then
  if "${LAUNCHER}" --print-interpreter >/dev/null; then
    printf '%s\n' "ok: existing PropertyQuarry release verifier is authentic"
    exit 0
  else
    launcher_status="$?"
    printf '%s\n' \
      "error: existing release verifier is invalid; automatic replacement is forbidden" \
      >&2
    printf '%s\n' \
      "error: preserve it and use the controlled release-environment recovery lane" \
      >&2
    exit "${launcher_status}"
  fi
fi

trap 'cleanup_on_exit "$?"' EXIT
trap 'terminate_from_signal 129' HUP
trap 'terminate_from_signal 130' INT
trap 'terminate_from_signal 143' TERM

umask 077
created_venv_identity="$(
  /usr/bin/env -i \
    HOME="${parent}" \
    LANG=C \
    LC_ALL=C \
    PATH=/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONNOUSERSITE=1 \
    "${SYSTEM_PYTHON}" -I -S -B "${CREATE_ROOT_HELPER}"
)" ||
  fail "release verifier environment cannot be identified after creation"
[[ "${created_venv_identity}" =~ ^[0-9]+:[0-9]+$ ]] ||
  fail "release verifier environment identity is invalid after creation"
created=1
/usr/bin/env -i \
  HOME="${parent}" \
  LANG=C \
  LC_ALL=C \
  PATH=/usr/bin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED=0 \
  PYTHONNOUSERSITE=1 \
  "${SYSTEM_PYTHON}" -I -m venv --copies "${VENV}"
/usr/bin/env -i \
  HOME="${parent}" \
  LANG=C \
  LC_ALL=C \
  PATH=/usr/bin:/bin \
  PIP_CONFIG_FILE=/dev/null \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_INDEX_URL=https://pypi.org/simple \
  PIP_NO_CACHE_DIR=1 \
  PIP_REQUIRE_VIRTUALENV=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED=0 \
  PYTHONNOUSERSITE=1 \
  "${VENV}/bin/python" -m pip install \
  --index-url https://pypi.org/simple \
  --require-hashes \
  --no-deps \
  --only-binary=:all: \
  --no-compile \
  -r "${LOCK}"

/usr/bin/find "${VENV}" -type d -name __pycache__ -prune -exec /usr/bin/rm -rf -- {} +
/usr/bin/chmod -R go-w -- "${VENV}"
"${LAUNCHER}" --print-interpreter >/dev/null
complete=1
printf '%s\n' "ok: bootstrapped authenticated PropertyQuarry release verifier"
