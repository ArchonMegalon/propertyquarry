# PropertyQuarry native release-control build v2

Status: repository-owned, non-authoritative native bootstrap and reproducible
build contract. It does not establish production trust, install a package, or
grant release authority.

## Source and package boundary

Native source lives only in
`native/propertyquarry-release-control-v2/`. Generated binaries live in
`build/propertyquarry-release-control-v2/linux-amd64/`. Neither source nor
generated output may be placed in
`packaging/propertyquarry-release-control-v2/`, whose exact seven files remain
system-package templates:

- `schema/controller-v2.schema.json`;
- `schema/watchdog-v2.schema.json`;
- `systemd/propertyquarry-release-control-v2.socket`;
- `systemd/propertyquarry-release-control-v2@.service`;
- `systemd/propertyquarry-release-watchdog-v2.service`;
- `sysusers.d/propertyquarry-release-control-v2.conf`; and
- `tmpfiles.d/propertyquarry-release-control-v2.conf`.

Package assembly must independently map the three verified build outputs to:

```text
/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2
/usr/libexec/propertyquarry-release-control/propertyquarry-release-controller-v2
/usr/libexec/propertyquarry-release-control/propertyquarry-release-watchdog-v2
```

The repository build never writes those root-owned paths.

## Pinned toolchain and network-closed module resolution

`toolchain.lock.json` fixes the official Go 1.26.5 Linux AMD64 archive:

```text
URL: https://go.dev/dl/go1.26.5.linux-amd64.tar.gz
bytes: 66879095
SHA-256: 5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053
```

The archive must be acquired separately over an authenticated channel. The
build verifies its size and SHA-256 before and after use, extracts it into a
private temporary root, and invokes only that root's `go/bin/go`. It verifies
the extracted front-end SHA-256 pinned in `toolchain.lock.json` and refuses any
other reported toolchain version. The reproducibility lane performs a separate
archive extraction for each build, so a caller-selected host `GOROOT`, compiler,
linker, standard library, or `go` front end is not an input. It uses only the Go
standard library and requires
`GOTOOLCHAIN=local`, `GOPROXY=off`, `GOSUMDB=off`, `GOWORK=off`,
`CGO_ENABLED=0`, `GOOS=linux`, `GOARCH=amd64`, and `GOAMD64=v1`. There is no
`go.sum`, vendor tree, module download, VCS stamping, timestamp, or host source
path in the intended input set. Module and toolchain network resolution is
disabled; the build process is not placed in a network namespace, and the
receipt therefore records `host_network_namespace_isolated: false`.

Every binary is built with `-mod=readonly -trimpath -buildvcs=false
-buildmode=exe` and the recorded linker flags explicitly select
`-linkmode=internal`, remove the build ID, and embed both the closed
source-manifest digest and the frozen scratch-execution contract. The Go test
and build subprocesses receive a
closed `env -i` allowlist that pins `GOFIPS140=off` and omits host linker,
loader, cache-helper, Python, and Go overrides. The build embeds a digest of the
closed `tools/source-files.txt` manifest in each binary. Before any toolchain
use, both build lanes require that manifest to be duplicate-free and equal to
the exact regular-file tree; an unlisted Go file, symlink, or special entry is
rejected rather than becoming an unbound compiler input.

`tools/build.sh` writes compiler outputs only into its private workspace. It
then takes a nonblocking exclusive lock on the requested output directory,
binds that lock to the directory identity captured before the build, and
rechecks that no reproducible-build receipt has appeared before publishing any
binary. Direct builds and reproducible publication therefore share the same
directory-lock boundary; a direct build already in flight cannot overwrite
binaries after a receipt is published.

`tools/repro-build.sh` copies the authenticated manifest into two different
absolute temporary source roots, builds with separate caches, archive
extractions, and outputs, runs the Go tests in both copies, re-hashes both
source copies, checks each embedded build-info record, and requires each binary
pair to be byte-identical. Publication takes a nonblocking exclusive lock on
the final directory. It accepts only expected bundle names so a previously
interrupted partial publication can be overwritten, durably removes any old
receipt and binary generation, publishes and synchronizes all three verified
binaries, and publishes and synchronizes the receipt last. A termination
signal exits with its signal-derived status after private-workspace cleanup; it
cannot resume the build after cleanup.

The receipt binds the source, toolchain archive and front end, full stable Go
environment, linker flags, modes, sizes, and three binary SHA-256 digests. It
is explicitly unsigned and records:

- `authoritative: false`;
- `production_ready: false`;
- `reproducible_double_build: true`;
- `distinct_absolute_source_roots: true`;
- `isolated_build_caches: true`;
- `independent_toolchain_extractions: true`;
- `go_subprocess_environment_allowlisted: true`;
- `receipt_published_last: true`;
- `root_install_performed: false`; and
- `package_signature_verified: false`.

Byte equality proves determinism for the recorded source, verified archive,
target, flags, and closed Go-subprocess environment. It does not authenticate
who downloaded the archive or built the bundle, prove source review, establish
package authenticity, perform host installation, or prove production behavior.

## Loaderless scratch-execution contract

The OCI image is based on `scratch`, so an executable may not rely on a host or
image-provided ELF interpreter. The former PIE build emitted a `PT_INTERP` for
`/lib64/ld-linux-x86-64.so.2`; a scratch image contains no such loader and the
kernel therefore could not start that binary. The v2 build contract is now
`linux-amd64-static-et-exec-v1`: pure-Go `CGO_ENABLED=0`, Linux AMD64 `ET_EXEC`
linked by the pinned Go internal linker, with no `PT_INTERP`, dynamic section,
`DT_NEEDED`, executable stack, or writable-executable load segment.

`tools/verify-static-elf.sh` requires exact, pinned `/usr/bin/file` and
`/usr/bin/readelf` evidence for every controller, supervisor, and watchdog
binary in both independent builds and again at publication and staging. The
native build receipt records the complete positive contract, and each binary's
`--build-info-json` record binds its contract name. Package assembly parses the
ELF headers and program headers directly from the snapshotted executable bytes;
OCI materialization and verification repeat that byte-level check on all three
signed layer members. These are structural prerequisites for direct kernel
execution in scratch. Actual container execution remains a separate six-service
local-container gate and is never inferred from the unsigned build receipt.

This choice deliberately gives up a position-independent executable text
segment: Linux can still randomize stack, heap, mappings, and runtime allocation,
but the main `ET_EXEC` text mapping has a fixed address. A loaderless static PIE
would require adding an external linker/static runtime to the pinned build
inputs. That larger supply-chain and ABI surface is not introduced implicitly;
changing it requires a separately pinned, reviewed contract and reproducibility
proof.

## Executable behavior

The native bundle is deliberately fail-closed by default. `--build-info-json`
and `--self-test` return zero and emit exactly one ASCII canonical-JSON record:
keys are lexicographically sorted, separators are compact, the record has one
trailing LF, and stderr is empty. The record says that the component is
non-authoritative, not production-ready, and performs no effects.
`--self-test` remains a metadata/entrypoint sanity check. It is not an
authority, transport, containment, or PID1 test.

Legacy operational modes accept only the fixed v2 grammar. They validate the local
bootstrap boundary, close owned descriptors, write no lifecycle response,
perform no network call or target mutation, remain silent on stdout and stderr,
and exit `50`, the protocol/authentication-failure class:

- the workflow supervisor accepts only `release-preflight` or `release-run`,
  adopts only FIFO read descriptor 9, sets `FD_CLOEXEC`, bounded-reads one
  bearer with exactly one trailing LF, closes the descriptor, and zeroes both
  read and retained bearer buffers;
- the systemd broker accepts only its fixed server/config/socket-activation
  argv and performs a quarantine-only receive on fd 0: it requires a connected
  `AF_UNIX`/`SOCK_STREAM` with `SO_PASSCRED`, binds the first
  `SCM_CREDENTIALS` to `SO_PEERCRED`, atomically adopts exactly one anonymous
  `O_WRONLY` response-pipe descriptor from byte zero, consumes one bounded
  terminal request frame under one monotonic deadline, strictly parses the
  closed `propertyquarry.release-request.v2` syntax, revalidates and closes the
  descriptor, zeroes retained request buffers, and still refuses without
  writing a response;
- the controller accepts only the fixed config, closed operation, response-fd,
  event-ID, and lowercase SHA-256 digest grammar, validates the response as an
  `O_WRONLY` FIFO, marks it non-inheritable, and closes it without writing; and
- the watchdog accepts only its fixed config path, never sends `READY=1`, and
  refuses to supervise recovery without authenticated authorities.

Unconfigured invocations and every legacy mode retain that exact silent exit
`50` behavior. They cannot accidentally select the Docker runtime.

### Explicit installed-local-authority mode

The Docker-native phase-A runtime is selected only by these exact arguments:

```text
propertyquarry-release-supervisor-v2 --installed-local-authority --docker-broker
propertyquarry-release-supervisor-v2 --installed-local-authority --request-smoke
propertyquarry-release-supervisor-v2 --installed-local-authority --docker-restart-stimulus
propertyquarry-release-watchdog-v2 --installed-local-authority --health-json
propertyquarry-release-watchdog-v2 --installed-local-authority --docker-watchdog
```

Before binding or reporting ready, both components authenticate the retained
payload and independently audit all 19 active installation roles under the
contract in `PROPERTYQUARRY_RELEASE_LOCAL_AUTHORITY_V2.md`. They require the
fixed external Docker-secret anchor, the signed canonical authentication
record, an exact descriptor-walked retained payload tree, matching active
bytes/modes/owners, and a stable empty phase-A state directory. Any unknown,
missing, writable, linked, replaced, raced, or mismatched material fails before
readiness.

The installed supervisor owns
`/run/propertyquarry-release-control-v2/request.sock`. It enables
`SO_PASSCRED` before accepting clients, consumes the existing closed framed
transport, requires the peer UID/GID to equal its runtime identity, and
revalidates the complete installed authority before every child. It then opens
the fixed controller without following symlinks, verifies its authenticated
metadata and digest, and executes that pinned file descriptor with only the
validated response pipe. A timeout, installation drift, or unexpected child
exit is terminal so Docker restart policy can act. Malformed connections are
closed without taking down the broker.

The one-shot watchdog health command emits one bounded canonical JSON record
only after both installation and socket verification succeed. Its persistent
mode emits the same initial readiness record, revalidates on a bounded poll,
and exits nonzero on any indeterminate or terminal result. It does not use
systemd notification. The health record states that local package
authentication is authoritative while release effects, production readiness,
and public launch authority remain false.

The fixed controller deliberately continues to close the response pipe and
exit `50` without a response or release effect. Installed-local-authority mode
therefore establishes an authenticated persistent Docker process boundary; it
does not authorize a release mutation.

The explicit `--request-smoke` mode is a bounded same-credential client for that
local boundary. It sends the fixed canonical conformance frame and one anonymous
response-pipe descriptor, requires zero response bytes followed by EOF, and
reauthenticates the installation and socket before returning success with no
output. It neither creates state nor turns controller EOF into a signed release
disposition. It exists so the scratch image can exercise `SCM_RIGHTS` without a
shell or auxiliary client binary; every unlisted supervisor mode still exits
`50` silently.

The explicit `--docker-restart-stimulus` mode is valid only in a non-PID-1
exec process beside the authenticated installed supervisor. It authenticates
itself, the live socket, and `/proc/1/exe`, then signals container PID 1 with
the dedicated handled `SIGUSR2` so Docker's restart policy can be proved without a manual container
stop. The container terminates the silent exec process; this local broker crash
is not a release effect.

The local transport's first receive is exactly one byte with
`MSG_CMSG_CLOEXEC`; all subsequent header, body, trailing-byte, and EOF reads
remain `recvmsg` calls. It rejects late or malformed rights, mismatched
credentials, non-pipe or aliased response descriptors, nonterminal frames, and
non-closed JSON. It retains distinct exact-body, canonical-body,
canonical-envelope, and signature-payload digests internally. Equality between
the claimed and derived envelope digests is syntax metadata only and never
establishes authentication.

It still does not implement the workflow-client sender, authenticate a request
signature or OIDC identity, apply a complete PID-stability/cgroup policy, write
a response, perform a release effect, or produce a signed disposition. The Go
runtime also starts before application descriptor validation. The local peer
credential and authenticated installation permit only bounded controller
quarantine execution; they are not promoted to request-signature authority.

## Proof labels

`tools/stage-verify.sh` strictly parses a closed reproducible-build receipt,
rejects duplicate/unknown/non-finite/oversized data, matches its hashes, sizes,
modes, source digest, toolchain, environment, linker flags, and complete static
ELF contract to all three binaries. It runs only the pinned `file`/`readelf`
structural verifier on those bytes. It never executes an input-bundle binary: a
`/tmp` bundle and its
unsigned receipt are untrusted byte inputs, even when internally consistent.
The trusted reproducible-build lane executes each freshly built binary and
checks its canonical build information before publication; stage verification
does not repeat that trust-sensitive action on caller-supplied paths. It copies
the binary bytes and exact seven package templates into a new isolated `/tmp`
root and runs `systemd-analyze verify --root` with explicit placeholder target
units. Its receipt binds the SHA-256 of every binary and template plus the build
receipt and records the systemd major version. Such results prove only static
byte/metadata/unit-path compatibility in that staged root, not binary or builder
authenticity. They retain
`authoritative: false`, `production_ready: false`,
`root_install_performed: false`, `package_signature_verified: false`, and
`placeholder_targets_used: true`. No staged or self-test receipt is a
production release receipt.

## Phase-B release-effect blockers

Release effects remain forbidden until independently reviewed code and the
local Docker authority implement and prove all of the following:

- GitHub OIDC retrieval and JWT/JWS/JWKS verification with issuer, audience,
  immutable identity, key-rotation, redirect, DNS, and trusted-clock policy;
- mTLS request, lifecycle-CAS, evidence, ledger, and response authorities with
  closed media types and signature algorithms;
- the exact RootPolicy and decision-policy authentication and digest profiles;
- durable encrypted replay/ready state and lookup-first admission recovery;
- all seven fence-enforcing target mediators and idempotent mutation protocols;
- signed evidence persistence, file and directory fsync, terminal framing,
  verifier receipts, containment, cgroup-empty proof, watchdog takeover,
  lease/fence renewal, reconciliation, and rollback; and
- an independently built and locally authenticated OCI package, root-owned
  active roles and external Docker-secret anchor, and hostile PID1 end-to-end
  acceptance receipts.

Until those proofs exist, every controller invocation continues to exit `50`
without a response frame or effect. Supervisor/watchdog readiness proves only
the authenticated phase-A Docker runtime described above.
