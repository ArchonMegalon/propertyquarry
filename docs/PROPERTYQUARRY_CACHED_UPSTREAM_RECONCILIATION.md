# PropertyQuarry Cached-Upstream Reconciliation Contract

## Authority and scope

This document records read-only planning evidence captured on 2026-07-26 for:

- local `HEAD`: `51e642f978e80ee7eb165151419cd49d5288342c`
- cached `origin/main`: `c2fdf5a17d58f5842c6841ae57cfabc53b844f99`
- relationship: 0 commits ahead and 322 commits behind

The cached remote-tracking ref is not current-upstream or release authority.
After an authorized fetch, recompute the complete inventory and re-review every
decision below before changing the worktree.

At the audit snapshot, 263 of 328 local paths overlapped cached upstream. Of the
overlap, 78 paths were byte-and-mode identical, 30 had a clean textual
three-way merge, and 155 required deliberate resolution. The 105 tracked text
conflicts contained 475 conflict hunks. Generated evidence is excluded from any
mechanical-merge tranche.

## Preconditions

Do not begin source reconciliation until all of these are captured:

1. The freshly fetched upstream commit and merge base.
2. A read-only inventory of every deployed
   `propertyquarry_schema_migrations` ledger and its exact checksums.
3. The installed release-controller ABI, service units, socket, configuration,
   and authority boundary.
4. The intended standalone repository identity, workflow identity, image
   labels, and provenance identity.
5. A recoverable worktree snapshot and a path-by-path resolution ledger.

## Dependency-lock recovery boundary

At the 2026-07-26 audit snapshot, the verifier input required
`jsonschema[format-nongpl]==4.26.0` while the committed hash lock did not contain
that format-validation contract. The authenticated v3 Python pin already bound
the canonical input, its included `ea/requirements.lock`, and the compiled hash
lock by path and SHA-256, so bootstrap correctly failed before environment
creation, installation, or test collection with the exact parity diagnostic:

```text
jsonschema[format-nongpl]==4.26.0 is missing from the compiled requirements lock
```

On 2026-07-26, `uv 0.11.32` was run with the recorded compile contract plus
`--offline`. Resolution failed because even the already-pinned
`annotated-doc==0.0.4` metadata was absent from the local cache. This is
evidence that the local registry cache is incomplete, not evidence that the
declared dependency set is unsatisfiable. The probe produced no authoritative
replacement lock and performed no download.

At that snapshot, canonical regeneration therefore required explicit operator
approval for registry metadata or package downloads and execution only in the
controlled release-environment recovery lane.

That specific lock blocker is now resolved in this worktree. The canonical
input and compiled hash lock both contain the exact
`jsonschema[format-nongpl]==4.26.0` pin, including the offline wheelhouse
contract. The input and compiled lock continue to use the strict
trusted-directory reader. The base lock is a declaration under an existing
peer-writable `ea` directory, so its first bootstrap access remains the
verifier's directory-FD-relative, nonblocking, no-follow snapshot reader. Pip
installs the strictly trusted compiled hash lock, never the base declaration,
and the post-bootstrap `FormatChecker` probe proves the optional format
providers at runtime.

On 2026-07-27 UTC, the authenticated `ci-gates-authenticated` lane booted that
hash-locked interpreter and completed the API test phase with 8,018 passed, 129
skipped, and 14 deselected tests. Its later release-asset phase stopped only on
generated artifacts that differ from immutable `HEAD`; that source-binding
failure remains a release blocker. Do not interpret dependency recovery as
authorization to reconcile the migration lineage, commit the dirty candidate,
or publish release evidence.

## Blocking migration-lineage decision

Migrations 1 through 14 agree. Both branches independently assigned migration
version 15 to different schemas:

- local v15: `authoritative_distributed_ingress_admission`, checksum prefix
  `02fe41df`
- cached-upstream v15: `durable_property_account_privacy_lifecycle`, checksum
  prefix `2f20534f`, followed by cached-upstream admission v16 and capacity v17

Never renumber or rewrite an already-applied migration merely to make source
tests agree. Inspect every live ledger first:

- If no target has the local v15 checksum, use the upstream lineage and port
  still-required local checks as new migrations.
- If any target has the local v15 checksum, design and prove a contained
  compatibility/cutover migration before choosing the final numbering.

The real PostgreSQL upgrade, disaster-recovery, readiness, and checksum matrix
must pass before application or traffic authority is granted.

## Controller and admission architecture

Keep one installed controller ABI and one distributed admission backend.
Parallel implementations would create split-brain release or capacity
authority.

- Preserve the installed-controller boundary: checkout code does not gain
  deployment, database, receipt, or traffic authority.
- Use the upstream admission service as the structural integration base only
  after porting strict body-length reconciliation, transfer-encoding rejection,
  renewable leases with fail-closed lease loss, pseudonymous principals, and
  distinct quota/concurrency/backend failure semantics.
- Decide explicitly whether a concurrency rejection consumes quota and encode
  the decision in PostgreSQL-backed multi-replica tests.
- Status GET requests remain observation-only; repair is scheduler/worker
  owned.

## Product, tour, and MagicFit truth

Use explicit truth lanes:

- `verified_tour_url` is limited to proven provider or captured-tour evidence.
- `layout_preview_url` is a disclosed AI-generated reconstruction.
- Generated media never satisfies a verified-provider or captured-360 claim.
- Authentication/tracking surfaces, retired hosts, proofless URLs, and raw
  provider diagnostics remain rejected or redacted.

The cached-upstream MagicFit importer is a cluster migration, not a file merge.
Its delivery contract, secure I/O, governed reservation, and publication-lock
modules must arrive atomically with the importer. Preserve stable-file,
no-follow, digest-bound private staging and explicit acceptance. Do not restore
direct public-bundle writes.

## Native release-control closure

Use the current offline Ed25519 authenticated-request, replay, principal-split,
publication-lock, and exact-source-closure hardening as the native base.

- Restore the precise build-info field name
  `performs_release_effects`; the shorter `performs_effects` is semantically
  wrong because the controller may perform local durable replay mutations while
  still having no release effects.
- Do not add cached-upstream `phase_b_oidc_linux.go` to the flagship native
  closure. It imports `net/http` and performs discovery/JWKS fetches, violating
  the offline native-authority contract.
- If phase B is retained, isolate it outside the authoritative native source
  root and mark it non-authorizing and experimental.
- Drive the no-network/no-`net/http` policy test from the exact
  `tools/source-files.txt` manifest and filesystem closure, not a hard-coded
  file tuple.
- Preserve private build outputs, early source authentication, reproducible
  static binaries, signal-safe cleanup, and receipt-last durable publication.

## Candidate, OCI, and identity contracts

The cached-upstream candidate builder is the structural base for modern
Docker-save parsing, audited Buildx cleanup, digest-pinned local stages, and a
bounded built-image smoke. It cannot be taken wholesale:

- Its producer adds `built_image_smoke` to a v1 receipt while the runtime
  binding rejects that extra key. Migrate producer and consumer atomically,
  preferably to an exactly validated v2 schema.
- Add a direct builder-output-to-runtime-binding compatibility test.
- Keep the current authority/caller UID split and durable replay state.
- Restore an explicit CPU ceiling unless a tested platform-specific replacement
  is documented.
- Resolve repository, workflow, image, manifest, and provenance identity
  atomically. Mixed `property` and `propertyquarry` identities must fail closed.

## Generated evidence

Never merge generated browser proof, flagship receipt, weekly pulse, release
manifest, timestamps, SHAs, or artifact hashes.

Reconcile their generators, source seed, approved baseline, strict JSON
parsing, hardened Git environment, index/mode/symlink validation, and
race-safe explicit repair first. Verification and preflight remain read-only
by default; exact-`HEAD` restoration requires the explicit
`--restore-exact-head` repair option. Direct `ci-gates`, authenticated
`ci-gates`, and release preflight never refresh canonical evidence. The smoke
workflow separately materializes browser -> flagship -> weekly evidence after
Chromium installation in a disposable canonical checkout and immediately
requires exact-`HEAD` reproduction before its read-only core gates. This proves
current-runtime reproduction only; it is not publication or release authority.
Then:

1. Commit the clean source candidate.
2. Materialize the browser workflow proof.
3. Materialize the flagship release gate.
4. Materialize the weekly product pulse.
5. Recompute the release manifest and metadata envelope.
6. Commit the reviewed generated evidence.
7. Prove detached exact reproduction and semantic cleanliness without
   rewriting the canonical checkout.

An upstream `pass` artifact bound to an older commit is historical evidence,
not evidence for the cached tip or merged candidate.

## Verification order

After reconciliation:

1. Check duplicate Python definitions and compare `pytest --collect-only`
   inventories before retiring tests.
2. Run schema, admission, controller, Compose, MagicFit, product-truth,
   publication, erasure, and browser suites.
3. Require candidate-builder-to-runtime-binding receipt compatibility.
4. Run the exact-manifest native no-network policy, pinned Go tests,
   reproducible builds, static ELF checks, and stage verification.
5. Run no-network candidate image build/smoke, OCI, container, and persistent
   runtime gates.
6. Regenerate evidence in the order above.
7. Run generated-artifact, release-asset, readiness, security, disaster
   recovery, observability, provenance, Gold, and rollback gates.

No source-only or cached-ref result replaces live database, installed
controller, immutable image, or production recovery evidence.

## 2026-07-26 local hardening evidence

The current dirty candidate has additional fail-closed local hardening, without
claiming release authority:

- The aggregate release shell now supplies all dashboard-render,
  structured-log-query, and distributed-trace-query receipts to the canonical
  observability verifier. The combined DR, Gold, and SLO integration regression
  set passed 224 tests.
- Native release-control pytest runs use a private gate temporary directory and
  disable pytest's cache provider. The authenticated entrypoint suite passed
  181 tests without source-tree cache or receipt mutation.
- PostgreSQL DR receipt inputs use bounded, duplicate-key-rejecting,
  descriptor-bound stable reads. Receipt publication uses a retained no-follow
  directory chain, random exclusive staging, descriptor-relative replacement,
  and directory fsync; parent swaps, symlinks, hardlinks, FIFOs, peer-writable
  ancestors, replacement races, and descriptor fault paths are covered. The DR
  suite passed 82 tests.
- Gold freshness rejects timestamps more than 300 seconds in the future and
  rejects non-finite direct-API age limits. The Gold suite passed 147 tests.
- The authenticated release dispatcher now forwards the exact target-specific
  environment required transitively by the live PropertyQuarry gate while
  stripping unrelated ambient values. Operator docs, workflows, the release
  manifest, and help examples use the authenticated dispatcher or direct
  privileged-shebang entrypoints instead of bare `bash` or direct authenticated
  Make targets.
- The security gate rejects ambiguous scanner JSON, requires `pip-audit` to
  cover every normalized lock package/version exactly, binds each Syft root to
  its immutable image digest, and requires native Trivy image evidence for that
  same digest. Its adversarial suite passed 31 tests.

These results prove the listed local invariants only. In particular, the
security gate records scanner versions and version-output hashes but does not
yet enforce approved executable paths or binary digests, and it does not
independently verify Trivy database age. Reviewed scanner provisioning and
current database evidence remain external flagship prerequisites.

## Verified residual release blockers

The 2026-07-26 local audit does not establish release authority. The current
candidate remains blocked by all of the following:

- Upstream has not been freshly fetched. Local `HEAD` is 0 commits ahead and
  322 commits behind only the cached `origin/main` ref.
- The worktree has 0 staged paths, 182 tracked-dirty paths, and 147 untracked
  paths (329 paths total). A canonical source binding requires a reviewed,
  clean commit.
- The verifier dependency lock needs the separately authorized recovery
  described above.
- Pinned Go 1.26.5, `pip-audit`, Syft, Trivy, `promtool`, and `amtool` are not
  available in the audited environment. Do not substitute unpinned downloads.
- The Gold aggregate still consumes fourteen required receipt families whose
  schemas do not intrinsically bind the exact release SHA/image. General Gold
  receipt inputs also lack one descriptor-bound, immutable input-set contract.
  A manifest assembled at Gold time would only relabel stale bytes, so this
  requires producer/controller provenance or schema migration before launch.
- The installed v2 controller, supervisor, watchdog, systemd units,
  configuration, and socket are not present. Source models and tests cannot
  substitute for that independent authority plane.
- The exact 29-file native source closure currently hashes to
  `sha256:e22bfabc5650288980f6632ec72657399ad4f642728df73153755aee7ea09d83`,
  while the available native receipt still binds
  `sha256:7ed1b69907f88fc4466dec0c14dd65e2aae3af9d371501bdbd3889639bf040c5`.
- Direct readiness verification reports exactly eight blockers: the weekly
  pulse's flagship SHA-256 and size do not match the current canonical flagship
  receipt, the flagship and browser receipts have missing or invalid source
  bindings, both receipts are blocked instead of passing, and both weekly
  candidate states are not ready. Preserve this mixed worktree evidence until
  an authorized clean-candidate materialization; do not silently repair it.
  Release-asset verification reports semantic drift only for the weekly pulse,
  flagship receipt, and browser proof.
- Live migration-ledger, disaster-recovery, monitoring/SLO, evidence-overlay,
  Rybbit, Gold, promotion, rollback, and host-recovery proof has not been
  captured for an immutable candidate.

Local browser availability is not canonical browser proof. The authenticated
release runtime can launch its installed Chromium, while the committed-head
receipt remains blocked by candidate/source binding and historical runtime-path
evidence; Firefox and WebKit availability is not established.

The next authorized critical-path sequence is:

1. Fetch upstream and capture the live migration ledger plus installed
   controller ABI without changing the candidate.
2. Recompute the overlap inventory and resolve source, schema, identity, and
   controller architecture under this contract.
3. Provision registry metadata and pinned tools only through their approved
   recovery lanes.
4. Commit and review a clean source candidate, then regenerate evidence in the
   required order.
5. Run installed-controller, immutable-image, PostgreSQL upgrade/DR,
   observability, Gold, promotion, rollback, and host-recovery proof before
   making a flagship release claim.
