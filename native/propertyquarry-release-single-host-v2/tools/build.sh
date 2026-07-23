#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C TZ=UTC
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH GOPATH GOROOT GOENV GOFLAGS
umask 077

[[ ("$#" -eq 2 || "$#" -eq 3) && "$1" = /* && "$2" = /* ]] || exit 2
archive="$1"
output="$2"
package_anchor="${3:-}"
module_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ -f "$archive" && ! -L "$archive" && ! -e "$output" ]] || exit 2
[[ "$(stat -Lc '%s' -- "$archive")" == "66879095" ]]
[[ "$(sha256sum -- "$archive" | cut -d' ' -f1)" == "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053" ]]

installer_anchor_der_base64=unbound
installer_anchor_key_id=unbound
if [[ -n "$package_anchor" ]]; then
  [[ "$package_anchor" = /* && -f "$package_anchor" && ! -L "$package_anchor" ]]
  readarray -t anchor_metadata < <(python3 - "$package_anchor" <<'PY'
import base64,hashlib,re,sys
raw=open(sys.argv[1],"rb").read()
match=re.fullmatch(br"-----BEGIN PUBLIC KEY-----\n([A-Za-z0-9+/=\n]+)-----END PUBLIC KEY-----\n",raw)
if match is None:
    raise SystemExit("package-anchor-invalid")
der=base64.b64decode(match.group(1),validate=False)
if len(der) != 44 or der[:12] != bytes.fromhex("302a300506032b6570032100"):
    raise SystemExit("package-anchor-not-ed25519")
print(base64.b64encode(der).decode("ascii").rstrip("="))
print("sha256:"+hashlib.sha256(der).hexdigest())
PY
  )
  [[ "${#anchor_metadata[@]}" -eq 2 ]]
  installer_anchor_der_base64="${anchor_metadata[0]}"
  installer_anchor_key_id="${anchor_metadata[1]}"
  unset anchor_metadata
fi

source_manifest="${module_root}/tools/source-files.txt"
[[ -f "$source_manifest" && ! -L "$source_manifest" ]]
closure_work="$(mktemp -d)"
source_snapshot="${closure_work}/snapshot"
manifest_material="${closure_work}/source-manifest.material"
mkdir -m 0700 "$source_snapshot"
work_one="$(mktemp -d)"
work_two="$(mktemp -d)"
stage_parent="$(dirname -- "$output")"
stage="$(mktemp -d "${stage_parent}/.propertyquarry-single-host-build.XXXXXX")"
cleanup() {
  python3 - "$closure_work" "$work_one" "$work_two" "$stage" <<'PY'
import os,shutil,sys
for value in sys.argv[1:]:
    try:
        if os.path.isdir(value) and not os.path.islink(value): shutil.rmtree(value)
        elif os.path.exists(value) or os.path.islink(value): os.unlink(value)
    except OSError: pass
PY
}
trap cleanup EXIT INT TERM HUP
chmod 700 "$work_one" "$work_two" "$stage"
python3 "${module_root}/tools/verify-source-closure.py" create \
  --module-root "$module_root" --manifest "$source_manifest" \
  --snapshot "$source_snapshot" --material "$manifest_material"
source_digest="sha256:$(sha256sum -- "$manifest_material" | cut -d' ' -f1)"

build_once() {
  local work="$1"
  mkdir -m 0700 "$work/toolchain" "$work/cache" "$work/modcache" "$work/home" "$work/output"
  tar -C "$work/toolchain" -xzf "$archive"
  go_binary="$work/toolchain/go/bin/go"
  [[ "$($go_binary version)" == "go version go1.26.5 linux/amd64" ]]
  [[ "$(sha256sum -- "$go_binary" | cut -d' ' -f1)" == "8da5fd321795754b994c64e3eb8a5a14ff47bd285559a7e876f3c79abafc67f9" ]]
  common_env=(
    PATH="$work/toolchain/go/bin:/usr/bin:/bin" HOME="$work/home" GOCACHE="$work/cache" GOMODCACHE="$work/modcache"
    CGO_ENABLED=0 GO111MODULE=on GOARCH=amd64 GOAMD64=v1 GOENV=off GOEXPERIMENT= GOFIPS140=off GOFLAGS=
    GOOS=linux GOPROXY=off GOSUMDB=off GOTELEMETRY=off GOTOOLCHAIN=local GOWORK=off LANG=C LC_ALL=C TZ=UTC
  )
  (
    cd -- "$source_snapshot"
    env -i "${common_env[@]}" "$go_binary" list -json -mod=readonly ./... >"$work/go-list.json"
  )
  python3 "${source_snapshot}/tools/verify-source-closure.py" verify-go-list \
    --snapshot "$source_snapshot" --material "$manifest_material" --go-list "$work/go-list.json"
  python3 "${source_snapshot}/tools/verify-source-closure.py" verify \
    --snapshot "$source_snapshot" --material "$manifest_material"
  (
    cd -- "$source_snapshot"
    env -i "${common_env[@]}" "$go_binary" test -mod=readonly ./...
  )
  python3 "${source_snapshot}/tools/verify-source-closure.py" verify \
    --snapshot "$source_snapshot" --material "$manifest_material"
  (
    cd -- "$source_snapshot"
    env -i "${common_env[@]}" "$go_binary" build -mod=readonly -trimpath -buildvcs=false -buildmode=exe \
    -ldflags="-buildid= -linkmode=internal -X propertyquarry.local/release-single-host-v2/internal/authority.SourceManifestDigest=${source_digest} -X propertyquarry.local/release-single-host-v2/internal/authority.ScratchExecutionContract=linux-amd64-static-et-exec-v1" \
    -o "$work/output/propertyquarry-release-single-host-v2" ./cmd/propertyquarry-release-single-host-v2
    env -i "${common_env[@]}" "$go_binary" build -mod=readonly -trimpath -buildvcs=false -buildmode=exe \
    -ldflags="-buildid= -linkmode=internal -X propertyquarry.local/release-single-host-v2/internal/authority.SourceManifestDigest=${source_digest} -X propertyquarry.local/release-single-host-v2/internal/authority.ScratchExecutionContract=linux-amd64-static-et-exec-v1 -X propertyquarry.local/release-single-host-v2/internal/installhelper.InstallerSourceManifestDigest=${source_digest} -X propertyquarry.local/release-single-host-v2/internal/installhelper.EmbeddedPackageAuthorityDERBase64=${installer_anchor_der_base64}" \
    -o "$work/output/propertyquarry-release-single-host-installer-v2" ./cmd/propertyquarry-release-single-host-installer-v2
  )
  python3 "${source_snapshot}/tools/verify-source-closure.py" verify \
    --snapshot "$source_snapshot" --material "$manifest_material"
  chmod 755 "$work/output/propertyquarry-release-single-host-v2"
  chmod 555 "$work/output/propertyquarry-release-single-host-installer-v2"
  "${source_snapshot}/tools/verify-static-elf.sh" "$work/output/propertyquarry-release-single-host-v2"
  "${source_snapshot}/tools/verify-static-elf.sh" "$work/output/propertyquarry-release-single-host-installer-v2"
}

build_once "$work_one"
build_once "$work_two"
cmp --silent "$work_one/output/propertyquarry-release-single-host-v2" "$work_two/output/propertyquarry-release-single-host-v2"
cmp --silent "$work_one/output/propertyquarry-release-single-host-installer-v2" "$work_two/output/propertyquarry-release-single-host-installer-v2"
install -m 0755 "$work_one/output/propertyquarry-release-single-host-v2" "$stage/propertyquarry-release-single-host-v2"
install -m 0555 "$work_one/output/propertyquarry-release-single-host-installer-v2" "$stage/propertyquarry-release-single-host-installer-v2"
binary_sha="sha256:$(sha256sum -- "$stage/propertyquarry-release-single-host-v2" | cut -d' ' -f1)"
binary_size="$(stat -Lc '%s' -- "$stage/propertyquarry-release-single-host-v2")"
installer_binary_sha="sha256:$(sha256sum -- "$stage/propertyquarry-release-single-host-installer-v2" | cut -d' ' -f1)"
installer_binary_size="$(stat -Lc '%s' -- "$stage/propertyquarry-release-single-host-installer-v2")"
"$stage/propertyquarry-release-single-host-v2" --self-test >"$stage/self-test.json"
"$stage/propertyquarry-release-single-host-installer-v2" --self-test >"$stage/installer-self-test.json"
python3 - "$stage/self-test.json" "$source_digest" <<'PY'
import json,sys
raw=open(sys.argv[1],'rb').read()
value=json.loads(raw)
assert value["authoritative"] is False
assert value["production_ready"] is False
assert value["performs_release_effects"] is False
assert value["self_test"] is True
assert value["source_manifest_digest"] == sys.argv[2]
assert value["scratch_execution_contract"] == "linux-amd64-static-et-exec-v1"
assert raw == json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")+b"\n"
PY
python3 - "$stage/installer-self-test.json" "$source_digest" "$installer_anchor_key_id" <<'PY'
import json,sys
raw=open(sys.argv[1],'rb').read()
value=json.loads(raw)
assert value["authoritative"] is False
assert value["production_ready"] is False
assert value["performs_release_effects"] is False
assert value["host_install_performed"] is False
assert value["self_test"] is True
assert value["source_manifest_digest"] == sys.argv[2]
if sys.argv[3] == "unbound":
    assert value["embedded_package_authority_bound"] is False
    assert value["embedded_package_authority_key_id"] == ""
else:
    assert value["embedded_package_authority_bound"] is True
    assert value["embedded_package_authority_key_id"] == sys.argv[3]
assert raw == json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")+b"\n"
PY
python3 - "$stage/build-receipt.v2.json" "$source_digest" "$binary_sha" "$binary_size" "$installer_binary_sha" "$installer_binary_size" "$installer_anchor_key_id" <<'PY'
import json,os,sys
value={
 "schema":"propertyquarry.release-control.single-host-native-build-receipt.v2","version":2,
 "authoritative":False,"production_ready":False,"performs_release_effects":False,
 "reproducible_double_build":True,"independent_toolchain_extractions":True,"module_network_resolution_disabled":True,
 "host_network_namespace_isolated":False,"go_tests_passed_in_both_builds":True,"static_elf_verified_in_both_builds":True,
 "toolchain":"go1.26.5 linux/amd64","toolchain_archive_bytes":66879095,
 "toolchain_archive_sha256":"5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053",
 "source_manifest_digest":sys.argv[2],"binary_sha256":sys.argv[3],"binary_size":int(sys.argv[4]),"binary_mode":"0755",
 "installer_binary_sha256":sys.argv[5],"installer_binary_size":int(sys.argv[6]),"installer_binary_mode":"0555",
 "installer_package_authority_key_id":sys.argv[7],"installer_package_authority_bound":sys.argv[7] != "unbound",
 "build_flags":["-mod=readonly","-trimpath","-buildvcs=false","-buildmode=exe"],
 "scratch_execution_contract":"linux-amd64-static-et-exec-v1","receipt_published_last":True,
 "package_signature_verified":False,"root_install_performed":False,
}
raw=json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")+b"\n"
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
try: os.write(fd,raw); os.fsync(fd)
finally: os.close(fd)
PY
chmod 644 "$stage/build-receipt.v2.json"
python3 - "$stage/self-test.json" "$stage/installer-self-test.json" <<'PY'
import os,sys
for path in sys.argv[1:]: os.unlink(path)
PY
python3 - "$stage" "$output" <<'PY'
import ctypes,errno,os,sys
source,destination=sys.argv[1:]
libc=ctypes.CDLL(None,use_errno=True)
renameat2=getattr(libc,"renameat2",None)
if renameat2 is None:
    raise SystemExit("renameat2-unavailable")
renameat2.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
renameat2.restype=ctypes.c_int
AT_FDCWD=-100
RENAME_NOREPLACE=1
if renameat2(AT_FDCWD,os.fsencode(source),AT_FDCWD,os.fsencode(destination),RENAME_NOREPLACE) != 0:
    failure=ctypes.get_errno()
    if failure == errno.EEXIST:
        raise SystemExit("build-output-already-exists")
    raise OSError(failure,os.strerror(failure),destination)
directory=os.open(os.path.dirname(destination),os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
stage=""
printf '%s\n' "$output/build-receipt.v2.json"
