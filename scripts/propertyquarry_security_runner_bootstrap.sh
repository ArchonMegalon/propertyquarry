#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly INSTALL_ROOT="/opt/propertyquarry-security"
readonly DOWNLOAD_ROOT="${INSTALL_ROOT}/downloads"
readonly SCANNER_ROOT="${INSTALL_ROOT}/scanners"
readonly RUNNER_ROOT="${INSTALL_ROOT}/runner"
readonly GOVERNANCE_ROOT="${INSTALL_ROOT}/governance"
readonly SNAPSHOT_ROOT="${INSTALL_ROOT}/trivy-snapshot"
readonly EVIDENCE_ROOT="${PQ_EVIDENCE_ROOT:?PQ_EVIDENCE_ROOT is required}"
readonly PQ_USER="pqsecurity"
readonly PQ_HOME="/home/${PQ_USER}"
readonly EXPECTED_IMAGE_OS="ubuntu24"
readonly EXPECTED_IMAGE_VERSION="20260714.240.1"
readonly EXPECTED_KERNEL="6.17.0-1020-azure"
readonly EXPECTED_DOCKER_VERSION="28.0.4"

readonly ROOTLESS_EXTRAS_URL="https://download.docker.com/linux/ubuntu/dists/noble/pool/stable/amd64/docker-ce-rootless-extras_28.0.4-1~ubuntu.24.04~noble_amd64.deb"
readonly ROOTLESS_EXTRAS_SHA256="2abb177d60561ac77b50a42b60500ab194b70f40f4b225d837c1fdccaaab7a28"
readonly ROOTLESS_EXTRAS_VERSION="5:28.0.4-1~ubuntu.24.04~noble"
readonly APPARMOR_URL="https://archive.ubuntu.com/ubuntu/pool/main/a/apparmor/apparmor_4.0.1really4.0.1-0ubuntu0.24.04.7_amd64.deb"
readonly APPARMOR_SHA256="45c30f4a9724a21e2f5f91a0556f979c13ab2042e6a38c7fdd6da87829e8d67e"
readonly APPARMOR_VERSION="4.0.1really4.0.1-0ubuntu0.24.04.7"
readonly DBUS_USER_URL="https://archive.ubuntu.com/ubuntu/pool/main/d/dbus/dbus-user-session_1.14.10-4ubuntu4.1_amd64.deb"
readonly DBUS_USER_SHA256="e585b1694b854c3b75bfb39cc4022cafe7b14e44fd435433b613b8fb9919cb41"
readonly DBUS_USER_VERSION="1.14.10-4ubuntu4.1"
readonly UIDMAP_URL="https://archive.ubuntu.com/ubuntu/pool/main/s/shadow/uidmap_4.13+dfsg1-4ubuntu3.2_amd64.deb"
readonly UIDMAP_SHA256="a80cb7f72dd18c73cbb0b07b7fbe855504f26bfafae072a9b3d125c89d499b9e"
readonly UIDMAP_VERSION="1:4.13+dfsg1-4ubuntu3.2"
readonly SLIRP_URL="https://archive.ubuntu.com/ubuntu/pool/universe/s/slirp4netns/slirp4netns_1.2.1-1build2_amd64.deb"
readonly SLIRP_SHA256="3fc72a72a376a3ad3b439434bc87d89d245f9d54a1d540e8a06b74d4e2385e0a"
readonly SLIRP_VERSION="1.2.1-1build2"

readonly RUNNER_URL="https://github.com/actions/runner/releases/download/v2.335.1/actions-runner-linux-x64-2.335.1.tar.gz"
readonly RUNNER_ARCHIVE_SHA256="4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf"
readonly SYFT_URL="https://github.com/anchore/syft/releases/download/v1.48.0/syft_1.48.0_linux_amd64.tar.gz"
readonly SYFT_ARCHIVE_SHA256="6cef9a7f37220d9067eaf9cfaaa2fce986e9f320a8d42cbc36658c99af78ea04"
readonly SYFT_BINARY_SHA256="fd260522b9695350ee23483c88b803e96ffe9f8f3954106a7bcad7940a1ade89"
readonly TRIVY_URL="https://github.com/aquasecurity/trivy/releases/download/v0.72.0/trivy_0.72.0_Linux-64bit.tar.gz"
readonly TRIVY_ARCHIVE_SHA256="bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea"
readonly TRIVY_BINARY_SHA256="0e69edd134a3c338baa1a6806920773615d682b18cbc6a0cba2a3b658ef9b63e"

BOOTSTRAP_STATUS="initializing"
BOOTSTRAP_MESSAGE="bootstrap started"
PQ_UID=""
PQ_GID=""
PQ_RUNTIME=""
DOCKER_SOCKET=""
DOCKER_HOST_VALUE=""
RUNNER_NAME_VALUE=""
RUNNER_AGENT_ID=""
ROOTLESS_SERVICE_ATTEMPTED="false"
LISTENER_EXIT_CODE=""

fail() {
  BOOTSTRAP_STATUS="failed"
  BOOTSTRAP_MESSAGE="$1"
  printf 'PropertyQuarry security runner bootstrap denied: %s\n' "$1" >&2
  return 1
}

on_unexpected_error() {
  local exit_code="$?"
  local line="$1"
  trap - ERR
  if [[ "${BOOTSTRAP_STATUS}" != "failed" ]]; then
    BOOTSTRAP_STATUS="failed"
    BOOTSTRAP_MESSAGE="unexpected command failure at line ${line} (exit ${exit_code})"
    printf 'PropertyQuarry security runner bootstrap denied: %s\n' \
      "${BOOTSTRAP_MESSAGE}" >&2
  fi
  return "${exit_code}"
}

sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}

write_receipt() {
  install -d -m 0700 "${EVIDENCE_ROOT}"
  jq -n \
    --arg schema "propertyquarry.security_runner_bootstrap.v1" \
    --arg status "${BOOTSTRAP_STATUS}" \
    --arg message "${BOOTSTRAP_MESSAGE}" \
    --arg repository "${PQ_REPOSITORY:-}" \
    --arg target_run_id "${PQ_SECURITY_RUN_ID:-}" \
    --arg target_run_attempt "${PQ_SECURITY_RUN_ATTEMPT:-}" \
    --arg target_job_id "${PQ_SECURITY_JOB_ID:-}" \
    --arg target_head_sha "${PQ_EXPECTED_HEAD_SHA:-}" \
    --arg outer_run_id "${GITHUB_RUN_ID:-}" \
    --arg outer_run_attempt "${GITHUB_RUN_ATTEMPT:-}" \
    --arg runner_name "${RUNNER_NAME_VALUE}" \
    --arg runner_label "${PQ_SECURITY_RUNNER_LABEL:-}" \
    --arg runner_agent_id "${RUNNER_AGENT_ID}" \
    --arg listener_exit_code "${LISTENER_EXIT_CODE}" \
    --arg runner_token_expires_at "${PQ_RUNNER_TOKEN_EXPIRES_AT:-}" \
    --arg image_os "${ImageOS:-}" \
    --arg image_version "${ImageVersion:-}" \
    --arg kernel "$(uname -r)" \
    --arg web_image "${PQ_WEB_IMAGE:-}" \
    --arg render_image "${PQ_RENDER_IMAGE:-}" \
    --arg recorded_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{schema:$schema,status:$status,message:$message,repository:$repository,target:{run_id:$target_run_id,run_attempt:$target_run_attempt,job_id:$target_job_id,head_sha:$target_head_sha},bootstrap:{run_id:$outer_run_id,run_attempt:$outer_run_attempt},runner:{name:$runner_name,label:$runner_label,agent_id:$runner_agent_id,listener_exit_code:$listener_exit_code,registration_token_expires_at:$runner_token_expires_at},hosted_image:{os:$image_os,version:$image_version,kernel:$kernel},images:{web:$web_image,render:$render_image},recorded_at:$recorded_at}' \
    >"${EVIDENCE_ROOT}/bootstrap-receipt.json"
  chmod 600 "${EVIDENCE_ROOT}/bootstrap-receipt.json"
}

as_pq() {
  runuser -u "${PQ_USER}" -- env -i \
    HOME="${PQ_HOME}" \
    USER="${PQ_USER}" \
    LOGNAME="${PQ_USER}" \
    PATH="/usr/bin:/bin" \
    XDG_RUNTIME_DIR="${PQ_RUNTIME}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${PQ_RUNTIME}/bus" \
    DOCKER_HOST="${DOCKER_HOST_VALUE}" \
    "$@"
}

capture_rootless_diagnostics() {
  [[ -n "${PQ_UID}" && "${ROOTLESS_SERVICE_ATTEMPTED}" == "true" ]] || return 0
  {
    printf 'PropertyQuarry rootless Docker diagnostic receipt\n'
    printf 'captured_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'user=%s uid=%s runtime=%s\n' "${PQ_USER}" "${PQ_UID}" "${PQ_RUNTIME}"
    printf '\n[systemctl-status]\n'
    as_pq systemctl --user --no-pager --full status docker.service
    printf '\n[systemctl-show]\n'
    as_pq systemctl --user show docker.service \
      --property=LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,FragmentPath,DropInPaths
    printf '\n[socket-posture]\n'
    if [[ -e "${PQ_RUNTIME}" ]]; then
      stat -Lc 'runtime_path=%n uid=%u gid=%g mode=%a type=%F' "${PQ_RUNTIME}"
    fi
    if [[ -e "${DOCKER_SOCKET}" ]]; then
      stat -Lc 'socket_path=%n uid=%u gid=%g mode=%a type=%F' "${DOCKER_SOCKET}"
    fi
    printf '\n[journal]\n'
    as_pq journalctl --user --unit docker.service --no-pager --output=short-precise --lines=500
  } >"${EVIDENCE_ROOT}/rootless-docker.log" 2>&1
  chmod 600 "${EVIDENCE_ROOT}/rootless-docker.log"
}

cleanup() {
  local exit_code="$?"
  set +e
  unset PQ_RUNNER_TOKEN PQ_GHCR_TOKEN GH_TOKEN GITHUB_TOKEN
  if [[ -n "${PQ_UID}" && "${ROOTLESS_SERVICE_ATTEMPTED}" == "true" ]]; then
    capture_rootless_diagnostics
    as_pq systemctl --user stop docker.service >/dev/null 2>&1
  fi
  if [[ "${BOOTSTRAP_STATUS}" != "listener_exited" && "${BOOTSTRAP_STATUS}" != "failed" ]]; then
    BOOTSTRAP_STATUS="failed"
    BOOTSTRAP_MESSAGE="bootstrap exited before the exact runner completed"
  fi
  write_receipt
  chmod -R u+rwX,go-rwx "${EVIDENCE_ROOT}"
  chown -R "${PQ_OUTER_UID:?}:${PQ_OUTER_GID:?}" "${EVIDENCE_ROOT}"
  if [[ -n "${PQ_UID}" ]]; then
    loginctl disable-linger "${PQ_USER}" >/dev/null 2>&1
  fi
  exit "${exit_code}"
}
trap cleanup EXIT
trap 'on_unexpected_error "${LINENO}"' ERR

[[ "${EUID}" -eq 0 ]] || fail "bootstrap must run as root on the disposable hosted VM"
[[ "${GITHUB_ACTIONS:-}" == "true" ]] || fail "not running in GitHub Actions"
[[ "${RUNNER_ENVIRONMENT:-}" == "github-hosted" ]] || fail "outer runner is not GitHub-hosted"
[[ "${GITHUB_REPOSITORY:-}" == "ArchonMegalon/propertyquarry" ]] || fail "outer repository mismatch"
[[ "${GITHUB_REF:-}" == "refs/heads/main" ]] || fail "outer ref is not main"
[[ "${GITHUB_SHA:-}" == "${PQ_EXPECTED_HEAD_SHA:?}" ]] || fail "outer head SHA mismatch"
[[ "${ImageOS:-}" == "${EXPECTED_IMAGE_OS}" ]] || fail "hosted image OS changed"
[[ "${ImageVersion:-}" == "${EXPECTED_IMAGE_VERSION}" ]] || fail "hosted image version changed"
[[ "$(uname -r)" == "${EXPECTED_KERNEL}" ]] || fail "hosted kernel changed"
[[ "${PQ_REPOSITORY:?}" == "ArchonMegalon/propertyquarry" ]] || fail "target repository mismatch"
[[ "${PQ_EXPECTED_WORKFLOW_REF:?}" == "ArchonMegalon/propertyquarry/.github/workflows/smoke-runtime.yml@refs/heads/main" ]] \
  || fail "target workflow ref mismatch"
[[ "${PQ_SECURITY_RUN_ID:?}" =~ ^[0-9]+$ ]] || fail "target run id is malformed"
[[ "${PQ_SECURITY_RUN_ATTEMPT:?}" =~ ^[1-9][0-9]*$ ]] || fail "target run attempt is malformed"
[[ "${PQ_SECURITY_JOB_ID:?}" =~ ^[0-9]+$ ]] || fail "target job id is malformed"
[[ "${PQ_SECURITY_RUNNER_LABEL:?}" =~ ^pqsec-[0-9a-f]{32}$ ]] \
  || fail "one-time runner label is malformed"
[[ "${PQ_EXPECTED_HEAD_SHA}" =~ ^[0-9a-f]{40}$ ]] || fail "target head SHA is malformed"
[[ "${PQ_RUNNER_TOKEN:?}" != *$'\n'* ]] || fail "runner token is malformed"
registration_expires_epoch="$(date -u -d "${PQ_RUNNER_TOKEN_EXPIRES_AT:?}" +%s)" \
  || fail "runner token expiration is malformed"
registration_remaining_seconds="$((registration_expires_epoch - $(date -u +%s)))"
(( registration_remaining_seconds >= 2400 )) || fail "runner token is too close to expiration"
(( registration_remaining_seconds <= 3700 )) || fail "runner token expiration is implausibly distant"
[[ "${PQ_GHCR_TOKEN:?}" != *$'\n'* ]] || fail "registry token is malformed"
[[ "${PQ_WEB_IMAGE:?}" =~ ^ghcr\.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:[0-9a-f]{64}$ ]] \
  || fail "web image is not the exact protected digest form"
[[ "${PQ_RENDER_IMAGE:?}" =~ ^ghcr\.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:[0-9a-f]{64}$ ]] \
  || fail "render image is not the exact protected digest form"
[[ "${PQ_WEB_IMAGE}" != "${PQ_RENDER_IMAGE}" ]] || fail "web and render image identities collide"
[[ "$(sha256_file "${PQ_LOCK_SOURCE:?}")" == "${PQ_LOCK_SHA256:?}" ]] || fail "pip-audit lock hash mismatch"
[[ "$(sha256_file "${PQ_PREFLIGHT_SOURCE:?}")" == "${PQ_PREFLIGHT_SHA256:?}" ]] || fail "preflight hook hash mismatch"
[[ "${PQ_PYTHON_BIN:?}" == "/opt/hostedtoolcache/Python/3.12.13/x64/bin/python" ]] \
  || fail "scanner Python path changed"
[[ "$("${PQ_PYTHON_BIN}" -c 'import platform; print(platform.python_version())')" == "3.12.13" ]] \
  || fail "scanner Python version changed"
[[ "$(docker version --format '{{.Client.Version}}/{{.Server.Version}}')" == "${EXPECTED_DOCKER_VERSION}/${EXPECTED_DOCKER_VERSION}" ]] \
  || fail "outer Docker package version changed"
[[ "$(stat -fc '%T' /sys/fs/cgroup)" == "cgroup2fs" ]] || fail "cgroup v2 is unavailable"
[[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns)" == "1" ]] \
  || fail "Ubuntu user-namespace restriction is not enforced"
if [[ -r /proc/sys/kernel/unprivileged_userns_clone ]]; then
  [[ "$(< /proc/sys/kernel/unprivileged_userns_clone)" == "1" ]] \
    || fail "unprivileged user namespaces are disabled"
fi
(( $(< /proc/sys/user/max_user_namespaces) > 0 )) \
  || fail "no unprivileged user namespaces are available"

available_bytes="$(df --output=avail -B1 /opt | awk 'NR==2 {print $1}')"
available_inodes="$(df --output=iavail /opt | awk 'NR==2 {print $1}')"
(( available_bytes >= 10737418240 )) || fail "less than 10 GiB is available for isolated scans"
(( available_inodes >= 500000 )) || fail "insufficient inodes for isolated scans"

[[ ! -e "${INSTALL_ROOT}" ]] || fail "security install root already exists on supposedly fresh VM"
install -d -m 0711 "${INSTALL_ROOT}"
install -d -m 0700 "${DOWNLOAD_ROOT}" "${SCANNER_ROOT}" \
  "${GOVERNANCE_ROOT}" "${SNAPSHOT_ROOT}" "${EVIDENCE_ROOT}"
write_receipt

download_verified() {
  local url="$1"
  local expected_sha="$2"
  local destination="$3"
  curl --proto '=https' --tlsv1.2 --fail --show-error --silent --location --retry 3 \
    --output "${destination}" "${url}"
  [[ "$(sha256_file "${destination}")" == "${expected_sha}" ]] \
    || fail "downloaded artifact hash mismatch"
}

download_verified "${ROOTLESS_EXTRAS_URL}" "${ROOTLESS_EXTRAS_SHA256}" "${DOWNLOAD_ROOT}/rootless-extras.deb"
download_verified "${APPARMOR_URL}" "${APPARMOR_SHA256}" "${DOWNLOAD_ROOT}/apparmor.deb"
download_verified "${DBUS_USER_URL}" "${DBUS_USER_SHA256}" "${DOWNLOAD_ROOT}/dbus-user-session.deb"
download_verified "${UIDMAP_URL}" "${UIDMAP_SHA256}" "${DOWNLOAD_ROOT}/uidmap.deb"
download_verified "${SLIRP_URL}" "${SLIRP_SHA256}" "${DOWNLOAD_ROOT}/slirp4netns.deb"

verify_deb() {
  local path="$1"
  local package="$2"
  local version="$3"
  [[ "$(dpkg-deb -f "${path}" Package)" == "${package}" ]] || fail "package identity mismatch"
  [[ "$(dpkg-deb -f "${path}" Version)" == "${version}" ]] || fail "package version mismatch"
  [[ "$(dpkg-deb -f "${path}" Architecture)" == "amd64" ]] || fail "package architecture mismatch"
}

verify_deb "${DOWNLOAD_ROOT}/rootless-extras.deb" docker-ce-rootless-extras "${ROOTLESS_EXTRAS_VERSION}"
verify_deb "${DOWNLOAD_ROOT}/apparmor.deb" apparmor "${APPARMOR_VERSION}"
verify_deb "${DOWNLOAD_ROOT}/dbus-user-session.deb" dbus-user-session "${DBUS_USER_VERSION}"
verify_deb "${DOWNLOAD_ROOT}/uidmap.deb" uidmap "${UIDMAP_VERSION}"
verify_deb "${DOWNLOAD_ROOT}/slirp4netns.deb" slirp4netns "${SLIRP_VERSION}"

DEBIAN_FRONTEND=noninteractive dpkg --force-confold --install \
  "${DOWNLOAD_ROOT}/dbus-user-session.deb" \
  "${DOWNLOAD_ROOT}/uidmap.deb" \
  "${DOWNLOAD_ROOT}/slirp4netns.deb" \
  "${DOWNLOAD_ROOT}/apparmor.deb" \
  "${DOWNLOAD_ROOT}/rootless-extras.deb"

[[ "$(dpkg-query -W -f='${Version}' docker-ce-rootless-extras)" == "${ROOTLESS_EXTRAS_VERSION}" ]] \
  || fail "installed rootless extras version mismatch"
[[ "$(dpkg-query -W -f='${Version}' apparmor)" == "${APPARMOR_VERSION}" ]] \
  || fail "installed AppArmor version mismatch"
[[ "$(dpkg-query -W -f='${Version}' dbus-user-session)" == "${DBUS_USER_VERSION}" ]] \
  || fail "installed dbus user-session version mismatch"
[[ "$(dpkg-query -W -f='${Version}' uidmap)" == "${UIDMAP_VERSION}" ]] \
  || fail "installed uidmap version mismatch"
[[ "$(dpkg-query -W -f='${Version}' slirp4netns)" == "${SLIRP_VERSION}" ]] \
  || fail "installed slirp4netns version mismatch"

for binary in docker dockerd containerd runc dockerd-rootless.sh dockerd-rootless-setuptool.sh rootlesskit slirp4netns newuidmap newgidmap; do
  command -v "${binary}" >/dev/null || fail "required rootless runtime binary is missing"
done
[[ "$(stat -c '%U:%G:%a' /usr/bin/newuidmap)" == "root:root:4755" ]] \
  || fail "newuidmap ownership or mode mismatch"
[[ "$(stat -c '%U:%G:%a' /usr/bin/newgidmap)" == "root:root:4755" ]] \
  || fail "newgidmap ownership or mode mismatch"
[[ "$(sha256_file /etc/apparmor.d/rootlesskit)" == "a6a1a760d88312275d64f195e6b2f51627e8cabfdf4e355262c86f0578c66d80" ]] \
  || fail "RootlessKit AppArmor profile changed"
apparmor_parser --replace /etc/apparmor.d/rootlesskit
awk '$1 == "rootlesskit" { found=1 } END { exit found ? 0 : 1 }' /sys/kernel/security/apparmor/profiles \
  || fail "RootlessKit AppArmor profile is not loaded"

if id "${PQ_USER}" >/dev/null 2>&1; then
  fail "dedicated security user already exists"
fi
useradd --create-home --user-group --shell /bin/bash "${PQ_USER}"
passwd --lock "${PQ_USER}" >/dev/null
PQ_UID="$(id -u "${PQ_USER}")"
PQ_GID="$(id -g "${PQ_USER}")"
PQ_RUNTIME="/run/user/${PQ_UID}"
DOCKER_SOCKET="${PQ_RUNTIME}/docker.sock"
DOCKER_HOST_VALUE="unix://${DOCKER_SOCKET}"
[[ "$(id -nG "${PQ_USER}")" == "${PQ_USER}" ]] || fail "security user gained supplementary groups"
if runuser -u "${PQ_USER}" -- sudo -n true >/dev/null 2>&1; then
  fail "security user unexpectedly has sudo authority"
fi

verify_subid() {
  local file="$1"
  awk -F: -v user="${PQ_USER}" '
    $1 == user { user_start=$2+0; user_end=user_start+$3-1; user_count++; next }
    { other_start[count]=$2+0; other_end[count]=other_start[count]+$3-1; count++ }
    END {
      if (user_count != 1 || user_end-user_start+1 < 65536) exit 1
      for (i=0; i<count; i++) {
        if (user_start <= other_end[i] && other_start[i] <= user_end) exit 1
      }
    }
  ' "${file}" || fail "security user subordinate ID range is missing or overlaps"
}
verify_subid /etc/subuid
verify_subid /etc/subgid

# The Actions runner resolves its installation path before it starts and needs
# to enumerate each directory in that path. Restrict that capability to the
# dedicated security group instead of making the install root world-readable.
chown root:"${PQ_USER}" "${INSTALL_ROOT}"
chmod 750 "${INSTALL_ROOT}"
[[ "$(stat -c '%u:%g:%a' "${INSTALL_ROOT}")" == "0:${PQ_GID}:750" ]] \
  || fail "security install root posture mismatch"

if runuser -u "${PQ_USER}" -- env -i HOME="${PQ_HOME}" PATH=/usr/bin:/bin \
  DOCKER_HOST=unix:///var/run/docker.sock docker info >/dev/null 2>&1; then
  fail "security user can reach the outer Docker daemon"
fi

loginctl enable-linger "${PQ_USER}"
systemctl start "user@${PQ_UID}.service"
for _attempt in $(seq 1 30); do
  [[ -S "${PQ_RUNTIME}/bus" ]] && break
  sleep 1
done
[[ -S "${PQ_RUNTIME}/bus" ]] || fail "security user systemd bus did not start"
[[ "$(stat -Lc '%u:%g:%a' "${PQ_RUNTIME}")" == "${PQ_UID}:${PQ_GID}:700" ]] \
  || fail "security user runtime directory posture mismatch"
install -d -o "${PQ_USER}" -g "${PQ_USER}" -m 0700 \
  "${PQ_HOME}/.config" "${PQ_HOME}/.config/docker" \
  "${PQ_HOME}/.config/systemd" "${PQ_HOME}/.config/systemd/user" \
  "${PQ_HOME}/.config/systemd/user/docker.service.d" \
  "${PQ_HOME}/.cache" "${PQ_HOME}/.local" "${PQ_HOME}/.local/share" \
  "${PQ_HOME}/.local/share/docker" "${PQ_HOME}/.local/state"
printf '%s\n' \
  '{' \
  '  "data-root": "/home/pqsecurity/.local/share/docker",' \
  '  "storage-driver": "overlay2",' \
  '  "bridge": "none",' \
  '  "ip-forward": false,' \
  '  "ip-masq": false,' \
  '  "icc": false,' \
  '  "userland-proxy": false,' \
  '  "log-driver": "local",' \
  '  "log-opts": {"max-size": "10m", "max-file": "3"}' \
  '}' >"${PQ_HOME}/.config/docker/daemon.json"
chown "${PQ_USER}:${PQ_USER}" "${PQ_HOME}/.config/docker/daemon.json"
chmod 600 "${PQ_HOME}/.config/docker/daemon.json"
as_pq dockerd --validate --config-file "${PQ_HOME}/.config/docker/daemon.json" >/dev/null \
  || fail "rootless Docker daemon configuration is invalid"

printf '%s\n' \
  '[Service]' \
  "Environment=HOME=${PQ_HOME}" \
  "Environment=XDG_RUNTIME_DIR=${PQ_RUNTIME}" \
  "Environment=XDG_CONFIG_HOME=${PQ_HOME}/.config" \
  "Environment=XDG_DATA_HOME=${PQ_HOME}/.local/share" \
  "Environment=XDG_STATE_HOME=${PQ_HOME}/.local/state" \
  "Environment=XDG_CACHE_HOME=${PQ_HOME}/.cache" \
  "Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=${PQ_RUNTIME}/bus" \
  'Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns' \
  'Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=builtin' \
  'Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_DISABLE_HOST_LOOPBACK=true' \
  'UnsetEnvironment=DOCKER_CONFIG DOCKER_CONTEXT DOCKER_HOST DOCKER_IGNORE_BR_NETFILTER_ERROR' \
  >"${PQ_HOME}/.config/systemd/user/docker.service.d/10-propertyquarry.conf"
chown "${PQ_USER}:${PQ_USER}" \
  "${PQ_HOME}/.config/systemd/user/docker.service.d/10-propertyquarry.conf"
chmod 600 "${PQ_HOME}/.config/systemd/user/docker.service.d/10-propertyquarry.conf"

as_pq rootlesskit true || fail "RootlessKit namespace smoke check failed"
ROOTLESS_SERVICE_ATTEMPTED="true"

if ! as_pq dockerd-rootless-setuptool.sh install --force; then
  BOOTSTRAP_STATUS="failed"
  BOOTSTRAP_MESSAGE="rootless Docker user service failed to start; diagnostic receipt preserved"
  printf 'PropertyQuarry security runner bootstrap denied: %s\n' \
    "${BOOTSTRAP_MESSAGE}" >&2
  exit 1
fi
[[ "$(as_pq systemctl --user is-active docker.service)" == "active" ]] \
  || fail "rootless Docker user service is not active after setup"
for _attempt in $(seq 1 60); do
  if [[ -S "${DOCKER_SOCKET}" ]] && as_pq docker info >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
[[ -S "${DOCKER_SOCKET}" ]] || fail "rootless Docker socket did not appear"
[[ ! -L "${DOCKER_SOCKET}" ]] || fail "rootless Docker socket is a symlink"
[[ "$(stat -Lc '%u' "${DOCKER_SOCKET}")" == "${PQ_UID}" ]] \
  || fail "rootless Docker socket owner UID mismatch"
socket_gid="$(stat -Lc '%g' "${DOCKER_SOCKET}")"
if [[ "${socket_gid}" != "${PQ_GID}" ]]; then
  awk -F: -v user="${PQ_USER}" -v socket_gid="${socket_gid}" '
    $1 == user && socket_gid >= $2 && socket_gid < ($2 + $3) { found=1 }
    END { exit found ? 0 : 1 }
  ' /etc/subgid || fail "rootless Docker socket group is outside the security user mapping"
fi
case "$(stat -Lc '%a' "${DOCKER_SOCKET}")" in
  600|660|1600|1660) ;;
  *) fail "rootless Docker socket mode mismatch" ;;
esac
if runuser -u runner -- env -i HOME=/home/runner PATH=/usr/bin:/bin \
  DOCKER_HOST="${DOCKER_HOST_VALUE}" docker info >/dev/null 2>&1; then
  fail "outer runner user can reach the isolated rootless Docker socket"
fi

docker_info="$(as_pq docker info --format '{{json .}}')"
jq -e --arg home "${PQ_HOME}" '
  .ServerVersion == "28.0.4" and
  .Driver == "overlay2" and
  .CgroupDriver == "systemd" and
  (.SecurityOptions | any(contains("name=rootless"))) and
  .DockerRootDir == ($home + "/.local/share/docker")
' <<<"${docker_info}" >/dev/null || fail "rootless Docker posture is not reviewed"
[[ -z "$(as_pq docker ps --all --quiet)" ]] || fail "rootless daemon is not empty"
ss -H -lnt | awk '$4 ~ /:2375$/ || $4 ~ /:2376$/ { bad=1 } END { exit bad ? 1 : 0 }' \
  || fail "unexpected Docker TCP listener exists"
pgrep -u "${PQ_USER}" -x dockerd >/dev/null || fail "rootless dockerd is not owned by pqsecurity"

download_verified "${RUNNER_URL}" "${RUNNER_ARCHIVE_SHA256}" "${DOWNLOAD_ROOT}/actions-runner.tar.gz"
download_verified "${SYFT_URL}" "${SYFT_ARCHIVE_SHA256}" "${DOWNLOAD_ROOT}/syft.tar.gz"
download_verified "${TRIVY_URL}" "${TRIVY_ARCHIVE_SHA256}" "${DOWNLOAD_ROOT}/trivy.tar.gz"
install -d -o "${PQ_USER}" -g "${PQ_USER}" -m 0755 "${RUNNER_ROOT}"
tar --extract --gzip --file "${DOWNLOAD_ROOT}/actions-runner.tar.gz" \
  --directory "${RUNNER_ROOT}" --no-same-owner --no-same-permissions
[[ "$(runuser -u "${PQ_USER}" -- "${RUNNER_ROOT}/bin/Runner.Listener" --version)" == "2.335.1" ]] \
  || fail "Actions runner version mismatch"

tar --extract --gzip --file "${DOWNLOAD_ROOT}/syft.tar.gz" \
  --directory "${SCANNER_ROOT}" --no-same-owner --no-same-permissions syft
tar --extract --gzip --file "${DOWNLOAD_ROOT}/trivy.tar.gz" \
  --directory "${SCANNER_ROOT}" --no-same-owner --no-same-permissions trivy
[[ "$(sha256_file "${SCANNER_ROOT}/syft")" == "${SYFT_BINARY_SHA256}" ]] || fail "Syft binary hash mismatch"
[[ "$(sha256_file "${SCANNER_ROOT}/trivy")" == "${TRIVY_BINARY_SHA256}" ]] || fail "Trivy binary hash mismatch"
[[ "$("${SCANNER_ROOT}/syft" version | awk -F': *' '$1 == "Version" {print $2}')" == "1.48.0" ]] \
  || fail "Syft version mismatch"
[[ "$("${SCANNER_ROOT}/trivy" --version | awk -F': *' '$1 == "Version" {print $2; exit}')" == "0.72.0" ]] \
  || fail "Trivy version mismatch"
chown root:root "${SCANNER_ROOT}/syft" "${SCANNER_ROOT}/trivy"
chmod 555 "${SCANNER_ROOT}/syft" "${SCANNER_ROOT}/trivy"
chmod 555 "${SCANNER_ROOT}"

install -m 0444 "${PQ_LOCK_SOURCE}" "${GOVERNANCE_ROOT}/pip-audit.lock"
"${PQ_PYTHON_BIN}" -m venv --copies "${INSTALL_ROOT}/pip-audit"
"${INSTALL_ROOT}/pip-audit/bin/python" -m pip install \
  --require-hashes --only-binary=:all: --requirement "${GOVERNANCE_ROOT}/pip-audit.lock"
[[ "$("${INSTALL_ROOT}/pip-audit/bin/python" -c 'import importlib.metadata, platform; print(platform.python_version() + "|" + importlib.metadata.version("pip-audit"))')" == "3.12.13|2.10.1" ]] \
  || fail "hash-locked pip-audit environment mismatch"
chown -R root:root "${INSTALL_ROOT}/pip-audit"
chmod -R a+rX,go-w "${INSTALL_ROOT}/pip-audit"
[[ "$(stat -c '%U:%G:%a' "${INSTALL_ROOT}/pip-audit")" == "root:root:755" ]] \
  || fail "pip-audit venv root posture mismatch"
[[ "$(stat -c '%U:%G:%a' "${INSTALL_ROOT}/pip-audit/lib/python3.12/site-packages/pip/_internal/__init__.py")" == "root:root:755" ]] \
  || fail "pip-audit package posture mismatch"
if ! writable_venv_path="$(find "${INSTALL_ROOT}/pip-audit" -xdev \( -type f -o -type d \) -perm /022 -print -quit)"; then
  fail "pip-audit venv writability audit failed"
fi
[[ -z "${writable_venv_path}" ]] \
  || fail "pip-audit venv contains a group- or world-writable path"
as_pq "${INSTALL_ROOT}/pip-audit/bin/pip-audit" --version >/dev/null \
  || fail "security user cannot execute the hash-locked pip-audit environment"

maintenance_cache="${INSTALL_ROOT}/trivy-maintenance"
install -d -m 0700 "${maintenance_cache}"
TRIVY_CACHE_DIR="${maintenance_cache}" TRIVY_SKIP_VERSION_CHECK=true \
  "${SCANNER_ROOT}/trivy" image --download-db-only
TRIVY_CACHE_DIR="${maintenance_cache}" TRIVY_SKIP_VERSION_CHECK=true \
  "${SCANNER_ROOT}/trivy" image --download-java-db-only
verify_downloaded_metadata() {
  local metadata="$1"
  local maximum_update_age="$2"
  local require_unexpired="$3"
  updated_epoch="$(date -u -d "$(jq -er '.UpdatedAt' "${metadata}")" +%s)"
  next_epoch="$(date -u -d "$(jq -er '.NextUpdate' "${metadata}")" +%s)"
  downloaded_epoch="$(date -u -d "$(jq -er '.DownloadedAt' "${metadata}")" +%s)"
  now_epoch="$(date -u +%s)"
  (( updated_epoch <= now_epoch && now_epoch - updated_epoch <= maximum_update_age )) \
    || fail "downloaded Trivy database update is stale or malformed"
  (( downloaded_epoch <= now_epoch && now_epoch - downloaded_epoch <= 3600 )) \
    || fail "Trivy database was not freshly downloaded by this bootstrap"
  if [[ "${require_unexpired}" == "true" ]]; then
    (( next_epoch > now_epoch )) || fail "downloaded Trivy vulnerability database is expired"
  else
    (( next_epoch > updated_epoch )) || fail "downloaded Trivy Java database cadence is malformed"
  fi
}
verify_downloaded_metadata "${maintenance_cache}/db/metadata.json" 172800 true
# Trivy publishes the Java index on a slower cadence than the vulnerability
# database. Require the current provider artifact to have been fetched by this
# bootstrap and to be no more than seven days old; its declared cadence must
# still be internally valid.
verify_downloaded_metadata "${maintenance_cache}/java-db/metadata.json" 604800 false
mv "${maintenance_cache}/db" "${SNAPSHOT_ROOT}/db"
mv "${maintenance_cache}/java-db" "${SNAPSHOT_ROOT}/java-db"
rmdir "${maintenance_cache}"
chown -R root:root "${SNAPSHOT_ROOT}"
chmod 555 "${SNAPSHOT_ROOT}" "${SNAPSHOT_ROOT}/db" "${SNAPSHOT_ROOT}/java-db"
chmod 444 "${SNAPSHOT_ROOT}/db/trivy.db" "${SNAPSHOT_ROOT}/db/metadata.json" \
  "${SNAPSHOT_ROOT}/java-db/trivy-java.db" "${SNAPSHOT_ROOT}/java-db/metadata.json"
operational_cache="${SNAPSHOT_ROOT}"

registry_config="${PQ_HOME}/.config/propertyquarry-ghcr"
install -d -o "${PQ_USER}" -g "${PQ_USER}" -m 0700 "${registry_config}"
printf '%s' "${PQ_GHCR_TOKEN}" | as_pq env DOCKER_CONFIG="${registry_config}" \
  docker login ghcr.io --username "${GITHUB_ACTOR}" --password-stdin
as_pq env DOCKER_CONFIG="${registry_config}" docker pull "${PQ_WEB_IMAGE}"
as_pq env DOCKER_CONFIG="${registry_config}" docker pull "${PQ_RENDER_IMAGE}"
as_pq env DOCKER_CONFIG="${registry_config}" docker logout ghcr.io >/dev/null
if [[ -f "${registry_config}/config.json" ]]; then
  shred --force --remove "${registry_config}/config.json"
fi
rmdir "${registry_config}"
unset PQ_GHCR_TOKEN

verify_image() {
  local image="$1"
  local found=""
  local digest
  while IFS= read -r digest; do
    [[ "${digest}" == "${image}" ]] && found="yes"
  done < <(as_pq docker image inspect "${image}" --format '{{range .RepoDigests}}{{println .}}{{end}}')
  [[ "${found}" == "yes" ]] || fail "pulled image digest identity mismatch"
  [[ "$(as_pq docker image inspect "${image}" --format '{{.Os}}/{{.Architecture}}')" == "linux/amd64" ]] \
    || fail "pulled image platform mismatch"
}
verify_image "${PQ_WEB_IMAGE}"
verify_image "${PQ_RENDER_IMAGE}"
as_pq docker image inspect "${PQ_WEB_IMAGE}" "${PQ_RENDER_IMAGE}" >"${EVIDENCE_ROOT}/images.json"
chmod 600 "${EVIDENCE_ROOT}/images.json"

install -m 0555 "${PQ_PREFLIGHT_SOURCE}" "${GOVERNANCE_ROOT}/preflight.sh"
RUNNER_NAME_VALUE="pq-security-${GITHUB_RUN_ID}-${PQ_SECURITY_RUN_ID}"
runuser -u "${PQ_USER}" -- env -i \
  HOME="${PQ_HOME}" \
  USER="${PQ_USER}" \
  LOGNAME="${PQ_USER}" \
  PATH="/usr/bin:/bin" \
  RUNNER_TOKEN="${PQ_RUNNER_TOKEN}" \
  "${RUNNER_ROOT}/config.sh" \
    --unattended \
    --url "https://github.com/${PQ_REPOSITORY}" \
    --token "${PQ_RUNNER_TOKEN}" \
    --name "${RUNNER_NAME_VALUE}" \
    --labels "propertyquarry-security,${PQ_SECURITY_RUNNER_LABEL}" \
    --work _work \
    --ephemeral \
    --disableupdate
unset PQ_RUNNER_TOKEN

RUNNER_AGENT_ID="$(jq -er '.agentId | tostring' "${RUNNER_ROOT}/.runner")"
[[ "$(jq -er '.agentName' "${RUNNER_ROOT}/.runner")" == "${RUNNER_NAME_VALUE}" ]] \
  || fail "configured runner name mismatch"
install -d -o "${PQ_USER}" -g "${PQ_USER}" -m 0700 \
  "${RUNNER_ROOT}/_work" "${RUNNER_ROOT}/_diag" "${INSTALL_ROOT}/evidence"

printf '%s\n' \
  "DOCKER_HOST=${DOCKER_HOST_VALUE}" \
  "XDG_RUNTIME_DIR=${PQ_RUNTIME}" \
  "PROPERTYQUARRY_WORKFLOW_HEAD_SHA=${PQ_EXPECTED_HEAD_SHA}" \
  "PROPERTYQUARRY_SECURITY_RUNNER_LABEL=${PQ_SECURITY_RUNNER_LABEL}" \
  "PROPERTYQUARRY_WEB_IMAGE=${PQ_WEB_IMAGE}" \
  "PROPERTYQUARRY_RENDER_IMAGE=${PQ_RENDER_IMAGE}" \
  "TRIVY_CACHE_DIR=${operational_cache}" \
  "TRIVY_CACHE_BACKEND=memory" \
  "TRIVY_SKIP_VERSION_CHECK=true" \
  "SYFT_CHECK_FOR_APP_UPDATE=false" \
  "PIP_DISABLE_PIP_VERSION_CHECK=1" \
  "PIP_NO_INDEX=1" \
  "LD_LIBRARY_PATH=/opt/hostedtoolcache/Python/3.12.13/x64/lib" \
  "ACTIONS_RUNNER_HOOK_JOB_STARTED=${GOVERNANCE_ROOT}/preflight.sh" \
  >"${RUNNER_ROOT}/.env"
printf '%s\n' \
  "${INSTALL_ROOT}/pip-audit/bin:${SCANNER_ROOT}:/usr/bin:/bin" \
  >"${RUNNER_ROOT}/.path"
chown root:root "${RUNNER_ROOT}/.env" "${RUNNER_ROOT}/.path"
chmod 444 "${RUNNER_ROOT}/.env" "${RUNNER_ROOT}/.path"

for config_file in .runner .credentials .credentials_rsaparams; do
  [[ -f "${RUNNER_ROOT}/${config_file}" ]] || fail "runner configuration file is missing"
  chown root:"${PQ_USER}" "${RUNNER_ROOT}/${config_file}"
  chmod 440 "${RUNNER_ROOT}/${config_file}"
done
chown -R root:root "${RUNNER_ROOT}/bin" "${RUNNER_ROOT}/externals"
chmod -R a+rX,go-w "${RUNNER_ROOT}/bin" "${RUNNER_ROOT}/externals"
if ! writable_runner_path="$(find "${RUNNER_ROOT}/bin" "${RUNNER_ROOT}/externals" \
  -xdev \( -type f -o -type d \) -perm /022 -print -quit)"; then
  fail "Actions runner runtime writability audit failed"
fi
[[ -z "${writable_runner_path}" ]] \
  || fail "Actions runner runtime contains a group- or world-writable path"
[[ "$(stat -c '%U:%G:%a' "${RUNNER_ROOT}/externals/node24/bin/node")" == "root:root:755" ]] \
  || fail "Actions runner Node 24 runtime posture mismatch"
as_pq "${RUNNER_ROOT}/externals/node24/bin/node" --version >/dev/null \
  || fail "security user cannot execute the Actions runner Node 24 runtime"
chown root:root "${RUNNER_ROOT}" "${RUNNER_ROOT}/run.sh" "${RUNNER_ROOT}/config.sh"
chmod 755 "${RUNNER_ROOT}"
chmod 555 "${RUNNER_ROOT}/run.sh" "${RUNNER_ROOT}/config.sh" "${RUNNER_ROOT}/bin/Runner.Listener"

RUNNER_SETTINGS_SHA256="$(sha256_file "${RUNNER_ROOT}/.runner")"
RUNNER_CREDENTIALS_SHA256="$(sha256_file "${RUNNER_ROOT}/.credentials")"
RUNNER_RSA_SHA256="$(sha256_file "${RUNNER_ROOT}/.credentials_rsaparams")"

expected_file="${GOVERNANCE_ROOT}/expected.env"
printf '%s\n' \
  "EXPECTED_REPOSITORY=${PQ_REPOSITORY}" \
  "EXPECTED_WORKFLOW_REF=${PQ_EXPECTED_WORKFLOW_REF}" \
  "EXPECTED_HEAD_SHA=${PQ_EXPECTED_HEAD_SHA}" \
  "EXPECTED_RUN_ID=${PQ_SECURITY_RUN_ID}" \
  "EXPECTED_RUN_ATTEMPT=${PQ_SECURITY_RUN_ATTEMPT}" \
  "EXPECTED_JOB_ID=${PQ_SECURITY_JOB_ID}" \
  "EXPECTED_RUNNER_NAME=${RUNNER_NAME_VALUE}" \
  "EXPECTED_RUNNER_LABEL=${PQ_SECURITY_RUNNER_LABEL}" \
  "EXPECTED_WEB_IMAGE=${PQ_WEB_IMAGE}" \
  "EXPECTED_RENDER_IMAGE=${PQ_RENDER_IMAGE}" \
  "EXPECTED_DOCKER_HOST=${DOCKER_HOST_VALUE}" \
  "EXPECTED_DOCKER_SOCKET=${DOCKER_SOCKET}" \
  "EXPECTED_DOCKER_ROOT=${PQ_HOME}/.local/share/docker" \
  "EXPECTED_TRIVY_CACHE_DIR=${operational_cache}" \
  "EXPECTED_PREFLIGHT_SHA256=${PQ_PREFLIGHT_SHA256}" \
  "EXPECTED_PIP_AUDIT_BIN=${INSTALL_ROOT}/pip-audit/bin/pip-audit" \
  "EXPECTED_PIP_AUDIT_SHA256=$(sha256_file "${INSTALL_ROOT}/pip-audit/bin/pip-audit")" \
  "EXPECTED_PYTHON_BIN=${INSTALL_ROOT}/pip-audit/bin/python" \
  "EXPECTED_PYTHON_SHA256=$(sha256_file "${INSTALL_ROOT}/pip-audit/bin/python")" \
  "EXPECTED_SYFT_BIN=${SCANNER_ROOT}/syft" \
  "EXPECTED_SYFT_SHA256=${SYFT_BINARY_SHA256}" \
  "EXPECTED_TRIVY_BIN=${SCANNER_ROOT}/trivy" \
  "EXPECTED_TRIVY_SHA256=${TRIVY_BINARY_SHA256}" \
  "EXPECTED_DOCKER_BIN=/usr/bin/docker" \
  "EXPECTED_DOCKER_SHA256=$(sha256_file /usr/bin/docker)" \
  "EXPECTED_DOCKERD_BIN=/usr/bin/dockerd" \
  "EXPECTED_DOCKERD_SHA256=$(sha256_file /usr/bin/dockerd)" \
  "EXPECTED_CONTAINERD_BIN=/usr/bin/containerd" \
  "EXPECTED_CONTAINERD_SHA256=$(sha256_file /usr/bin/containerd)" \
  "EXPECTED_RUNC_BIN=/usr/bin/runc" \
  "EXPECTED_RUNC_SHA256=$(sha256_file /usr/bin/runc)" \
  "EXPECTED_ROOTLESSKIT_BIN=/usr/bin/rootlesskit" \
  "EXPECTED_ROOTLESSKIT_SHA256=$(sha256_file /usr/bin/rootlesskit)" \
  "EXPECTED_SLIRP_BIN=/usr/bin/slirp4netns" \
  "EXPECTED_SLIRP_SHA256=$(sha256_file /usr/bin/slirp4netns)" \
  "EXPECTED_NEWUIDMAP_BIN=/usr/bin/newuidmap" \
  "EXPECTED_NEWUIDMAP_SHA256=$(sha256_file /usr/bin/newuidmap)" \
  "EXPECTED_NEWGIDMAP_BIN=/usr/bin/newgidmap" \
  "EXPECTED_NEWGIDMAP_SHA256=$(sha256_file /usr/bin/newgidmap)" \
  "EXPECTED_SYSCTL_BIN=/usr/sbin/sysctl" \
  "EXPECTED_SYSCTL_SHA256=$(sha256_file /usr/sbin/sysctl)" \
  "EXPECTED_APPARMOR_PROFILE=/etc/apparmor.d/rootlesskit" \
  "EXPECTED_APPARMOR_PROFILE_SHA256=a6a1a760d88312275d64f195e6b2f51627e8cabfdf4e355262c86f0578c66d80" \
  "EXPECTED_RUNNER_LISTENER=${RUNNER_ROOT}/bin/Runner.Listener" \
  "EXPECTED_RUNNER_LISTENER_SHA256=$(sha256_file "${RUNNER_ROOT}/bin/Runner.Listener")" \
  "EXPECTED_RUNNER_ENV=${RUNNER_ROOT}/.env" \
  "EXPECTED_RUNNER_ENV_SHA256=$(sha256_file "${RUNNER_ROOT}/.env")" \
  "EXPECTED_RUNNER_PATH=${RUNNER_ROOT}/.path" \
  "EXPECTED_RUNNER_PATH_SHA256=$(sha256_file "${RUNNER_ROOT}/.path")" \
  "EXPECTED_RUNNER_LOCK=${GOVERNANCE_ROOT}/pip-audit.lock" \
  "EXPECTED_RUNNER_LOCK_SHA256=${PQ_LOCK_SHA256}" \
  "EXPECTED_TRIVY_DB_SHA256=$(sha256_file "${operational_cache}/db/trivy.db")" \
  "EXPECTED_TRIVY_DB_METADATA_SHA256=$(sha256_file "${operational_cache}/db/metadata.json")" \
  "EXPECTED_TRIVY_JAVA_DB_SHA256=$(sha256_file "${operational_cache}/java-db/trivy-java.db")" \
  "EXPECTED_TRIVY_JAVA_METADATA_SHA256=$(sha256_file "${operational_cache}/java-db/metadata.json")" \
  >"${expected_file}"
chown root:root "${expected_file}"
chmod 444 "${expected_file}"
chmod 555 "${GOVERNANCE_ROOT}"

cp "${expected_file}" "${EVIDENCE_ROOT}/expected.env"
cp "${operational_cache}/db/metadata.json" "${EVIDENCE_ROOT}/trivy-db-metadata.json"
cp "${operational_cache}/java-db/metadata.json" "${EVIDENCE_ROOT}/trivy-java-db-metadata.json"
printf '%s\n' \
  "docker-ce-rootless-extras=${ROOTLESS_EXTRAS_VERSION}|${ROOTLESS_EXTRAS_SHA256}" \
  "apparmor=${APPARMOR_VERSION}|${APPARMOR_SHA256}" \
  "dbus-user-session=${DBUS_USER_VERSION}|${DBUS_USER_SHA256}" \
  "uidmap=${UIDMAP_VERSION}|${UIDMAP_SHA256}" \
  "slirp4netns=${SLIRP_VERSION}|${SLIRP_SHA256}" \
  >"${EVIDENCE_ROOT}/package-manifest.txt"
chmod 600 "${EVIDENCE_ROOT}/expected.env" "${EVIDENCE_ROOT}/trivy-db-metadata.json" \
  "${EVIDENCE_ROOT}/trivy-java-db-metadata.json" "${EVIDENCE_ROOT}/package-manifest.txt"

as_pq env \
  PATH="${INSTALL_ROOT}/pip-audit/bin:${SCANNER_ROOT}:/usr/bin:/bin" \
  TRIVY_CACHE_DIR="${operational_cache}" \
  "${INSTALL_ROOT}/pip-audit/bin/pip-audit" --version >/dev/null
as_pq docker info >/dev/null

BOOTSTRAP_STATUS="listener_running"
BOOTSTRAP_MESSAGE="exact ephemeral runner listener started"
write_receipt
LISTENER_EXIT_CODE="0"
# Runner.Listener loads .env but does not consume the .path snapshot when it
# is invoked directly, so bind the listener to that root-owned exact value.
runuser -u "${PQ_USER}" -- env -i \
  HOME="${PQ_HOME}" \
  USER="${PQ_USER}" \
  LOGNAME="${PQ_USER}" \
  SHELL="/bin/bash" \
  PATH="$(<"${RUNNER_ROOT}/.path")" \
  /bin/bash -c 'cd /opt/propertyquarry-security/runner && exec ./bin/Runner.Listener run' \
  || LISTENER_EXIT_CODE="$?"

# The pinned ephemeral runner removes its local configuration only after the
# exact one-time job has completed. Those files and their parent deliberately
# remain root-owned and non-writable during the untrusted scan, so that local
# cleanup is denied with the pinned terminated-error code. The outer workflow
# remains the sole authority for proving that the exact remote job succeeded.
[[ "${LISTENER_EXIT_CODE}" == "2" ]] \
  || fail "pinned runner exited outside the immutable local-config cleanup boundary"

# Export only the named preflight receipt from the untrusted runner-owned
# evidence directory. The outer bootstrap validates every binding before
# copying it into the root-governed artifact bundle; no directory copy or
# wildcard is allowed across this boundary.
preflight_receipt_name="preflight-${PQ_SECURITY_RUN_ID}-${PQ_SECURITY_RUN_ATTEMPT}.json"
preflight_receipt="${INSTALL_ROOT}/evidence/${preflight_receipt_name}"
[[ -f "${preflight_receipt}" && ! -L "${preflight_receipt}" ]] \
  || fail "exact preflight receipt is missing or unsafe"
[[ "$(stat -c '%U:%G:%a' "${preflight_receipt}")" == "${PQ_USER}:${PQ_USER}:600" ]] \
  || fail "preflight receipt ownership or mode changed"
jq -e \
  --arg repository "${PQ_REPOSITORY}" \
  --arg run_id "${PQ_SECURITY_RUN_ID}" \
  --arg run_attempt "${PQ_SECURITY_RUN_ATTEMPT}" \
  --arg head_sha "${PQ_EXPECTED_HEAD_SHA}" \
  --arg runner_name "${RUNNER_NAME_VALUE}" '
    .schema == "propertyquarry.security_runner_preflight.v1" and
    .status == "pass" and
    .repository == $repository and
    .run_id == $run_id and
    .run_attempt == $run_attempt and
    .head_sha == $head_sha and
    .runner_name == $runner_name
  ' "${preflight_receipt}" >/dev/null \
  || fail "preflight receipt binding changed"
install -m 0600 "${preflight_receipt}" "${EVIDENCE_ROOT}/${preflight_receipt_name}"

verify_post_job_file() {
  local path="$1"
  local expected_sha="$2"
  local expected_mode="$3"
  [[ -f "${path}" ]] || fail "post-job governed file is missing"
  [[ "$(sha256_file "${path}")" == "${expected_sha}" ]] \
    || fail "post-job governed file identity changed"
  [[ "$(stat -c '%U:%G:%a' "${path}")" == "${expected_mode}" ]] \
    || fail "post-job governed file ownership or mode changed"
}

verify_post_job_file "${RUNNER_ROOT}/.runner" \
  "${RUNNER_SETTINGS_SHA256}" "root:${PQ_USER}:440"
verify_post_job_file "${RUNNER_ROOT}/.credentials" \
  "${RUNNER_CREDENTIALS_SHA256}" "root:${PQ_USER}:440"
verify_post_job_file "${RUNNER_ROOT}/.credentials_rsaparams" \
  "${RUNNER_RSA_SHA256}" "root:${PQ_USER}:440"
[[ "$(stat -c '%U:%G:%a' "${RUNNER_ROOT}")" == "root:root:755" ]] \
  || fail "post-job runner root posture changed"

# Re-check mutable-time boundaries after the untrusted job has ended. The
# separate outer workflow step is still the only authority that can declare
# that the exact queued job succeeded and consumed this runner.
verify_post_job_file "${operational_cache}/db/trivy.db" \
  "$(sed -n 's/^EXPECTED_TRIVY_DB_SHA256=//p' "${expected_file}")" "root:root:444"
verify_post_job_file "${operational_cache}/db/metadata.json" \
  "$(sed -n 's/^EXPECTED_TRIVY_DB_METADATA_SHA256=//p' "${expected_file}")" "root:root:444"
verify_post_job_file "${operational_cache}/java-db/trivy-java.db" \
  "$(sed -n 's/^EXPECTED_TRIVY_JAVA_DB_SHA256=//p' "${expected_file}")" "root:root:444"
verify_post_job_file "${operational_cache}/java-db/metadata.json" \
  "$(sed -n 's/^EXPECTED_TRIVY_JAVA_METADATA_SHA256=//p' "${expected_file}")" "root:root:444"
[[ "$(stat -c '%U:%G:%a' "${operational_cache}")" == "root:root:555" ]] \
  || fail "post-job Trivy snapshot root changed"
verify_image "${PQ_WEB_IMAGE}"
verify_image "${PQ_RENDER_IMAGE}"

jq -n \
  --arg schema "propertyquarry.security_runner_post_job_integrity.v1" \
  --arg status "pass" \
  --arg run_id "${PQ_SECURITY_RUN_ID}" \
  --arg run_attempt "${PQ_SECURITY_RUN_ATTEMPT}" \
  --arg job_id "${PQ_SECURITY_JOB_ID}" \
  --arg runner_name "${RUNNER_NAME_VALUE}" \
  --arg runner_label "${PQ_SECURITY_RUNNER_LABEL}" \
  --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{schema:$schema,status:$status,run_id:$run_id,run_attempt:$run_attempt,job_id:$job_id,runner_name:$runner_name,runner_label:$runner_label,checked_at:$checked_at}' \
  >"${EVIDENCE_ROOT}/post-job-integrity.json"
chmod 600 "${EVIDENCE_ROOT}/post-job-integrity.json"

BOOTSTRAP_STATUS="listener_exited"
BOOTSTRAP_MESSAGE="ephemeral listener reached immutable cleanup boundary; awaiting exact remote job verification"
write_receipt
