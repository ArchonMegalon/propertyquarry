# PropertyQuarry release package payload v2

Status: unsigned, non-installing, non-authoritative payload assembly. This tool
does not create a `.deb`, authenticate its inputs, verify a signature, install
under `/`, or establish release readiness.

## Purpose and invocation

`scripts/propertyquarry_release_package_payload.py` creates one deterministic
file-content and projected-metadata payload for the fixed 19-role installation
model:

```sh
python3 scripts/propertyquarry_release_package_payload.py \
  --native-bundle /absolute/path/linux-amd64 \
  --private-bundle /absolute/path/private-material \
  --service-gid 1999 \
  --output /absolute/controlled-parent/package-payload-linux-amd64
```

The output parent must already exist, be owned by the invoking uid, have no
group/other write bits, and contain no symlink component. The output itself
must not exist. The assembler creates temporary and final payload material only
inside that pinned output-parent directory. A root caller can choose a
root-owned output parent, so the tool does write the requested payload tree;
its narrower guarantee is that it never maps or writes the 19 role paths into
the live `/` filesystem and performs no installation or repair.

The successful output is exactly:

```text
OUT/
  rootfs/                              # exact 19 role files
  installation-manifest.v2.json       # canonical production projection
  package-payload-receipt.v2.json      # written and fsynced last
```

`OUT` remains mode `0700`. Public/rootfs directories use fixed `0755` modes;
the private configuration/trust directory boundary uses `0750`. Determinism is
scoped to relative paths, file bytes, file/directory modes, and projected
manifest metadata. Filesystem inode numbers and ctime/mtime values are neither
normalized nor claimed to be archive-level reproducible.

## Closed inputs

Input names are code-fixed and never selected by a caller manifest.

The native bundle must contain exactly these four single-link regular files:

- `build-receipt.json` at mode `0644` and no more than 64 KiB;
- `propertyquarry-release-controller-v2` at mode `0755`;
- `propertyquarry-release-supervisor-v2` at mode `0755`; and
- `propertyquarry-release-watchdog-v2` at mode `0755`.

The assembler strictly parses the closed native build-receipt schema and binds
the three binary sizes and SHA-256 digests, pinned Go toolchain/archive/front
end, source-manifest digest grammar, build flags, linker flags, closed build
environment, reproducibility statements, and false authentication/signing/
installation fields. It never executes supplied binaries or invokes their
build-info entrypoints. A matching receipt is an integrity statement only:
the receipt itself says builder identity and package signature are unverified.

The exact seven repository templates remain unchanged in
`packaging/propertyquarry-release-control-v2/`. Each is capped at 1 MiB. The
assembler consumes them as public role bytes but never adds generated content
to that templates-only directory.

The private bundle is exactly:

```text
controller-v2.json
watchdog-v2.json
policy-v2.json
trust.d/request-authority-v2.pem
trust.d/response-authority-v2.pem
trust.d/lifecycle-cas-v2.pem
trust.d/evidence-authority-v2.pem
trust.d/resource-mediator-v2.pem
trust.d/package-authority-v2.pem
```

Each private input must be nonempty, no larger than 1 MiB, and have one of
`0400`, `0440`, `0600`, or `0640`. Controller and watchdog JSON are parsed with
duplicate-key, non-finite, control-character, surrogate, depth, and closed
repository-schema checks. The root policy must be the canonical closed
`propertyquarry.release-root-policy.v2` object. Trust-root bytes are opaque and
hash-bound only: this lane does not authenticate them, parse a certificate or
public-key profile, or bootstrap trust from the packaged package-authority
file. Production remains blocked until an out-of-band trusted authority binds
and authenticates all material.

All three input roots reject extra/missing entries, unsafe directories,
symlinks, hardlinks, special files, empty/oversized files, mode drift, same-size
byte changes, path replacement, and concurrent metadata changes. Native
binaries retain the installation model's 128 MiB per-file ceiling; the lower
receipt/template/private limits prevent untrusted metadata from forcing an
unbounded aggregate read.

## Manifest and ownership projection

`installation-manifest.v2.json` uses the existing
`propertyquarry.release-installation-manifest.v2` schema. Entries occur only in
`ROLE_CONTRACTS` order and contain exact role, absolute installation path,
SHA-256, size, integer mode, uid, and gid:

- three executables: `0755`, uid/gid `0`;
- seven public templates: `0644`, uid/gid `0`; and
- nine private roles: `0640`, uid `0`, gid equal to the required positive
  target-bound `--service-gid`.

Those numeric owners are projected package metadata. The assembler does not
chown staging files. It creates a second in-memory simulation manifest whose
only differences are uid/gid values projected from actual staging ownership,
requires every other field to equal the production projection, and calls
`audit_installation(..., mode="simulation")` against the isolated temporary
root. It never calls production audit. A successful simulation remains
explicitly non-authoritative and does not prove production ownership.

## Atomic publication and race boundary

Sources and destinations are opened descriptor-relative with `O_NOFOLLOW` and
bounded reads. Files are created with `O_EXCL`, copied, fsynced, re-read, and
matched to expected bytes. Directories are fchmoded and checked so a restrictive
caller umask cannot change the fixed `0755`/`0750` payload layout. The canonical
manifest is written and fsynced before simulation audit. The deterministic
receipt is written and fsynced last.

Before publication, the assembler snapshots the complete temporary tree:
exact top-level set, all 19 role bytes, manifest and receipt bytes, every
directory, modes, owners, and directory identities. It reopens the output
parent pathname and binds it to the pinned parent descriptor. Every rootfs
role's uid/gid and stable device/inode/mode/link identity remains bound to the
simulation-audited snapshot on both the pre-rename and post-rename checks. The
manifest and receipt must remain regular single-link `0644` files owned by the
invoking uid/gid. It then binds the temporary name to the still-open
temporary-root descriptor, performs
same-parent `renameat2(RENAME_NOREPLACE)`; then reopens the destination,
requires the same inode, rechecks the complete tree, reopens the parent again,
and fsyncs the parent. An existing or racing destination is never replaced.

Linux does not provide a portable rename-directory-by-open-fd operation. A
same-uid adversary therefore retains a very small name-swap window between the
last source-name check and `renameat2`. The before/after inode and full-content
checks fail closed if that window is won, but they cannot turn a same-uid
hostile environment into a trusted builder. Assembly must run in a dedicated
account/container with a private output parent for stronger isolation.

Cleanup is descriptor-relative to the pinned parent and first binds the temp
name to its expected inode. If the temp name has already been swapped, cleanup
refuses to traverse or delete the replacement and intentionally leaves both
trees for operator quarantine. If the final rename succeeds but the parent
fsync fails, the typed result is `output-parent-durability-unknown`; the visible
destination is left intact because rollback could destroy the only valid copy.
The operator must quarantine and inspect that destination before retrying.

## Receipt claims

The deterministic receipt contains no timestamp, hostname, temp path, inode,
or systemd version. It binds the native build-receipt digest, aggregate native
material, exact seven templates, private material, canonical installation
manifest, service-gid projection, role count, and simulation summary. It says:

```json
{
  "authoritative": false,
  "production_ready": false,
  "readiness_authority": false,
  "payload_signed": false,
  "installs_or_repairs": false,
  "writes_payload_output": true,
  "payload_material_writes_only_within_output_parent": true,
  "performs_installation_writes": false,
  "root_install_performed": false,
  "package_signature_verified": false,
  "verifies_signatures": false,
  "builder_identity_authenticated": false,
  "input_authentication_verified": false,
  "native_bundle_authenticated": false,
  "private_material_authenticated": false,
  "production_ownership_verified": false,
  "receipt_published_last": true
}
```

## Verification lanes

The mandatory synthetic lane is self-contained:

```sh
pytest -q tests/test_propertyquarry_release_package_payload.py
```

It covers deterministic double assembly under umask `0077`, exact metadata,
input and JSON attacks, no native execution, late role/manifest/receipt and
directory mutation, parent/temp swaps, no-replace races, descriptor cleanup,
resource limits, and durability ambiguity.

The same file contains an explicit real-bundle integration node. It consumes
`build/propertyquarry-release-control-v2/linux-amd64/` when that generated exact
four-file bundle exists and otherwise skips only that node. The native build
recipe must produce and independently verify that bundle before treating the
real lane as a release receipt.

CI or an operator can make that prerequisite fail-visible rather than skipped:

```sh
PROPERTYQUARRY_REQUIRE_REAL_NATIVE_BUNDLE=1 \
  pytest -q tests/test_propertyquarry_release_package_payload.py
```

`native/propertyquarry-release-control-v2/tools/stage-verify.sh` remains a
separate trusted-local-build static-unit and metadata-consistency gate. It does
not execute bundle binaries. Its host-dependent systemd result is intentionally
absent from deterministic payload bytes and is not an input-authentication
substitute.

## Durable non-production proof

**WARNING: `_completion/controller-bundle-v2-20260717/` is an unsigned,
non-production proof. Its nine-file private fixture contains only `.invalid`
endpoints and deterministic test-public-key text. It contains no trusted key or
secret and must not be installed.**

This proof is workspace-local under the gitignored `_completion/` tree. It is
not versioned, shipped, or available from a fresh checkout; operators must
preserve or export it separately if they need the local evidence bytes.

The proof uses projected service gid `1999` and the exact generated native
four-file bundle available during this run. Its external evidence receipt is
`_completion/controller-bundle-v2-20260717/external-evidence-receipt.json` with
SHA-256
`ae1998ba1479748bafc9d8a6ffee7110697a18fda6b7c09075ff091004fef7ec`.
The bound core hashes are:

- installation manifest:
  `bec34a94c0e41753c1deb39673e8c2d77f725a17e1aa543bd0f7782cf07a9135`;
- payload receipt:
  `6b78b5136a93edeb0c682b70f81740b1d6721eeb7d8eb01f0cb98f31d208ccd8`;
- payload tree:
  `ceeb87870462f1f815273f12b091bd1d64d2deeeea2f0ad53de918ec450e8d03`;
  and
- private fixture tree:
  `b24112762c5c4d9012073c64536a554dc0c7839a96876a1aa7ec736a8e1cb067`.

These hashes are durable build evidence only, not authentication, signing,
installation, or readiness evidence.
