# PropertyQuarry release-control protocol v1

Status: repository-owned conformance contract. This document is not a deploy
controller, installer, policy, keyring, trust root, or grant of release
authority.

## Boundary

`scripts/deploy_propertyquarry.sh` is an unprivileged handoff. It opens stable
file descriptors for a request, the candidate root, and independently installed
root-owned controller material, then replaces itself with that controller. The
repository may define the wire contract used at this boundary. It must not
manufacture the production authority on the other side of it.

The protocol-v1 validator therefore proves only that a document:

- is bounded UTF-8 JSON with no duplicate object keys;
- has an explicit, supported schema name and version;
- contains no unknown properties;
- uses bounded identifiers, strings, arrays, counters, timestamps, and digests;
- has the required internal operation/mode/effect and digest bindings; and
- carries syntactically valid signature metadata whose declared payload digest
  matches protocol-v1 canonical JSON.

It deliberately does **not**:

- verify a cryptographic signature or decide that a key is trusted;
- decide that a signer, request, host, image, database, or policy is authorized;
- compare the request with the current clock or consume a nonce;
- read or update an external monotonic CAS service;
- validate controller provenance or root ownership;
- inspect the candidate-root file descriptor or Docker/database/traffic state;
- perform containment, migration, promotion, rollback, or any other mutation.

A `VALID` result is conformance evidence only. It grants no authority.

## Files

- Machine-readable schema:
  `docs/propertyquarry-release-control-protocol.v1.schema.json`
- Dependency-free offline validator:
  `scripts/validate_propertyquarry_release_protocol.py`
- Production-controller public-tour volume profile:
  `docs/PROPERTYQUARRY_PUBLIC_TOUR_VOLUME_PROFILE_V1.md`

Run the validator with Python's standard library only:

```text
python3 scripts/validate_propertyquarry_release_protocol.py \
  --kind signed-request /absolute/path/to/request.json
```

Kinds are `signed-request`, `preflight-disposition`, `controller-receipt`,
`controller-manifest`, and `auto`. Success exits 0. Invalid input exits 2 and
prints one deterministic error to stderr.

## Transport rules

Every protocol document is at least 1 byte and at most 1,048,576 bytes. It is a
single UTF-8 JSON value whose top level is an object. JSON extensions such as
`NaN` and `Infinity` are forbidden. Duplicate keys are rejected during parsing,
including in nested objects; last-key-wins parsing is not acceptable.

All objects are closed. Every object schema sets `additionalProperties: false`,
and the offline validator independently rejects unknown fields. Every array and
string has a finite bound. Schema patterns use an explicit absolute-end guard,
so a terminal line break cannot satisfy a `$` anchor. Counters are integers in
the range 0 through 9,223,372,036,854,775,807; JSON booleans are not integers.

Protocol timestamps use exactly `YYYY-MM-DDTHH:MM:SSZ`. The validator checks
that they describe a real UTC second. A request must expire after issuance and
has a maximum declared lifetime of 900 seconds. The installed controller must
still compare those times with its trusted current clock.

SHA-256 bindings use `sha256:` followed by exactly 64 lowercase hexadecimal
characters. A Git release SHA is exactly 40 or 64 lowercase hexadecimal
characters. Image references are content digests, never tags. Request and
receipt IDs are canonical lowercase UUIDv4 values. A request nonce is exactly
32 lowercase hexadecimal characters (128 bits). A CAS challenge is a SHA-256
binding.

Host names are canonical lowercase DNS names without a trailing dot. Host
authorization remains external policy.

## Canonical signed preimage

The signed body is named `payload` for a request, `preflight` for a preflight
disposition, and `receipt` for a controller receipt. The Ed25519 signed preimage
is a derived object with exactly these fields:

```json
{
  "domain": "propertyquarry.release-control.signature.v1",
  "schema": "<the envelope schema>",
  "version": 1,
  "<payload|preflight|receipt>": "<the complete named body>",
  "signature_context": {
    "algorithm": "ed25519",
    "key_id": "<the envelope signature key_id>",
    "encoding": "base64"
  }
}
```

`signature.signed_preimage_sha256` and `signature.value` are excluded, avoiding
any circular dependency. The explicit domain, envelope schema, protocol
version, named body key, algorithm, key ID, and encoding are all inside the
signed bytes. A body cannot be replayed as a different document kind or
version, and changing `key_id` invalidates the binding.

Protocol-v1 canonical bytes are produced by:

1. rejecting duplicate keys and non-standard JSON numbers;
2. serializing UTF-8 with object keys sorted lexicographically;
3. emitting no insignificant whitespace; and
4. emitting literal non-ASCII characters rather than ASCII escapes.

This is identified as `propertyquarry-json-sort-keys-v1`. The
`signature.signed_preimage_sha256` field must equal the SHA-256 digest of those
bytes, and Ed25519 verification is performed over those canonical bytes.
Protocol v1 fixes `algorithm` to `ed25519` and requires canonical base64 encoding
of exactly 64 signature bytes. A different algorithm requires a new protocol
version, preventing algorithm-confusion or downgrade within v1. The validator
checks the digest, algorithm label, encoding, and byte length. It does not
perform Ed25519 verification; `key_id` becomes authenticated only when the
installed controller verifies the signature over the protected preimage. Key
lookup, revocation, purpose, signer role, and signature verification belong to
the independently installed controller and its root-owned trust material.

## Signed request envelope

Schema name: `propertyquarry.release.signed-request`; version: `1`.

The top-level fields are `schema`, `version`, `payload`, and `signature`.
`payload` binds all of the following:

- `operation`: `deploy-run`, `deploy-preflight`, `candidate-run`, or
  `candidate-preflight`;
- `mode`: `production` for `deploy-*`, `candidate` for `candidate-*`;
- `audience`: exactly `propertyquarry-release-controller`;
- target `host`, UUIDv4 `request_id`, 128-bit `nonce`, `issued_at`, and
  `expires_at`;
- `cas.namespace`, `cas.challenge`, and the externally expected monotonic
  `cas.expected_counter`;
- an immutable release binding; and
- `requested_effect.mutation`.

The immutable release binding contains:

- `release_sha`;
- `candidate_artifact_digest` for the immutable release/evidence bundle;
- `web_image_digest` for the exact application image;
- `render_image_digest` for the exact governed render worker image;
- `controller_digest`;
- `controller_manifest_digest`; and
- SHA-256 bindings for the canonical Compose plan, database-fence policy,
  drain keyring, operator-gateway trust, monitoring topology, and monitoring
  tools.

Those fields name expected bytes; the installed controller must compare them
with securely opened authority-owned artifacts and with the candidate it
actually evaluates.

`requested_effect.mutation` is requester intent, never authorization. It is
exactly `forbidden` for both preflight operations. It is
`controller-policy-gated` for run operations, which means the controller must
independently authenticate and authorize every effect. No request field can
turn a failed policy decision into permission.

The handoff separately supplies the stable request FD, the SHA-256 of the exact
transport bytes, and the candidate-root FD/device-inode identity. These exec
arguments are intentionally not replaced by requester-authored JSON fields.

## Preflight disposition

Schema name: `propertyquarry.release.preflight-disposition`; version: `1`.

This is the controller's signed, explicit read-only answer for a conformant
`deploy-preflight` or `candidate-preflight` request. The envelope contains
`schema`, `version`, a canonical `preflight` payload, and an Ed25519
`signature` binding that payload. The payload binds the exact request transport
digest, request ID, nonce, operation, mode, audience, host, CAS challenge,
CAS namespace and expected counter, release, controller binary, and controller
manifest. The offline validator checks only the signature metadata and payload
digest; the operator or consuming controller must cryptographically verify the
signature through independent trust before acting on it.

The semantic validator also requires the response
`controller.binary_digest` to equal
`request.release.controller_digest`, and the response
`controller.manifest_digest` to equal
`request.release.controller_manifest_digest`. JSON Schema can constrain both
pairs to digest-shaped values but cannot express cross-field equality; this
validator rule prevents a response from one controller identity from being
presented as the disposition for a request bound to another.

`mutation_performed` and `cas_consumed` are literal `false`. `ready` requires
every bounded check to have status `pass`. `not-ready` requires at least one
`fail` and permits no `not-run` checks. `indeterminate` requires at least one
`not-run` check, so incomplete observation can never collapse into a definitive
answer. Checks carry bounded machine codes instead of unbounded prose, and
check IDs are unique. JSON Schema's `uniqueItems` rejects exact duplicate check
objects; property-level ID uniqueness across otherwise different check objects
is an additional semantic-validator rule.

A preflight disposition is a response document, not permission to run and not
a durable deployment receipt. Producing it must not contain, fence, journal,
write a receipt, open Docker or the database for mutation, consume CAS state, or
change traffic.

## Controller receipt

Schema name: `propertyquarry.release.controller-receipt`; version: `1`.

Only `deploy-run` and `candidate-run` can produce this envelope. Its nested
`receipt` binds the request transport digest and all request/release/controller
identities, times, outcome, mutation facts, the complete signed CAS tuple, and
bounded content-addressed evidence. The envelope signature binds canonical
receipt bytes. The receipt's CAS namespace and challenge must equal the signed
request values, and `previous_counter` must equal the request's
`cas_expected_counter`; this prevents a valid-looking receipt from being
replayed across CAS lanes or counter positions.

As with preflight, the semantic validator requires the receipt's controller
binary and manifest digests to equal their corresponding signed release
bindings. This cross-object equality is enforced by the offline validator in
addition to each field's JSON Schema shape.

Candidate receipts cannot claim a production traffic change. Every subordinate
mutation fact implies `mutation.performed: true`, and performed mutation
requires `containment_before_candidate_validation: true`. Outcome semantics are
closed:

- `succeeded` requires performed mutation after containment, a committed CAS
  seal, no rollback, and at least one content-addressed evidence binding; a
  successful `deploy-run` must also report the production traffic change;
- `rejected` requires no mutation and no CAS commit;
- `failed` and `rolled-back` require a committed external CAS seal so the
  terminal result is externally sequenced; `failed` cannot claim a completed
  rollback; and
- `rolled-back` additionally requires `rollback_performed: true`.

Evidence bindings are unique `(kind, digest)` pairs. Because evidence objects
are closed to exactly those two fields, JSON Schema `uniqueItems` and the
semantic validator enforce the same evidence identity rule.

`rollback_performed` means that restoration completed and was verified, not
merely that a rollback command was attempted. Therefore it is true if and only
if the outcome is `rolled-back`. An attempted, incomplete, or unverifiable
rollback remains `failed` and must not claim `rollback_performed`.

A committed CAS counter advances exactly once; an uncommitted counter remains
unchanged. These constraints reject internally contradictory receipts before
cryptographic or runtime verification begins.

Receipt conformance does not prove receipt authenticity or runtime truth. The
consumer must verify its signature and controller identity through independent
trust, compare every digest, and reconcile the CAS seal with the external
monotonic service before using it as release evidence.

## Controller manifest

Schema name: `propertyquarry.release.controller-manifest`; version: `1`.

This schema describes compatibility metadata for the independently built and
installed native controller: controller ID, protocol version, audience, host,
binary digest, source release SHA, issuance time, all four operations,
the fixed v1 `ed25519` signature algorithm, protocol limits, canonicalization ID,
and policy digests.

The repository provides no manifest instance, binary, key, policy, installer,
or privileged service. A conformant manifest is not authentic merely because
it parses. The manifest is not one of the three signed-envelope kinds; its
authenticity comes only from the independently installed root-owned file,
controller digest pin, and the signed request/response manifest-digest
cross-bindings. The production authority must build the controller
independently, install and pin its exact bytes as root-owned material, establish
trust and policy outside the candidate checkout, and audit the resulting host
state.

## Controller obligations beyond conformance

Before relying on any conformant document, the installed controller remains
responsible for all privileged and security-sensitive checks, including:

- stable-FD identity and exact transport-hash verification;
- cryptographic verification against root-owned, purpose-limited trust;
- signer authorization, host/audience/operation matching, trusted-clock
  freshness, nonce uniqueness, and atomic external monotonic CAS;
- equality of every signed release, candidate artifact, web image, render
  image, controller, manifest, and policy digest with independently opened
  bytes;
- server-derived database identity and signed allowed-target enforcement;
- containment before candidate validation, canonical Compose enforcement,
  dedicated durable-worker fencing and heartbeat readiness, immutable
  Cloudflared image/config binding, migration fencing, health gates, promotion,
  crash reconciliation, and verified rollback; and
- independently verifiable, content-addressed receipts and monitoring evidence.

The production canonical Compose plan must include the public-tour volume, and
the installed controller must enforce
`docs/PROPERTYQUARRY_PUBLIC_TOUR_VOLUME_PROFILE_V1.md`. That profile is bound
through the existing signed `canonical_compose_plan` digest and defines
controller behavior and evidence beyond protocol conformance. It adds no field,
operation, semantic-validator rule, or authority to the closed v1 wire schema.

Failing any one of those checks must fail closed. Protocol conformance must
never be used as a substitute for them.

## Versioning

Version 1 is closed and additive changes are not accepted silently. A new field,
operation, canonicalization algorithm, semantic rule, or bound requires a new
schema/protocol version plus explicit controller support. Installed controllers
must reject versions and schema names they do not implement.
