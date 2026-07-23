#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077

fail() {
  printf '%s\n' 'propertyquarry-docker-root-install-rejected' >&2
  exit 50
}

verify_local_docker() {
  [[ "$(command -v docker 2>/dev/null)" == "/usr/bin/docker" ]] || fail
  [[ -f /usr/bin/docker && ! -L /usr/bin/docker ]] || fail
  [[ "$(stat -Lc '%F:%a:%u:%h' -- /usr/bin/docker 2>/dev/null)" == \
    "regular file:755:0:1" ]] || fail
  [[ -S /var/run/docker.sock && ! -L /var/run/docker.sock ]] || fail
  [[ "$(stat -Lc '%F:%a:%u:%g:%h' -- /var/run/docker.sock 2>/dev/null)" == \
    "socket:660:0:112:1" ]] || fail
  [[ -r /var/run/docker.sock && -w /var/run/docker.sock ]] || fail
  [[ "${DOCKER_HOST}" == "unix:///var/run/docker.sock" ]] || fail
  [[ "$(docker context show 2>/dev/null)" == "default" ]] || fail
  [[ "$(docker context inspect default --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" == \
    "unix:///var/run/docker.sock" ]] || fail
  [[ "$(docker version --format '{{.Server.Os}}:{{.Server.Arch}}' 2>/dev/null)" == \
    "linux:amd64" ]] || fail
}

private_workspace=""
private_workspace_identity=""

cleanup_private_workspace() {
  local path="${private_workspace:-}"
  [[ -n "$path" ]] || return 0
  [[ "$path" == /tmp/propertyquarry-docker-image-verify-?????? ]] || return 1
  [[ -d "$path" && ! -L "$path" ]] || return 1
  [[ "$(stat -Lc '%d:%i' -- "$path" 2>/dev/null)" == "$private_workspace_identity" ]] || return 1
  find "$path" -xdev -depth -mindepth 1 -delete >/dev/null 2>&1 || return 1
  rmdir -- "$path" || return 1
  private_workspace=""
  private_workspace_identity=""
}

begin_private_workspace() {
  [[ -z "${private_workspace:-}" ]] || fail
  private_workspace="$(mktemp -d /tmp/propertyquarry-docker-image-verify-XXXXXX)" || fail
  [[ -d "$private_workspace" && ! -L "$private_workspace" ]] || fail
  [[ "$(stat -Lc '%a:%u:%g' -- "$private_workspace")" == \
    "700:$(id -u):$(id -g)" ]] || fail
  private_workspace_identity="$(stat -Lc '%d:%i' -- "$private_workspace")"
  trap 'cleanup_private_workspace || true' EXIT
  trap 'exit 50' INT TERM HUP
}

finish_private_workspace() {
  cleanup_private_workspace || fail
  trap - EXIT INT TERM HUP
}

verify_installer_rootfs() {
  local helper_image_id="$1"
  local expected_digest="$2"
  local expected_size="$3"
  local expected_key_id="$4"
  local expected_source_digest="$5"
  [[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
  [[ "$expected_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
  [[ "$expected_size" =~ ^[1-9][0-9]{0,8}$ ]] || fail
  (( 10#$expected_size <= 268435456 )) || fail
  [[ "$expected_key_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
  [[ "$expected_source_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail

  verify_local_docker
  begin_private_workspace
  local inspect_path="${private_workspace}/image-inspect.json"
  local image_archive="${private_workspace}/image.tar"
  local extracted_binary="${private_workspace}/propertyquarry-release-single-host-installer-v2"
  local self_test_path="${private_workspace}/self-test.json"

  docker image inspect "$helper_image_id" >"$inspect_path" 2>/dev/null || fail
  /usr/bin/python3 - "$inspect_path" "$helper_image_id" "$expected_size" <<'PY' || fail
import json
import sys

with open(sys.argv[1], "rb") as stream:
    raw = stream.read()
value = json.loads(raw)
assert isinstance(value, list) and len(value) == 1
image = value[0]
assert image.get("Id") == sys.argv[2]
assert image.get("Architecture") == "amd64"
assert image.get("Os") == "linux"
assert image.get("Size") == int(sys.argv[3])
rootfs = image.get("RootFS")
assert isinstance(rootfs, dict) and rootfs.get("Type") == "layers"
layers = rootfs.get("Layers")
assert isinstance(layers, list) and len(layers) == 1
assert isinstance(layers[0], str) and layers[0].startswith("sha256:") and len(layers[0]) == 71
config = image.get("Config")
assert isinstance(config, dict)
assert config.get("Entrypoint") == ["/propertyquarry-release-single-host-installer-v2"]
assert config.get("Cmd") is None
assert config.get("User") in (None, "")
default_path = ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
legacy_defaults = config.get("WorkingDir") in (None, "") and config.get("Env") in (None, [])
docker_defaults = config.get("WorkingDir") == "/" and config.get("Env") == default_path
assert legacy_defaults or docker_defaults
for key in ("ExposedPorts", "Volumes", "Healthcheck", "Labels", "OnBuild", "Shell"):
    assert config.get(key) in (None, [], {})
assert config.get("StopSignal") in (None, "")
PY

  docker image save --output "$image_archive" "$helper_image_id" >/dev/null 2>&1 || fail
  [[ -f "$image_archive" && ! -L "$image_archive" ]] || fail
  local archive_size
  archive_size="$(stat -Lc '%s' -- "$image_archive")"
  [[ "$archive_size" =~ ^[1-9][0-9]*$ ]] || fail
  (( archive_size >= expected_size && archive_size <= expected_size + 4194304 )) || fail

  /usr/bin/python3 - "$image_archive" "$helper_image_id" "$expected_digest" "$expected_size" "$extracted_binary" <<'PY' || fail
import hashlib
import io
import json
import os
import stat
import sys
import tarfile

archive_path, image_id, expected_digest, size_text, output_path = sys.argv[1:]
expected_size = int(size_text)
expected_hex = image_id.removeprefix("sha256:")

def safe_name(name: str) -> bool:
    return bool(name) and not name.startswith("/") and "\\" not in name and all(
        part not in ("", ".", "..") for part in name.split("/")
    )

def safe_member_name(info: tarfile.TarInfo) -> bool:
    name = info.name[:-1] if info.isdir() and info.name.endswith("/") else info.name
    return safe_name(name)

with tarfile.open(archive_path, mode="r:") as outer:
    infos = outer.getmembers()
    assert 1 <= len(infos) <= 64
    by_name = {}
    for info in infos:
        assert safe_member_name(info)
        assert info.name not in by_name
        assert info.isdir() or info.isfile()
        by_name[info.name] = info
    manifest_info = by_name.get("manifest.json")
    assert manifest_info is not None and manifest_info.isfile() and manifest_info.size <= 65536
    manifest_stream = outer.extractfile(manifest_info)
    assert manifest_stream is not None
    manifest_raw = manifest_stream.read(manifest_info.size + 1)
    assert len(manifest_raw) == manifest_info.size
    manifest = json.loads(manifest_raw)
    assert isinstance(manifest, list) and len(manifest) == 1
    entry = manifest[0]
    assert isinstance(entry, dict)
    config_name = entry.get("Config")
    layers = entry.get("Layers")
    legacy_config_name = expected_hex + ".json"
    oci_config_name = "blobs/sha256/" + expected_hex
    assert config_name in (legacy_config_name, oci_config_name)
    assert isinstance(layers, list) and len(layers) == 1 and safe_name(layers[0])
    config_info = by_name.get(config_name)
    layer_info = by_name.get(layers[0])
    assert config_info is not None and config_info.isfile() and config_info.size <= 1048576
    assert layer_info is not None and layer_info.isfile()
    config_stream = outer.extractfile(config_info)
    layer_stream = outer.extractfile(layer_info)
    assert config_stream is not None and layer_stream is not None
    config_raw = config_stream.read(config_info.size + 1)
    layer_raw = layer_stream.read(layer_info.size + 1)
    assert len(config_raw) == config_info.size and len(layer_raw) == layer_info.size

assert hashlib.sha256(config_raw).hexdigest() == expected_hex
if config_name.startswith("blobs/sha256/"):
    assert config_name == "blobs/sha256/" + hashlib.sha256(config_raw).hexdigest()
if layers[0].startswith("blobs/sha256/"):
    assert layers[0] == "blobs/sha256/" + hashlib.sha256(layer_raw).hexdigest()
config = json.loads(config_raw)
assert config.get("architecture") == "amd64" and config.get("os") == "linux"
rootfs = config.get("rootfs")
assert isinstance(rootfs, dict) and rootfs.get("type") == "layers"
diff_ids = rootfs.get("diff_ids")
assert isinstance(diff_ids, list) and len(diff_ids) == 1
assert diff_ids[0] == "sha256:" + hashlib.sha256(layer_raw).hexdigest()
embedded = config.get("config")
assert isinstance(embedded, dict)
assert embedded.get("Entrypoint") == ["/propertyquarry-release-single-host-installer-v2"]
assert embedded.get("Cmd") is None
assert embedded.get("User") in (None, "")
default_path = ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
legacy_defaults = embedded.get("WorkingDir") in (None, "") and embedded.get("Env") in (None, [])
docker_defaults = embedded.get("WorkingDir") == "/" and embedded.get("Env") == default_path
assert legacy_defaults or docker_defaults
for key in ("ExposedPorts", "Volumes", "Healthcheck", "Labels", "OnBuild", "Shell"):
    assert embedded.get(key) in (None, [], {})
assert embedded.get("StopSignal") in (None, "")

with tarfile.open(fileobj=io.BytesIO(layer_raw), mode="r:") as layer:
    infos = layer.getmembers()
    assert len(infos) == 1
    info = infos[0]
    assert info.name == "propertyquarry-release-single-host-installer-v2"
    assert info.isfile() and info.type == tarfile.REGTYPE
    assert info.size == expected_size and info.mode == 0o555
    assert info.uid == 0 and info.gid == 0 and info.linkname == ""
    assert not info.pax_headers
    stream = layer.extractfile(info)
    assert stream is not None
    binary = stream.read(info.size + 1)
    assert len(binary) == expected_size

assert "sha256:" + hashlib.sha256(binary).hexdigest() == expected_digest
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
flags |= getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(output_path, flags, 0o600)
try:
    view = memoryview(binary)
    written = 0
    while written < len(binary):
        count = os.write(descriptor, view[written:])
        assert count > 0
        written += count
    os.fsync(descriptor)
    os.fchmod(descriptor, 0o555)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY

  [[ -f "$extracted_binary" && ! -L "$extracted_binary" ]] || fail
  [[ "$(stat -Lc '%a:%h:%s' -- "$extracted_binary")" == "555:1:${expected_size}" ]] || fail
  [[ "sha256:$(sha256sum -- "$extracted_binary" | cut -d' ' -f1)" == "$expected_digest" ]] || fail
  "$extracted_binary" --self-test >"$self_test_path" 2>/dev/null || fail
  /usr/bin/python3 - "$self_test_path" "$expected_key_id" "$expected_source_digest" <<'PY' || fail
import json
import sys

with open(sys.argv[1], "rb") as stream:
    raw = stream.read()
value = json.loads(raw)
canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
assert raw == canonical
assert set(value) == {
    "authoritative", "component", "embedded_package_authority_bound",
    "embedded_package_authority_key_id", "host_install_performed",
    "performs_release_effects", "production_ready", "root_helper_required",
    "schema", "self_test", "source_manifest_digest", "version",
}
assert value["schema"] == "propertyquarry.release-control.single-host-installer-build-info.v2"
assert value["version"] == 2
assert value["component"] == "propertyquarry-release-single-host-installer-v2"
assert value["embedded_package_authority_bound"] is True
assert value["embedded_package_authority_key_id"] == sys.argv[2]
assert value["source_manifest_digest"] == sys.argv[3]
assert value["self_test"] is True and value["root_helper_required"] is True
for key in ("authoritative", "host_install_performed", "performs_release_effects", "production_ready"):
    assert value[key] is False
PY
  finish_private_workspace
}

verify_signed_package_image() {
  local helper_image_id="$1"
  local package_path="$2"
  local package_anchor_path="$3"
  local module_root package_tool stage_path result_path fields_path receipt_path
  [[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
  [[ "$package_path" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
  [[ "$package_anchor_path" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
  [[ -f "$package_path" && ! -L "$package_path" ]] || fail
  [[ "$(stat -Lc '%a:%h' -- "$package_path")" == "400:1" ]] || fail
  [[ -f "$package_anchor_path" && ! -L "$package_anchor_path" ]] || fail
  [[ "$(stat -Lc '%a:%h' -- "$package_anchor_path")" == "444:1" ]] || fail
  local anchor_size
  anchor_size="$(stat -Lc '%s' -- "$package_anchor_path")"
  [[ "$anchor_size" =~ ^[1-9][0-9]*$ ]] || fail
  (( 10#$anchor_size <= 4096 )) || fail

  module_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
  package_tool="${module_root}/tools/package.py"
  [[ -f "$package_tool" && ! -L "$package_tool" ]] || fail
  begin_private_workspace
  stage_path="${private_workspace}/stage"
  result_path="${private_workspace}/stage-result.json"
  fields_path="${private_workspace}/receipt-fields"
  receipt_path="${stage_path}/payload/etc/propertyquarry-release-single-host-v2/native-build-receipt.v2.json"
  /usr/bin/python3 "$package_tool" stage \
    --package "$package_path" \
    --package-authority-public-key "$package_anchor_path" \
    --output "$stage_path" >"$result_path" 2>/dev/null || fail

  /usr/bin/python3 - "$result_path" "$receipt_path" >"$fields_path" <<'PY' || fail
import json
import re
import sys

sha = re.compile(r"^sha256:[0-9a-f]{64}$")
def load(path):
    raw = open(path, "rb").read()
    value = json.loads(raw)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    assert raw == canonical and isinstance(value, dict)
    return value

result = load(sys.argv[1])
receipt = load(sys.argv[2])
assert result["schema"] == "propertyquarry.release-control.single-host-package-stage-result.v2"
assert result["version"] == 2
assert result["authoritative"] is False and result["production_ready"] is False
assert result["performs_release_effects"] is False and result["root_install_performed"] is False
key_id = result["package_authority_key_id"]
assert isinstance(key_id, str) and sha.fullmatch(key_id)
assert receipt["schema"] == "propertyquarry.release-control.single-host-native-build-receipt.v2"
assert receipt["version"] == 2
assert receipt["installer_package_authority_bound"] is True
assert receipt["installer_package_authority_key_id"] == key_id
assert receipt["installer_binary_mode"] == "0555"
digest = receipt["installer_binary_sha256"]
source = receipt["source_manifest_digest"]
size = receipt["installer_binary_size"]
assert isinstance(digest, str) and sha.fullmatch(digest)
assert isinstance(source, str) and sha.fullmatch(source)
assert isinstance(size, int) and not isinstance(size, bool) and 1 <= size <= 268435456
print(digest)
print(size)
print(key_id)
print(source)
PY
  local -a receipt_fields
  readarray -t receipt_fields <"$fields_path"
  [[ "${#receipt_fields[@]}" -eq 4 ]] || fail
  local installer_digest="${receipt_fields[0]}"
  local installer_size="${receipt_fields[1]}"
  local package_key_id="${receipt_fields[2]}"
  local source_digest="${receipt_fields[3]}"
  unset receipt_fields
  finish_private_workspace

  verify_installer_rootfs "$helper_image_id" "$installer_digest" "$installer_size" "$package_key_id" "$source_digest"
}

if [[ "$#" -eq 6 && "${1:-}" == "verify-rootfs" ]]; then
  verify_installer_rootfs "$2" "$3" "$4" "$5" "$6"
  exit 0
fi

if [[ "$#" -eq 4 && "${1:-}" == "verify-image" ]]; then
  verify_signed_package_image "$2" "$3" "$4"
  exit 0
fi

[[ "$#" -eq 4 ]] || fail
helper_image_id="$1"
package_path="$2"
package_anchor_path="$3"
receipt_directory="$4"
[[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
[[ "$package_path" =~ ^/[A-Za-z0-9._/+:-]+$ && "$package_anchor_path" =~ ^/[A-Za-z0-9._/+:-]+$ && \
  "$receipt_directory" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
[[ -d "$receipt_directory" && ! -L "$receipt_directory" ]] || fail
[[ "$(stat -Lc '%a:%u:%g' -- "$receipt_directory")" == "700:$(id -u):$(id -g)" ]] || fail
[[ -z "$(ls -A -- "$receipt_directory")" ]] || fail

verify_signed_package_image "$helper_image_id" "$package_path" "$package_anchor_path"
verify_local_docker
[[ "$(docker image inspect --format '{{.Id}}' "$helper_image_id" 2>/dev/null)" == "$helper_image_id" ]] || fail

# Prove the exact root-helper container envelope can perform and completely
# collect one reversible host systemd mutation before any install effect.
begin_private_workspace
systemd_canary_receipt="${private_workspace}/host-systemd-canary.json"
docker run --rm --pull never \
  --name "propertyquarry-release-systemd-canary-${helper_image_id#sha256:}" \
  --network none --pid host --read-only --user 0:0 \
  --entrypoint /propertyquarry-release-single-host-installer-v2 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SYS_CHROOT \
  --security-opt no-new-privileges --security-opt apparmor=unconfined \
  --pids-limit 128 --memory 256m --memory-swap 256m --cpus 1 \
  --mount type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave \
  --mount "type=bind,src=${package_path},dst=/input/propertyquarry-release-single-host-v2.tar,readonly" \
  --mount "type=bind,src=${receipt_directory},dst=/output,readonly=false" \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1048576,mode=0700 \
  "$helper_image_id" host-systemd-canary >"$systemd_canary_receipt" || fail
/usr/bin/python3 - "$systemd_canary_receipt" <<'PY' || fail
import json, sys
raw=open(sys.argv[1], 'rb').read()
assert raw.endswith(b'\n') and b'\n' not in raw[:-1]
value=json.loads(raw[:-1])
assert raw == json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()+b'\n'
assert set(value)=={'apparmor_contract','command','host_install_performed','mutation_performed','no_new_privileges','residue_present','schema','unit','version'}
assert value['schema']=='propertyquarry.release-control.single-host-systemd-mutation-canary.v2' and value['version']==2
assert value['apparmor_contract']=='explicitly-unconfined-root-helper-envelope'
assert value['host_install_performed'] is False and value['mutation_performed'] is True
assert value['no_new_privileges'] is True and value['residue_present'] is False
assert value['unit']=='propertyquarry-release-install-mutation-canary-v2.service'
assert value['command'][0]=='/usr/bin/systemd-run' and value['command'][-1]=='/usr/bin/true'
PY
finish_private_workspace
[[ -z "$(ls -A -- "$receipt_directory")" ]] || fail

docker run --rm --pull never \
  --name "propertyquarry-release-install-${helper_image_id#sha256:}" \
  --network none --pid host --read-only --user 0:0 \
  --entrypoint /propertyquarry-release-single-host-installer-v2 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SYS_CHROOT \
  --security-opt no-new-privileges --security-opt apparmor=unconfined \
  --pids-limit 128 --memory 256m --memory-swap 256m --cpus 1 \
  --mount type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave \
  --mount "type=bind,src=${package_path},dst=/input/propertyquarry-release-single-host-v2.tar,readonly" \
  --mount "type=bind,src=${receipt_directory},dst=/output,readonly=false" \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1048576,mode=0700 \
  "$helper_image_id" install || fail

receipt="${receipt_directory}/propertyquarry-release-single-host-v2-install-receipt.json"
[[ -f "$receipt" && ! -L "$receipt" && "$(stat -Lc '%a:%h' -- "$receipt")" == "600:1" ]] || fail
receipt_verifier="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/verify-install-receipt.py"
[[ -f "$receipt_verifier" && ! -L "$receipt_verifier" ]] || fail
/usr/bin/python3 "$receipt_verifier" --kind install --package "$package_path" \
  --package-authority-public-key "$package_anchor_path" --receipt "$receipt" >/dev/null 2>&1 || fail
printf '%s\n' "$receipt"
