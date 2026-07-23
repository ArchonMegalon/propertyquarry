#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
credential_token_fd="${PROPERTYQUARRY_GITHUB_CREDENTIAL_TOKEN_FD:-}"
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME \
  PYTHONINSPECT PYTHONSTARTUP PYTHONBREAKPOINT PYTHONWARNINGS \
  GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN GH_HOST GH_CONFIG_DIR \
  HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy \
  PROPERTYQUARRY_GITHUB_CREDENTIAL_TOKEN_FD
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077

fail() {
  printf '%s\n' 'propertyquarry-github-credential-provision-rejected' >&2
  exit 50
}

ulimit -c 0 || fail
[[ "$(ulimit -c)" == "0" ]] || fail

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
credential_broker_pid=""

cleanup() {
  exec 7>&- 8<&- 9>&- || true
  if [[ -n "${credential_broker_pid:-}" ]] && kill -0 "$credential_broker_pid" 2>/dev/null; then
    kill "$credential_broker_pid" >/dev/null 2>&1 || true
    wait "$credential_broker_pid" >/dev/null 2>&1 || true
  fi
  credential_broker_pid=""
  local path="${private_workspace:-}"
  [[ -n "$path" ]] || return 0
  [[ "$path" == /tmp/propertyquarry-github-credential-?????? ]] || return 1
  [[ -d "$path" && ! -L "$path" ]] || return 1
  [[ "$(stat -Lc '%d:%i' -- "$path" 2>/dev/null)" == "$private_workspace_identity" ]] || return 1
  find "$path" -xdev -depth -mindepth 1 -delete 7>&- 8<&- 9>&- >/dev/null 2>&1 || return 1
  rmdir -- "$path" 7>&- 8<&- 9>&- || return 1
  private_workspace=""
  private_workspace_identity=""
}

[[ "$#" -eq 4 ]] || fail
[[ "$credential_token_fd" == "8" ]] || fail
[[ -r /proc/self/fd/8 && -p /proc/self/fd/8 ]] || fail
helper_image_id="$1"
package_path="$2"
package_anchor_path="$3"
receipt_directory="$4"
[[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
[[ "$package_path" =~ ^/[A-Za-z0-9._/+:-]+$ && "$package_anchor_path" =~ ^/[A-Za-z0-9._/+:-]+$ && \
  "$receipt_directory" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail

script_directory="${BASH_SOURCE[0]%/*}"
module_root="$(cd -- "${script_directory}/.." && pwd -P)" || fail
image_verifier="${module_root}/tools/install-with-docker.sh"
receipt_verifier="${module_root}/tools/verify-install-receipt.py"
credential_broker="${module_root}/tools/verify-github-credential-stream.py"
[[ -f "$image_verifier" && ! -L "$image_verifier" && -x "$image_verifier" ]] || fail
[[ -f "$receipt_verifier" && ! -L "$receipt_verifier" ]] || fail
[[ -f "$credential_broker" && ! -L "$credential_broker" && -x "$credential_broker" ]] || fail

private_workspace="$(mktemp -d /tmp/propertyquarry-github-credential-XXXXXX 8<&-)" || fail
[[ -d "$private_workspace" && ! -L "$private_workspace" ]] || fail
[[ "$(stat -Lc '%a:%u:%g' -- "$private_workspace" 8<&-)" == "700:1000:1000" ]] || fail
private_workspace_identity="$(stat -Lc '%d:%i' -- "$private_workspace" 8<&-)"
trap 'cleanup || true' EXIT
trap 'exit 50' INT TERM HUP

credential_fifo="${private_workspace}/github-api-token.pipe"
gate_fifo="${private_workspace}/verification-gate.pipe"
status_fifo="${private_workspace}/verification-status.pipe"
mkfifo -m 0600 -- "$credential_fifo" "$gate_fifo" "$status_fifo" 8<&- || fail
[[ -p "$credential_fifo" && ! -L "$credential_fifo" && \
  "$(stat -Lc '%a:%u:%g:%h' -- "$credential_fifo" 8<&-)" == "600:1000:1000:1" ]] || fail
[[ -p "$gate_fifo" && ! -L "$gate_fifo" && \
  "$(stat -Lc '%a:%u:%g:%h' -- "$gate_fifo" 8<&-)" == "600:1000:1000:1" ]] || fail
[[ -p "$status_fifo" && ! -L "$status_fifo" && \
  "$(stat -Lc '%a:%u:%g:%h' -- "$status_fifo" 8<&-)" == "600:1000:1000:1" ]] || fail

# The broker is the first and only child to inherit caller FD 8. It blocks on
# the private gate until package/image checks pass; the wrapper then closes its
# copy of FD 8 before invoking any other process.
exec 7<>"$gate_fifo" 9<>"$status_fifo" || fail
/usr/bin/python3 -I -B "$credential_broker" \
  --token-fd 8 --gate-fd 7 --status-fd 9 \
  --credential-fifo "$credential_fifo" </dev/null >/dev/null 2>/dev/null &
credential_broker_pid="$!"
exec 8<&-

[[ -f "$package_path" && ! -L "$package_path" && \
  "$(stat -Lc '%a:%h' -- "$package_path")" == "400:1" ]] || fail
[[ -f "$package_anchor_path" && ! -L "$package_anchor_path" && \
  "$(stat -Lc '%a:%h' -- "$package_anchor_path")" == "444:1" ]] || fail
[[ -d "$receipt_directory" && ! -L "$receipt_directory" ]] || fail
[[ "$(stat -Lc '%a:%u:%g' -- "$receipt_directory")" == "700:$(id -u):$(id -g)" ]] || fail
[[ -z "$(ls -A -- "$receipt_directory")" ]] || fail
[[ "$(id -u):$(id -g)" == "1000:1000" ]] || fail
[[ "$(stat -Lc '%F:%a:%u:%g:%h' -- "$credential_broker")" == \
  "regular file:755:1000:1000:1" ]] || fail

"$image_verifier" verify-image "$helper_image_id" "$package_path" "$package_anchor_path" >/dev/null || fail
verify_local_docker
[[ "$(docker image inspect --format '{{.Id}}' "$helper_image_id" 2>/dev/null)" == "$helper_image_id" ]] || fail

printf '%s\n' 'verify' >&7 || fail
exec 7>&-
credential_status=""
IFS= read -r -t 60 credential_status <&9 || fail
exec 9>&-
[[ "$credential_status" =~ ^credential-instance-sha256=sha256:[0-9a-f]{64}$ ]] || fail
credential_instance_sha256="${credential_status#credential-instance-sha256=}"
kill -0 "$credential_broker_pid" 2>/dev/null || fail

if ! docker run --rm --pull never \
  --name "propertyquarry-release-credential-${helper_image_id#sha256:}" \
  --network none --read-only --user 0:0 \
  --entrypoint /propertyquarry-release-single-host-installer-v2 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SYS_CHROOT \
  --security-opt no-new-privileges --security-opt apparmor=unconfined \
  --pids-limit 64 --memory 128m --memory-swap 128m --cpus 1 \
  --ulimit core=0:0 \
  --mount type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave \
  --mount "type=bind,src=${package_path},dst=/input/propertyquarry-release-single-host-v2.tar,readonly" \
  --mount "type=bind,src=${credential_fifo},dst=/input/github-api-token.pipe,readonly" \
  --mount "type=bind,src=${receipt_directory},dst=/output,readonly=false" \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1048576,mode=0700 \
  "$helper_image_id" provision-github-credential "$credential_instance_sha256"; then
  fail
fi
if ! wait "$credential_broker_pid"; then
  credential_broker_pid=""
  fail
fi
credential_broker_pid=""

receipt="${receipt_directory}/propertyquarry-release-single-host-v2-github-credential-receipt.json"
[[ -f "$receipt" && ! -L "$receipt" && "$(stat -Lc '%a:%h' -- "$receipt")" == "600:1" ]] || fail
[[ "$(find "$receipt_directory" -xdev -mindepth 1 -maxdepth 1 -printf '%f\n')" == \
  "$(basename -- "$receipt")" ]] || fail
/usr/bin/python3 -I -B "$receipt_verifier" --kind credential --package "$package_path" \
  --package-authority-public-key "$package_anchor_path" --receipt "$receipt" \
  --expected-credential-instance-sha256 "$credential_instance_sha256" >/dev/null 2>&1 || fail

cleanup || fail
trap - EXIT INT TERM HUP
printf '%s\n' "$receipt"
