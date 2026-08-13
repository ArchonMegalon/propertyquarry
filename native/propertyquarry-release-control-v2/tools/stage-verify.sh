#!/bin/sh
set -eu
PATH=/usr/bin:/bin
LANG=C
LC_ALL=C
TZ=UTC
export PATH LANG LC_ALL TZ

SOURCE_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
REPOSITORY_ROOT=$(CDPATH='' cd -- "$SOURCE_ROOT/../.." && pwd)
DEFAULT_BINARY_ROOT="$REPOSITORY_ROOT/build/propertyquarry-release-control-v2/linux-amd64"
BINARY_ROOT=${1:-"$DEFAULT_BINARY_ROOT"}
PACKAGE_ROOT="$REPOSITORY_ROOT/packaging/propertyquarry-release-control-v2"

fail() {
  printf '%s\n' "error: $1" >&2
  exit 1
}

digest() {
  digest_output=$(sha256sum -- "$1") || return 1
  printf '%s\n' "${digest_output%% *}"
}

[ "$#" -le 2 ] || fail "usage: stage-verify.sh [binary-root] [new-stage-root]"

CANONICAL_BINARY_ROOT=$(realpath -m -- "$BINARY_ROOT")
CANONICAL_DEFAULT_BINARY_ROOT=$(realpath -m -- "$DEFAULT_BINARY_ROOT")
[ "$CANONICAL_DEFAULT_BINARY_ROOT" = "$DEFAULT_BINARY_ROOT" ] ||
  fail "repository build path contains a symlink"
case "$CANONICAL_BINARY_ROOT" in
  "$CANONICAL_DEFAULT_BINARY_ROOT"|/tmp/*) ;;
  *) fail "binary root must be the repository build path or an isolated /tmp path" ;;
esac
[ -d "$BINARY_ROOT" ] && [ ! -L "$BINARY_ROOT" ] ||
  fail "binary root must be a non-symlink directory"
[ "$(realpath -e -- "$BINARY_ROOT")" = "$CANONICAL_BINARY_ROOT" ] || fail "binary root path changed"

EXPECTED_BUNDLE_FILES=$(printf '%s\n' \
  build-receipt.json \
  propertyquarry-release-controller-v2 \
  propertyquarry-release-supervisor-v2 \
  propertyquarry-release-watchdog-v2)
ACTUAL_BUNDLE_FILES=$(find "$BINARY_ROOT" -mindepth 1 -maxdepth 1 -printf '%f\n') ||
  fail "binary bundle cannot be enumerated"
ACTUAL_BUNDLE_FILES=$(printf '%s\n' "$ACTUAL_BUNDLE_FILES" | LC_ALL=C sort)
[ "$ACTUAL_BUNDLE_FILES" = "$EXPECTED_BUNDLE_FILES" ] || fail "binary bundle is not the exact four-file set"
[ -z "$(find "$BINARY_ROOT" -mindepth 1 -maxdepth 1 -type l -print -quit)" ] ||
  fail "binary bundle contains a symlink"

BUILD_RECEIPT="$BINARY_ROOT/build-receipt.json"
[ -f "$BUILD_RECEIPT" ] && [ ! -L "$BUILD_RECEIPT" ] || fail "verified build receipt is required"
[ "$(stat -c '%a' -- "$BUILD_RECEIPT")" = 644 ] || fail "build receipt mode is invalid"
[ "$(stat -c '%s' -- "$BUILD_RECEIPT")" -le 65536 ] || fail "build receipt is oversized"

for component in supervisor controller watchdog; do
  name="propertyquarry-release-${component}-v2"
  source_path="$BINARY_ROOT/$name"
  [ -f "$source_path" ] && [ ! -L "$source_path" ] && [ -x "$source_path" ] ||
    fail "native binary is missing or invalid"
  [ "$(stat -c '%a' -- "$source_path")" = 755 ] || fail "native binary mode is invalid"
  "$SOURCE_ROOT/tools/verify-static-elf.sh" "$source_path" >/dev/null ||
    fail "native binary is not scratch-executable static ELF"
done

BUILD_RECEIPT_SHA=$(digest "$BUILD_RECEIPT") || fail "build receipt cannot be hashed"
CONTROLLER_SHA=$(digest "$BINARY_ROOT/propertyquarry-release-controller-v2") || fail "controller cannot be hashed"
SUPERVISOR_SHA=$(digest "$BINARY_ROOT/propertyquarry-release-supervisor-v2") || fail "supervisor cannot be hashed"
WATCHDOG_SHA=$(digest "$BINARY_ROOT/propertyquarry-release-watchdog-v2") || fail "watchdog cannot be hashed"
CONTROLLER_BYTES=$(stat -c '%s' -- "$BINARY_ROOT/propertyquarry-release-controller-v2")
SUPERVISOR_BYTES=$(stat -c '%s' -- "$BINARY_ROOT/propertyquarry-release-supervisor-v2")
WATCHDOG_BYTES=$(stat -c '%s' -- "$BINARY_ROOT/propertyquarry-release-watchdog-v2")

if ! env -i PATH=/usr/bin:/bin PYTHONNOUSERSITE=1 python3 -I -S - \
  "$BUILD_RECEIPT" \
  "sha256:$CONTROLLER_SHA" \
  "sha256:$SUPERVISOR_SHA" \
  "sha256:$WATCHDOG_SHA" \
  "$CONTROLLER_BYTES" \
  "$SUPERVISOR_BYTES" \
  "$WATCHDOG_BYTES" <<'PY'
import json
from pathlib import Path
import re
import sys


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


try:
    receipt_path = Path(sys.argv[1])
    raw = receipt_path.read_bytes()
    if len(raw) > 65_536:
        raise ValueError("oversized receipt")
    receipt = json.loads(
        raw,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if type(receipt) is not dict:
        raise TypeError("receipt must be an object")
    expected_keys = {
        "schema",
        "authoritative",
        "production_ready",
        "reproducible_double_build",
        "distinct_absolute_source_roots",
        "isolated_build_caches",
        "independent_toolchain_extractions",
        "go_subprocess_environment_allowlisted",
        "go_subprocess_inherited_environment_cleared",
        "module_network_resolution_disabled",
        "host_network_namespace_isolated",
        "go_tests_passed_in_both_builds",
        "scratch_execution",
        "source_manifest_reverified_after_build",
        "receipt_published_last",
        "root_install_performed",
        "package_signature_verified",
        "builder_identity_authenticated",
        "toolchain",
        "toolchain_archive_bytes",
        "toolchain_archive_sha256",
        "go_binary_sha256",
        "source_manifest_sha256",
        "build_flags",
        "ldflags",
        "build_environment",
        "binary_mode",
        "binary_sizes",
        "binaries",
    }
    names = (
        "propertyquarry-release-controller-v2",
        "propertyquarry-release-supervisor-v2",
        "propertyquarry-release-watchdog-v2",
    )
    expected_digests = dict(zip(names, sys.argv[2:5], strict=True))
    expected_sizes = dict(
        zip(names, (int(value) for value in sys.argv[5:8]), strict=True)
    )
    source_digest = receipt.get("source_manifest_sha256", "")
    expected_environment = {
        "CGO_ENABLED": "0",
        "GO111MODULE": "on",
        "GOARCH": "amd64",
        "GOAMD64": "v1",
        "GOENV": "off",
        "GOEXPERIMENT": "",
        "GOFIPS140": "off",
        "GOFLAGS": "",
        "GOOS": "linux",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTELEMETRY": "off",
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    expected_scratch_execution = {
        "contract": "linux-amd64-static-et-exec-v1",
        "elf_class": "ELF64",
        "elf_data": "little-endian",
        "elf_machine": "Advanced Micro Devices X86-64",
        "elf_type": "ET_EXEC",
        "statically_linked": True,
        "pt_interp_absent": True,
        "dynamic_section_absent": True,
        "dt_needed_absent": True,
        "non_executable_stack": True,
        "writable_executable_load_segments_absent": True,
        "file_gate_passed": True,
        "readelf_gate_passed": True,
    }
    valid = (
        set(receipt) == expected_keys
        and receipt["schema"] == "propertyquarry.release-control.native-build-receipt.v2"
        and receipt["authoritative"] is False
        and receipt["production_ready"] is False
        and receipt["reproducible_double_build"] is True
        and receipt["distinct_absolute_source_roots"] is True
        and receipt["isolated_build_caches"] is True
        and receipt["independent_toolchain_extractions"] is True
        and receipt["go_subprocess_environment_allowlisted"] is True
        and receipt["go_subprocess_inherited_environment_cleared"] is True
        and receipt["module_network_resolution_disabled"] is True
        and receipt["host_network_namespace_isolated"] is False
        and receipt["go_tests_passed_in_both_builds"] is True
        and receipt["scratch_execution"] == expected_scratch_execution
        and receipt["source_manifest_reverified_after_build"] is True
        and receipt["receipt_published_last"] is True
        and receipt["root_install_performed"] is False
        and receipt["package_signature_verified"] is False
        and receipt["builder_identity_authenticated"] is False
        and receipt["toolchain"] == "go1.26.5 linux/amd64"
        and receipt["toolchain_archive_bytes"] == 66_879_095
        and receipt["toolchain_archive_sha256"]
        == "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
        and receipt["go_binary_sha256"]
        == "sha256:8da5fd321795754b994c64e3eb8a5a14ff47bd285559a7e876f3c79abafc67f9"
        and re.fullmatch(r"sha256:[0-9a-f]{64}", source_digest)
        and receipt["build_flags"]
        == ["-mod=readonly", "-trimpath", "-buildvcs=false", "-buildmode=exe"]
        and receipt["ldflags"]
        == (
            "-buildid= -linkmode=internal -X propertyquarry.local/"
            "release-control-v2/internal/"
            f"releasecontrol.SourceManifestDigest={source_digest} -X "
            "propertyquarry.local/release-control-v2/internal/releasecontrol."
            "ScratchExecutionContract=linux-amd64-static-et-exec-v1"
        )
        and receipt["build_environment"] == expected_environment
        and receipt["binary_mode"] == "0755"
        and receipt["binary_sizes"] == expected_sizes
        and receipt["binaries"] == expected_digests
    )
    if not valid:
        raise ValueError("receipt binding mismatch")
except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
PY
then
  fail "build receipt metadata does not match the staged binary bytes"
fi

[ "$(digest "$BUILD_RECEIPT")" = "$BUILD_RECEIPT_SHA" ] || fail "build receipt changed during validation"
[ "$(digest "$BINARY_ROOT/propertyquarry-release-controller-v2")" = "$CONTROLLER_SHA" ] ||
  fail "controller changed during validation"
[ "$(digest "$BINARY_ROOT/propertyquarry-release-supervisor-v2")" = "$SUPERVISOR_SHA" ] ||
  fail "supervisor changed during validation"
[ "$(digest "$BINARY_ROOT/propertyquarry-release-watchdog-v2")" = "$WATCHDOG_SHA" ] ||
  fail "watchdog changed during validation"

EXPECTED_TEMPLATE_FILES=$(printf '%s\n' \
  schema/controller-v2.schema.json \
  schema/watchdog-v2.schema.json \
  systemd/propertyquarry-release-control-v2.socket \
  systemd/propertyquarry-release-control-v2@.service \
  systemd/propertyquarry-release-watchdog-v2.service \
  sysusers.d/propertyquarry-release-control-v2.conf \
  tmpfiles.d/propertyquarry-release-control-v2.conf)
[ -d "$PACKAGE_ROOT" ] && [ ! -L "$PACKAGE_ROOT" ] || fail "package template root is invalid"
ACTUAL_TEMPLATE_FILES=$(find "$PACKAGE_ROOT" -type f -printf '%P\n') ||
  fail "package template cannot be enumerated"
ACTUAL_TEMPLATE_FILES=$(printf '%s\n' "$ACTUAL_TEMPLATE_FILES" | LC_ALL=C sort)
[ "$ACTUAL_TEMPLATE_FILES" = "$EXPECTED_TEMPLATE_FILES" ] ||
  fail "package template is not the exact seven-file v2 set"
[ -z "$(find "$PACKAGE_ROOT" -type l -print -quit)" ] || fail "package template contains a symlink"
for relative in $EXPECTED_TEMPLATE_FILES; do
  source_path="$PACKAGE_ROOT/$relative"
  [ -f "$source_path" ] && [ ! -L "$source_path" ] || fail "package template entry is invalid"
  [ "$(realpath -e -- "$source_path")" = "$source_path" ] ||
    fail "package template entry contains a symlink"
done

if [ "$#" -eq 2 ]; then
  STAGE_ROOT=$2
  CANONICAL_STAGE_ROOT=$(realpath -m -- "$STAGE_ROOT")
  case "$CANONICAL_STAGE_ROOT" in
    /tmp/*) ;;
    *) fail "stage root must be a new isolated /tmp path" ;;
  esac
  [ ! -e "$STAGE_ROOT" ] && [ ! -L "$STAGE_ROOT" ] || fail "stage root must not already exist"
  mkdir -m 0755 "$STAGE_ROOT"
else
  STAGE_ROOT=$(mktemp -d /tmp/propertyquarry-release-control-v2-stage.XXXXXX)
  CANONICAL_STAGE_ROOT=$(realpath -e -- "$STAGE_ROOT")
fi
[ "$(realpath -e -- "$STAGE_ROOT")" = "$CANONICAL_STAGE_ROOT" ] || fail "stage root path changed"

UNIT_TARGET="$STAGE_ROOT/etc/systemd/system"
BINARY_TARGET="$STAGE_ROOT/usr/libexec/propertyquarry-release-control"
SCHEMA_TARGET="$STAGE_ROOT/usr/share/propertyquarry-release-control/schema"
SYSUSERS_TARGET="$STAGE_ROOT/usr/lib/sysusers.d"
TMPFILES_TARGET="$STAGE_ROOT/usr/lib/tmpfiles.d"
mkdir -p "$UNIT_TARGET" "$BINARY_TARGET" "$SCHEMA_TARGET" "$SYSUSERS_TARGET" "$TMPFILES_TARGET"

for unit in \
  propertyquarry-release-control-v2.socket \
  propertyquarry-release-control-v2@.service \
  propertyquarry-release-watchdog-v2.service
do
  install -m 0644 "$PACKAGE_ROOT/systemd/$unit" "$UNIT_TARGET/$unit"
done
install -m 0644 "$PACKAGE_ROOT/schema/controller-v2.schema.json" "$SCHEMA_TARGET/controller-v2.schema.json"
install -m 0644 "$PACKAGE_ROOT/schema/watchdog-v2.schema.json" "$SCHEMA_TARGET/watchdog-v2.schema.json"
install -m 0644 "$PACKAGE_ROOT/sysusers.d/propertyquarry-release-control-v2.conf" "$SYSUSERS_TARGET/propertyquarry-release-control-v2.conf"
install -m 0644 "$PACKAGE_ROOT/tmpfiles.d/propertyquarry-release-control-v2.conf" "$TMPFILES_TARGET/propertyquarry-release-control-v2.conf"
for component in supervisor controller watchdog; do
  name="propertyquarry-release-${component}-v2"
  install -m 0755 "$BINARY_ROOT/$name" "$BINARY_TARGET/$name"
done

[ "$(digest "$BINARY_TARGET/propertyquarry-release-controller-v2")" = "$CONTROLLER_SHA" ] ||
  fail "staged controller differs"
[ "$(digest "$BINARY_TARGET/propertyquarry-release-supervisor-v2")" = "$SUPERVISOR_SHA" ] ||
  fail "staged supervisor differs"
[ "$(digest "$BINARY_TARGET/propertyquarry-release-watchdog-v2")" = "$WATCHDOG_SHA" ] ||
  fail "staged watchdog differs"

for target in sysinit basic sockets multi-user network-online shutdown; do
  printf '%s\n' \
    '[Unit]' \
    "Description=Static verification placeholder for $target.target" \
    'DefaultDependencies=no' \
    >"$UNIT_TARGET/$target.target"
done

systemd-analyze verify \
  --root="$STAGE_ROOT" \
  /etc/systemd/system/propertyquarry-release-control-v2.socket \
  /etc/systemd/system/propertyquarry-release-control-v2@.service \
  /etc/systemd/system/propertyquarry-release-watchdog-v2.service

SYSTEMD_VERSION_OUTPUT=$(systemd-analyze --version) || fail "systemd-analyze version is unavailable"
IFS=' ' read -r SYSTEMD_NAME SYSTEMD_MAJOR _SYSTEMD_VERSION_REST <<EOF
$SYSTEMD_VERSION_OUTPUT
EOF
[ "${SYSTEMD_NAME:-}" = systemd ] || fail "unexpected systemd-analyze version output"
case "${SYSTEMD_MAJOR:-}" in
  ""|*[!0-9]*) fail "unexpected systemd-analyze major version" ;;
esac
SYSTEMD_VERSION="systemd $SYSTEMD_MAJOR"
CONTROLLER_SCHEMA_SHA=$(digest "$SCHEMA_TARGET/controller-v2.schema.json")
WATCHDOG_SCHEMA_SHA=$(digest "$SCHEMA_TARGET/watchdog-v2.schema.json")
SOCKET_SHA=$(digest "$UNIT_TARGET/propertyquarry-release-control-v2.socket")
BROKER_UNIT_SHA=$(digest "$UNIT_TARGET/propertyquarry-release-control-v2@.service")
WATCHDOG_UNIT_SHA=$(digest "$UNIT_TARGET/propertyquarry-release-watchdog-v2.service")
SYSUSERS_SHA=$(digest "$SYSUSERS_TARGET/propertyquarry-release-control-v2.conf")
TMPFILES_SHA=$(digest "$TMPFILES_TARGET/propertyquarry-release-control-v2.conf")

printf '%s\n' \
  '{' \
  '  "schema": "propertyquarry.release-control.staged-unit-verification.v2",' \
  '  "authoritative": false,' \
  '  "production_ready": false,' \
  '  "static_unit_compatibility": true,' \
  '  "package_template_exact_seven": true,' \
  '  "placeholder_targets_used": true,' \
  '  "root_install_performed": false,' \
  '  "package_signature_verified": false,' \
  "  \"systemd_analyze_version\": \"$SYSTEMD_VERSION\"," \
  "  \"build_receipt_sha256\": \"sha256:$BUILD_RECEIPT_SHA\"," \
  '  "binaries": {' \
  "    \"propertyquarry-release-controller-v2\": \"sha256:$CONTROLLER_SHA\"," \
  "    \"propertyquarry-release-supervisor-v2\": \"sha256:$SUPERVISOR_SHA\"," \
  "    \"propertyquarry-release-watchdog-v2\": \"sha256:$WATCHDOG_SHA\"" \
  '  },' \
  '  "package_templates": {' \
  "    \"schema/controller-v2.schema.json\": \"sha256:$CONTROLLER_SCHEMA_SHA\"," \
  "    \"schema/watchdog-v2.schema.json\": \"sha256:$WATCHDOG_SCHEMA_SHA\"," \
  "    \"systemd/propertyquarry-release-control-v2.socket\": \"sha256:$SOCKET_SHA\"," \
  "    \"systemd/propertyquarry-release-control-v2@.service\": \"sha256:$BROKER_UNIT_SHA\"," \
  "    \"systemd/propertyquarry-release-watchdog-v2.service\": \"sha256:$WATCHDOG_UNIT_SHA\"," \
  "    \"sysusers.d/propertyquarry-release-control-v2.conf\": \"sha256:$SYSUSERS_SHA\"," \
  "    \"tmpfiles.d/propertyquarry-release-control-v2.conf\": \"sha256:$TMPFILES_SHA\"" \
  '  }' \
  '}' >"$STAGE_ROOT/staged-unit-receipt.json"
chmod 0644 "$STAGE_ROOT/staged-unit-receipt.json"

printf '%s\n' "$STAGE_ROOT/staged-unit-receipt.json"
