# PropertyQuarry release local transport v2

Status: repository-owned, non-authoritative wire and process-topology
conformance contract. This document does not install an authority, authenticate
GitHub, verify a production signature, provision a credential, or extend
protocol v1.

## Decision

The protected release uses three distinct process roles:

1. `/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2`
   is the unprivileged workflow client. It consumes the GitHub OIDC bearer,
   exchanges that bearer for one exact externally signed request, connects to
   the fixed local socket, concurrently drains the outer response pipe, and
   verifies the resulting exact frame through an external/root-policy-bound
   verifier.
2. The same installed supervisor binary in fixed `--server-broker` and
   `--socket-activation` mode is the trusted per-request broker started by
   systemd. It authenticates the signed request, resolves every trusted input,
   and supervises the fixed controller process.
3. `/usr/libexec/propertyquarry-release-control/propertyquarry-release-controller-v2`
   is the broker's fixed worker. It owns the durable release transaction and
   constructs the signed lifecycle response. It is never selected or directly
   launched by the workflow client.

This split is mandatory. With `Accept=yes`, systemd—not the workflow client—is
the controller service's parent and cgroup owner. The workflow client cannot
`waitpid` that service, observe its process status, reap it, stop its cgroup, or
prove the cgroup empty. A design that treats the workflow client as the direct
controller supervisor is therefore nonconformant.

The socket is request transport only. Authority is returned only through the
supervisor-created anonymous response pipe.

## Fixed endpoints and modes

The client connects only to:

```text
/run/propertyquarry-release-control-v2/request.sock
```

No argument, environment variable, checkout file, symlink, discovery response,
or request field may select another path, executable, configuration, policy,
trust root, candidate, manifest, credential, or output.

The only workflow client operations are `release-preflight` and `release-run`.
`reconcile-run` is reserved for the installed controller/watchdog recovery
path. The supervisor has exactly two package-defined modes:

- `release-preflight|release-run`: unprivileged workflow client; and
- `--server-broker --config` followed by the fixed
  `/etc/propertyquarry-release-control/controller-v2.json` path and
  `--socket-activation`: server-side broker on systemd-provided fd 0.

The controller has one broker-only worker interface with fixed descriptor roles
and no public workflow mode.

Unknown flags, additional positional arguments, caller-selected descriptor
numbers, or mixed modes fail before request authentication or mutation.

## Outer request wire

The connected transport is Linux `AF_UNIX` `SOCK_STREAM`. The current package
uses `Accept=yes`, `StandardInput=socket`, and `PassCredentials=yes`; therefore
the accepted connected socket is fd 0 in the broker and may legitimately alias
stdin. stdout and stderr are bounded, redacted journald diagnostics and are not
the connection or an authority channel.

The workflow client performs these steps in order:

1. Create one anonymous pipe with `pipe2(O_CLOEXEC)`. Retain the read end and
   start bounded concurrent draining before the broker can fill the pipe.
2. Encode the request body as the exact 1 through 1,048,576 bytes returned by
   the external request authority. The body is the strict
   `propertyquarry.release-request.v2` JSON transport. It contains no OIDC
   bearer and no caller-selected preflight receipt, candidate, policy, policy
   digest, trust material, or credential.
3. Prefix the body with its four-byte unsigned big-endian length.
4. Send the first header byte with exactly one `SCM_RIGHTS` descriptor: the
   anonymous pipe's write end. The first positive `sendmsg` transfers that
   descriptor once. A retry before any positive transfer may repeat it; no
   later write may carry ancillary data.
5. Close the client's write-end copy immediately after successful descriptor
   transfer, send the remaining three header bytes and exact request body, and
   call `shutdown(SHUT_WR)`. The client may then close the request socket; it
   keeps the response read end open until the bounded operation resolves.

The broker's first read is `recvmsg` for exactly one byte with
`MSG_CMSG_CLOEXEC` and enough ancillary capacity to observe Linux's maximum
descriptor batch plus credentials. A normal `read`, stdio read, JSON parser, or
larger first receive is forbidden because it can discard ancillary data or lose
proof that the descriptor accompanied byte zero.

Every later read, including the trailing-byte/EOF probe, also uses `recvmsg`.
The broker rejects `MSG_CTRUNC`, `MSG_TRUNC`, out-of-band flags, unknown control
messages, malformed or duplicate credentials, zero or multiple rights, a right
attached after byte zero, any repeated right, a truncated header or payload,
an empty or oversized payload, trailing data, or failure to receive write EOF
before the absolute deadline. Every delivered descriptor is closed on every
rejection path.

The replay/request transport digest is `sha256:` of the exact request body. It
does not include the four-byte local header, descriptor number or inode, peer
PID, socket segmentation, or systemd instance name. The broker derives the
operation, request/event ID, nonce, canonical-envelope digest, and run identity
from the authenticated signed body; it does not accept unsigned duplicates of
those values from argv or a local wrapper.

The v2 request signature string is exactly
`ed25519-v2/<sha256-key-id>/<unpadded-base64url-raw-64-byte-signature>`.
The signed message is the ASCII domain
`propertyquarry.release-request-signature.v2` followed by NUL, the unsigned
64-bit big-endian length and canonical signature-payload bytes, then the
unsigned 64-bit big-endian length and canonical envelope bytes. The key ID is
the `sha256:` digest of the DER SubjectPublicKeyInfo for the one Ed25519 key in
the installed request-authority anchor. Any alternate prefix, key ID, padding,
encoding, key type, framing, or noncanonical root policy is a protocol failure.

An exact replay can return stored response bytes only after current OIDC/caller,
request-signature, and run-identity authentication succeeds and the digest
derived from the current installed RootPolicy exactly equals the digest in the
durable replay record and stored signed response. It may bypass trusted-clock,
lifecycle-head, and policy decision/freshness re-evaluation, but it never
bypasses authentication or immutable policy-digest continuity. Drift fails
closed before stored bytes are released. A successful replay forwards the
original bytes and original policy binding; it never re-signs a response under
a changed policy or exposes one to an unauthenticated holder of request bytes.

## Peer credentials

The broker requires `SO_PASSCRED=1`, snapshots `SO_PEERCRED` before consuming
the request, and requires the first kernel `SCM_CREDENTIALS` to match it exactly.
Later credentials, when present, must repeat the same `(pid, uid, gid)` tuple.
A different writer on an inherited connected socket is rejected.

The filesystem socket ACL and kernel credentials provide local admission and
audit context only. They are not GitHub identity, request authentication, or
release authority. The reported GID is not evidence of supplementary membership
in `propertyquarry-release-callers`. Production code additionally binds the
peer PID to a stable pidfd/start-time/executable/cgroup observation according to
root-owned policy; PID numbers alone are not race-safe identity.

The workflow client independently verifies that it connected to the fixed
root/systemd-owned socket endpoint. Client-side `SO_PEERCRED` identifies the
listener/systemd boundary, not the later `Accept=yes` broker process.

## Outer response descriptor

The sole `SCM_RIGHTS` descriptor must be fd 3 or greater after receipt and must
be a unique anonymous pipe write end:

- `fstat` reports a FIFO and `F_GETFL & O_ACCMODE` is exactly `O_WRONLY`;
- native production validation reports anonymous pipefs, not a named FIFO;
- it is not a socket, regular file, directory, device, memfd, eventfd, named
  FIFO, pipe read end, or `O_RDWR` FIFO;
- it does not share descriptor identity with fd 0, 1, 2, the request socket, or
  any other accepted descriptor; and
- `FD_CLOEXEC` was set atomically by `MSG_CMSG_CLOEXEC`, not repaired after a
  fork/exec race.

The broker snapshots device, inode, mode, access direction, and anonymous-pipe
identity on receipt and revalidates them immediately before use. It owns and
closes the received descriptor exactly once.

The Unix socket, stdout, stderr, journal, exit text, and GitHub outputs never
carry authority. They may carry only bounded redacted diagnostics or a
non-authorizing transport status.

## Broker and worker boundary

After complete local framing, EOF, peer-policy validation, and
request-signature/OIDC/run-identity verification, the broker opens the fixed
root-owned installed RootPolicy and derives its canonical, versioned,
domain-separated digest as specified by the lifecycle contract. It compares
that independently derived value with any durable replay and ready-preflight
binding and authenticates the decision/check-definition/trust-policy artifact
named by the RootPolicy against the actual evaluator/verifier configuration
before replay handling. Only an authenticated exact match may release a stored
response. For a new request, only after those comparisons may the broker
resolve the remaining trusted inputs. The workflow client never passes a
candidate, manifest, policy, policy-digest, decision-policy, trust-root, or
credential descriptor, and a request field cannot become the broker's expected
digest.

Before starting the worker, the broker opens or obtains the exact root-policy,
request, immutable candidate, and manifest objects from fixed root-owned or
authenticated external sources. It verifies their issuer, digest, purpose,
environment, and request bindings, then presents fixed, non-inheritable,
read-only descriptor roles to the worker together with a broker-created inner
response pipe. The worker bootstrap adopts those fixed roles before any library
that may create a thread, fork, or exec, and closes every descriptor outside the
explicit allowlist. Private credentials remain least-privilege broker/worker
runtime inputs and never cross the outer client socket.

The current native bootstrap fixes fd 3 as the private inner response writer,
fd 4 as the already-open pinned controller executable, and fd 5 as an anonymous
read pipe containing the exact authenticated request bytes followed by EOF.
The broker transfers fd 5 under the same absolute child deadline. The
controller reparses those bytes, recomputes the full transport digest and event
ID, and independently revalidates the installed package, request anchor, root
policy, signature, identity, and validity window. Caller-derived argv fields
cannot substitute for that descriptor verification.

The broker self-executes the one fixed controller binary in private worker mode;
the request cannot select it. The worker remains in the non-delegated systemd
unit cgroup. The broker:

- concurrently drains the inner response and bounded redacted diagnostics;
- observes the worker using pidfd/wait semantics, not PID polling;
- enforces fixed absolute worker, response, EOF, callback, cleanup, and broker
  deadlines that never extend on retry or progress;
- validates the worker result, operation, request bindings, lifecycle state,
  external ledger/CAS state, evidence durability, and mapped status;
- terminates every descendant on failure and proves that the unit cgroup
  contains only the broker before any successful outer response; and
- relies on systemd `KillMode=control-group`/runtime expiry as an independent
  final containment layer if the broker itself dies.

The controller constructs, signs, content-addresses, persists, file-fsyncs, and
directory-fsyncs its exact inner frame before writing it. The broker retains
those exact bytes, waits for the real controller status, validates the fixed
exit/class/operation mapping, kills and reaps every controller descendant, and
proves that no controller descendant remains. It then forwards the exact inner
frame to the outer pipe without parsing and re-encoding it. Exact retry returns
the stored bytes byte-for-byte.

The broker keeps the outer write descriptor open until a raw process exit with
the mapped class; it does not close it in an ordinary destructor path. The
client therefore accepts neither a payload without EOF nor EOF without the
complete frame. Because the broker itself remains in the unit cgroup while
forwarding, the honest pre-forward invariant is “controller descendant set
empty,” not “entire unit cgroup empty.” PID 1 proves the unit cgroup empty after
the broker exits; a stricter pre-forward whole-cgroup proof would require a
separately managed worker cgroup and external witness.

The broker writes exactly the existing lifecycle response transport: four
unsigned big-endian length bytes plus one strict UTF-8 JSON object of 1 through
1,048,576 bytes, then closes the outer pipe. The client retains and verifies
those exact bytes without re-encoding. The signed/full-verification context
binds at least the operation, event/request ID, exact request-body digest,
canonical-envelope digest, exact outer-frame digest, controller/broker binary
digest, exact installed root-policy digest, lifecycle seal, and broker-observed
worker result.

The client-side full verifier receives the expected policy digest only through
a trusted installed-policy context established independently of the outer
frame. It requires exact equality with the digest signed into the response and,
for `release-run`, with the persisted ready-preflight, replay, admission, and
lifecycle bindings. A valid signature with a missing, stale, request-derived,
or mismatched policy digest remains non-authorizing.

## Deadlines, cancellation, and ambiguous completion

One monotonic absolute client deadline covers request exchange, connect, send,
response drain, EOF, external verification, and authenticated ledger lookup.
Independent stage caps may be shorter but never extend the absolute deadline.
The worker deadline is shorter than the broker deadline; the broker deadline is
shorter than systemd `RuntimeMaxSec`; and the workflow client/job deadline is
longer than the systemd cap plus ledger-reconciliation margin.

The package schema currently caps the controller operation at 9,600 seconds,
the systemd request unit at 9,900 seconds, and the workflow job at 10,800
seconds. Root policy must choose smaller stage limits that retain explicit
cleanup, signing, EOF, and authenticated-ledger margins inside those caps.

Closing the client socket or response reader is not cancellation and does not
stop the service. After authentication or possible admission, client loss,
timeout, signal, missing frame, or verifier failure requires authenticated
external-ledger lookup and lifecycle reconciliation. The broker follows its
trusted deadline and reaches a signed terminal state or containment outcome.

No frame is eligible when it is missing, malformed, truncated, repeated,
unsigned, unbound, not externally verified, inconsistent with the worker result,
or emitted before durable persistence/cgroup proof. The client never infers
non-admission from EOF or a socket error.

## Package and conformance consequences

The existing 19 installation roles remain unchanged. No fourth executable is
introduced. The systemd request service invokes the installed supervisor in
fixed server-broker mode; the workflow invokes the same supervisor only as
`propertyquarry-release-supervisor-v2 release-preflight|release-run`; and the
broker alone launches the fixed controller worker.

The socket unit must not use directives that are ineffective or incompatible
with `Accept=yes` as security claims. `Backlog`, `MaxConnections`, trigger
limits, and caller-group ACLs are local availability controls only; external
replay/CAS and target fencing remain mandatory.

Repository models may validate framing, descriptor ownership, exact-byte
digests, status mappings, and hostile process behavior. They remain explicitly
non-authoritative. Production conformance still requires an independently
built and signed package, real systemd/PID1 fault tests, external authorities,
all seven target-side fence mediators, real watchdog takeover, and signed
terminal receipts.
