#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077

fail() {
  printf '%s\n' 'propertyquarry-docker-runner-install-rejected' >&2
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

[[ "$#" -eq 5 ]] || fail
helper_image_id="$1"
archive_path="$2"
package_path="$3"
package_anchor_path="$4"
receipt_directory="$5"
[[ "$helper_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
[[ "$archive_path" =~ ^/[A-Za-z0-9._/+:-]+$ && "$package_path" =~ ^/[A-Za-z0-9._/+:-]+$ && \
  "$package_anchor_path" =~ ^/[A-Za-z0-9._/+:-]+$ && "$receipt_directory" =~ ^/[A-Za-z0-9._/+:-]+$ ]] || fail
[[ -f "$archive_path" && ! -L "$archive_path" && "$(stat -Lc '%a:%h:%s' -- "$archive_path")" == \
  "400:1:225628509" ]] || fail
[[ "$(sha256sum -- "$archive_path" | cut -d' ' -f1)" == \
  "4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf" ]] || fail
[[ -d "$receipt_directory" && ! -L "$receipt_directory" && "$(stat -Lc '%a:%u:%g' -- "$receipt_directory")" == \
  "700:$(id -u):$(id -g)" ]] || fail
[[ -z "$(ls -A -- "$receipt_directory")" ]] || fail

module_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
image_verifier="${module_root}/tools/install-with-docker.sh"
[[ -f "$image_verifier" && ! -L "$image_verifier" && -x "$image_verifier" ]] || fail
verify_local_docker
"$image_verifier" verify-image "$helper_image_id" "$package_path" "$package_anchor_path" || fail
verify_local_docker
[[ "$(docker image inspect --format '{{.Id}}' "$helper_image_id" 2>/dev/null)" == "$helper_image_id" ]] || fail

docker run --rm --pull never \
  --name "propertyquarry-runner-install-${helper_image_id#sha256:}" \
  --network none --read-only --user 0:0 \
  --entrypoint /propertyquarry-release-single-host-installer-v2 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --security-opt no-new-privileges --pids-limit 32 --memory 128m --memory-swap 128m --cpus 1 \
  --mount type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave \
  --mount "type=bind,src=${archive_path},dst=/runner-input/actions-runner-linux-x64-2.335.1.tar.gz,readonly" \
  --mount "type=bind,src=${receipt_directory},dst=/output,readonly=false" \
  "$helper_image_id" install-runner || fail

receipt="${receipt_directory}/propertyquarry-release-single-host-v2-runner-install-receipt.json"
[[ -f "$receipt" && ! -L "$receipt" && "$(stat -Lc '%a:%h' -- "$receipt")" == "600:1" ]] || fail
receipt_verifier="${module_root}/tools/verify-install-receipt.py"
[[ -f "$receipt_verifier" && ! -L "$receipt_verifier" ]] || fail
/usr/bin/python3 "$receipt_verifier" --kind runner --package "$package_path" \
  --package-authority-public-key "$package_anchor_path" --receipt "$receipt" >/dev/null 2>&1 || fail
printf '%s\n' "$receipt"
