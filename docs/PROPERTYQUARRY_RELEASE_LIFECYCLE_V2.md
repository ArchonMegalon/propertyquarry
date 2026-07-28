# PropertyQuarry release lifecycle v2

Status: repository-owned design and offline conformance contract. This document
does not install a controller, verify a signature, establish trust, grant
release authority, or make protocol v1 extensible.

## Decision

The protected release is one atomic operation owned by an independently built,
root-installed controller behind a systemd-owned supervisor broker. GitHub
Actions must not orchestrate deploy, live
verification, activation, overlay mutation, and final authority as separate
candidate-code jobs. A workflow may request a read-only preflight and one
`release-run`; the controller owns the complete transaction, its credentials,
its durable journal, and rollback.

Normal v2 operations are:

- `release-preflight`: read-only, non-authorizing, and incapable of lifecycle
  CAS or target mutation;
- `release-run`: a distinct fresh request that binds one exact ready preflight,
  acquires fencing before mutation, executes every release phase, and reaches a
  signed terminal state; and
- `reconcile-run`: controller/watchdog recovery for an already admitted,
  nonterminal lifecycle. It cannot start a release or roll back a sealed final
  lifecycle.

The repository-level, read-only authenticated preflight is invoked from the
canonical checkout with
`./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight`.
This repository proof does not replace or authorize the independently installed
controller operation described above.

Candidate and disposable-environment operations remain protocol-v1 concerns.
The v1 wire schema is closed and is not changed by this design.

## Trust boundary

The workflow-facing process has no Docker socket, database URL, traffic token,
provider credentials, persona credentials, signing key, trust root, or
controller-selecting environment variable. It invokes one fixed absolute
native entrypoint outside the checkout. The controller obtains secrets and
root-owned policy from its private runtime.

The controller resolves the immutable candidate from signed content digests;
it never executes a candidate checkout as release authority. Repository tools
may be independently rebuilt, pinned, and invoked by the controller, but
candidate bytes and GitHub artifacts are untrusted inputs until their exact
digests and issuers are verified.

## Immutable RootPolicy identity

Every request-authority, preflight, run, replay, admission, lifecycle, and
response-verification decision binds one immutable RootPolicy document opened
from its fixed installed, root-owned source. The installed broker/controller
obtains it with no-follow, exact-type, owner, mode, size, schema, and purpose
checks. The workflow identity, request transport, candidate checkout,
environment, arguments, and GitHub artifacts cannot provide, select, shadow,
replace, or override either the document or its digest. A service user must not
be able to write the source. Package installation evidence must bind the exact
source provenance separately from the digest described here.

The v2 reference-model policy document is the closed top-level JSON object with
exactly these keys and exact field types:

```text
schema = "propertyquarry.release-root-policy.v2"
identity = the closed RunIdentity object
required_checks = the ordered, nonempty check-name array
max_request_ttl = a positive integer
max_preflight_validity = a positive integer
decision_policy_digest = the exact sha256:<64 lowercase hex> digest of the
                         authenticated, closed
                         decision/check-definition/trust-policy artifact
```

Unknown or duplicate fields, non-finite numbers, type coercion, Unicode
surrogates, and noncanonical encodings are invalid. Its canonical bytes are the
strict recursive JSON encoding of that object with keys sorted
lexicographically, no insignificant whitespace, comma/colon separators, JSON
strings ASCII-escaped, and the result encoded as UTF-8. The one versioned,
domain-separated digest is exactly:

```text
canonical_policy = strict_canonical_json(root_policy_v2_document)
root_policy_digest =
  "sha256:" || lowercase_hex(
    SHA-256(
      ASCII("propertyquarry.release-root-policy-digest.v2") || 0x00 ||
      uint64_be(byte_length(canonical_policy)) || canonical_policy
    )
  )
```

The digest is derived from the independently obtained canonical policy bytes;
it is never trusted merely because a request or response names it. A production
schema revision requires a new document schema and digest domain. It may not
reinterpret bytes under this v2 domain.

Check names and TTLs are not sufficient policy semantics. The mandatory
`decision_policy_digest` is part of the canonical RootPolicy preimage and binds
the authenticated artifact that defines check behavior, decision rules,
accepted issuers/trust material, and verifier policy. Before evaluation or
verification, production code must authenticate the actual evaluator and
verifier configuration against that artifact and require its exact digest.
Merely configuring callbacks with matching check names is not proof of this
binding. The repository model injects evaluators/verifiers and does not
authenticate that artifact, so this production proof remains external and
unfulfilled by the model.

The exact digest is a mandatory continuity field. It is persisted in every
replay record and every ready-preflight record, signed into every response
(including non-ready, rejection, conflict, recovery, and terminal responses),
passed to and repeated by admission, and committed into the `admitted` CAS and
all lifecycle successors. Before a ready preflight is made durable, before a
stored replay response is released, and immediately before admission, the
controller recomputes the digest from the current installed source and requires
exact equality with every already-bound value. Missing provenance, unreadable
bytes, a changed file, a digest mismatch, or an internally inconsistent set of
bindings fails closed before replay, admission, or mutation.

These canonicalization and continuity rules describe the repository's
non-authoritative model. They neither authenticate the installed file nor turn
the current Python model into a production RootPolicy authority.

At minimum, one fencing domain covers these mutable resources together:

- database target and migration fence;
- runtime/Compose state;
- public ingress and traffic selection;
- evidence-overlay active pointer;
- public-tour volume state;
- launch-authority state; and
- monitoring and delivery proof state.

The domain identity binds the host, server-derived database identity, public
origin, and the canonical sorted resource set. Splitting those resources across
independent locks is nonconformant because two releases could interleave.

## Two external monotonic authorities

Replay protection and release state are separate.

The replay ledger atomically consumes every authenticated request ID and nonce,
including a signed rejection. Its durable record binds the exact RootPolicy
digest in addition to the request identity and response bytes. An exact retry
with identical transport bytes returns the stored signed response only after
the current caller, request signature, and exact run-identity binding
authenticate again and the current installed RootPolicy digest equals the
record's original digest. Replay bypasses clock, lifecycle-head, and policy
decision re-evaluation, not authentication or immutable policy-digest
continuity. It never re-signs a historical response under a changed policy:
success returns the byte-identical stored response with its original binding;
policy drift fails closed without returning stored bytes or repeating effects.
Reusing an ID or nonce with different bytes is rejected. An unauthenticated or
malformed transport grants nothing, reveals no stored response, and does not
create a release-state transition.

The lifecycle CAS is an externally signed, append-only hash chain. Every state
successor binds:

- authority, namespace, target, generation, previous seal, and complete state
  digest;
- lifecycle and epoch IDs;
- exact request transport and canonical-envelope digests;
- release, image, controller, manifest, and the exact installed RootPolicy
  digest;
- the immutable resource set;
- lease ID, holder, deadline, global fencing token, and a fencing token for
  every bound resource; and
- the immediately preceding phase and evidence digest.

The external authority atomically enforces event-ID uniqueness and hash-chain
append in one transaction. A restart cannot replay an event ID onto a different
head or append the same transition twice.

Returning an existing lifecycle record also requires the exact immutable
command context recorded for that event: the complete lifecycle binding and
its policy, controller, resource set, epoch, lease identity, and
effect-specific proof, renewal, recovery, or outcome inputs. Event ID, request
digest, kind, and phase alone are insufficient. A fresh trusted-clock
observation and the caller's now-stale CAS predecessor are deliberately not
part of this replay comparison, so a lost-response retry can recover the
effect-free stored result after time or the head advances. Substituting any
authority-bearing context fails closed instead of returning the old record.

A local journal is only a cache. Missing, restored, forked, or advanced cache
state requires reconciliation against the external authority.

## Preflight and admission

`release-preflight` consumes only its replay-ledger nonce. Its signed response
and durable ready-preflight record bind the current lifecycle seal, exact
release/controller identities, exact installed RootPolicy digest, evaluation
time, validity deadline, and a closed required check set. The digest is sampled
from the installed source before evaluation and revalidated immediately before
the response and ready record become durable; drift yields no `ready` record.
`ready` requires every check to pass. `not-ready` requires a failed check;
`indeterminate` requires a check that could not run. The validity deadline may
not exceed either the signed request expiry or the root-owned freshness policy.

`release-run` uses a different request ID and nonce. It binds the canonical
digest and transport digest of one exact `ready` preflight, including the seal
and RootPolicy digest observed by that preflight. Before invoking admission,
the controller requires the digest derived from the current installed policy
to equal the ready-preflight record, its signed response, the authenticated run
record, and the proposed admission input. The controller rechecks volatile
safety conditions immediately before admission. A preflight is consumed at
most once. Policy drift is a signed, consumed rejection, never permission to
reevaluate the preflight under new rules.

Before any containment, database, runtime, traffic, overlay, tour, or authority
mutation, the controller atomically commits an `admitted` CAS successor. That
successor acquires the resource lease and its strictly increasing fencing
token. A terminal-only CAS after mutation is forbidden because it permits two
controllers to mutate concurrently.

The admission callback receives the expected RootPolicy digest only from the
trusted installed-policy snapshot, atomically revalidates it with every
volatile condition, and repeats it in its signed result and the `admitted`
successor. A missing or different digest rejects before the callback can grant
fences or mutate any target. No request field, workflow output, or callback
default may fill this value.

Admission idempotency uses one closed canonical binding rather than a caller's
request ID alone. The document schema is
`propertyquarry.release-admission-binding.v2` and contains exactly:

- the installed RootPolicy digest;
- the release request ID, nonce, exact transport digest, canonical-envelope
  digest, and complete run identity;
- the ready-preflight request ID, nonce, exact transport and canonical-envelope
  digests, complete run identity, RootPolicy digest, observed lifecycle head,
  ordered checks, evaluation/validity bounds, and exact signed-response digest;
  and
- the immutable expected predecessor copied from that ready preflight.

It uses the same strict canonical JSON rules as RootPolicy. Its digest is:

```text
canonical_admission = strict_canonical_json(admission_binding_v2_document)
admission_binding_digest =
  "sha256:" || lowercase_hex(
    SHA-256(
      ASCII("propertyquarry.release-admission-binding-digest.v2") || 0x00 ||
      uint64_be(byte_length(canonical_admission)) || canonical_admission
    )
  )
```

`AdmissionRequest` carries this digest, `AdmissionResult` must echo it exactly,
and the `admitted` successor binds it; the external CAS uses it as the stable
retry/idempotency key. The release-run evaluation timestamp and newly observed
current head are intentionally excluded because either may advance after a
callback commit followed by a process crash, while every authority-bearing
identity and expected predecessor remains fixed. The callback first looks up
that binding digest and returns its stored result when present. Only an absent
binding may require the separately supplied observed current head to equal the
expected predecessor before performing a new atomic CAS. An exact retry must
reconstruct the same binding and recover the same CAS result. Any field or
RootPolicy drift creates a different or invalid binding and is rejected before
admission, never treated as the original retry.

Every target-side mutation carries both the global token and that resource's
token. Database, runtime, traffic, overlay, public-tour, authority, and
monitoring endpoints each persist and reject stale tokens; acceptance by only
some resources is nonconformant. A process that merely holds an old file lock,
lease ID, or still-live credential has no continuing authority.

Raw credentials may not bypass fencing. Each mutable target is reached through
a fence-enforcing mediator or transaction, or uses short-lived credentials
cryptographically scoped to the current resource token. A stale controller
that retained a database URL, gateway token, or overlay credential must still
be unable to write.

## Lifecycle state machine

Safe lineage roots are `genesis`, `sealed-final`, and a `rolled-back` state with
typed, complete reconciliation for every bound resource. A new lifecycle epoch
may be admitted only from one of those roots and must link the prior terminal
seal. `sealed-final` forbids further transitions in that same lifecycle; it
does not prevent a later release from starting a new linked epoch. Lifecycle
terminality, successful launch, containment, and ordinary-admission eligibility
are separate facts: `contained-failed` may be terminal for a controller attempt
while remaining unresolved and ineligible as a normal successor.

Every side-effecting phase has a durable intent successor before the side
effect and a result successor after independent verification. The successful
phase order is exact:

```text
safe root
  -> admitted
  -> containment-started
  -> contained
  -> deploy-started
  -> deployed
  -> live-verification-started
  -> live-verified
  -> activation-started
  -> activation-verified
  -> overlay-activation-started
  -> overlay-activated
  -> finalization-started
  -> sealed-final
```

Each arrow is a distinct external CAS successor. An intent state binds the
exact planned mutation, recovery target, external idempotency key, expected
resource version, global/resource fencing tokens, and input digests before the
target can change. A result state is committed only after its ordered evidence
manifest is persisted and fsynced. This closes the process-death gap where a side effect
could occur without the journal revealing that it may have occurred. Skipped,
reversed, repeated, forked, or cross-lifecycle transitions are invalid. The
controller cannot infer several completed phases from one terminal receipt.

While its lease is live, the admitted controller may enter `rollback-started`
from any nonterminal state. After process death or lease expiry, the watchdog
must first commit `reconciliation-admitted` for the same lifecycle with a new
lease ID, a strictly greater fencing token, and the unchanged resource set.
Only that new holder may inspect intent states or enter `rollback-started`.
Reusing the expired holder or fencing token is invalid and risks split brain.

Verified recovery ends in `rolled-back`. Incomplete or unverifiable recovery
ends in `contained-failed`, which is not a safe lineage root and blocks every
new release. `reconcile-run` may resume that same lifecycle only by a new
`reconciliation-admitted` takeover; it may not create a new lifecycle or erase
an earlier seal. Recovery from `contained-failed` inherits the exact unresolved
hazard set and predecessor seal; it receives fresh recovery fences but cannot
silently drop a database, traffic, overlay, tour, or authority hazard. This
recovery-only path prevents a permanent wedge without reopening normal release
admission.

Rollback always advances the chain. It never restores an earlier counter or
deletes the failed epoch. A normal rollback after `sealed-final` is forbidden.
Emergency post-launch recovery is a separately authorized, explicitly linked
future recovery protocol, not a v2 rollback loophole.

## Lease, watchdog, and crash behavior

The admitted state fixes the lease holder, resource set, and global/per-resource
fencing tokens.
Renewal is a CAS successor that keeps those identities unchanged, increases the
generation, and uses a policy-bounded future deadline. Renewal after expiry,
holder/resource mutation, deadline rollback, or fencing-token rollback is
invalid.

Lease issue time and deadline come from the external authority's trusted clock,
not request or controller input. Root-owned policy fixes maximum initial and
renewal TTLs; a caller-supplied `now` value or far-future deadline is invalid.

Every phase checks the trusted clock, current external seal, and target-side
fencing token before mutation. The target must persist and compare the token;
checking it only in the controller is insufficient. The controller releases
the lease only after a terminal seal and signed receipt are durable. A watchdog
observes admitted nonterminal lifecycles. On controller death, timeout,
cancellation, lost response, or lease expiry it atomically installs the new
reconciliation holder with strictly higher global and per-resource tokens
before reading or changing a target. The expired owner may perform no write,
including rollback. The watchdog infers an orphaned intent from the external
journal and probes actual target state; a dead process cannot be trusted to
append its own crash marker. No new lifecycle starts while reconciliation is
required.

An exact retry after a lost response returns the stored signed response without
repeating mutation or advancing CAS.

## Database recovery semantics

Preflight rejects a migration unless root-owned policy can prove one of these
outcomes:

- `unchanged`, proven against the pre-operation schema and database identity;
- `forward-compatible`, proven against both prior and candidate runtimes;
- `restored-verified`, bound to the pre-schema, backup identity, WAL/LSN or
  equivalent recovery position, restore checksum, and post-restore probes; or
- a separately policy-authorized forward repair that produces equivalent
  typed proof.

A traffic rollback cannot claim database rollback. A restore checksum,
database identity, schema, or WAL/LSN mismatch yields `unresolved`, forces
`contained-failed`, and blocks normal admission. `rolled-back` requires the
declared database outcome plus independent post-recovery verification.
Irreversible migration without a verified safe outcome is not admissible.

## Required typed evidence

Evidence is an ordered, closed discriminated union, never an arbitrary
`{kind,digest}`
pair. Every item binds the lifecycle, release and image identities, environment,
subject, observation time, status, verifier binary/policy identities, canonical
artifact digest, byte size/media type, and dependencies. Ledger generation,
not timestamp, establishes order. Every evidence manifest is signed and
content-addressed, then persisted and fsynced before the phase-completion or
final seal; persistence failure leaves the intent nonterminal.

Successful release phases require at least these evidence subjects:

| Phase | Required evidence |
| --- | --- |
| `admitted` | signed request, ready preflight, replay consumption, lease/fence acquisition |
| every `*-started` intent | exact input and planned-side-effect digest, recovery target, current target-side fencing proof |
| `contained` | writer drain, database fence, prior runtime/traffic/overlay recovery targets |
| `deployed` | candidate artifact, image/config identities, migration result, runtime health, target-side fencing proof |
| `live-verified` | exact live release/image/config/replica identity, public and authenticated probes, SLO/alert observation |
| `activation-verified` | fixed persona/broker identity, idempotency key, exact release, before/after account state, zero unauthorized provider or send effects |
| `overlay-activated` | staged and prior snapshot digests, compare-and-swap result, active revalidation |
| `sealed-final` | complete ancestry, Gold result, final authority digest, monitoring continuity |
| `reconciliation-admitted` | expired/stale holder evidence, new lease/fence acquisition, unchanged resource set and complete last-intent inspection |
| `rolled-back` | traffic, runtime, overlay, database and public-route recovery, target-side fencing proof, plus post-rollback verification |

Success evidence must have `status=pass`, unique semantic subjects, complete
dependencies, and exact candidate bindings. Failure and recovery states require
typed failure/containment evidence; an empty evidence list is invalid.

## Controller response transport

Authority is returned only on the write end of a workflow-supervisor-created
anonymous pipe. The descriptor cannot alias stdin, stdout, stderr, a regular file, a
socket, or the pipe's read end. The frame is four unsigned big-endian length
bytes followed by exactly one strict UTF-8 JSON object of 1 through 1,048,576
bytes. The controller closes the descriptor after the frame. Zero, multiple,
truncated, oversized, BOM-prefixed, duplicate-key, non-finite, non-object, or
trailing-byte frames are invalid.

The closed local topology and wire are specified in
`PROPERTYQUARRY_RELEASE_LOCAL_TRANSPORT_V2.md`. The workflow client transfers
only the exact externally signed request and the outer response-pipe write end
over the fixed systemd Unix socket. At native entry, before any library
initialization that may fork or exec, the systemd-side supervisor broker adopts
fd 0 and the outer response descriptor, verifies their fixed identities and
directions, marks them non-inheritable, and closes every descriptor outside its
explicit allowlist. After authenticating the request, the broker resolves and
preopens trusted inputs itself; the unprivileged client never supplies a
candidate, manifest, policy, trust root, or credential. The controller worker
then adopts the broker-created inner response, request, candidate, manifest, and
policy descriptors before starting helpers. Checking either response descriptor
only when writing is too late.

stdout and stderr are bounded redacted diagnostics and never authority. The
workflow supervisor drains the outer pipe concurrently, while the systemd-side
supervisor broker drains a separate inner pipe concurrently with the controller
worker. This prevents either pipe from deadlocking its writer. The broker
enforces absolute read, write, process, cleanup, and EOF deadlines; observes the
real worker status; validates signed response, exit class, operation, outcome,
and terminal-state agreement; and kills and reaps every controller descendant
before forwarding. Child processes inherit neither response descriptor. The
controller persists and fsyncs the exact signed inner frame before emitting it.
Only after status agreement and controller-descendant-empty proof may the broker
forward those exact bytes, without re-encoding, to the outer pipe for later
verification. The broker retains the outer writer until its raw mapped exit, so
payload without EOF is never eligible. A retry returns the stored bytes
byte-for-byte.

A process group is not production containment by itself: a descendant can
create a new session or process group. The systemd-side broker and controller
worker therefore remain in the fixed, non-delegated, root-owned service cgroup.
The broker acts as a subreaper, uses pidfd/wait semantics, terminates and reaps
the complete controller descendant set on every failure, and proves that set
empty before forwarding. Because the broker is itself still in the unit cgroup,
PID 1 proves the whole unit cgroup empty only after the broker exits; claiming a
pre-forward whole-cgroup-empty proof would be false without a separate managed
worker cgroup and external witness. Likewise, verifier and ledger calls must use
kernel-enforced deadlines or killable isolated helpers; a timed-out in-process
thread is not cancelled authority.

The full response verifier consumes a closed context binding the event ID,
`sha256:`-prefixed request-transport digest, expected operation, exact frame
digest, and the exact installed root-policy digest selected before launch. It
returns a typed receipt that repeats those bindings and the same policy digest,
and proves signature verification with literal
booleans. A truthy value or signature check lacking event/request/frame binding
is invalid. The decoded response exposed for diagnostics is immutable; the exact
frame bytes remain the evidence object.

The verifier obtains its expected RootPolicy digest independently from the
fixed root-owned installed source and applies the versioned canonical digest
contract above. It never copies the expected value from the request, response,
workflow, diagnostics, or candidate. The signed response's policy digest must
equal that expected value and, for a run, the ready-preflight, replay,
admission-result, and lifecycle-CAS bindings. Drift or omission makes the frame
ineligible before admission is accepted or authority is reported, even when
the response signature itself is cryptographically valid.

Fixed process classes are:

- `0`: signed ready preflight or `sealed-final` success;
- `10`: signed non-authorizing preflight disposition;
- `20`: signed rejection before admission;
- `30`: signed `rolled-back` terminal result;
- `31`: signed `contained-failed` result requiring reconciliation;
- `40`: signed replay/CAS conflict; and
- `50`: protocol/authentication failure, for which no signed response may be
  available.

Signal, timeout, missing frame, or any unmapped exit is failure and triggers
an authenticated external-ledger lookup before any retry, plus journal
reconciliation when admission may have occurred. The workflow never infers
"not admitted" from a missing frame.

## Target workflow

The protected workflow uses one non-interleaving concurrency domain with
`cancel-in-progress: false` and these authority calls:

```text
ordinary immutable-artifact CI
  -> installed-supervisor client release-preflight
  -> systemd supervisor-broker -> fixed controller worker
  -> installed-supervisor client release-run
  -> systemd supervisor-broker -> fixed controller worker
  -> publish signed terminal receipt as untrusted transport
```

The controller job has no checkout, repository-local action, dependency
installation, cache, Docker command, production secret, or candidate artifact
execution. It invokes only the fixed absolute installed entrypoint. Public
read-only probes may run later as monitoring, but they cannot manufacture or
replace final authority.

The installed supervisor in unprivileged client mode, not workflow code,
obtains and consumes the two distinct signed requests. It exchanges the job's
short-lived GitHub OIDC identity with an external request authority, and
root-owned policy verifies the repository, ref, candidate SHA, workflow ref
and workflow SHA, run ID and attempt, job identity, environment, and current
external seal. The broker derives the RootPolicy digest from its installed
source; neither the client nor workflow supplies an expected digest. Candidate
YAML may forward identity values for diagnostic cross-checking, but it cannot
select a controller, trust root, request body, nonce, preflight receipt,
candidate artifact, policy, policy digest, output path, or credential. The
`release-run` lookup binds and consumes the exact ready preflight stored by the
external authority for the same authenticated run identity and unchanged
installed RootPolicy digest.

The workflow passes the short-lived OIDC bearer only through inherited file
descriptor 9 and unsets its shell variable before `/usr/bin/env -i`; the bearer
is never an environment argument to the installed supervisor. At native entry,
the supervisor bounded-reads descriptor 9, strips exactly the single trailing
LF added by the Bash here-string, and rejects a missing, unreadable, oversized,
wrong-type, or aliased descriptor, any additional trailing byte, and an empty
bearer. It marks the descriptor `FD_CLOEXEC` and closes it immediately after
the read. Neither the request document, replay record, response, diagnostic,
journal, nor any other persisted or serialized object may contain the bearer.

During migration, legacy candidate-executed production jobs are conformant
only when they are unconditionally disabled and no enabled job depends on
them. Their presence is historical text, not a fallback. If the fixed v2
supervisor or its external authorities are absent, the enabled release job
fails closed before any production mutation.

The existing candidate-executed protected jobs remain non-authoritative and
must be removed from the production path before controller provisioning is
declared complete.

## OS package source contract and installation audit

`packaging/propertyquarry-release-control-v2/` now defines source templates for
the fixed systemd socket, per-request supervisor-broker service, watchdog service,
dedicated system users, private state directories, and closed controller and
watchdog configuration schemas. The request service is socket-activated only,
admits one connection, runs in a non-delegated systemd cgroup with
`ExitType=cgroup` and `KillMode=control-group`, and cannot be enabled or started
directly. The broker launches only the fixed controller worker, enforces the
inner frame/status/cleanup contract, and forwards exact bytes to the outer pipe.
The watchdog uses a separate identity, state directory, and takeover
credentials. Both services drop capabilities, prohibit namespace creation,
scrub GitHub/OIDC environment variables, and use rate-limited journald only for
redacted diagnostics.

Those files are package inputs, not an installable authority. They deliberately
contain no supervisor, controller, or watchdog binary; no production config,
policy, trust root, credential, package signature, or artifact digest; and no
checkout-copy installer. An independently built and signed OS package must
supply those objects and preserve the fixed paths and protections.

`scripts/propertyquarry_release_installation_model.py` is a read-only,
non-authoritative audit model for that future package. It accepts an exact
19-role byte/metadata manifest and checks the fixed binary, unit, schema,
config, policy, and trust-root paths with descriptor-relative `O_NOFOLLOW`
traversal, single-link regular-file enforcement, bounded exact-byte hashing,
and exact mode/owner/group checks. Isolated-root results are explicitly labeled
simulations. Production mode accepts only `/`, requires root ownership where
fixed, and requires one consistent non-root service group for private config
and trust roots. The model neither verifies a package signature nor grants
readiness; an external root-owned package verifier must authenticate the
manifest before treating the same checks as installation evidence.

## Conformance versus authority

Repository validators and executable reference models may prove document and
state-machine consistency. They do not verify external signatures, trust a key,
read a trusted clock, consume replay state, acquire a real lease, enforce a
target fencing token, mutate production, or reconcile a crashed controller.

Production remains fail-closed until an independently built controller and
watchdog implement this contract, root-owned policy fixes every required check
and evidence issuer, the external replay/CAS authorities are provisioned, and
hostile end-to-end receipts prove the installed system.
