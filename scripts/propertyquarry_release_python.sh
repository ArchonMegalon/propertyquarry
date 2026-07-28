#!/bin/bash -p
set -euo pipefail

PATH=/usr/bin:/bin
export PATH
readonly PATH
unset BASH_ENV ENV CDPATH GLOBIGNORE
IFS=$' \t\n'

script_source="${BASH_SOURCE[0]}"
[[ "${script_source}" == */* ]] || {
  printf '%s\n' "error: release interpreter must be invoked with an explicit path" >&2
  exit 2
}
ROOT="$(cd -P -- "${script_source%/*}/.." && pwd -P)"
SYSTEM_PYTHON=/usr/bin/python3.12
SYSTEM_PYTHON_SHA256=1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118
VERIFY_SCRIPT="${ROOT}/scripts/propertyquarry_release_python_verify.py"

fail() {
  printf '%s\n' "error: $1" >&2
  exit 2
}

if [[ -v PYTHON_BIN || -v PYTEST_PYTHON_BIN ]]; then
  fail "release interpreter override forbidden"
fi

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
  PROPERTYQUARRY_RELEASE_DISPATCH \
  PYTEST_ADDOPTS \
  PYTEST_PLUGINS
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=0
export PYTHONNOUSERSITE=1
export PYTHONSAFEPATH=1

[[ -f "${SYSTEM_PYTHON}" && ! -L "${SYSTEM_PYTHON}" && -x "${SYSTEM_PYTHON}" ]] ||
  fail "pinned source interpreter is unavailable"
source_python_sha="$(
  /usr/bin/sha256sum -- "${SYSTEM_PYTHON}" 2>/dev/null
)" || fail "pinned source interpreter cannot be hashed"
[[ "${source_python_sha%% *}" == "${SYSTEM_PYTHON_SHA256}" ]] ||
  fail "pinned source interpreter digest mismatch"

verified_python="$(
  "${SYSTEM_PYTHON}" -I -S -B "${VERIFY_SCRIPT}"
)" || exit $?
[[ -n "${verified_python}" ]] ||
  fail "release verifier returned no interpreter"

probe="$(
  "${verified_python}" -I -B -c '
import fastapi
import httpx
from importlib.metadata import version
from jsonschema import FormatChecker
import pytest

required_jsonschema_formats = {
    "color",
    "date-time",
    "duration",
    "hostname",
    "idn-hostname",
    "iri",
    "iri-reference",
    "json-pointer",
    "relative-json-pointer",
    "time",
    "uri",
    "uri-reference",
    "uri-template",
}
invalid_jsonschema_format_values = {
    "color": "not a color",
    "date-time": "not-a-date-time",
    "duration": "not-a-duration",
    "hostname": "bad host name",
    "idn-hostname": "bad host name",
    "iri": "not a valid iri",
    "iri-reference": "not a valid iri reference",
    "json-pointer": "not/a/pointer",
    "relative-json-pointer": "not-relative",
    "time": "not-a-time",
    "uri": "not a uri",
    "uri-reference": "not a uri reference",
    "uri-template": "{unterminated",
}
format_checker = FormatChecker()
available_jsonschema_formats = set(format_checker.checkers)
if (
    fastapi.__version__ != "0.135.1"
    or httpx.__version__ != "0.28.1"
    or version("pip-audit") != "2.10.1"
    or pytest.__version__ != "9.0.2"
    or version("jsonschema") != "4.26.0"
    or not required_jsonschema_formats <= available_jsonschema_formats
    or any(
        format_checker.conforms(value, format_name)
        for format_name, value in invalid_jsonschema_format_values.items()
    )
):
    raise SystemExit(1)
print("propertyquarry-release-python-v3")
'
)" || fail "release verifier dependency probe failed"
[[ "${probe}" == "propertyquarry-release-python-v3" ]] ||
  fail "release verifier dependency probe mismatch"

if [[ "${1:-}" == "--print-interpreter" ]]; then
  [[ "$#" -eq 1 ]] || fail "--print-interpreter accepts no additional arguments"
  printf '%s\n' "${verified_python}"
  exit 0
fi

export PYTHONPATH="${ROOT}/scripts:${ROOT}/ea:${ROOT}"
exec "${verified_python}" -B "$@"
