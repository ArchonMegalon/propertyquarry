#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC DOCKER_BUILDKIT=1 SOURCE_DATE_EPOCH=0
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077

fail() {
  printf '%s\n' 'propertyquarry-installer-image-build-rejected' >&2
  exit 50
}

verify_local_docker() {
  [[ "$(command -v docker 2>/dev/null)" == "/usr/bin/docker" ]] || fail
  [[ -f /usr/bin/docker && ! -L /usr/bin/docker ]] || fail
  [[ "$(stat -Lc '%F:%a:%u:%h' -- /usr/bin/docker 2>/dev/null)" == \
    "regular file:755:0:1" ]] || fail
  [[ -S /var/run/docker.sock && ! -L /var/run/docker.sock ]] || fail
  [[ "$(stat -Lc '%F:%h:%u' -- /var/run/docker.sock 2>/dev/null)" == \
    "socket:1:0" ]] || fail
  [[ "${DOCKER_HOST}" == "unix:///var/run/docker.sock" ]] || fail
  [[ "$(docker context show 2>/dev/null)" == "default" ]] || fail
  [[ "$(docker context inspect default --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" == \
    "unix:///var/run/docker.sock" ]] || fail
  [[ "$(docker version --format '{{.Server.Os}}:{{.Server.Arch}}' 2>/dev/null)" == \
    "linux:amd64" ]] || fail
}

[[ "$#" -eq 2 && "$1" = /* && "$2" = /* ]] || fail
build_directory="$1"
receipt_path="$2"
module_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
binary="${build_directory}/propertyquarry-release-single-host-installer-v2"
build_receipt="${build_directory}/build-receipt.v2.json"
rootfs_verifier="${module_root}/tools/install-with-docker.sh"
[[ -d "$build_directory" && ! -L "$build_directory" && -f "$binary" && ! -L "$binary" && \
  -f "$build_receipt" && ! -L "$build_receipt" && ! -e "$receipt_path" ]] || fail
[[ "$(stat -Lc '%a:%h' -- "$binary")" == "555:1" && \
  "$(stat -Lc '%a:%h' -- "$build_receipt")" == "644:1" ]] || fail
[[ -f "$rootfs_verifier" && ! -L "$rootfs_verifier" && -x "$rootfs_verifier" ]] || fail
[[ -f /usr/libexec/docker/cli-plugins/docker-buildx && ! -L /usr/libexec/docker/cli-plugins/docker-buildx ]] || fail
[[ "$(stat -Lc '%F:%a:%u:%h' -- /usr/libexec/docker/cli-plugins/docker-buildx)" == \
  "regular file:755:0:1" ]] || fail

workspace="$(mktemp -d /tmp/propertyquarry-installer-image-build-XXXXXX)" || fail
workspace_identity="$(stat -Lc '%d:%i' -- "$workspace")"
context="${workspace}/context"
docker_config_directory="${workspace}/docker-config"
mkdir -m 0700 -- "$context" "$docker_config_directory"
export DOCKER_CONFIG="$docker_config_directory"
tag_one=""
tag_two=""
owned_tag_one=""
owned_tag_one_image=""
owned_tag_two=""
owned_tag_two_image=""

remove_owned_tag_if_unchanged() {
  local owned_tag="${1:-}"
  local owned_image="${2:-}"
  local current_image=""
  [[ -n "$owned_tag" && -n "$owned_image" ]] || return 0
  current_image="$(docker image inspect --format '{{.Id}}' "$owned_tag" 2>/dev/null || true)"
  [[ "$current_image" == "$owned_image" ]] || return 0
  docker image rm "$owned_tag" >/dev/null 2>&1 || true
}

read_build_image_id() {
  /usr/bin/python3 - "$1" <<'PY'
import os
import re
import stat
import sys

flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(sys.argv[1], flags)
try:
    metadata = os.fstat(descriptor)
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert metadata.st_size in (71, 72)
    raw = os.read(descriptor, 73)
finally:
    os.close(descriptor)
assert re.fullmatch(rb"sha256:[0-9a-f]{64}\n?", raw)
sys.stdout.write(raw.removesuffix(b"\n").decode("ascii"))
PY
}

cleanup() {
  remove_owned_tag_if_unchanged "${owned_tag_one:-}" "${owned_tag_one_image:-}"
  remove_owned_tag_if_unchanged "${owned_tag_two:-}" "${owned_tag_two_image:-}"
  if [[ -n "${workspace:-}" && "$workspace" == /tmp/propertyquarry-installer-image-build-?????? && \
    -d "$workspace" && ! -L "$workspace" && \
    "$(stat -Lc '%d:%i' -- "$workspace" 2>/dev/null)" == "$workspace_identity" ]]; then
    find "$workspace" -xdev -depth -mindepth 1 -delete >/dev/null 2>&1 || true
    rmdir -- "$workspace" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 50' INT TERM HUP

verify_local_docker
builder_state="${workspace}/builder-state.jsonl"
docker buildx ls --format '{{json .}}' >"$builder_state" 2>/dev/null || fail
/usr/bin/python3 - "$builder_state" <<'PY' || fail
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], "r", encoding="utf-8") if line.strip()]
defaults = [record for record in records if record.get("Name") == "default"]
assert defaults
assert any(record.get("Current") is True for record in defaults)
for record in defaults:
    assert record.get("Driver") == "docker"
    assert record.get("Dynamic") is False
    nodes = record.get("Nodes")
    assert isinstance(nodes, list) and len(nodes) == 1
    node = nodes[0]
    assert node.get("Name") == "default"
    assert node.get("Endpoint") == "default"
    assert node.get("Status") == "running"
PY

receipt_fields_path="${workspace}/build-receipt-fields"
/usr/bin/python3 - "$build_receipt" "$binary" >"$receipt_fields_path" <<'PY' || fail
import hashlib
import json
import re
import sys

receipt_raw = open(sys.argv[1], "rb").read()
value = json.loads(receipt_raw)
canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
assert receipt_raw == canonical
assert value["schema"] == "propertyquarry.release-control.single-host-native-build-receipt.v2"
assert value["installer_binary_mode"] == "0555"
assert value["installer_package_authority_bound"] is True
assert re.fullmatch(r"sha256:[0-9a-f]{64}", value["installer_package_authority_key_id"])
assert re.fullmatch(r"sha256:[0-9a-f]{64}", value["source_manifest_digest"])
binary_raw = open(sys.argv[2], "rb").read()
assert value["installer_binary_sha256"] == "sha256:" + hashlib.sha256(binary_raw).hexdigest()
assert value["installer_binary_size"] == len(binary_raw)
print(value["installer_binary_sha256"])
print(value["installer_binary_size"])
print(value["installer_package_authority_key_id"])
print(value["source_manifest_digest"])
PY
readarray -t receipt_fields <"$receipt_fields_path"
[[ "${#receipt_fields[@]}" -eq 4 ]] || fail
installer_digest="${receipt_fields[0]}"
installer_size="${receipt_fields[1]}"
package_key_id="${receipt_fields[2]}"
source_digest="${receipt_fields[3]}"
unset receipt_fields

self_test_path="${workspace}/source-self-test.json"
"$binary" --self-test >"$self_test_path" 2>/dev/null || fail
/usr/bin/python3 - "$self_test_path" "$package_key_id" "$source_digest" <<'PY' || fail
import json
import sys

raw = open(sys.argv[1], "rb").read()
value = json.loads(raw)
canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
assert raw == canonical
assert value["embedded_package_authority_bound"] is True
assert value["embedded_package_authority_key_id"] == sys.argv[2]
assert value["source_manifest_digest"] == sys.argv[3]
assert value["host_install_performed"] is False
assert value["production_ready"] is False
PY

install -m 0555 "$binary" "$context/propertyquarry-release-single-host-installer-v2"
touch -h -d @0 "$context/propertyquarry-release-single-host-installer-v2"
tag_suffix="${installer_digest#sha256:}"
tag_nonce="${workspace##*-}"
[[ "$tag_nonce" =~ ^[A-Za-z0-9]{6}$ ]] || fail
tag_one="propertyquarry-release-installer-build-one:${tag_suffix}-${tag_nonce}"
tag_two="propertyquarry-release-installer-build-two:${tag_suffix}-${tag_nonce}"
final_tag="propertyquarry-release-installer-v2:${tag_suffix}"
[[ -z "$(docker image inspect --format '{{.Id}}' "$tag_one" 2>/dev/null || true)" ]] || fail
[[ -z "$(docker image inspect --format '{{.Id}}' "$tag_two" 2>/dev/null || true)" ]] || fail

iid_one_path="${workspace}/image-one.id"
iid_two_path="${workspace}/image-two.id"
docker buildx build --builder default --load --platform linux/amd64 \
  --network none --pull=false --no-cache --provenance=false --sbom=false \
  --iidfile "$iid_one_path" \
  --build-arg SOURCE_DATE_EPOCH=0 \
  -f "$module_root/Dockerfile.installer" -t "$tag_one" "$context" >/dev/null 2>&1 || fail
image_one="$(read_build_image_id "$iid_one_path")" || fail
tagged_image_one="$(docker image inspect --format '{{.Id}}' "$tag_one" 2>/dev/null)"
[[ "$image_one" =~ ^sha256:[0-9a-f]{64}$ && "$tagged_image_one" == "$image_one" ]] || fail
# Ownership begins only after buildx's iid and the newly absent-checked tag agree.
owned_tag_one="$tag_one"
owned_tag_one_image="$image_one"
docker buildx build --builder default --load --platform linux/amd64 \
  --network none --pull=false --no-cache --provenance=false --sbom=false \
  --iidfile "$iid_two_path" \
  --build-arg SOURCE_DATE_EPOCH=0 \
  -f "$module_root/Dockerfile.installer" -t "$tag_two" "$context" >/dev/null 2>&1 || fail
image_two="$(read_build_image_id "$iid_two_path")" || fail
tagged_image_two="$(docker image inspect --format '{{.Id}}' "$tag_two" 2>/dev/null)"
[[ "$image_two" =~ ^sha256:[0-9a-f]{64}$ && "$tagged_image_two" == "$image_two" ]] || fail
owned_tag_two="$tag_two"
owned_tag_two_image="$image_two"
[[ "$image_one" == "$image_two" ]] || fail

"$rootfs_verifier" verify-rootfs "$image_one" "$installer_digest" "$installer_size" "$package_key_id" "$source_digest" || fail
existing_final="$(docker image inspect --format '{{.Id}}' "$final_tag" 2>/dev/null || true)"
[[ -z "$existing_final" || "$existing_final" == "$image_one" ]] || fail
docker image tag "$image_one" "$final_tag" || fail

/usr/bin/python3 - "$receipt_path" "$image_one" "$final_tag" "$installer_digest" "$package_key_id" "$source_digest" <<'PY' || fail
import json
import os
import sys

value = {
    "authoritative": False,
    "component": "propertyquarry-release-single-host-installer-v2",
    "digest_pinned": True,
    "image_id": sys.argv[2],
    "image_tag": sys.argv[3],
    "installer_binary_sha256": sys.argv[4],
    "package_authority_key_id": sys.argv[5],
    "performs_release_effects": False,
    "production_ready": False,
    "reproducible_double_image_build": True,
    "root_install_performed": False,
    "schema": "propertyquarry.release-control.single-host-installer-image-build-receipt.v2",
    "scratch_rootfs_verified": True,
    "source_manifest_digest": sys.argv[6],
    "verified_local_docker_daemon": True,
    "version": 2,
}
raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
try:
    view = memoryview(raw)
    written = 0
    while written < len(raw):
        count = os.write(descriptor, view[written:])
        assert count > 0
        written += count
    os.fsync(descriptor)
    os.fchmod(descriptor, 0o644)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(os.path.dirname(sys.argv[1]), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
printf '%s\n' "$receipt_path"
