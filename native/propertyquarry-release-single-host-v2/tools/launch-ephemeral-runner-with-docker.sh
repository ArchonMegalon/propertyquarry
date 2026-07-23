#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
token_fd_marker="${PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD:-}"
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME \
  GH_TOKEN GITHUB_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_RUNNER_INPUT_TOKEN \
  PROPERTYQUARRY_RUNNER_ADMIN_TOKEN PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077
ulimit -c 0

private_workspace=""
private_workspace_identity=""
container_id=""
broker_pid=""
token_fd_open=1

cleanup() {
  local status="${1:-50}"
  trap - EXIT INT TERM HUP
  if [[ "$token_fd_open" == "1" ]]; then
    exec 8<&- 2>/dev/null || true
    token_fd_open=0
  fi
  exec 7>&- 7<&- 9>&- 9<&- 2>/dev/null || true
  if [[ "$broker_pid" =~ ^[1-9][0-9]*$ ]]; then
    kill -TERM "$broker_pid" >/dev/null 2>&1 || true
    wait "$broker_pid" >/dev/null 2>&1 || true
    broker_pid=""
  fi
  if [[ "$container_id" =~ ^[0-9a-f]{64}$ ]]; then
    docker rm -f "$container_id" >/dev/null 2>&1 || true
    container_id=""
  fi
  if [[ "$private_workspace" == /tmp/propertyquarry-runner-launch.?????? &&
        -d "$private_workspace" && ! -L "$private_workspace" &&
        "$(stat -Lc '%d:%i' -- "$private_workspace" 2>/dev/null)" == \
          "$private_workspace_identity" ]]; then
    find "$private_workspace" -xdev -depth -mindepth 1 -delete \
      >/dev/null 2>&1 || true
    rmdir -- "$private_workspace" >/dev/null 2>&1 || true
  fi
  exit "$status"
}

fail() {
  printf '%s\n' 'propertyquarry-docker-runner-launch-rejected' >&2
  exit 50
}

verify_local_docker() {
  [[ "$(command -v docker 2>/dev/null)" == "/usr/bin/docker" ]] || fail
  [[ -f /usr/bin/docker && ! -L /usr/bin/docker ]] || fail
  [[ "$(stat -Lc '%F:%a:%u:%g:%h' -- /usr/bin/docker 2>/dev/null)" == \
    "regular file:755:0:0:1" ]] || fail
  [[ -S /var/run/docker.sock && ! -L /var/run/docker.sock ]] || fail
  [[ "$(stat -Lc '%F:%a:%u:%g:%h' -- /var/run/docker.sock 2>/dev/null)" == \
    "socket:660:0:112:1" ]] || fail
  [[ -r /var/run/docker.sock && -w /var/run/docker.sock ]] || fail
  [[ "$DOCKER_HOST" == "unix:///var/run/docker.sock" ]] || fail
  [[ "$(docker context show 2>/dev/null)" == "default" ]] || fail
  [[ "$(docker context inspect default --format \
    '{{.Endpoints.docker.Host}}' 2>/dev/null)" == \
    "unix:///var/run/docker.sock" ]] || fail
  [[ "$(docker version --format '{{.Server.Os}}:{{.Server.Arch}}' 2>/dev/null)" == \
    "linux:amd64" ]] || fail
}

capture_file_contract() {
  local path="$1"
  stat -Lc '%d:%i:%s:%Y:%Z:%a:%u:%g:%h' -- "$path" 2>/dev/null || fail
}

verify_file_contracts() {
  [[ "$(capture_file_contract "$package_path")" == "$package_identity" ]] || fail
  [[ "$(capture_file_contract "$package_anchor_path")" == "$anchor_identity" ]] || fail
  [[ "$(capture_file_contract "$resolver_path")" == "$resolver_identity" ]] || fail
  [[ "$(sha256sum -- "$package_path" | cut -d' ' -f1)" == \
    "$package_digest" ]] || fail
  [[ "$(sha256sum -- "$package_anchor_path" | cut -d' ' -f1)" == \
    "$anchor_digest" ]] || fail
  [[ "$(sha256sum -- "$resolver_path" | cut -d' ' -f1)" == \
    "486c423f110722ca4217f91dda8a187e07d4ac8ac08d8d7ed4f59f51abc1ac3d" ]] || fail
}

[[ "$#" -eq 3 ]] || fail
[[ "$EUID" == "1000" && "${GROUPS[0]}" == "1000" ]] || fail
[[ "$token_fd_marker" == "8" && -p /proc/self/fd/8 ]] || fail
helper_image_id="$1"
package_path="$2"
package_anchor_path="$3"
[[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
[[ "$package_path" =~ ^/[A-Za-z0-9._/+:-]+$ &&
   "$package_anchor_path" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
[[ "$package_path" != *"//"* && "$package_path" != *"/./"* &&
   "$package_path" != *"/../"* && "$package_path" != */. &&
   "$package_path" != */.. ]] || fail
[[ "$package_anchor_path" != *"//"* && "$package_anchor_path" != *"/./"* &&
   "$package_anchor_path" != *"/../"* && "$package_anchor_path" != */. &&
   "$package_anchor_path" != */.. ]] || fail
[[ -f "$package_path" && ! -L "$package_path" ]] || fail
[[ -f "$package_anchor_path" && ! -L "$package_anchor_path" ]] || fail

script_source="${BASH_SOURCE[0]}"
[[ "$script_source" =~ ^/[A-Za-z0-9._/+:-]+/tools/launch-ephemeral-runner-with-docker\.sh$ &&
   "$script_source" != *"//"* && "$script_source" != *"/./"* &&
   "$script_source" != *"/../"* ]] || fail
script_directory="${script_source%/*}"
module_root="${script_directory%/*}"
image_verifier="${script_directory}/install-with-docker.sh"
token_relay="${script_directory}/relay-runner-admin-token.py"
[[ -f "$image_verifier" && ! -L "$image_verifier" && -x "$image_verifier" ]] || fail
[[ -f "$token_relay" && ! -L "$token_relay" && -x "$token_relay" ]] || fail

trap 'cleanup $?' EXIT
trap 'cleanup 130' INT
trap 'cleanup 143' TERM HUP
private_workspace="$(mktemp -d /tmp/propertyquarry-runner-launch.XXXXXX 8<&-)" || fail
[[ -d "$private_workspace" && ! -L "$private_workspace" ]] || fail
[[ "$(stat -Lc '%a:%u:%g' -- "$private_workspace" 8<&-)" == \
  "700:1000:1000" ]] || fail
private_workspace_identity="$(
  stat -Lc '%d:%i' -- "$private_workspace" 8<&-
)" || fail
gate_fifo="${private_workspace}/release-gate.fifo"
status_fifo="${private_workspace}/relay-status.fifo"
relay_fifo="${private_workspace}/runner-admin-token.fifo"
inspect_path="${private_workspace}/container-inspect.json"
mkfifo -m 600 -- "$gate_fifo" "$status_fifo" "$relay_fifo" 8<&- || fail
[[ "$(stat -Lc '%F:%a:%u:%g:%h' -- "$gate_fifo" 8<&-)" == \
  "fifo:600:1000:1000:1" ]] || fail
[[ "$(stat -Lc '%F:%a:%u:%g:%h' -- "$status_fifo" 8<&-)" == \
  "fifo:600:1000:1000:1" ]] || fail
[[ "$(stat -Lc '%F:%a:%u:%g:%h' -- "$relay_fifo" 8<&-)" == \
  "fifo:600:1000:1000:1" ]] || fail
exec 7<>"$gate_fifo"
exec 9<>"$status_fifo"

env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC \
  /usr/bin/python3 -I -B "$token_relay" \
  --token-fd 8 --gate-fd 7 --status-fd 9 --relay-fifo "$relay_fifo" \
  8<&8 7<&7 9>&9 &
broker_pid="$!"
exec 8<&-
token_fd_open=0

[[ "$broker_pid" =~ ^[1-9][0-9]*$ ]] || fail
kill -0 "$broker_pid" >/dev/null 2>&1 || fail
[[ "$(stat -Lc '%a:%u:%g:%h' -- "$package_path")" == \
  "400:1000:1000:1" ]] || fail
[[ "$(stat -Lc '%a:%u:%g:%h' -- "$package_anchor_path")" == \
  "444:1000:1000:1" ]] || fail
package_identity="$(capture_file_contract "$package_path")"
anchor_identity="$(capture_file_contract "$package_anchor_path")"
package_digest="$(sha256sum -- "$package_path" | cut -d' ' -f1)"
anchor_digest="$(sha256sum -- "$package_anchor_path" | cut -d' ' -f1)"
[[ "$package_digest" =~ ^[0-9a-f]{64}$ &&
   "$anchor_digest" =~ ^[0-9a-f]{64}$ ]] || fail
resolver_path="${private_workspace}/bridge-resolv.conf"
printf '%s\n' 'nameserver 127.0.0.11' 'options ndots:0' >"$resolver_path" || fail
chmod 0444 -- "$resolver_path" || fail
[[ -f "$resolver_path" && ! -L "$resolver_path" ]] || fail
[[ "$(stat -Lc '%F:%a:%u:%g:%h:%s' -- "$resolver_path")" == \
  "regular file:444:1000:1000:1:38" ]] || fail
resolver_identity="$(capture_file_contract "$resolver_path")"

"$image_verifier" verify-image \
  "$helper_image_id" "$package_path" "$package_anchor_path" || fail
verify_file_contracts
verify_local_docker
[[ "$(docker image inspect --format '{{.Id}}' \
  "$helper_image_id" 2>/dev/null)" == "$helper_image_id" ]] || fail

container_name="propertyquarry-release-runner-launch-${private_workspace##*.}"
[[ "$container_name" =~ ^propertyquarry-release-runner-launch-[A-Za-z0-9]{6}$ ]] || fail
[[ -z "$(docker ps -aq --filter "name=^/${container_name}$")" ]] || fail
verify_file_contracts
container_id="$(
  docker create --pull never -i \
    --name "$container_name" --network bridge \
    --read-only --user 0:0 \
    --entrypoint /propertyquarry-release-single-host-installer-v2 \
    --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE \
    --cap-add FOWNER --cap-add SYS_CHROOT \
    --security-opt no-new-privileges \
    --pids-limit 128 --memory 256m --memory-swap 256m --cpus 1 \
    --ulimit core=0:0 --log-driver none \
    --mount type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave \
    --mount "type=bind,src=${package_path},dst=/input/propertyquarry-release-single-host-v2.tar,readonly" \
    --mount "type=bind,src=${resolver_path},dst=/host/run/systemd/resolve/stub-resolv.conf,readonly" \
    "$helper_image_id" launch-ephemeral-runner
)" || fail
[[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || fail
docker inspect "$container_id" >"$inspect_path" || fail
/usr/bin/python3 -I -B - \
  "$inspect_path" "$container_id" "$container_name" \
  "$helper_image_id" "$package_path" "$resolver_path" <<'PY' || fail
import json
import sys

path, container_id, name, image_id, package_path, resolver_path = sys.argv[1:]
with open(path, "rb") as stream:
    items = json.load(stream)
assert isinstance(items, list) and len(items) == 1
item = items[0]
assert item.get("Id") == container_id
assert item.get("Name") == "/" + name
assert item.get("Path") == "/propertyquarry-release-single-host-installer-v2"
assert item.get("Args") == ["launch-ephemeral-runner"]
config = item.get("Config")
host = item.get("HostConfig")
assert isinstance(config, dict) and isinstance(host, dict)
assert config.get("Image") == image_id
assert config.get("User") == "0:0"
assert config.get("Entrypoint") == [
    "/propertyquarry-release-single-host-installer-v2"
]
assert config.get("Cmd") == ["launch-ephemeral-runner"]
assert config.get("OpenStdin") is True
assert config.get("AttachStdin") is True
assert config.get("Tty") is False
assert config.get("Env") in (
    None,
    [],
    ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"],
)
assert host.get("ReadonlyRootfs") is True
assert host.get("Privileged") is False
assert host.get("CapDrop") == ["ALL"]
assert sorted(host.get("CapAdd") or []) == [
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_FOWNER",
    "CAP_SYS_CHROOT",
]
assert host.get("SecurityOpt") == ["no-new-privileges"]
assert host.get("PidMode") in (None, "")
assert host.get("NetworkMode") == "bridge"
assert host.get("AutoRemove") is False
assert host.get("PidsLimit") == 128
assert host.get("Memory") == 268435456
assert host.get("MemorySwap") == 268435456
assert host.get("NanoCpus") == 1000000000
assert host.get("LogConfig") == {"Config": {}, "Type": "none"}
assert host.get("Ulimits") == [
    {"Hard": 0, "Name": "core", "Soft": 0}
]
assert host.get("Binds") in (None, [])
assert host.get("Devices") in (None, [])
assert host.get("GroupAdd") in (None, [])
assert host.get("RestartPolicy") == {
    "MaximumRetryCount": 0,
    "Name": "no",
}
mounts = item.get("Mounts")
assert isinstance(mounts, list) and len(mounts) == 3
by_destination = {mount.get("Destination"): mount for mount in mounts}
assert set(by_destination) == {
    "/host",
    "/input/propertyquarry-release-single-host-v2.tar",
    "/host/run/systemd/resolve/stub-resolv.conf",
}
host_mount = by_destination["/host"]
assert host_mount.get("Type") == "bind"
assert host_mount.get("Source") == "/"
assert host_mount.get("RW") is True
assert host_mount.get("Propagation") == "rslave"
package_mount = by_destination[
    "/input/propertyquarry-release-single-host-v2.tar"
]
assert package_mount.get("Type") == "bind"
assert package_mount.get("Source") == package_path
assert package_mount.get("RW") is False
resolver_mount = by_destination[
    "/host/run/systemd/resolve/stub-resolv.conf"
]
assert resolver_mount.get("Type") == "bind"
assert resolver_mount.get("Source") == resolver_path
assert resolver_mount.get("RW") is False
networks = (item.get("NetworkSettings") or {}).get("Networks")
assert isinstance(networks, dict) and set(networks) == {"bridge"}
serialized = json.dumps(
    {
        "env": config.get("Env"),
        "path": item.get("Path"),
        "args": item.get("Args"),
        "mounts": mounts,
    },
    sort_keys=True,
)
for forbidden in (
    "ACTIONS_RUNNER_INPUT_TOKEN",
    "PROPERTYQUARRY_RUNNER_ADMIN_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "github_pat_",
    "ghp_",
):
    assert forbidden not in serialized
assert "/var/run/docker.sock" not in {
    mount.get("Destination") for mount in mounts
}
PY

verify_file_contracts
[[ "$(docker image inspect --format '{{.Id}}' \
  "$helper_image_id" 2>/dev/null)" == "$helper_image_id" ]] || fail
kill -0 "$broker_pid" >/dev/null 2>&1 || fail
printf '%s\n' release >&7
exec 7>&- 7<&-
relay_status=""
IFS= read -r -t 75 relay_status <&9 || fail
[[ "$relay_status" == "runner-admin-token-ready" ]] || fail
exec 9>&- 9<&-
docker start --attach --interactive "$container_id" <"$relay_fifo" || fail
wait "$broker_pid" || fail
broker_pid=""
[[ "$(docker inspect --format \
  '{{.State.ExitCode}}:{{.State.Running}}' "$container_id")" == \
  "0:false" ]] || fail
docker rm "$container_id" >/dev/null || fail
container_id=""
cleanup 0
