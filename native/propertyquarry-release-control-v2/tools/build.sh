#!/bin/sh
set -eu
PATH=/usr/bin:/bin
LANG=C
LC_ALL=C
TZ=UTC
export PATH LANG LC_ALL TZ

SOURCE_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
REPOSITORY_ROOT=$(CDPATH='' cd -- "$SOURCE_ROOT/../.." && pwd)
DEFAULT_OUTPUT="$REPOSITORY_ROOT/build/propertyquarry-release-control-v2/linux-amd64"
OUTPUT_ROOT=${1:-"$DEFAULT_OUTPUT"}
GO_ARCHIVE=${PROPERTYQUARRY_GO_ARCHIVE:-}
EXPECTED_ARCHIVE_SHA=5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053
EXPECTED_ARCHIVE_BYTES=66879095
EXPECTED_GO_SHA=8da5fd321795754b994c64e3eb8a5a14ff47bd285559a7e876f3c79abafc67f9

fail() {
  printf '%s\n' "error: $1" >&2
  exit 1
}

digest() {
  digest_output=$(sha256sum -- "$1") || return 1
  printf '%s\n' "${digest_output%% *}"
}

manifest_digest() (
  manifest_root=$1
  hash_list=$2
  manifest_path="$manifest_root/tools/source-files.txt"
  expected_paths="${hash_list}.expected-paths"
  observed_paths="${hash_list}.observed-paths"
  sorted_expected_paths="${hash_list}.expected-paths.sorted"
  sorted_observed_paths="${hash_list}.observed-paths.sorted"
  [ -f "$manifest_path" ] && [ ! -L "$manifest_path" ] || fail "source manifest is invalid"
  [ "$(realpath -e -- "$manifest_path")" = "$manifest_path" ] ||
    fail "source manifest contains a symlink"
  : >"$hash_list"
  : >"$expected_paths"
  entry_count=0
  while IFS= read -r relative || [ -n "$relative" ]; do
    case "$relative" in
      ""|/*|*..*) fail "source manifest path is invalid" ;;
    esac
    source_path="$manifest_root/$relative"
    [ -f "$source_path" ] && [ ! -L "$source_path" ] || fail "source manifest entry is invalid"
    [ "$(realpath -e -- "$source_path")" = "$source_path" ] ||
      fail "source manifest entry contains a symlink"
    file_sha=$(digest "$source_path") || fail "source manifest entry cannot be hashed"
    printf '%s  %s\n' "$file_sha" "$relative" >>"$hash_list"
    printf '%s\0' "$relative" >>"$expected_paths"
    entry_count=$((entry_count + 1))
  done <"$manifest_path"
  [ "$entry_count" -gt 0 ] || fail "source manifest is empty"
  [ -z "$(find -P "$manifest_root" -mindepth 1 ! -type d ! -type f -printf x -quit)" ] ||
    fail "source tree contains a symlink or special entry"
  find -P "$manifest_root" -type f -printf '%P\0' >"$observed_paths" ||
    fail "source tree cannot be enumerated"
  LC_ALL=C sort -z "$expected_paths" >"$sorted_expected_paths" ||
    fail "source manifest paths cannot be sorted"
  LC_ALL=C sort -z "$observed_paths" >"$sorted_observed_paths" ||
    fail "source tree paths cannot be sorted"
  cmp "$sorted_expected_paths" "$sorted_observed_paths" >/dev/null ||
    fail "source tree is not the exact source manifest"
  digest "$hash_list" || fail "source manifest cannot be hashed"
)

[ "$#" -le 1 ] || fail "usage: build.sh [output-root]"
CANONICAL_OUTPUT=$(realpath -m -- "$OUTPUT_ROOT")
CANONICAL_REPOSITORY_OUTPUT=$(realpath -m -- "$DEFAULT_OUTPUT")
[ "$CANONICAL_REPOSITORY_OUTPUT" = "$DEFAULT_OUTPUT" ] ||
  fail "repository build path contains a symlink"
case "$CANONICAL_OUTPUT" in
  "$CANONICAL_REPOSITORY_OUTPUT"|/tmp/*) ;;
  *) fail "output must be the repository build path or an isolated /tmp path" ;;
esac

# Keep the authenticated toolchain beneath the caller-owned output parent.
# A globally named /tmp directory can be reclaimed by unrelated host cleanup
# while the compiler is active. The parent must already be a private, stable
# directory (or the repository's own build directory), never shared bare /tmp.
OUTPUT_PARENT=$(dirname -- "$CANONICAL_OUTPUT")
mkdir -p -- "$OUTPUT_PARENT"
[ -d "$OUTPUT_PARENT" ] && [ ! -L "$OUTPUT_PARENT" ] ||
  fail "output parent must be a trusted directory"
[ "$(realpath -e -- "$OUTPUT_PARENT")" = "$OUTPUT_PARENT" ] ||
  fail "output parent contains a symlink"
[ -z "$(find "$OUTPUT_PARENT" -maxdepth 0 -perm /022 -printf x -quit)" ] ||
  fail "output parent must not be group- or world-writable"
[ "$(stat -c '%u' -- "$OUTPUT_PARENT")" = "$(id -u)" ] ||
  fail "output parent must be owned by the invoking user"
PRIVATE_ROOT=$(mktemp -d "$OUTPUT_PARENT/.pq-native-build.XXXXXX")
cleanup() {
  status=$1
  trap '' HUP INT TERM
  trap - EXIT
  rm -rf -- "$PRIVATE_ROOT" || :
  exit "$status"
}
trap 'cleanup "$?"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

SOURCE_MANIFEST_SHA=$(
  manifest_digest "$SOURCE_ROOT" "$PRIVATE_ROOT/source-hashes-before"
) || exit 1

[ -n "$GO_ARCHIVE" ] || fail "PROPERTYQUARRY_GO_ARCHIVE is required"
[ -f "$GO_ARCHIVE" ] && [ ! -L "$GO_ARCHIVE" ] ||
  fail "toolchain archive must be a regular non-symlink file"
[ "$(stat -c '%s' -- "$GO_ARCHIVE")" = "$EXPECTED_ARCHIVE_BYTES" ] ||
  fail "toolchain archive size mismatch"
[ "$(digest "$GO_ARCHIVE")" = "$EXPECTED_ARCHIVE_SHA" ] ||
  fail "toolchain archive digest mismatch"

TOOLCHAIN_ROOT="$PRIVATE_ROOT/toolchain"
mkdir -m 0700 "$TOOLCHAIN_ROOT"
env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC tar --extract \
  --gzip \
  --file "$GO_ARCHIVE" \
  --directory "$TOOLCHAIN_ROOT" \
  --no-same-owner \
  --no-same-permissions
[ "$(stat -c '%s' -- "$GO_ARCHIVE")" = "$EXPECTED_ARCHIVE_BYTES" ] ||
  fail "toolchain archive changed during extraction"
[ "$(digest "$GO_ARCHIVE")" = "$EXPECTED_ARCHIVE_SHA" ] ||
  fail "toolchain archive changed during extraction"
GOROOT="$TOOLCHAIN_ROOT/go"
GO_BINARY="$GOROOT/bin/go"
[ -d "$GOROOT" ] && [ ! -L "$GOROOT" ] || fail "extracted GOROOT is invalid"
[ -f "$GO_BINARY" ] && [ ! -L "$GO_BINARY" ] && [ -x "$GO_BINARY" ] ||
  fail "extracted Go binary is invalid"
[ "$(realpath -e -- "$GO_BINARY")" = "$GO_BINARY" ] || fail "extracted Go binary contains a symlink"
[ "$(digest "$GO_BINARY")" = "$EXPECTED_GO_SHA" ] || fail "extracted Go binary digest mismatch"
[ "$(env -i PATH=/usr/bin:/bin GOROOT="$GOROOT" GOTOOLCHAIN=local "$GO_BINARY" version)" = "go version go1.26.5 linux/amd64" ] ||
  fail "exact Go 1.26.5 linux/amd64 toolchain required"

[ ! -L "$OUTPUT_ROOT" ] || fail "output directory must not be a symlink"
mkdir -p "$OUTPUT_ROOT"
[ "$(realpath -e -- "$OUTPUT_ROOT")" = "$CANONICAL_OUTPUT" ] ||
  fail "output path changed during creation"
INITIAL_OUTPUT_IDENTITY=$(
  /usr/bin/stat -Lc '%d:%i:%f:%u:%g' -- "$OUTPUT_ROOT"
) || fail "output identity is unavailable"
[ ! -e "$OUTPUT_ROOT/build-receipt.json" ] && [ ! -L "$OUTPUT_ROOT/build-receipt.json" ] ||
  fail "refusing to overwrite a receipted bundle; use repro-build.sh"

if [ -n "${PROPERTYQUARRY_BUILD_CACHE_ROOT:-}" ]; then
  CACHE_ROOT=$PROPERTYQUARRY_BUILD_CACHE_ROOT
else
  CACHE_ROOT="$PRIVATE_ROOT/cache"
fi
CANONICAL_CACHE=$(realpath -m -- "$CACHE_ROOT")
case "$CANONICAL_CACHE" in
  /tmp/*) ;;
  *) fail "build cache must be isolated under /tmp" ;;
esac
[ ! -e "$CACHE_ROOT" ] && [ ! -L "$CACHE_ROOT" ] ||
  fail "build cache must be a new non-symlink path"
mkdir -p \
  "$CACHE_ROOT/gocache" \
  "$CACHE_ROOT/gomodcache" \
  "$CACHE_ROOT/gopath" \
  "$CACHE_ROOT/home" \
  "$CACHE_ROOT/tmp" \
  "$CACHE_ROOT/xdg-cache" \
  "$CACHE_ROOT/xdg-config"
[ "$(realpath -e -- "$CACHE_ROOT")" = "$CANONICAL_CACHE" ] || fail "build cache path changed"

run_go() {
  env -i \
    PATH=/usr/bin:/bin \
    HOME="$CACHE_ROOT/home" \
    LANG=C \
    LC_ALL=C \
    TMPDIR="$CACHE_ROOT/tmp" \
    TZ=UTC \
    XDG_CACHE_HOME="$CACHE_ROOT/xdg-cache" \
    XDG_CONFIG_HOME="$CACHE_ROOT/xdg-config" \
    CGO_ENABLED=0 \
    GO111MODULE=on \
    GOARCH=amd64 \
    GOAMD64=v1 \
    GOCACHE="$CACHE_ROOT/gocache" \
    GOENV=off \
    GOEXPERIMENT= \
    GOFIPS140=off \
    GOFLAGS= \
    GOMODCACHE="$CACHE_ROOT/gomodcache" \
    GOOS=linux \
    GOPATH="$CACHE_ROOT/gopath" \
    GOPROXY=off \
    GOROOT="$GOROOT" \
    GOSUMDB=off \
    GOTELEMETRY=off \
    GOTOOLCHAIN=local \
    GOTMPDIR="$CACHE_ROOT/tmp" \
    GOWORK=off \
    "$GO_BINARY" "$@"
}

run_go test -C "$SOURCE_ROOT" -mod=readonly ./... >&2

SCRATCH_EXECUTION_CONTRACT=linux-amd64-static-et-exec-v1
LDFLAGS="-buildid= -linkmode=internal -X propertyquarry.local/release-control-v2/internal/releasecontrol.SourceManifestDigest=sha256:$SOURCE_MANIFEST_SHA -X propertyquarry.local/release-control-v2/internal/releasecontrol.ScratchExecutionContract=$SCRATCH_EXECUTION_CONTRACT"
BUILD_ROOT="$PRIVATE_ROOT/output"
mkdir -m 0700 "$BUILD_ROOT"
for component in supervisor controller watchdog; do
  name="propertyquarry-release-${component}-v2"
  target="$BUILD_ROOT/$name"
  run_go build \
    -C "$SOURCE_ROOT" \
    -mod=readonly \
    -trimpath \
    -buildvcs=false \
    -buildmode=exe \
    -ldflags "$LDFLAGS" \
    -o "$target" \
    "./cmd/$name"
  [ -f "$target" ] && [ ! -L "$target" ] || fail "binary output is invalid"
  chmod 0755 "$target"
  "$SOURCE_ROOT/tools/verify-static-elf.sh" "$target" >/dev/null ||
    fail "binary is not scratch-executable static ELF"
done

SOURCE_MANIFEST_SHA_AFTER=$(manifest_digest "$SOURCE_ROOT" "$PRIVATE_ROOT/source-hashes-after") ||
  fail "source manifest cannot be re-authenticated"
[ "$SOURCE_MANIFEST_SHA_AFTER" = "$SOURCE_MANIFEST_SHA" ] || fail "source changed during the build"
[ "$(digest "$GO_BINARY")" = "$EXPECTED_GO_SHA" ] || fail "Go binary changed during the build"

publish_binaries() {
  exec 9<"$OUTPUT_ROOT" || fail "output lock descriptor cannot be opened"
  /usr/bin/flock -n -x 9 || fail "another output publisher is active"
  locked_output_identity=$(
    /usr/bin/stat -Lc '%d:%i:%f:%u:%g' -- /proc/self/fd/9
  ) || fail "output lock identity is unavailable"
  [ "$locked_output_identity" = "$INITIAL_OUTPUT_IDENTITY" ] ||
    fail "output changed before publication"
  [ "$(/usr/bin/stat -Lc '%d:%i:%f:%u:%g' -- "$OUTPUT_ROOT")" = "$locked_output_identity" ] ||
    fail "output changed before publication"
  [ ! -e "$OUTPUT_ROOT/build-receipt.json" ] &&
    [ ! -L "$OUTPUT_ROOT/build-receipt.json" ] ||
    fail "refusing to overwrite a receipted bundle; use repro-build.sh"

  for component in supervisor controller watchdog; do
    name="propertyquarry-release-${component}-v2"
    source="$BUILD_ROOT/$name"
    target="$OUTPUT_ROOT/$name"
    [ -f "$source" ] && [ ! -L "$source" ] ||
      fail "private binary output is invalid"
    [ ! -L "$target" ] || fail "binary target must not be a symlink"
    [ ! -e "$target" ] || [ -f "$target" ] ||
      fail "binary target type is invalid"
    if [ -e "$target" ]; then
      rm -- "$target" || fail "previous binary cannot be invalidated"
    fi
    [ ! -e "$target" ] && [ ! -L "$target" ] ||
      fail "previous binary remains visible"
    install -m 0755 "$source" "$target"
    cmp "$source" "$target"
    [ "$(stat -c '%a' -- "$target")" = 755 ] ||
      fail "published binary mode is invalid"
    "$SOURCE_ROOT/tools/verify-static-elf.sh" "$target" >/dev/null ||
      fail "published binary is not scratch-executable static ELF"
    /usr/bin/sync -d "$target" ||
      fail "published binary cannot be synchronized"
  done
  /usr/bin/sync -d "$OUTPUT_ROOT" ||
    fail "published binary directory entries cannot be synchronized"
  [ "$(/usr/bin/stat -Lc '%d:%i:%f:%u:%g' -- "$OUTPUT_ROOT")" = "$locked_output_identity" ] ||
    fail "output changed during publication"
}

publish_binaries
