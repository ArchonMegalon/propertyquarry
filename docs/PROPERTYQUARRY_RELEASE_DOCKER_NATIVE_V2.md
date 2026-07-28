# PropertyQuarry Docker-native release-control v2

Status: authenticated local package/runtime bootstrap with no release effects.
It is not a Docker lifecycle actuator and never receives `docker.sock`.

## Authority boundary

The installed Docker runtime trusts exactly one external bootstrap anchor:

```text
/run/secrets/propertyquarry-package-authority-v2.pem
```

It must be a no-follow, single-link, root-owned `0444` regular file no larger
than 4096 bytes and contain exactly one Ed25519 SubjectPublicKeyInfo PEM. The
derived SHA-256 key ID must match both the signed authentication record and the
active package-authority role.

The image retains the exact payload at:

```text
/usr/share/propertyquarry-release-control-v2/local-authority/payload/
```

The canonical authentication JSON and raw 64-byte signature are its siblings:

```text
/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.json
/usr/share/propertyquarry-release-control-v2/local-authority/authentication.v2.sig
```

The payload copy is authenticated first. The 19 live absolute paths are then
audited independently against its installation manifest. A valid retained copy
cannot mask a changed live executable, config, policy, schema, unit template,
or trust root.

## Phase-A writable paths

Only these runtime directories exist:

```text
/var/lib/propertyquarry-release-control-v2
/run/propertyquarry-release-control-v2
```

The state directory is a pinned no-follow `0700` directory owned by authority
UID `65532` and the authenticated service GID. It contains only immutable,
canonical `0600` replay-claim files whose names are the SHA-256 digest of their
canonical bytes. Each claim binds the verified request trust-root key ID,
request ID, nonce, and canonical envelope digest. The supervisor opens and
locks the directory descriptor, rejects every unknown, linked, noncanonical,
misowned, or internally duplicated entry, and rejects request-ID and nonce
reuse independently. It creates a claim with `O_EXCL`, writes and fsyncs the
file, reads it back, fsyncs the directory, and revalidates the complete closed
state before starting the controller. A crash after that commit therefore
leaves the request consumed. The controller independently reauthenticates the
transferred request and may only adopt the exact preexisting supervisor claim;
adoption never writes or consumes a second record.

The runtime directory is a pinned no-follow `0750` directory owned by authority
UID `65532` and the shared service GID. It is either empty or
contains exactly one validated `request.sock` left by an abrupt prior death.
The supervisor refuses unknown entries, links, wrong metadata, and a live
listener. Before a directory-relative unlink, it twice validates the same
same-owner mode-`0660` socket around a local `ECONNREFUSED` probe and revalidates
the owning mode-`0750` directory descriptor. It then requires the same directory
object to be empty before rebinding. Authority UID `65532` owns the
denial-of-service boundary; caller UID `65534` can traverse and connect through
the shared group but cannot rewrite that directory. The supervisor creates only
`request.sock` and removes it on an orderly stop. This bounded recovery permits
restart after `SIGKILL` without taking over a live listener.

The persistent Compose plane supplies a local-driver tmpfs socket volume and a
separate versioned local named volume, `authority-replay-state-v1`, for replay
state. The socket volume is mounted into both services with
`uid=65532,gid=<authenticated service GID>,mode=0750,nosuid,nodev,noexec`. The
state volume has no tmpfs driver options and is mounted only into the
supervisor. Its first mount deliberately permits image copy-up of the empty
authority-owned `0700` directory; subsequent mounts must retain the exact
native-validated metadata and closed claim set. The versioned name prevents an
older tmpfs-backed `runtime-state` volume from being reused during upgrade. The
caller/watchdog service has no state mount and cannot traverse the inert
authority-owned state directory in the image. The external public anchor is a
separate read-only bind at its fixed filename. The authenticated retained
wrapper and all 19 active roles remain in the read-only image. Native processes
accept no environment or path override for these locations.

## Process model

Compose runs the supervisor as PID 1 with this complete invocation:

```text
/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2 --installed-local-authority --docker-broker
```

The broker is sequential and bounded. It rejects wrong peer credentials,
malformed ancillary data, missing/multiple/late descriptors, nonterminal
frames, oversized JSON, digest mismatch, and every installation race. For a
valid local frame it reauthenticates the installation, pins the controller
inode by an open descriptor, passes only the anonymous response pipe and pinned
executable, and waits under a fixed deadline. The controller still exits `50`
without writing or performing an effect. The socket is group-accessible and
uses `SO_PASSCRED`, but framed requests are admitted only from exact caller UID
`65534` with the authenticated primary service GID. Authority UID `65532` is
explicitly rejected as a caller. Each accepted connection carries one bounded
frame with exactly one anonymous `O_WRONLY` response-pipe descriptor in the
first byte's `SCM_RIGHTS` message.

Compose runs the watchdog separately with:

```text
/usr/libexec/propertyquarry-release-control/propertyquarry-release-watchdog-v2 --installed-local-authority --docker-watchdog
```

Its healthcheck uses:

```text
/usr/libexec/propertyquarry-release-control/propertyquarry-release-watchdog-v2 --installed-local-authority --health-json
```

Readiness JSON is emitted only after the package, active roles, socket metadata,
and socket accept path verify. Generic package/watchdog validation never
traverses authority state. The one-shot health command and
the persistent watchdog's single initial stdout record are identical canonical
ASCII JSON with sorted keys, one trailing LF, and empty stderr:

```json
{"authentication_digest":"sha256:<authentication>","authoritative_for_package_authentication":true,"authoritative_for_release_effects":false,"authority_key_id":"sha256:<key-id>","component":"propertyquarry-release-watchdog-v2","installed_local_authority_verified":true,"payload_tree_digest":"sha256:<payload-tree>","performs_release_effects":false,"production_ready":false,"ready":true,"schema":"propertyquarry.release-control.local-runtime-health.v2","socket_accepting":true,"source_manifest_digest":"sha256:<source-manifest>","version":2}
```

Health makes one ephemeral socket connection. The broker treats its empty EOF
as connection-local malformed input; the listener remains ready and the socket
identity is rechecked. The persistent
watchdog emits no further stdout and exits `50` on anchor, retained-package,
active-role, or socket drift. Before readiness it installs an inotify
watch on the exact runtime directory and pins the initial socket identity; any
queued unlink, replacement, move, attribute change, directory loss, unmount, or
watch overflow is terminal even when supervisor replacement occurs entirely
between five-second authority polls. One-shot health connections do not mutate
the directory and cannot clear or mask these events. Graceful SIGINT/SIGTERM
exits zero. Both services use bounded `restart: on-failure:3`; persistent drift
therefore ends in a visible failed plane rather than an endless restart loop.

The no-effects request smoke is a separate, exact supervisor invocation:

```text
/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2 --installed-local-authority --request-smoke
```

The governed container gate invokes it inside the running watchdog container
with the exact sealed, non-TTY Compose shape:

```text
/usr/bin/docker --host unix:///var/run/docker.sock compose --progress quiet --project-name <validated-random-project> --file /proc/<gate-pid>/fd/<sealed-compose-fd> exec -T release-watchdog /usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2 --installed-local-authority --request-smoke
```

This executes the fixed smoke client binary as caller UID `65534` with the
shared primary service GID while the listener remains authority UID `65532` in
the sibling supervisor service. The gate requires exact empty stdout/stderr and
revalidates the public anchor and sealed Compose input before and after the
exec. The smoke command
authenticates the same installation and live socket, sends the fixed
canonical conformance request and one response pipe as the caller UID/GID,
requires bounded EOF with zero response bytes, then reauthenticates the package
and socket. Success is exit `0` with empty stdout/stderr. This proves the
peer-credential/frame/descriptor/controller-quarantine path remains fail-closed;
EOF is not promoted to a signed disposition or release response. Any failure is
silent exit `50`.

The lifecycle gate's separate restart stimulus runs only through sealed
non-TTY Compose exec in the live supervisor container:

```text
/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2 --installed-local-authority --docker-restart-stimulus
```

It rejects container PID 1, an unavailable socket, or any self/PID-1/package
authentication mismatch. From a verified exec process it sends the dedicated
handled `SIGUSR2` to
the authenticated supervisor at container PID 1, then emits nothing while the
container terminates it with exit `137`. This proves Docker's bounded
`on-failure` recovery using a real process crash; Docker's manual
`container kill` is intentionally not used because manual stops suppress the
restart policy. Killing this local no-effects broker is not a release effect.

## Containment contract

The runtime requires:

- a read-only root filesystem;
- all Linux capabilities dropped and `no-new-privileges` enabled;
- no host port;
- no application/database/provider/public-tour volume;
- no raw Docker Engine socket;
- a dedicated internal network only if a later local mediator requires it;
- an authority-private state volume plus the shared-group socket volume with
  the exact ownership above; and
- runtime GID equal to the private-role GID projected by the authenticated
  manifest.

The broker performs no network request and no release mutation in phase A.

## Verification

The native Go suite covers valid startup, fixed-controller fork/EOF behavior,
one-shot and persistent watchdog readiness, the exact request-smoke lifecycle,
signature and anchor substitution, noncanonical JSON, payload
extras/symlinks/mode drift, active byte/hardlink drift, explicit authority-state
pollution, concurrent duplicate replay admission, independent request-ID and
nonce reuse, every claim-binding mismatch, partial-crash and hostile-state
recovery, non-consuming controller adoption, caller/authority peer separation,
descriptor/credential attacks, controller replacement, stale-socket restart,
live-listener takeover refusal, and terminal watchdog exit on anchor,
retained-package, or socket drift. The pinned build lane reruns the full Go
suite in two independent source roots before publishing reproducible binaries.

These proofs establish an authenticated Docker-native quarantine boundary.
They do not establish request-signature authority, a lifecycle mediator,
release effects, rollback, or production readiness.
