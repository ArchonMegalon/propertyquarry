#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077

fail() {
  printf '%s\n' 'propertyquarry-tour-v4-docker-dispatch-rejected' >&2
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

manifest_sha256='sha256:5bc06e8758bc3d9b9e88a82a7608a1238340c19fd581d3ec3565a56d75d1fa06'
public_tree_sha256='sha256:d69c032b96264d892bbd6e269b884a9f33cc11cf3d0f5a7d96a878a062058548'
bundle_path='/tmp/property-f7-tour-final-v4.HUQw8lU4/ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d'

valid_old_tree() {
  [[ "$1" == "absent" || "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

valid_existing_tree() {
  [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

valid_transaction() {
  [[ "$1" =~ ^[0-9a-f]{32}$ ]]
}

[[ "$#" -ge 4 ]] || fail
helper_image_id="$1"
package_path="$2"
package_anchor_path="$3"
shift 3
dispatch_args=("$@")

[[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
[[ "$package_path" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
[[ "$package_anchor_path" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
[[ -f "$package_path" && ! -L "$package_path" &&
  "$(stat -Lc '%a:%h' -- "$package_path")" == "400:1" ]] || fail
[[ -f "$package_anchor_path" && ! -L "$package_anchor_path" &&
  "$(stat -Lc '%a:%h' -- "$package_anchor_path")" == "444:1" ]] || fail
anchor_size="$(stat -Lc '%s' -- "$package_anchor_path")"
[[ "$anchor_size" =~ ^[1-9][0-9]*$ ]] || fail
(( 10#$anchor_size <= 4096 )) || fail

case "${dispatch_args[0]:-}" in
  tour-v4-authority-info)
    [[ "${#dispatch_args[@]}" -eq 1 ]] || fail
    ;;
  tour-inspect-v4)
    [[ "${#dispatch_args[@]}" -eq 3 &&
      "${dispatch_args[1]}" == "--expected-manifest-sha256" &&
      "${dispatch_args[2]}" == "$manifest_sha256" ]] || fail
    ;;
  tour-publish-v4)
    [[ "${#dispatch_args[@]}" -eq 9 &&
      "${dispatch_args[1]}" == "--bundle" &&
      "${dispatch_args[2]}" == "$bundle_path" &&
      "${dispatch_args[3]}" == "--expected-manifest-sha256" &&
      "${dispatch_args[4]}" == "$manifest_sha256" &&
      "${dispatch_args[5]}" == "--expected-old-tree" &&
      "${dispatch_args[7]}" == "--transaction-id" ]] || fail
    valid_old_tree "${dispatch_args[6]}" || fail
    valid_transaction "${dispatch_args[8]}" || fail
    ;;
  tour-recover-v4)
    [[ "${#dispatch_args[@]}" -eq 7 &&
      "${dispatch_args[1]}" == "--expected-manifest-sha256" &&
      "${dispatch_args[2]}" == "$manifest_sha256" &&
      "${dispatch_args[3]}" == "--expected-old-tree" &&
      "${dispatch_args[5]}" == "--transaction-id" ]] || fail
    valid_old_tree "${dispatch_args[4]}" || fail
    valid_transaction "${dispatch_args[6]}" || fail
    ;;
  tour-rollback-v4)
    [[ "${#dispatch_args[@]}" -eq 9 &&
      "${dispatch_args[1]}" == "--expected-manifest-sha256" &&
      "${dispatch_args[2]}" == "$manifest_sha256" &&
      "${dispatch_args[3]}" == "--expected-old-tree" &&
      "${dispatch_args[5]}" == "--expected-current-tree" &&
      "${dispatch_args[6]}" == "$public_tree_sha256" &&
      "${dispatch_args[7]}" == "--transaction-id" ]] || fail
    valid_existing_tree "${dispatch_args[4]}" || fail
    valid_transaction "${dispatch_args[8]}" || fail
    ;;
  *)
    fail
    ;;
esac

module_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
image_verifier="${module_root}/tools/install-with-docker.sh"
[[ -f "$image_verifier" && ! -L "$image_verifier" && -x "$image_verifier" ]] || fail

"$image_verifier" verify-image \
  "$helper_image_id" "$package_path" "$package_anchor_path" >/dev/null || fail
verify_local_docker
[[ "$(docker image inspect --format '{{.Id}}' "$helper_image_id" 2>/dev/null)" == \
  "$helper_image_id" ]] || fail

docker run --rm --pull never \
  --name "propertyquarry-tour-v4-dispatch-${helper_image_id#sha256:}" \
  --network none --read-only --user 0:0 \
  --entrypoint /propertyquarry-release-single-host-installer-v2 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SYS_CHROOT \
  --security-opt no-new-privileges --security-opt apparmor=unconfined \
  --pids-limit 64 --memory 256m --memory-swap 256m --cpus 1 \
  --mount type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave \
  --mount "type=bind,src=${package_path},dst=/input/propertyquarry-release-single-host-v2.tar,readonly" \
  "$helper_image_id" dispatch-tour-v4 "${dispatch_args[@]}" || fail
