#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH \
  DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY \
  DOCKER_API_VERSION BUILDKIT_HOST BUILDX_HOST PYTHONOPTIMIZE PYTHONPATH PYTHONHOME \
  GH_TOKEN GITHUB_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_RUNNER_INPUT_TOKEN \
  PROPERTYQUARRY_RUNNER_ADMIN_TOKEN PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD
export DOCKER_HOST=unix:///var/run/docker.sock
umask 077
ulimit -c 0

fail() {
  printf '%s\n' 'propertyquarry-docker-ephemeral-runner-rejected' >&2
  exit 50
}

controller=/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2
launcher_directory=/usr/libexec/propertyquarry-release-control
launcher="${launcher_directory}/run-propertyquarry-ephemeral-runner-v2"
archive_directory=/usr/lib/propertyquarry-release-runner-v2
archive="${archive_directory}/actions-runner-linux-x64-2.335.1.tar.gz"
session_root=/var/lib/propertyquarry-release-runner-v2/sessions
authority_config=/etc/propertyquarry-release-single-host-v2
authority_runtime=/run/propertyquarry-release-single-host-v2

# This function must run before the first external command. The coprocess is
# the only pre-supervisor process that receives FD 8. It waits on a non-secret
# gate without reading the token. The orchestrator closes FD 8 immediately, so
# every admission, filesystem, Docker, controller, and Python child launched
# before the gate is descriptor- and marker-free.
start_runner_token_broker() {
  [[ -p /proc/self/fd/8 ]] || fail
  [[ -z "${PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD:-}" ]] || fail
  coproc RUNNER_TOKEN_BROKER {
    broker_gate=""
    IFS= read -r broker_gate || exit 50
    [[ "$broker_gate" == "release-supervisor" ]] || exit 50
    unset HOME DOCKER_HOST
    export PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8
    exec "$controller" runner-supervise 8<&8
  } 8<&8
  supervisor_pid="$RUNNER_TOKEN_BROKER_PID"
  supervisor_output_fd="${RUNNER_TOKEN_BROKER[0]}"
  supervisor_gate_fd="${RUNNER_TOKEN_BROKER[1]}"
  exec 8<&-
  [[ ! -e /proc/self/fd/8 ]] || fail
  [[ -z "${PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD:-}" ]] || fail
}
# End fixed runner token broker.

verify_local_docker() {
  [[ "$(command -v docker 2>/dev/null)" == "/usr/bin/docker" ]] || fail
  [[ -f /usr/bin/docker && ! -L /usr/bin/docker ]] || fail
  [[ "$(stat -Lc '%F:%a:%u:%g:%h' -- /usr/bin/docker 2>/dev/null)" == \
    "regular file:755:0:0:1" ]] || fail
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

[[ "$#" -eq 0 ]] || fail
[[ "$EUID" == "0" && "${GROUPS[0]}" == "0" ]] || fail
start_runner_token_broker
[[ -f "$controller" && ! -L "$controller" && -x "$controller" ]] || fail
[[ "$(stat -Lc '%a:%u:%g:%h' -- "$controller")" == "755:0:0:1" ]] || fail
[[ -f "$launcher" && ! -L "$launcher" && -x "$launcher" ]] || fail
[[ "$(stat -Lc '%a:%u:%g:%h' -- "$launcher")" == "555:0:0:1" ]] || fail
[[ -f "$archive" && ! -L "$archive" && "$(stat -Lc '%a:%u:%g:%h:%s' -- "$archive")" == \
  "444:0:0:1:225628509" ]] || fail
[[ "$(sha256sum -- "$archive" | cut -d' ' -f1)" == \
  "4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf" ]] || fail
[[ -d "$session_root" && ! -L "$session_root" && "$(stat -Lc '%a:%u:%g' -- "$session_root")" == \
  "700:1999:1999" ]] || fail
[[ -d "$authority_config" && ! -L "$authority_config" ]] || fail
[[ -d "$authority_runtime" && ! -L "$authority_runtime" ]] || fail

exec 7>/run/lock/propertyquarry-release-runner-lifecycle-v2.lock || fail
flock -n 7 || fail
[[ "$(stat -Lc '%a:%u:%g:%h' -- /run/lock/propertyquarry-release-runner-lifecycle-v2.lock)" == \
  "600:0:0:1" ]] || fail
verify_local_docker

workspace="$(mktemp -d /run/propertyquarry-release-runner-v2.XXXXXX)"
chmod 700 "$workspace"
admission="${workspace}/admission.json"
start_proof="${workspace}/start-proof.json"
configure_fifo="${workspace}/configure-token.fifo"
configure_cidfile="${workspace}/configure-container.id"
listener_cidfile="${workspace}/listener-container.id"
inspect_file="${workspace}/container-inspect.json"
configure_container=""
listener_container=""
configure_pid=""
listener_pid=""
token_writer_pid=""
session=""
terminal_status=50

cleanup_session() {
  [[ -n "${session:-}" && "$session" =~ ^/var/lib/propertyquarry-release-runner-v2/sessions/session-[0-9a-f]{32}\.[A-Za-z0-9]+$ ]] || return 0
  [[ -d "$session" && ! -L "$session" ]] || return 0
  chmod -R u+rwX,go-rwx -- "$session" 2>/dev/null || true
  find "$session" -xdev -depth -mindepth 1 -delete >/dev/null 2>&1 || true
  rmdir -- "$session" 2>/dev/null || true
}

cleanup() {
  local status="${1:-50}"
  trap - EXIT INT TERM HUP
  for name in "${configure_container:-}" "${listener_container:-}"; do
    [[ -n "$name" ]] || continue
    timeout --signal=TERM --kill-after=5s 20s docker stop --time 10 "$name" >/dev/null 2>&1 || true
    timeout --signal=KILL 10s docker rm -f "$name" >/dev/null 2>&1 || true
  done
  for pid in "${configure_pid:-}" "${listener_pid:-}" "${token_writer_pid:-}" "${supervisor_pid:-}"; do
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    kill -TERM "$pid" >/dev/null 2>&1 || true
    timeout --signal=KILL 55s tail --pid="$pid" -f /dev/null >/dev/null 2>&1 || true
    kill -KILL "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  done
  cleanup_session
  if [[ "$workspace" =~ ^/run/propertyquarry-release-runner-v2\.[A-Za-z0-9]+$ && -d "$workspace" && ! -L "$workspace" ]]; then
    find "$workspace" -xdev -depth -mindepth 1 -delete >/dev/null 2>&1 || true
    rmdir -- "$workspace" 2>/dev/null || true
  fi
  exit "$status"
}
trap 'cleanup $?' EXIT
trap 'cleanup 130' INT
trap 'cleanup 143' TERM HUP

wait_for_cid() {
  local cidfile="$1"
  local process="$2"
  for _attempt in $(seq 1 100); do
    [[ -s "$cidfile" ]] && break
    kill -0 "$process" >/dev/null 2>&1 || break
    sleep 0.1
  done
  [[ -s "$cidfile" ]] || fail
  local value
  value="$(<"$cidfile")"
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || fail
  printf '%s\n' "$value"
}

inspect_runner_phase() {
  local container_id="$1"
  local phase="$2"
  local container_name="$3"
  docker inspect "$container_id" >"$inspect_file" || fail
  /usr/bin/python3 - "$inspect_file" "$phase" "$container_name" "$launcher" "$runner_label" \
    "$launch_ticket_digest" "$runner_image" "$launcher_directory" "$archive_directory" \
    "$session_root" "$authority_config" "$authority_runtime" "${session:-}" "${authorized_runner_id:-}" \
    "${session_device:-}" "${session_inode:-}" "${session_tree_digest:-}" <<'PY' || fail
import json, sys
(
    path, phase, name, launcher, label, ticket, image, launcher_dir,
    archive_dir, sessions, authority_config, authority_runtime, session,
    runner_id, session_device, session_inode, session_tree,
) = sys.argv[1:]
items=json.load(open(path, encoding='utf-8'))
assert isinstance(items, list) and len(items)==1
item=items[0]
assert item['Name']=='/'+name and item['Path']==launcher
if phase == 'configure':
    assert item['Args']==['configure',label,ticket]
    expected={launcher_dir:False,archive_dir:False,sessions:True}
    assert item['HostConfig'].get('GroupAdd') in (None, [])
else:
    assert item['Args']==['run',label,ticket,session,runner_id,session_device,session_inode,session_tree]
    expected={
        launcher_dir:False,sessions:True,authority_config:False,
        authority_runtime:False,'/var/run/docker.sock':True,
    }
    assert item['HostConfig']['GroupAdd']==['112']
mounts={entry['Destination']: bool(entry['RW']) for entry in item['Mounts']}
assert mounts==expected
assert item['Config']['Image']==image and item['Config']['User']=='1999:1999'
assert item['HostConfig']['ReadonlyRootfs'] is True
assert item['HostConfig']['CapDrop']==['ALL'] and item['HostConfig']['CapAdd'] is None
assert 'no-new-privileges' in item['HostConfig']['SecurityOpt']
serialized=json.dumps({'env':item['Config'].get('Env'),'path':item['Path'],'args':item['Args'],'mounts':mounts})
for forbidden in ('ACTIONS_RUNNER_INPUT_TOKEN','PROPERTYQUARRY_RUNNER_ADMIN_TOKEN','GITHUB_TOKEN','GH_TOKEN','github_pat_','ghp_'):
    assert forbidden not in serialized
if phase == 'configure':
    assert '/var/run/docker.sock' not in mounts
    assert authority_runtime not in mounts and authority_config not in mounts
PY
}

env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC \
  "$controller" runner-ticket-admit >"$admission" 2>/dev/null || fail
chmod 600 "$admission"
mapfile -t admission_fields < <(/usr/bin/python3 - "$admission" <<'PY'
import json, re, stat, sys
path=sys.argv[1]
info=__import__('os').stat(path, follow_symlinks=False)
assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o600 and info.st_uid==0 and info.st_gid==0 and info.st_nlink==1
raw=open(path, 'rb').read()
assert len(raw) <= 65536 and raw.endswith(b'\n') and b'\n' not in raw[:-1]
value=json.loads(raw[:-1])
assert raw == json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()+b'\n'
assert set(value)=={'dispatch_ticket_sha256','disposition','execution_expires_at_epoch','job_id','launch_ticket_sha256','run_attempt','run_id','runner_image','runner_label','schema','ticket_expires_at_epoch','version'}
assert value['disposition'] in {'admitted','already-admitted'}
assert re.fullmatch(r'pqrelease-[0-9a-f]{32}', value['runner_label'])
assert re.fullmatch(r'sha256:[0-9a-f]{64}', value['launch_ticket_sha256'])
assert re.fullmatch(r'ghcr\.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:[0-9a-f]{64}', value['runner_image'])
print(value['runner_label'])
print(value['launch_ticket_sha256'])
print(value['runner_image'])
PY
) || fail
[[ "${#admission_fields[@]}" -eq 3 ]] || fail
runner_label="${admission_fields[0]}"
launch_ticket_digest="${admission_fields[1]}"
runner_image="${admission_fields[2]}"
runner_nonce="${runner_label#pqrelease-}"
configure_container="propertyquarry-release-runner-configure-${runner_nonce}"
listener_container="propertyquarry-release-runner-listen-${runner_nonce}"
[[ -z "$(find "$session_root" -mindepth 1 -maxdepth 1 -type d -name "session-${runner_nonce}.*" -print -quit)" ]] || fail
for name in "$configure_container" "$listener_container"; do
  [[ -z "$(docker ps -aq --filter "name=^/${name}$")" ]] || fail
done

docker pull --quiet "$runner_image" >/dev/null || fail
verify_local_docker
image_id="$(docker image inspect --format '{{.Id}}' "$runner_image" 2>/dev/null)"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
docker image inspect "$runner_image" >"${workspace}/image-inspect.json" || fail
/usr/bin/python3 - "${workspace}/image-inspect.json" "$runner_image" "$image_id" <<'PY' || fail
import json, sys
items=json.load(open(sys.argv[1], encoding='utf-8'))
assert len(items)==1
item=items[0]
assert item['Id']==sys.argv[3] and item['Os']=='linux' and item['Architecture']=='amd64'
assert sys.argv[2] in item.get('RepoDigests', [])
PY

kill -0 "$supervisor_pid" >/dev/null 2>&1 || fail
printf '%s\n' 'release-supervisor' >&"$supervisor_gate_fd" || fail
exec {supervisor_gate_fd}>&-

IFS= read -r -t 300 registration_token <&"$supervisor_output_fd" || fail
[[ "$registration_token" =~ ^[A-Za-z0-9._-]{20,2048}$ ]] || fail
mkfifo -m 600 "$configure_fifo"
docker run --rm --pull never -i \
  --cidfile "$configure_cidfile" --name "$configure_container" \
  --read-only --user 1999:1999 \
  --entrypoint "$launcher" \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 256 --memory 1g --memory-swap 1g --cpus 1 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777,size=128m \
  --mount "type=bind,src=${launcher_directory},dst=${launcher_directory},readonly" \
  --mount "type=bind,src=${archive_directory},dst=${archive_directory},readonly" \
  --mount "type=bind,src=${session_root},dst=${session_root},readonly=false" \
  "$runner_image" configure "$runner_label" "$launch_ticket_digest" \
  <"$configure_fifo" &
configure_pid="$!"
( printf '%s\n' "$registration_token" >"$configure_fifo" ) &
token_writer_pid="$!"
unset registration_token
configure_id="$(wait_for_cid "$configure_cidfile" "$configure_pid")"
inspect_runner_phase "$configure_id" configure "$configure_container"
configure_status=0
wait "$configure_pid" || configure_status="$?"
configure_pid=""
wait "$token_writer_pid" || fail
token_writer_pid=""
rm -- "$configure_fifo"
[[ "$configure_status" == "0" ]] || fail

mapfile -t sessions < <(find "$session_root" -mindepth 1 -maxdepth 1 -type d -name "session-${runner_nonce}.*" -print)
[[ "${#sessions[@]}" -eq 1 ]] || fail
session="${sessions[0]}"
[[ ! -L "$session" && "$(stat -Lc '%a:%u:%g' -- "$session")" == "700:1999:1999" ]] || fail
for evidence in .registration-token.sha256 .session-content.sha256 .configuration-complete configure-exit-status; do
  [[ -f "$session/$evidence" && ! -L "$session/$evidence" && "$(stat -Lc '%a:%u:%g:%h' -- "$session/$evidence")" == \
    "600:1999:1999:1" ]] || fail
done
[[ "$(<"$session/.configuration-complete")" == "configured-without-host-authority" ]] || fail
[[ "$(<"$session/configure-exit-status")" == "0" ]] || fail

IFS=' ' read -r -t 180 authorization_marker authorized_runner_id authorized_ticket authorization_extra <&"$supervisor_output_fd" || fail
[[ "$authorization_marker" == "START" && "$authorized_runner_id" =~ ^[1-9][0-9]*$ && \
  "$authorized_ticket" == "$launch_ticket_digest" && -z "${authorization_extra:-}" ]] || fail
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC \
  "$controller" runner-start-verify "$runner_label" "$launch_ticket_digest" "$authorized_runner_id" \
  >"$start_proof" 2>/dev/null || fail
chmod 600 "$start_proof"
mapfile -t session_proof_fields < <(/usr/bin/python3 - "$start_proof" "$runner_label" "$launch_ticket_digest" "$authorized_runner_id" <<'PY'
import json, re, sys
raw=open(sys.argv[1], 'rb').read()
value=json.loads(raw)
assert raw==json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()+b'\n'
assert set(value)=={'execution_expires_at_epoch','launch_ticket_sha256','runner_id','runner_label','schema','session_device','session_inode','session_tree_sha256','version'}
assert value['schema']=='propertyquarry.release-control.single-host-runner-start-result.v2' and value['version']==2
assert value['runner_label']==sys.argv[2] and value['launch_ticket_sha256']==sys.argv[3] and value['runner_id']==sys.argv[4]
assert isinstance(value['execution_expires_at_epoch'],int) and value['execution_expires_at_epoch']>0
assert isinstance(value['session_device'],int) and value['session_device']>0
assert isinstance(value['session_inode'],int) and value['session_inode']>0
assert re.fullmatch(r'sha256:[0-9a-f]{64}', value['session_tree_sha256'])
print(value['session_device'])
print(value['session_inode'])
print(value['session_tree_sha256'])
PY
 ) || fail
[[ "${#session_proof_fields[@]}" -eq 3 ]] || fail
session_device="${session_proof_fields[0]}"
session_inode="${session_proof_fields[1]}"
session_tree_digest="${session_proof_fields[2]}"
unset session_proof_fields

docker run --rm --pull never \
  --cidfile "$listener_cidfile" --name "$listener_container" \
  --read-only --user 1999:1999 --group-add 112 \
  --entrypoint "$launcher" \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 512 --memory 2g --memory-swap 2g --cpus 2 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777,size=256m \
  --mount "type=bind,src=${launcher_directory},dst=${launcher_directory},readonly" \
  --mount "type=bind,src=${session_root},dst=${session_root},readonly=false" \
  --mount "type=bind,src=${authority_config},dst=${authority_config},readonly" \
  --mount "type=bind,src=${authority_runtime},dst=${authority_runtime},readonly" \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly=false \
  "$runner_image" run "$runner_label" "$launch_ticket_digest" "$session" "$authorized_runner_id" \
  "$session_device" "$session_inode" "$session_tree_digest" \
  </dev/null &
listener_pid="$!"
listener_id="$(wait_for_cid "$listener_cidfile" "$listener_pid")"
inspect_runner_phase "$listener_id" run "$listener_container"
listener_container_status=0
wait "$listener_pid" || listener_container_status="$?"
listener_pid=""

IFS=' ' read -r -t 300 cleanup_marker cleanup_runner_id cleanup_ticket cleanup_extra <&"$supervisor_output_fd" || fail
[[ "$cleanup_marker" == "CLEAN" && "$cleanup_runner_id" == "$authorized_runner_id" && \
  "$cleanup_ticket" == "$launch_ticket_digest" && -z "${cleanup_extra:-}" ]] || fail
supervisor_status=0
wait "$supervisor_pid" || supervisor_status="$?"
supervisor_pid=""
printf '%s\n' 'remote-cleanup-authorized' >"$session/remote-cleanup-status"
chown 1999:1999 "$session/remote-cleanup-status"
chmod 600 "$session/remote-cleanup-status"

for evidence in .registration-token.sha256 .session-content.sha256 .configuration-complete configure-exit-status listener-exit-status launcher-exit-status remote-cleanup-status; do
  [[ -f "$session/$evidence" && ! -L "$session/$evidence" && "$(stat -Lc '%a:%u:%g:%h' -- "$session/$evidence")" == \
    "600:1999:1999:1" ]] || fail
done
[[ "$(<"$session/listener-exit-status")" == "0" || "$(<"$session/listener-exit-status")" == "2" ]] || fail
[[ "$(<"$session/launcher-exit-status")" == "0" ]] || fail
[[ "$(<"$session/configure-exit-status")" == "0" ]] || fail
[[ "$(<"$session/remote-cleanup-status")" == "remote-cleanup-authorized" ]] || fail
[[ "$(<"$session/.registration-token.sha256")" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
if grep -R -a -E -q 'ACTIONS_RUNNER_INPUT_TOKEN|PROPERTYQUARRY_RUNNER_ADMIN_TOKEN|github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+' -- "$session"; then
  fail
fi
[[ "$listener_container_status" == "0" && "$supervisor_status" == "0" ]] || fail
terminal_status=0
cleanup_session
session=""
exit "$terminal_status"
