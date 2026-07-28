# PropertyQuarry local authenticated OCI lane v2

Status: implemented daemonless materialization and a separate, hardened,
verification-only compose plane. Materialization does not contact or modify
Docker. Loading and running the verified image are explicit later operations.
This phase does not install or start a persistent controller, supervisor,
watchdog, broker, state service, socket service, or secret service.

## Authority boundary

The input is the exact phase-A authenticated wrapper, not an unsigned payload:

```text
WRAPPER/
  payload/                    # exact 21-file package payload
  authentication.v2.json      # canonical local-Docker authority statement
  authentication.v2.sig       # raw 64-byte Ed25519 signature
```

The materializer calls
`propertyquarry_release_authenticated_package.verify_wrapper()` with a
separately supplied external Ed25519 SPKI anchor. It requires the frozen
`propertyquarry.release-control.local-package-authentication.v2` schema and the
`propertyquarry-local-docker` scope. The statement must bind exactly 21 payload
files, 19 installation roles, the complete payload-tree digest, the
installation manifest, package receipt, and native build receipt.

After phase-A verification, the materializer independently descriptor-snapshots
the exact 21-file payload with the fixed directory modes, recomputes phase A's
domain-separated tree digest from that captured snapshot, and requires it to
equal the signed value. It repeats the full authority verification and snapshot
after image audit, closing the verify/read path-swap boundary.

The private signing key is never an input to this lane. The external public
anchor is verified in place and is not copied into the image. The authenticated
payload's packaged package-authority root, the authentication key ID, and the
external anchor are already required to match by phase A.

## Daemonless materialization

Create the OCI image without a daemon, base image, build context, subprocess,
or network implementation:

```sh
python3 scripts/propertyquarry_release_oci_materializer.py materialize \
  --wrapper /absolute/private-parent/authenticated-wrapper \
  --external-anchor /absolute/state/anchors/package-authority-v2.pem \
  --output /absolute/private-parent/propertyquarry-release-control-image-v2
```

The output parent must exist, have no group/other write bit, and the output must
not exist. Publication uses a private same-parent temporary directory,
descriptor-backed writes, fsyncs, complete revalidation,
`renameat2(RENAME_NOREPLACE)`, and a final descriptor-rooted audit of the
published directory. If that final audit fails, rollback removes the published
tree only when its parent entry still identifies the exact directory created by
this invocation; a substituted entry is never removed.

The first complete staged audit freezes the device/inode identities of the
output root, `blobs`, `blobs/sha256`, every top-level file, and all three blobs.
The materializer requires those exact objects before rename, after rename, and
again after the parent-directory fsync. Replacing a file or directory with a
new object containing identical bytes and modes is therefore a failure, not a
successful publication.

The successful output is exactly:

```text
OUT/
  oci-layout
  index.json
  installation-manifest.v2.json
  materialization-receipt.v2.json
  control-plane.compose.yml       # exact compose bytes bound by the receipt
  docker-image.tar                 # deterministic Docker-load archive, 0600
  blobs/sha256/<config>
  blobs/sha256/<manifest>
  blobs/sha256/<uncompressed-layer>
```

The OCI image is a single-layer `linux/amd64` scratch image. It has no default
entrypoint or command, so an accidental bare run fails closed. Because scratch
provides no ELF loader or shared libraries, all three native members must satisfy
the authenticated `linux-amd64-static-et-exec-v1` contract: AMD64 `ET_EXEC`,
statically linked, with no `PT_INTERP`, dynamic section, `DT_NEEDED`, executable
stack, or writable-executable load segment. Materialization parses this contract
from the signed layer-member bytes, and standalone verification recomputes it
instead of trusting the unsigned materialization receipt. Its configuration
contains only a fixed PATH, the numeric authority user `65532` with the
manifest-projected service group, and public digest labels. The unsigned
receipt separately records caller/watchdog UID `65534` with the same primary
group. It has no creation timestamp, history, source path, environment
credential, hostname, build context, or external anchor bytes.

The active 19 paths receive the manifest's exact numeric uid/gid and modes. The
complete authenticated package is retained for local runtime revalidation at:

```text
/usr/share/propertyquarry-release-control-v2/local-authority/payload/
/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.json
/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.sig
```

Those retained trust files are public SPKI anchors, not private keys. The
materializer rejects standard PEM/OpenSSH private-key markers anywhere in the
authenticated payload. The external native anchor remains a separate runtime
secret mount at
`/run/secrets/propertyquarry-package-authority-v2.pem`, root:root mode `0444`;
it is never baked into the image.

Phase A excludes the payload root itself from its tree digest. The image projects
that single retained-package traversal boundary as root:root `0755`, exactly as
the frozen native installed-authority verifier requires; all authenticated
descendant modes remain unchanged, and private descendants stay restricted to
the projected runtime group.

The image also seeds an empty
`/var/lib/propertyquarry-release-control-v2` directory as authority UID `65532`,
the manifest-projected service GID, and mode `0700`. The persistent runtime's
versioned `authority-replay-state-v1` named volume deliberately permits
first-mount copy-up of that metadata, then retains only native-validated
canonical replay claims. This is not a baked-in ledger: the image directory is
empty, and the caller/watchdog service never mounts the replay volume.

## Independent image audit

The layer builder emits only canonical USTAR headers and two terminal zero
blocks. A separate parser then re-reads every raw header and byte range. It
rejects non-USTAR metadata, timestamps, link targets, symlinks, hardlinks,
devices, PAX records, duplicate/unsafe paths, nonzero padding, extra files,
owner/mode drift, and digest drift. It independently matches every active role
against the installation manifest after the image files are written and again
after atomic publication.

Every output audit starts from one pinned root descriptor, opens `blobs` and
`blobs/sha256` descriptor-relatively with no-follow semantics, and requires the
exact directory and file sets. Intermediate symlink substitution, extra names,
renamed directories, and descriptor/name identity drift therefore fail closed.
The post-publication audit uses that same exact-tree discipline before success
is returned.

The Docker-load archive embeds the exact same audited config and layer bytes.
Its digest, OCI config image ID, OCI manifest image digest, layer digest,
authentication JSON/signature digests, authenticated tree digest, three package
digests, compose digest, authority and caller uid:gid values, and the positive
active-role audit are bound in `materialization-receipt.v2.json`.

Re-audit without Docker:

```sh
python3 scripts/propertyquarry_release_oci_materializer.py verify \
  --output /absolute/private-parent/propertyquarry-release-control-image-v2 \
  --wrapper /absolute/private-parent/authenticated-wrapper \
  --external-anchor /absolute/state/anchors/package-authority-v2.pem
```

Standalone verification requires the original wrapper and external anchor so it
rechecks the Ed25519 authority instead of trusting the unsigned materialization
receipt by itself.

## Separate verification-only compose plane

`compose.propertyquarry-release-control-v2.yml` is intentionally separate from
the canonical application compose file. The materialized output contains the
exact verified copy as `control-plane.compose.yml`. It defines only three native
`--self-test` services and three operational-refusal checks. These six services
are one-shot tests; they are not persistent runtime services and do not prove
that any installed controller loop, supervisor loop, watchdog loop, broker,
state store, runtime socket, or secret-delivery service exists. Every service
has:

- `pull_policy: never` and an explicitly supplied local image ID;
- no network namespace (`network_mode: none`);
- a read-only root filesystem;
- all capabilities dropped and `no-new-privileges`;
- numeric non-root uid and the manifest-projected service gid;
- bounded PID, memory, and swap limits;
- no environment, app volume, Docker socket, device, host port, or extra host;
- no init process, interactive input, TTY, dependency, or restart policy.

The one-shot self-tests require exit `0`, empty stderr, and one ASCII canonical
native build-info JSON line with lexicographically sorted keys, compact
separators, and exactly one trailing LF, whose component matches the selected binary, whose source
manifest digest and scratch-execution contract match the native receipt, and
whose `authoritative`,
`production_ready`, and `performs_effects` fields are false while `self_test` is
true. The refusal checks require exit `50`, empty stdout/stderr, and no state
change. Compose 5.1.3 writes one line-feed byte to stderr when transporting an
otherwise silent exit-50 one-shot, so the supported gate uses Compose only for
the three self-tests and invokes the refusal entrypoints with direct hardened
`docker run` commands. The line-feed is not normalized or tolerated. These
assertions are runtime gates; a container merely starting is not a pass.

## Explicit local-Docker boundary

The opt-in gate below is the supported mutation boundary. It first re-runs the
daemonless OCI audit and Ed25519 wrapper verification, requires the exact native
build receipt authenticated by the image, loads the deterministic archive over
a local Unix Docker socket, inspects the immutable image ID, runs the three
Compose self-tests and three direct refusal checks, proves their temporary
containers are gone, and atomically publishes a hash-only success receipt:

```sh
python3 scripts/propertyquarry_release_local_container_gate.py \
  --layout /absolute/private-parent/propertyquarry-release-control-image-v2 \
  --wrapper /absolute/private-parent/authenticated-wrapper \
  --external-anchor /absolute/state/anchors/package-authority-v2.pem \
  --native-build-receipt /absolute/native/linux-amd64/build-receipt.json \
  --output-receipt /absolute/private-parent/local-container-gate.v2.json
```

This command mutates the local image/container store and was not run during the
implementation-only phase. It accepts only an absolute Docker client path and a
local `unix:///` socket, uses an empty temporary Docker configuration, passes no
operator credentials, forbids pulls, and records no Docker output or local
source paths. Before the first Docker command it snapshots the already-audited
compose and Docker archive bytes into sealed Linux memory files. Every Docker
command that consumes Compose or the archive receives only those immutable
descriptors, and both descriptors are revalidated before and after every Docker
command; the gate never reopens a mutable repository compose or published
archive pathname for execution. Independently of the unsigned
materialization receipt, the gate first requires the frozen canonical Compose
digest and parses an exact contract containing only the three self-tests and
three refusal tests. A coordinated rewrite of both the layout Compose and its
receipt digest therefore causes zero Docker-command calls. A native output,
exit-code, archive, image-ID, cleanup, immutable-input, or hash mismatch fails
without publishing a success receipt.

Each direct refusal command uses the immutable image ID and exact native
entrypoint/arguments with `--rm`, `--pull never`, no network, a read-only root,
all capabilities dropped, no-new-privileges, the same PID/memory/swap bounds,
numeric receipt user, and `/` as its working directory. It supplies no
environment, mount, port, device, TTY, or stdin option. A random gate label and
per-service container name isolate the three invocations; a final filtered
container query must return byte-empty output, including on refusal-validation
failure, before success can be published.

The equivalent manual boundary is shown only for operator diagnosis:

```sh
docker image load --input OUT/docker-image.tar
docker image inspect --format '{{.Id}}' 'sha256:<config digest from receipt>'

export PROPERTYQUARRY_RELEASE_CONTROL_IMAGE='sha256:<config digest from receipt>'
export PROPERTYQUARRY_RELEASE_CONTROL_USER='<runtime_user from receipt>'

docker compose -f OUT/control-plane.compose.yml \
  --profile self-test run --rm -T --no-deps controller-self-test
docker compose -f OUT/control-plane.compose.yml \
  --profile self-test run --rm -T --no-deps supervisor-self-test
docker compose -f OUT/control-plane.compose.yml \
  --profile self-test run --rm -T --no-deps watchdog-self-test
```

Do not use Compose refusal services to judge byte-exact stream silence; use the
scripted gate's direct-run lane and require exit `50` with truly empty stdout
and stderr. Never substitute a mutable tag for the receipt's
`sha256:<config digest>` image ID. These manual diagnostics do not reproduce the
gate's sealed-descriptor race protection and are not a success-receipt path.
The external anchor would be needed by a separately installed local-authority
operational runtime, which is outside this phase; it is not needed by the
effect-free native `--self-test` command.

## Verification

The deterministic implementation gate is:

```sh
pytest -q \
  tests/test_propertyquarry_release_oci_materializer.py \
  tests/test_propertyquarry_release_local_container_gate.py
```

It exercises deterministic double materialization, projected metadata, retained
authentication evidence, public-only config, private-key rejection, late input
mutation, intermediate-directory symlink substitution, post-rename mutation and
name substitution, same-byte inode and directory replacement across the parent
fsync boundary, layer tampering, exact authority scope, and the complete
six-test compose hardening contract. The local-container gate tests include a
real authenticated materialized-layout coordinated-rewrite rejection and use an
injected command runner to prove fail-closed
load/inspect/self-test/refusal/cleanup receipts plus immutable compose/archive
inputs and required Linux seals under adversarial pathname substitution. They
also assert the exact hardened direct refusal argv, rejection of the Compose
transport's stderr line-feed, and an empty unique-label cleanup query; they
neither load nor invoke Docker.
