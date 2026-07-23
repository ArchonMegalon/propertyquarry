#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH PYTHONPATH PERL5LIB RUBYLIB \
  GH_TOKEN GITHUB_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_RUNNER_INPUT_TOKEN
umask 077
ulimit -c 0

fail() {
  printf '%s\n' 'propertyquarry-ephemeral-runner-rejected' >&2
  exit 50
}

[[ "$#" -ge 3 ]] || fail
phase="$1"
runner_label="$2"
launch_ticket_digest="$3"
[[ "$runner_label" =~ ^pqrelease-[0-9a-f]{32}$ ]] || fail
[[ "$launch_ticket_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
[[ "$(id -u):$(id -g)" == "1999:1999" ]] || fail

archive=/usr/lib/propertyquarry-release-runner-v2/actions-runner-linux-x64-2.335.1.tar.gz
session_root=/var/lib/propertyquarry-release-runner-v2/sessions
[[ -d "$session_root" && ! -L "$session_root" ]] || fail
[[ "$(stat -Lc '%a:%u:%g' -- "$session_root")" == "700:1999:1999" ]] || fail
exec 7>"${session_root}/.propertyquarry-release-runner-v2.lock" || fail
flock -n 7 || fail
[[ "$(stat -Lc '%a:%u:%g:%h' -- "${session_root}/.propertyquarry-release-runner-v2.lock")" == \
  "600:1999:1999:1" ]] || fail

session=""
child_pid=""

stop_child() {
  [[ "${child_pid:-}" =~ ^[1-9][0-9]*$ ]] || return 0
  kill -TERM -- "-${child_pid}" >/dev/null 2>&1 || true
  for _attempt in $(seq 1 50); do
    kill -0 "$child_pid" >/dev/null 2>&1 || break
    sleep 0.1
  done
  kill -KILL -- "-${child_pid}" >/dev/null 2>&1 || true
  wait "$child_pid" >/dev/null 2>&1 || true
  child_pid=""
}

session_digest() {
  local target="$1"
  (
    cd -- "$target"
    find . -xdev \( -type f -o -type l \) \
      ! -name '.session-content.sha256' \
      ! -name '.configuration-complete' \
      ! -name 'configure-exit-status' \
      ! -name 'listener-exit-status' \
      ! -name 'launcher-exit-status' \
      -print0 | sort -z | xargs -0 -r sha256sum --zero | sha256sum | cut -d' ' -f1
  )
}

configure_cleanup() {
  local status="${1:-50}"
  trap - EXIT INT TERM HUP
  stop_child
  exec 8<&- 2>/dev/null || true
  if [[ -n "${session:-}" && "$session" =~ ^/var/lib/propertyquarry-release-runner-v2/sessions/session-[0-9a-f]{32}\.[A-Za-z0-9]+$ && -d "$session" && ! -L "$session" ]]; then
    printf '%s\n' "$status" >"$session/configure-exit-status" 2>/dev/null || true
    chmod 600 "$session/configure-exit-status" 2>/dev/null || true
  fi
  exit "$status"
}

run_cleanup() {
  local status="${1:-50}"
  trap - EXIT INT TERM HUP
  stop_child
  if [[ -n "${session:-}" && "$session" =~ ^/var/lib/propertyquarry-release-runner-v2/sessions/session-[0-9a-f]{32}\.[A-Za-z0-9]+$ && -d "$session" && ! -L "$session" ]]; then
    printf '%s\n' "$status" >"$session/launcher-exit-status" 2>/dev/null || true
    chmod 600 "$session/launcher-exit-status" 2>/dev/null || true
  fi
  exit "$status"
}

case "$phase" in
  configure)
    [[ "$#" -eq 3 ]] || fail
    [[ "$(id -G | tr ' ' '\n' | sort -n -u | paste -sd, -)" == "1999" ]] || fail
    [[ "$(stat -Lc '%F' -- /proc/self/fd/0 2>/dev/null)" == "fifo" ]] || fail
    [[ ! -e /var/run/docker.sock ]] || fail
    [[ ! -e /run/propertyquarry-release-single-host-v2 ]] || fail
    [[ ! -e /etc/propertyquarry-release-single-host-v2 ]] || fail
    [[ -f "$archive" && ! -L "$archive" ]] || fail
    [[ "$(stat -Lc '%a:%u:%g:%h:%s' -- "$archive")" == "444:0:0:1:225628509" ]] || fail
    [[ "$(sha256sum -- "$archive" | cut -d' ' -f1)" == \
      "4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf" ]] || fail
    exec 8<&0
    exec 0</dev/null
    session="$(mktemp -d "${session_root}/session-${runner_label#pqrelease-}.XXXXXX")"
    chmod 700 "$session"
    trap 'configure_cleanup $?' EXIT
    trap 'configure_cleanup 130' INT
    trap 'configure_cleanup 143' TERM HUP

    tar -C "$session" --extract --gzip --file "$archive" \
      --no-same-owner --no-same-permissions --delay-directory-restore
    for required in env.sh bin/Runner.Listener; do
      [[ -f "$session/$required" && ! -L "$session/$required" ]] || fail
    done
    [[ "$($session/bin/Runner.Listener --version 2>/dev/null)" == "2.335.1" ]] || fail
    mkdir -m 0700 "$session/home"
    (
      cd -- "$session"
      HOME="$session/home" USER=propertyquarry-runner-v2 LOGNAME=propertyquarry-runner-v2 \
        ./env.sh >/dev/null 2>&1
    )
    [[ -f "$session/.env" && ! -L "$session/.env" && -f "$session/.path" && ! -L "$session/.path" ]] || fail

    runner_labels="propertyquarry-release-controller-v2,${runner_label}"
    /usr/bin/setsid /usr/bin/timeout --signal=TERM --kill-after=5s 90s /usr/bin/env -i \
      HOME="$session/home" USER=propertyquarry-runner-v2 LOGNAME=propertyquarry-runner-v2 \
      PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC \
      /bin/bash --noprofile --norc -p -c '
        set -euo pipefail
        IFS= read -r registration_token <&8
        exec 8<&-
        [[ "$registration_token" =~ ^[A-Za-z0-9._-]{20,2048}$ ]]
        export ACTIONS_RUNNER_INPUT_TOKEN="$registration_token"
        cd "$1"
        ./bin/Runner.Listener configure \
          --unattended --ephemeral --disableupdate --no-default-labels \
          --url https://github.com/ArchonMegalon/propertyquarry \
          --name "pq-release-${2#pqrelease-}" \
          --labels "$3" \
          --work _work
        unset ACTIONS_RUNNER_INPUT_TOKEN
        registration_token_sha256="$(printf "%s" "$registration_token" | sha256sum | cut -d" " -f1)"
        while IFS= read -r -d "" candidate; do
          while IFS= read -r line || [[ -n "$line" ]]; do
            [[ "$line" != *"$registration_token"* ]] || exit 68
          done <"$candidate"
        done < <(find . -xdev -type f -print0)
        unset registration_token
        [[ "$registration_token_sha256" =~ ^[0-9a-f]{64}$ ]]
        printf "sha256:%s\n" "$registration_token_sha256" >.registration-token.sha256
        chmod 600 .registration-token.sha256
      ' propertyquarry-runner-configure "$session" "$runner_label" "$runner_labels" \
      8<&8 >/dev/null 2>&1 &
    child_pid="$!"
    configure_status=0
    wait "$child_pid" || configure_status="$?"
    child_pid=""
    exec 8<&-
    [[ "$configure_status" == "0" ]] || fail
    configured_digest="$(session_digest "$session")"
    [[ "$configured_digest" =~ ^[0-9a-f]{64}$ ]] || fail
    printf 'sha256:%s\n' "$configured_digest" >"$session/.session-content.sha256"
    printf '%s\n' 'configured-without-host-authority' >"$session/.configuration-complete"
    chmod 600 "$session/.session-content.sha256" "$session/.configuration-complete"
    configure_cleanup 0
    ;;

  run)
    [[ "$#" -eq 8 ]] || fail
    session="$4"
    authorized_runner_id="$5"
    session_device="$6"
    session_inode="$7"
    session_tree_digest="$8"
    [[ "$authorized_runner_id" =~ ^[1-9][0-9]*$ ]] || fail
    [[ "$session_device" =~ ^[1-9][0-9]*$ && "$session_inode" =~ ^[1-9][0-9]*$ && \
      "$session_tree_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
    [[ "$session" =~ ^/var/lib/propertyquarry-release-runner-v2/sessions/session-${runner_label#pqrelease-}\.[A-Za-z0-9]+$ ]] || fail
    [[ -d "$session" && ! -L "$session" && "$(stat -Lc '%a:%u:%g' -- "$session")" == "700:1999:1999" ]] || fail
    [[ "$(id -G | tr ' ' '\n' | sort -n -u | paste -sd, -)" == "112,1999" ]] || fail
    [[ -S /var/run/docker.sock && ! -L /var/run/docker.sock ]] || fail
    [[ "$(stat -Lc '%F:%a:%u:%g:%h' -- /var/run/docker.sock 2>/dev/null)" == "socket:660:0:112:1" ]] || fail
    [[ -r /var/run/docker.sock && -w /var/run/docker.sock ]] || fail
    request_socket=/run/propertyquarry-release-single-host-v2/request.sock
    [[ -S "$request_socket" && ! -L "$request_socket" ]] || fail
    [[ "$(stat -Lc '%F:%a:%u:%g:%h' -- "$request_socket" 2>/dev/null)" == "socket:660:0:1999:1" ]] || fail
    [[ -r "$request_socket" && -w "$request_socket" ]] || fail
    [[ "$(stat -Lc '%F' -- /proc/self/fd/0 2>/dev/null)" == "character special file" ]] || fail
    trap 'run_cleanup $?' EXIT
    trap 'run_cleanup 130' INT
    trap 'run_cleanup 143' TERM HUP
    for evidence in .registration-token.sha256 .session-content.sha256 .configuration-complete configure-exit-status; do
      [[ -f "$session/$evidence" && ! -L "$session/$evidence" && "$(stat -Lc '%a:%u:%g:%h' -- "$session/$evidence")" == \
        "600:1999:1999:1" ]] || fail
    done
    [[ "$(<"$session/.configuration-complete")" == "configured-without-host-authority" ]] || fail
    [[ "$(<"$session/configure-exit-status")" == "0" ]] || fail
    [[ "$(<"$session/.registration-token.sha256")" =~ ^sha256:[0-9a-f]{64}$ ]] || fail
    [[ ! -e "$session/listener-exit-status" && ! -e "$session/launcher-exit-status" ]] || fail
    controller=/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2
    [[ -x "$controller" && ! -L "$controller" ]] || fail
    /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC \
      "$controller" runner-session-verify "$runner_label" "$session_device" "$session_inode" "$session_tree_digest" \
      >/dev/null 2>&1 || fail
    safe_path="$(<"$session/.path")"
    [[ -n "$safe_path" && "$safe_path" != *$'\n'* && "$safe_path" != *$'\r'* && "$safe_path" != *'$'* && "$safe_path" != *'`'* ]] || fail

    /usr/bin/setsid /usr/bin/timeout --signal=TERM --kill-after=15s 20000s /bin/bash --noprofile --norc -p -c '
      set -euo pipefail
      session="$1"
      safe_path="$2"
      cd -- "$session"
      exec /usr/bin/env -i \
        HOME="$session/home" USER=propertyquarry-runner-v2 LOGNAME=propertyquarry-runner-v2 \
        SHELL=/bin/bash PATH="$safe_path" LANG=C LC_ALL=C TZ=UTC \
        ./bin/Runner.Listener run
      ' propertyquarry-runner-listen "$session" "$safe_path" </dev/null &
    child_pid="$!"
    listener_status=0
    wait "$child_pid" || listener_status="$?"
    child_pid=""
    printf '%s\n' "$listener_status" >"$session/listener-exit-status"
    chmod 600 "$session/listener-exit-status"
    if [[ "$listener_status" != "0" && "$listener_status" != "2" ]]; then
      exit "$listener_status"
    fi
    run_cleanup 0
    ;;
  *)
    fail
    ;;
esac
