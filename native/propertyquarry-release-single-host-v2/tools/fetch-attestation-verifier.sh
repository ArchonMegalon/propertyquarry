#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy GH_TOKEN GITHUB_TOKEN GH_HOST GH_CONFIG_DIR
umask 077

fail() {
  printf '%s\n' 'propertyquarry-attestation-verifier-download-rejected' >&2
  exit 50
}

[[ "$#" -eq 1 && "$1" = /* && "$1" =~ ^/[A-Za-z0-9._/+:-]+$ && ! -e "$1" ]] || fail
output="$1"
parent="$(dirname -- "$output")"
[[ -d "$parent" && ! -L "$parent" && "$(stat -Lc '%a:%u:%g' -- "$parent")" == "700:$(id -u):$(id -g)" ]] || fail
stage="$(mktemp -d "${parent}/.gh-attestation-verifier.XXXXXX")"
archive="${stage}/gh_2.96.0_linux_amd64.tar.gz"
publish="${stage}/publish"
module_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
trusted_root_source="${module_root}/packaging/trust/sigstore-public-good-trusted-root.jsonl"
cleanup() {
  if [[ -n "${stage:-}" && -d "$stage" && ! -L "$stage" ]]; then
    python3 - "$stage" <<'PY'
import os, shutil, sys
path = sys.argv[1]
if os.path.isdir(path) and not os.path.islink(path):
    shutil.rmtree(path)
PY
  fi
}
trap cleanup EXIT INT TERM HUP
chmod 0700 "$stage"
mkdir -m 0700 "$publish"
[[ -f "$trusted_root_source" && ! -L "$trusted_root_source" ]] || fail
[[ "$(stat -Lc '%h:%s' -- "$trusted_root_source")" == "1:5748" ]] || fail
[[ "$(sha256sum -- "$trusted_root_source" | cut -d' ' -f1)" == "3c2cc7f357dc064ec527fdcd78da6e9245c21a381e1abaa0f2b62b186bcac1a1" ]] || fail
curl --fail --silent --show-error --location --max-redirs 3 --proto '=https' --proto-redir '=https' --tlsv1.2 --noproxy '*' --max-time 600 \
  --output "$archive" \
  https://github.com/cli/cli/releases/download/v2.96.0/gh_2.96.0_linux_amd64.tar.gz
chmod 0400 "$archive"
[[ "$(stat -Lc '%a:%h:%s' -- "$archive")" == "400:1:14652560" ]] || fail
[[ "$(sha256sum -- "$archive" | cut -d' ' -f1)" == "83d5c2ccad5498f58bf6368acb1ab32588cf43ab3a4b1c301bf36328b1c8bd60" ]] || fail
tar -C "$stage" -xzf "$archive" gh_2.96.0_linux_amd64/bin/gh
binary="${stage}/gh_2.96.0_linux_amd64/bin/gh"
[[ -f "$binary" && ! -L "$binary" && "$(stat -Lc '%h:%s' -- "$binary")" == "1:40722594" ]] || fail
[[ "$(sha256sum -- "$binary" | cut -d' ' -f1)" == "56b8bbbb27b066ecb33dbef9a256dc9d1314adaeff0908a752feba6c34053b40" ]] || fail
chmod 0500 "$binary"
install -m 0400 "$trusted_root_source" "${publish}/trusted_root.jsonl"
[[ "$(stat -Lc '%a:%h:%s' -- "${publish}/trusted_root.jsonl")" == "400:1:5748" ]] || fail
[[ "$(sha256sum -- "${publish}/trusted_root.jsonl" | cut -d' ' -f1)" == "3c2cc7f357dc064ec527fdcd78da6e9245c21a381e1abaa0f2b62b186bcac1a1" ]] || fail
install -m 0500 "$binary" "${publish}/gh"
[[ "$(${publish}/gh version | sed -n '1p')" == "gh version 2.96.0 (2026-07-02)" ]] || fail
python3 - "$publish" "$output" <<'PY'
import ctypes, errno, os, sys
source, destination = sys.argv[1:]
for name in ("gh", "trusted_root.jsonl"):
    descriptor = os.open(os.path.join(source, name), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = libc.renameat2
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
renameat2.restype = ctypes.c_int
if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
    failure = ctypes.get_errno()
    raise OSError(failure, os.strerror(failure), destination)
directory = os.open(os.path.dirname(destination), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
printf '%s\n' "$output"
