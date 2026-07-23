#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
umask 077

fail() {
  printf '%s\n' 'propertyquarry-runner-download-rejected' >&2
  exit 50
}

[[ "$#" -eq 1 && "$1" = /* && "$1" =~ ^/[A-Za-z0-9._/+:-]+$ && ! -e "$1" ]] || fail
output="$1"
parent="$(dirname -- "$output")"
[[ -d "$parent" && ! -L "$parent" && "$(stat -Lc '%a:%u:%g' -- "$parent")" == "700:$(id -u):$(id -g)" ]] || fail
stage="$(mktemp "${parent}/.actions-runner-linux-x64-2.335.1.XXXXXX")"
cleanup() {
  if [[ -n "${stage:-}" && -f "$stage" && ! -L "$stage" ]]; then
    unlink "$stage" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM HUP
chmod 0600 "$stage"
curl --fail --silent --show-error --location --max-redirs 3 --proto '=https' --proto-redir '=https' --tlsv1.2 --noproxy '*' --max-time 1800 \
  --output "$stage" \
  https://github.com/actions/runner/releases/download/v2.335.1/actions-runner-linux-x64-2.335.1.tar.gz
chmod 0400 "$stage"
[[ "$(stat -Lc '%a:%h:%s' -- "$stage")" == "400:1:225628509" ]] || fail
[[ "$(sha256sum -- "$stage" | cut -d' ' -f1)" == "4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf" ]] || fail
python3 - "$stage" "$output" <<'PY'
import ctypes,errno,os,sys
source,destination=sys.argv[1:]
file_descriptor=os.open(source,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
try: os.fsync(file_descriptor)
finally: os.close(file_descriptor)
libc=ctypes.CDLL(None,use_errno=True)
renameat2=libc.renameat2
renameat2.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
renameat2.restype=ctypes.c_int
if renameat2(-100,os.fsencode(source),-100,os.fsencode(destination),1) != 0:
    failure=ctypes.get_errno()
    raise OSError(failure,os.strerror(failure),destination)
directory=os.open(os.path.dirname(destination),os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
try: os.fsync(directory)
finally: os.close(directory)
PY
stage=""
printf '%s\n' "$output"
