#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME \
  GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN GH_HOST GH_CONFIG_DIR
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077

fail() {
  printf '%s\n' 'propertyquarry-github-credential-provision-rejected' >&2
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
credential_producer_pid=""

cleanup() {
  if [[ -n "${credential_producer_pid:-}" ]] && kill -0 "$credential_producer_pid" 2>/dev/null; then
    kill "$credential_producer_pid" >/dev/null 2>&1 || true
    wait "$credential_producer_pid" >/dev/null 2>&1 || true
  fi
  credential_producer_pid=""
  local path="${private_workspace:-}"
  [[ -n "$path" ]] || return 0
  [[ "$path" == /tmp/propertyquarry-github-credential-?????? ]] || return 1
  [[ -d "$path" && ! -L "$path" ]] || return 1
  [[ "$(stat -Lc '%d:%i' -- "$path" 2>/dev/null)" == "$private_workspace_identity" ]] || return 1
  find "$path" -xdev -depth -mindepth 1 -delete >/dev/null 2>&1 || return 1
  rmdir -- "$path" || return 1
  private_workspace=""
  private_workspace_identity=""
}

[[ "$#" -eq 4 ]] || fail
helper_image_id="$1"
package_path="$2"
package_anchor_path="$3"
receipt_directory="$4"
[[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
[[ "$package_path" =~ ^/[A-Za-z0-9._/+:-]+$ && "$package_anchor_path" =~ ^/[A-Za-z0-9._/+:-]+$ && \
  "$receipt_directory" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
[[ -f "$package_path" && ! -L "$package_path" && \
  "$(stat -Lc '%a:%h' -- "$package_path")" == "400:1" ]] || fail
[[ -f "$package_anchor_path" && ! -L "$package_anchor_path" && \
  "$(stat -Lc '%a:%h' -- "$package_anchor_path")" == "444:1" ]] || fail
[[ -d "$receipt_directory" && ! -L "$receipt_directory" ]] || fail
[[ "$(stat -Lc '%a:%u:%g' -- "$receipt_directory")" == "700:$(id -u):$(id -g)" ]] || fail
[[ -z "$(ls -A -- "$receipt_directory")" ]] || fail
[[ "$(id -u):$(id -g)" == "1000:1000" ]] || fail

module_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
image_verifier="${module_root}/tools/install-with-docker.sh"
receipt_verifier="${module_root}/tools/verify-install-receipt.py"
[[ -f "$image_verifier" && ! -L "$image_verifier" && -x "$image_verifier" ]] || fail
[[ -f "$receipt_verifier" && ! -L "$receipt_verifier" ]] || fail
[[ "$(command -v gh 2>/dev/null)" == "/usr/bin/gh" ]] || fail
[[ -f /usr/bin/gh && ! -L /usr/bin/gh && \
  "$(stat -Lc '%F:%a:%u:%g:%h' -- /usr/bin/gh)" == "regular file:755:0:0:1" ]] || fail
github_config_directory="/home/tibor/.config/gh"
github_config="${github_config_directory}/hosts.yml"
[[ -d "$github_config_directory" && ! -L "$github_config_directory" && \
  "$(stat -Lc '%u:%g' -- "$github_config_directory")" == "1000:1000" ]] || fail
[[ -f "$github_config" && ! -L "$github_config" && \
  "$(stat -Lc '%a:%u:%g:%h' -- "$github_config")" == "600:1000:1000:1" ]] || fail
export HOME=/home/tibor GH_CONFIG_DIR="$github_config_directory" GH_PROMPT_DISABLED=1

"$image_verifier" verify-image "$helper_image_id" "$package_path" "$package_anchor_path" >/dev/null || fail
verify_local_docker
[[ "$(docker image inspect --format '{{.Id}}' "$helper_image_id" 2>/dev/null)" == "$helper_image_id" ]] || fail

# Verify that the configured identity can perform the two exact read-only
# activation-canary calls before any host mutation. Responses stay discarded.
/usr/bin/gh api --method GET repos/ArchonMegalon/propertyquarry/actions/runners >/dev/null 2>&1 || fail
/usr/bin/gh api --method GET repos/ArchonMegalon/propertyquarry/actions/oidc/customization/sub >/dev/null 2>&1 || fail

private_workspace="$(mktemp -d /tmp/propertyquarry-github-credential-XXXXXX)" || fail
[[ -d "$private_workspace" && ! -L "$private_workspace" ]] || fail
[[ "$(stat -Lc '%a:%u:%g' -- "$private_workspace")" == "700:1000:1000" ]] || fail
private_workspace_identity="$(stat -Lc '%d:%i' -- "$private_workspace")"
trap 'cleanup || true' EXIT
trap 'exit 50' INT TERM HUP

credential_fifo="${private_workspace}/github-api-token.pipe"
mkfifo -m 0600 -- "$credential_fifo" || fail
[[ -p "$credential_fifo" && ! -L "$credential_fifo" && \
  "$(stat -Lc '%a:%u:%g:%h' -- "$credential_fifo")" == "600:1000:1000:1" ]] || fail

# FD 8 is the only token-bearing descriptor in the host wrapper. The token is
# never assigned to a shell variable, argv, environment variable, or file.
(
  exec 8>"$credential_fifo" || exit 50
  exec /usr/bin/gh auth token --hostname github.com >&8 2>/dev/null
) &
credential_producer_pid="$!"

if ! docker run --rm --pull never \
  --name "propertyquarry-release-credential-${helper_image_id#sha256:}" \
  --network none --read-only --user 0:0 \
  --entrypoint /propertyquarry-release-single-host-installer-v2 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SYS_CHROOT \
  --security-opt no-new-privileges --security-opt apparmor=unconfined \
  --pids-limit 64 --memory 128m --memory-swap 128m --cpus 1 \
  --mount type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave \
  --mount "type=bind,src=${package_path},dst=/input/propertyquarry-release-single-host-v2.tar,readonly" \
  --mount "type=bind,src=${credential_fifo},dst=/input/github-api-token.pipe,readonly" \
  --mount "type=bind,src=${receipt_directory},dst=/output,readonly=false" \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1048576,mode=0700 \
  "$helper_image_id" provision-github-credential; then
  fail
fi
if ! wait "$credential_producer_pid"; then
  credential_producer_pid=""
  fail
fi
credential_producer_pid=""

receipt="${receipt_directory}/propertyquarry-release-single-host-v2-github-credential-receipt.json"
[[ -f "$receipt" && ! -L "$receipt" && "$(stat -Lc '%a:%h' -- "$receipt")" == "600:1" ]] || fail
[[ "$(find "$receipt_directory" -xdev -mindepth 1 -maxdepth 1 -printf '%f\n')" == \
  "$(basename -- "$receipt")" ]] || fail
/usr/bin/python3 "$receipt_verifier" --kind credential --package "$package_path" \
  --package-authority-public-key "$package_anchor_path" --receipt "$receipt" >/dev/null 2>&1 || fail

cleanup || fail
trap - EXIT INT TERM HUP
printf '%s\n' "$receipt"
