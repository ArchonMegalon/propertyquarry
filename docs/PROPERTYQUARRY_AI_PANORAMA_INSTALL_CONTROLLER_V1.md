# PropertyQuarry AI panorama install controller v1

Status: reference implementation only. This repository does not currently
contain an active AI-panorama signing key, a signed Prater permit, or an
installed authoritative controller profile. Building or importing the Python
modules does not authorize a release.

## Closed Prater operation

The sole v1 operation is the exact Prater candidate:

- research run `98bed75e984549c6bd4371d602662ab8`
- candidate `053ad185e1c44b2e`
- listing `1807240910`
- public slug `prater-messe-maisonette-ai-360-053ad185e1c44b2e`
- governed Docker volume `property_propertyquarry_governed_public_tours`
- governed application mount target `/data/governed_public_property_tours`
- governed application setting `EA_GOVERNED_PUBLIC_TOUR_DIR`
- governed volume identity `propertyquarry-governed-public-tours-production`

None of the entrypoints accepts an identity, path, URL, digest, permit, volume, or
database argument. Each reads one canonical, root-owned trust assertion and
the fixed permit name from the controller mounts. The signed
permit, trusted current release context, source receipts, publication-record
CAS digest, public-volume profile, image/Compose plan, and one-time nonce must
all agree exactly.

## One-shot container contract

Only the native single-host release controller may start either operation.
Before permit issuance, it runs the fixed read-only record-discovery interface:

```text
/usr/local/bin/python -I -B \
  /usr/local/libexec/propertyquarry-prater-ai-panorama-record-discovery-v1.py
```

Discovery reads a root-owned mode-`0600`
`prater-record-discovery-request.v1.json` containing only a controller-generated
128-bit lowercase-hex `request_id`. It derives the unique owner from the fixed
run in PostgreSQL, reacquires the record under the owner's erasure advisory
lock and fence, requires the exact record hash to remain unchanged, validates
the terminal candidate, and returns owner plus canonical record SHA in a
private non-authorizing projection. It performs no mutation. The one-shot
container uses `--log-driver=none`; the native controller captures stdout only
in root-only memory or a mode-`0600` file, never records it in Docker/audit
logs, and zeroizes it after constructing the approved request and permit.
Owner principal IDs are opaque but restricted at this boundary to the exact
ASCII grammar `[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,255}` so native and Python
canonical JSON/signature bytes cannot diverge on Unicode escaping.
The native authority signs outside the container afterward; a private signing
key never enters the image. Apply independently re-queries and row-locks the
record and must match the signed SHA, so discovery is not authorization.

The only permit file is
`/var/lib/propertyquarry/release-control/ai-panorama-install/permits/prater-ai-panorama-install.json`.
The native controller creates that exact root-owned mode-`0600` canonical
JSON+LF file atomically after discovery; preflight reads it without consuming
it, and apply reads and consumes the same inode/content. It is retained
unchanged through the 24-hour recovery window and audit retention. A
request-ID-derived permit filename, mutable rewrite, deletion, or alternate
permit path is invalid and cannot restore authority.

The read-only, no-network artifact preflight interface is:

```text
/usr/local/bin/python -I -B \
  /usr/local/libexec/propertyquarry-prater-ai-panorama-preflight-v1.py
```

It verifies the unconsumed permit, sealed source artifacts, target-volume
identity, and signed release profile. It has no database access, does not
consume the permit, and does not write the target volume, controller state, or
operation journal.

The separately authorized apply interface is:

```text
/usr/local/bin/python -I -B \
/usr/local/libexec/propertyquarry-prater-ai-panorama-controller-v1.py
```

The fixed no-network virgin-volume bootstrap and closeout interfaces are:

```text
/usr/local/bin/python -I -B \
  /usr/local/libexec/propertyquarry-prater-governed-volume-bootstrap-v1.py
/usr/local/bin/python -I -B \
  /usr/local/libexec/propertyquarry-prater-ai-panorama-closeout-v1.py
```

All five have no command-line arguments and no interactive input. The
bootstrap and closeout scripts self-attest their exact root-owned,
single-link, mode-`0555` libexec inode and require effective UID/GID `0:0`.
The controller must
derive the web image by digest from its signed release profile and verify the
same image labels, SBOM, provenance, and deployment receipts used by the
normal runtime release. Workspace images, tags, mutable local builds, and
caller-selected entrypoints are forbidden.

Each container is a one-shot root process with a read-only root filesystem.
The preflight mounts the public-tour volume and controller state read-only.
Only the apply container mounts those locations read-write during the fenced
operation. The fixed mounts are:

| Container path | Access | Source |
| --- | --- | --- |
| `/var/lib/propertyquarry-release-single-host-v2/ai-panorama-artifacts/prater-v1` | read-only | native-authority sealed Prater artifact root |
| `/data/governed_public_property_tours` | read-only preflight; read-write apply | Docker volume `property_propertyquarry_governed_public_tours` |
| `/var/lib/propertyquarry/release-control/ai-panorama-install` | read-only preflight; read-write apply | root-owned permits, tombstones, ledger, operation journal, terminal receipt |
| `/run/propertyquarry-release-control/ai-panorama-install/public-tour-volume-profile.v2.json` | read-only | controller-generated volume profile v2 |
| `/run/propertyquarry-release-control/ai-panorama-install/public-tour-compose-plan.v1.json` | read-only | controller-generated signed Compose and image plan |
| `/run/propertyquarry-release-control/ai-panorama-install/ai-panorama-install-trust-assertion.v1.json` | read-only | controller-generated signed/current trust assertion |
| `/etc/propertyquarry/release-control/ai-panorama-install-keyring.v1.json` | read-only | active external purpose-specific signing keyring |
| `/run/propertyquarry-release-control/ai-panorama-install/prater-ai-panorama-db-secrets.v1.json` | read-only discovery/apply only; absent preflight | ephemeral native-controller DB secret file |
| `/run/propertyquarry-release-control/ai-panorama-install/prater-ai-panorama-closeout-request.v1.json` | read-only closeout only | exact root-owned mode-`0400` canonical closeout marker bytes |

Preflight has no network. Discovery and apply receive the authenticated
`PROPERTYQUARRY_SCHEDULER_DATABASE_URL` runtime value as `DATABASE_URL` and
the authenticated `PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET` through the
native controller's
signed, root-owned runtime materialization; no repository `.env` file or
secret-bearing Docker environment/argument is used. The secret file is a
root-owned regular file with one link, exact mode `0400`, canonical ASCII
JSON+LF, and the exact keys
`schema`, `version`, `DATABASE_URL`, and
`PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET`. Its schema is
`propertyquarry.prater-ai-panorama-db-secrets.v1`, version `1`.
The URL must use `postgresql://` and the exact internal endpoint
`propertyquarry-db:5432`; the erasure secret is at least 32 printable ASCII
characters. The entrypoint reads it descriptor-safely with no symlink
following, exposes the two values only in its in-memory process environment
during the DB phase, restores/unsets them afterward, and never projects them
into stdout, errors, terminals, or journals. For each operation the controller creates a fresh Docker `--internal`
bridge containing only the pinned `propertyquarry-db:5432` service and the
one-shot container, then removes it. The long-lived `property_default`
network, public egress, and the Docker socket are absent. The controller records the container
ID, image digest, runtime
mount identities, network identity, entrypoint, capability set, start/end
times, exit status, and pre/post volume manifests. Numeric mount IDs are used
only within one controller invocation to reject nested mount crossings; they
are not compared across host and container mount namespaces.

The runtime control root is root-owned mode `0700`. The profile, Compose plan,
trust assertion, and DB-secret files are canonical ASCII JSON+LF, regular,
single-link, root-owned mode `0400`, and read descriptor-stably. The native
controller atomically regenerates the first three from authenticated signed
inputs for each service operation. It creates the DB-secret file only for a DB
phase and securely unlinks it immediately afterward. The external keyring
alone remains under read-only `/etc`, root-owned mode `0444`.

Before either container starts, the native authority descriptor-safely copies
and hash-verifies only the exact source bundle, candidate marker, and immutable
materialization receipt into:

```text
bundle/prater-messe-maisonette-ai-360-053ad185e1c44b2e/
bundle/.propertyquarry-ai-panorama-candidate.json
materialization.receipt.json
```

The sealed root and its directories are root-owned mode `0500`; regular files
are root-owned mode `0400`. The immutable receipt retains its historical
`candidate_public_root` under `/docker/property/state`. Governed V2 intake
accepts that relocation only for this exact receipt digest, historical path,
signed sealed layout, and exact source/tree/tour/core/marker identities.

The existing dynamic volume `property_propertyquarry_public_tours` remains
read-write in the normal API, scheduler, and render producer services. That is
required by the existing Crezlo, Magicfit, FeelEstate, floorplan, and ordinary
hosted-tour publication paths. The separate governed volume is mounted
read-only in those long-lived services. The exact protected Prater slug is
resolved only from the governed root; it skips dynamic revocation state,
rejects legacy `<slug>.json`, and never falls back to the dynamic root. Every
other slug continues to resolve from the dynamic root.

The exact Prater slug namespace is reserved from generic binding,
hosted-tour write, browser-proof, generated-reconstruction, import, and
revocation paths. Generic URL binding rejects every URL-like spelling
containing the protected slug after bounded percent-decoding. Only the
dedicated planner may bind the one exact canonical `/control` URL, and it
freshly revalidates the consumed Prater admission and governed volume
identity.

The native-governed one-shot containers are the sole writers for the
governed volume. On first activation only, Docker may present a virgin
root-owned mode-`0755` named-volume root. Native mounts only that volume
read-write into the pinned web image, with no network or secrets, and invokes
the fixed bootstrap with only `CAP_CHOWN`. The bootstrap requires the exact
empty `0:0` root, applies mode `0755` before changing only that root to
`10001:10001`, fsyncs it, re-proves emptiness and identity, and emits the
exact canonical ten-key redacted result
`propertyquarry.prater-governed-volume-bootstrap-result.v1`. Native then binds its
device, inode, ownership, Docker name, logical purpose
`governed-public-tours`, and volume identity into the signed profile and plan
before permit issuance. It must never reinitialize, reset, or ownership-weaken
a non-virgin volume. A post-deploy inspection gate rejects any writable
long-lived governed mount, any alternate governed volume, or an
API/scheduler/render service running a different image than the signed release
plan.

Closeout is a one-way, idempotent native-governed operation. Native supplies
only the fixed request leaf whose bytes are the canonical eight-key marker:
`authority`, `revocation_id`, `revoked_at`, `schema`, `slug`, `status`,
`tour_sha256`, and `version`. The no-network closeout container mounts only
the governed volume read-write and that leaf read-only, uses only
`CAP_DAC_OVERRIDE`, and requires the root inventory to be exactly the Prater
slug before creation, or slug plus the exact existing marker on retry. It
creates the marker with `O_EXCL`, fsyncs the bytes, enforces root ownership,
mode `0444`, one link, fsyncs the directory, and emits byte-identical marker
bytes. An existing byte-identical valid marker succeeds idempotently; any
unknown root entry or different/malformed marker fails without rewrite or
deletion. Long-lived services observe that marker read-only and return
`410 tour_revoked`; malformed closeout state returns
`503 governed_tour_closeout_invalid`.

The web image runs as UID/GID `10001:10001` normally. This one-shot operation
runs as root only because it must update the root-owned replay state and then
set copied tour files to the verified volume runtime owner. Root is not a
substitute for admission: the signed permit, dedicated key use, tombstone,
ledger, fixed context, DB owner binding, and journal are all mandatory.

## Transaction and recovery

Controller state is provisioned exactly once by the authenticated native
package activation, never by a one-shot container. The root, `permits/`, and
`tombstones/` directories are root-owned mode `0700`; these four files are
root-owned mode `0600`:

- `consumption-ledger.v2.json`
- `consumption-ledger.v2.lock`
- `operation-journal.v1.json`
- `operation-journal.v1.lock`

Each lock file is exactly the five bytes `lock\n`. The two JSON files use
independent CSPRNG 128-bit lowercase-hex `instance_id` values. Their exact
canonical JSON+LF genesis payloads are:

```json
{"authority":"propertyquarry-release-control","entries":[],"instance_id":"<independent-32-lowercase-hex>","schema":"propertyquarry.ai-panorama-install-consumption-ledger.v2","sequence":0,"tip_sha256":"0000000000000000000000000000000000000000000000000000000000000000"}
{"authority":"propertyquarry-release-control","entries":[],"instance_id":"<independent-32-lowercase-hex>","schema":"propertyquarry.ai-panorama-install-operation-journal.v1","sequence":0,"tip_sha256":"0000000000000000000000000000000000000000000000000000000000000000"}
```

`build_state_genesis()` in the inert controller source-contract module emits
the exact bytes. Activation must create all four files exclusively, fsync
them and their directory, and bind their identities/digests into the signed
activation receipt. Any partial preexistence, deletion, replacement, or
missing file after activation is a hard failure; the native controller must
never recreate genesis or reset an instance.

The permit is consumed by creating an immutable nonce/request tombstone before
the append-only consumption ledger is changed. A prepared operation-journal
event is durable before the installer starts.

The installer holds the per-slug publication lock and the principal-owned
research-row lock together. It verifies the locked record digest, writes the
private owner receipt in a hidden staging directory, persists the candidate
tour binding with compare-and-swap, fsyncs the staged tree, and only then
renames it into the public volume. A known failure after rename but before the
database transaction is complete removes only the exact newly installed tree.

Terminal journal states are:

- `committed`: target manifest and DB owner binding are both verified;
- `failed-clean`: no public target mutation occurred;
- `rolled-back`: an exact newly installed target was removed;
- `recovery-required`: commit state or public-volume state is ambiguous.

A process death leaves `prepared` without a terminal event. Recovery must
authenticate and revalidate the signed permit, all nonce/request/permit
tombstones, consumption ledger, current trust/profile/Compose/key context, DB
row, and exact public-target manifest. It must never re-consume the permit or
infer success from the presence of a directory alone. Within the bounded
24-hour recovery window, the journal recovery API accepts only the fixed permit
path plus exact expected bindings and performs ledger/tombstone revalidation
internally. A caller-constructed evidence dataclass is rejected. The resulting
evidence may authorize terminal journal classification only; it is never
install authority and cannot retry or mutate the release operation.
There is currently no rollback or recovery execution entrypoint: the native
controller must reject those phases. Apply performs its own exact compensation;
any future recovery entrypoint must be a separately reviewed, fixed
classification-only path.

## Packaging boundary

Run:

```text
python3 scripts/propertyquarry_ai_panorama_controller_contract.py \
  --build-info-json
```

to emit the inert source manifest. An independent build must bind those exact
files into the attested web-image digest and the native controller's signed
operation profile. The source hook reports `authoritative: false` and
`performs_release_effects: false`.

The checked-in generic `install_ai_panorama_tour_bundle.py --apply` path has no
admission object and must continue to fail closed. The public-volume ownership
profile authorizes ownership preparation only; it does not authorize this
separate signed, fenced, journaled content publication.

Controller state is private operational evidence. Retention and cleanup may
archive expired permits and terminal journals only after preserving the
consumption tombstone, terminal receipt digest, and signed audit chain.
Deleting or replacing a ledger, journal, permit, profile, context assertion, or
tombstone never restores release authority.
