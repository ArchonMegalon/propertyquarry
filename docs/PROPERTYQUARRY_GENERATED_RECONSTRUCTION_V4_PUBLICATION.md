# Generated reconstruction v4 publication authority

This lane publishes one exact, audited PropertyQuarry generated reconstruction.
Its production entry point is the package/self-bound static installer in its
digest-pinned scratch image. A repository checkout, an installed controller,
the render container, a GitHub credential, EA, and MyExternalBrain are not
publication authorities.

The compiled permit authorizes only:

- slug
  `ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d`;
- reconstruction kind `layout_preview`;
- full 23-file source tree
  `sha256:0ca9b17ddea489b013cafd1c72416e943becf071d0b369847587e52e4138b73a`;
- exact 21-file served tree
  `sha256:d69c032b96264d892bbd6e269b884a9f33cc11cf3d0f5a7d96a878a062058548`;
- manifest
  `sha256:5bc06e8758bc3d9b9e88a82a7608a1238340c19fd581d3ec3565a56d75d1fa06`;
- browser receipt
  `sha256:800b2ba29a7c33ec64651db26ef23a1e0756d0223286eb43d742fc23c6bb34f8`;
- browser evidence tree
  `sha256:838ff0b8e236dbd5e5695c6bc20a862182d42075bba4202cd1a63083bfcb0d08`;
- walkthrough
  `sha256:16197ec466ed41eab0ed05034e8e0132a1f15cd2387263d792b7b4492c6d7aed`;
- quality receipt
  `sha256:0fc7dc5b0a63a49bfd3d63a9c748a0a04d7397a46d5f81d3b517d85ca753b721`.

The permit also binds every path, byte count, mode, and SHA-256, plus the tour,
reconstruction, render-commit, floor-plan, GLB, viewer, and vendor assets. The
source-only `tour.private.json` and `.propertyquarry-render-commit.json` must
remain mode `0600`; they are validated and bound into the source-tree receipt
but never copied into the served tree.

## Authority and filesystem boundary

The self-contained dispatch refuses unless all of the following hold:

1. the host wrapper proves the local Docker daemon and an exact local `sha256:`
   image ID, saves and inspects the image, and verifies that its scratch rootfs
   contains exactly the expected static installer;
2. the wrapper verifies the distinct tour-publication package with the
   independent package anchor before starting the bounded root-helper
   container; the ordinary runtime-package verifier rejects this protocol;
3. the container has no network, no shell, no generic exec, a read-only
   rootfs, a fixed host bind, and only the capabilities needed for host chroot
   and the bounded filesystem operation;
4. inside the container, the root helper re-verifies the package, its own
   binary digest/size/mode, and the shared source-manifest/static-ELF binding;
5. after chrooting onto the host, the authority independently re-verifies the
   canonical-authority-signed receipt-key bootstrap and the short-lived signed
   tour materialization, including host machine ID, build/source digest, exact
   f7 artifact, destination, five-operation allowlist, and receipt key from
   package memory, then accepts only the exact v4 command/argument grammar.

The tour package has a distinct signature domain and exactly nine material
members: the source-bound controller and native build receipt, canonical
package anchor, signed receipt-authority bootstrap, bootstrapped receipt key
and anchor, and signed tour materialization. It contains no runtime plan,
runtime deployment helper, runner material, systemd unit, sysusers/tmpfiles
definition, or install path. Its signed claims explicitly forbid host
installation, runtime deployment, network use, and persistent credential
installation. The materialization is valid for exactly 3,600 seconds for a new
publish. Same-transaction recovery remains governed by the durable signed
prepared receipt and exact compare-and-swap values.

The only destination is
`/var/lib/docker/volumes/property_propertyquarry_public_tours/_data/<permit-slug>`.
Receipts live in the root-owned mode-`0700`
`/var/lib/propertyquarry-release-single-host-v2/tour-publication-receipts`.
The self-contained authority creates only those fixed state directories when
needed. Every path component is opened without following symlinks.

Publication rejects extra or missing paths, symlinks, hardlinks, special files,
wrong file or directory modes, byte/hash/size drift, source replacement during
staging, public geographic-coordinate keys, exact private identity/location
values in public files, and any live-tree compare-and-swap mismatch.

Files are copied to a no-replace stage inside the same-volume, root-owned
mode-`0700` `.propertyquarry-publisher-v4` control directory and individually
fsynced. The authority retains an open descriptor for that directory and
revalidates its name-to-inode binding across every exchange. The control
directory and volume root are fsynced before mutation. First publication uses
cross-directory `renameat2(RENAME_NOREPLACE)`; replacement uses
`renameat2(RENAME_EXCHANGE)`. A replacement retains the exact old inode under a
deterministic rollback name inside that control directory. Name substitution
detected immediately after an exchange is reversed through the retained
descriptor before the command fails closed. A receipt-authority-signed
prepared receipt is durable before the rename, and a separately signed terminal
receipt binds the old tree, new live tree, retained rollback tree, source
artifact, all audit hashes, tour materialization/source authority, and receipt
key.

## Exact production sequence

Bootstrap the fixed receipt authority if it is absent, then build a
package-authority-bound controller and installer, create the short-lived
machine-bound tour materialization, and build/verify the distinct tour package.
All output paths must be fresh absolute paths:

```sh
PQ_NATIVE='/docker/property/native/propertyquarry-release-single-host-v2'
PQ_TOOLCHAIN='/docker/property/state/runtime/propertyquarry_go_toolchain_1.26.5/go1.26.5.linux-amd64.tar.gz'
PQ_PACKAGE_ANCHOR='/etc/propertyquarry-release-control-v2/package-authority-v2.pem'
PQ_BUILD='/absolute/fresh/private/path/native-build'
PQ_MATERIALIZATION='/absolute/fresh/private/path/tour-materialization'
PQ_SIGNED_PACKAGE='/absolute/fresh/private/path/propertyquarry-tour-publication-v4.tar'

python3 "$PQ_NATIVE/tools/materialize.py" bootstrap-authority
"$PQ_NATIVE/tools/build.sh" "$PQ_TOOLCHAIN" "$PQ_BUILD" "$PQ_PACKAGE_ANCHOR"
python3 "$PQ_NATIVE/tools/tour_package.py" materialize \
  --controller "$PQ_BUILD/propertyquarry-release-single-host-v2" \
  --native-build-receipt "$PQ_BUILD/build-receipt.v2.json" \
  --output "$PQ_MATERIALIZATION"
python3 "$PQ_NATIVE/tools/tour_package.py" build \
  --controller "$PQ_BUILD/propertyquarry-release-single-host-v2" \
  --native-build-receipt "$PQ_BUILD/build-receipt.v2.json" \
  --materialization-root "$PQ_MATERIALIZATION" \
  --output "$PQ_SIGNED_PACKAGE"
python3 "$PQ_NATIVE/tools/tour_package.py" verify \
  --package "$PQ_SIGNED_PACKAGE" \
  --package-authority-public-key "$PQ_PACKAGE_ANCHOR"
```

Build the deterministic scratch image from that exact native build and retain
the receipt emitted by the builder. Then set the exact image ID from the
receipt; tags are not accepted:

```sh
PQ_IMAGE_RECEIPT='/absolute/fresh/private/path/installer-image-receipt.json'
"$PQ_NATIVE/tools/build-installer-image.sh" "$PQ_BUILD" "$PQ_IMAGE_RECEIPT"

PQ_TOUR_DISPATCH="$PQ_NATIVE/tools/dispatch-tour-v4-with-docker.sh"
PQ_INSTALLER_IMAGE='sha256:<exact-64-lowercase-hex-image-id>'
```

The package must be mode `0400`, the anchor mode `0444`, and both must be
single-link regular files. `tour-v4-authority-info` is non-authoritative and
read-only; it is useful for checking the compiled permit:

```sh
"$PQ_TOUR_DISPATCH" \
  "$PQ_INSTALLER_IMAGE" "$PQ_SIGNED_PACKAGE" "$PQ_PACKAGE_ANCHOR" \
  tour-v4-authority-info
```

Obtain a signed live inspection. Its
`payload.expected_old_tree_argument` is the only value allowed in the publish
command. It is either the explicit sentinel `absent` or a `sha256:` tree digest:

```sh
PQ_INSPECTION_COPY="$(mktemp -p /tmp propertyquarry-tour-v4-inspection.XXXXXX)"
chmod 0600 "$PQ_INSPECTION_COPY"
"$PQ_TOUR_DISPATCH" \
  "$PQ_INSTALLER_IMAGE" "$PQ_SIGNED_PACKAGE" "$PQ_PACKAGE_ANCHOR" \
  tour-inspect-v4 \
  --expected-manifest-sha256 \
  sha256:5bc06e8758bc3d9b9e88a82a7608a1238340c19fd581d3ec3565a56d75d1fa06 \
  > "$PQ_INSPECTION_COPY"
```

Verify that signed wrapper with the installed receipt anchor. Then copy the
exact `expected_old_tree_argument` into `PQ_EXPECTED_OLD_TREE`; do not infer or
normalize it. For this replacement workflow it must be a `sha256:` digest, not
`absent`, and the inspected live predecessor must still contain its private
owner receipt.

Before the public-only exchange, bind the exact principal-scoped terminal
search-run candidate while that private receipt remains live. Keep the
principal in the environment, use the exact identity values from the private
receipt, and retain both redacted receipts:

```sh
PQ_BIND_SCRIPT='/docker/property/scripts/bind_property_search_candidate_tour.py'
PQ_BIND_RUN_ID='<exact-search-run-id>'
PQ_BIND_CANDIDATE_REF='<exact-candidate-ref>'
PQ_BIND_LISTING_ID='<exact-listing-id>'
PQ_BIND_TOUR_URL='https://propertyquarry.com/tours/ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d'
PQ_BIND_DISCLOSURE='<exact-public-disclosure>'
PQ_BIND_DRY_RUN="$(mktemp -p /tmp propertyquarry-tour-bind-dry-run.XXXXXX)"
PQ_BIND_APPLY="$(mktemp -p /tmp propertyquarry-tour-bind-apply.XXXXXX)"
chmod 0600 "$PQ_BIND_DRY_RUN" "$PQ_BIND_APPLY"
export PROPERTYQUARRY_TOUR_BINDING_PRINCIPAL_ID='<exact-principal-id>'

python3 "$PQ_BIND_SCRIPT" \
  --run-id "$PQ_BIND_RUN_ID" \
  --candidate-ref "$PQ_BIND_CANDIDATE_REF" \
  --listing-id "$PQ_BIND_LISTING_ID" \
  --tour-url "$PQ_BIND_TOUR_URL" \
  --reconstruction-kind layout_preview \
  --disclosure "$PQ_BIND_DISCLOSURE" \
  > "$PQ_BIND_DRY_RUN"
```

Stop unless the dry-run says either `status=change_required` or
`status=already_bound`. For `change_required`, copy its exact `before_sha256`
without normalizing it and apply once:

```sh
PQ_BIND_BEFORE_SHA256='<exact-before_sha256-from-fresh-dry-run>'
python3 "$PQ_BIND_SCRIPT" \
  --run-id "$PQ_BIND_RUN_ID" \
  --candidate-ref "$PQ_BIND_CANDIDATE_REF" \
  --listing-id "$PQ_BIND_LISTING_ID" \
  --tour-url "$PQ_BIND_TOUR_URL" \
  --reconstruction-kind layout_preview \
  --disclosure "$PQ_BIND_DISCLOSURE" \
  --expected-record-sha256 "$PQ_BIND_BEFORE_SHA256" \
  --apply \
  > "$PQ_BIND_APPLY"
```

Stop unless apply says `status=applied`, or an exact concurrent writer made it
return `status=already_bound` with the strict metadata below. If the dry-run
itself says `status=already_bound`, do not perform a compare-and-swap. Every
accepted `already_bound` receipt must say `changed=false`, all candidate
occurrences matched, no occurrence updated, and
`binding_verified_from=principal_scoped_terminal_run`.

Only after the binding is proven may the public-only publication begin. Choose
one fresh 32-lowercase-hex transaction ID and retain it for publish, recovery,
and rollback:

```sh
PQ_EXPECTED_OLD_TREE='sha256:<exact-64-lowercase-hex-inspected-live-tree>'
PQ_TOUR_TRANSACTION_ID='f7ddedd7bd1cad7a8074620860000001'
PQ_PUBLICATION_COPY="$(mktemp -p /tmp propertyquarry-tour-v4-publication.XXXXXX)"
chmod 0600 "$PQ_PUBLICATION_COPY"
"$PQ_TOUR_DISPATCH" \
  "$PQ_INSTALLER_IMAGE" "$PQ_SIGNED_PACKAGE" "$PQ_PACKAGE_ANCHOR" \
  tour-publish-v4 \
  --bundle \
  /tmp/property-f7-tour-final-v4.HUQw8lU4/ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d \
  --expected-manifest-sha256 \
  sha256:5bc06e8758bc3d9b9e88a82a7608a1238340c19fd581d3ec3565a56d75d1fa06 \
  --expected-old-tree "$PQ_EXPECTED_OLD_TREE" \
  --transaction-id "$PQ_TOUR_TRANSACTION_ID" \
  > "$PQ_PUBLICATION_COPY"
```

Stop unless the signed terminal payload says `status=succeeded`, binds exactly
21 public files, says `private_source_files_published=false`, and contains the
artifact, manifest, browser, quality, and walkthrough hashes listed above.
Also rerun the binding command without `--apply` after publication. The
private receipt is now intentionally absent, so the post-state check must say
`status=already_bound`, `changed=false`, all occurrences matched, no
occurrence updated, and
`binding_verified_from=principal_scoped_terminal_run`. Never retry with a new
expected-old value or transaction ID after an ambiguous publication failure.

Recover the same prepared transaction with the same CAS values:

```sh
"$PQ_TOUR_DISPATCH" \
  "$PQ_INSTALLER_IMAGE" "$PQ_SIGNED_PACKAGE" "$PQ_PACKAGE_ANCHOR" \
  tour-recover-v4 \
  --expected-manifest-sha256 \
  sha256:5bc06e8758bc3d9b9e88a82a7608a1238340c19fd581d3ec3565a56d75d1fa06 \
  --expected-old-tree "$PQ_EXPECTED_OLD_TREE" \
  --transaction-id "$PQ_TOUR_TRANSACTION_ID"
```

For a replacement only, rollback requires both the original old-tree digest
and an explicit CAS on the current published tree:

```sh
"$PQ_TOUR_DISPATCH" \
  "$PQ_INSTALLER_IMAGE" "$PQ_SIGNED_PACKAGE" "$PQ_PACKAGE_ANCHOR" \
  tour-rollback-v4 \
  --expected-manifest-sha256 \
  sha256:5bc06e8758bc3d9b9e88a82a7608a1238340c19fd581d3ec3565a56d75d1fa06 \
  --expected-old-tree "$PQ_EXPECTED_OLD_TREE" \
  --expected-current-tree \
  sha256:d69c032b96264d892bbd6e269b884a9f33cc11cf3d0f5a7d96a878a062058548 \
  --transaction-id "$PQ_TOUR_TRANSACTION_ID"
```

There is no rollback tree for an `absent` first publication. Recovery remains
available because the durable prepared receipt and candidate inode fully bind
the no-replace transition.

This bind-before-publish workflow does not authorize an `absent` first
publication. With no live private owner receipt and no exact persisted binding,
the first bind must fail closed; the public manifest, slug, URL, and public
property digest are not ownership evidence. Stop rather than publishing an
unbound first tree. A separately reviewed owner-proven first-publication
procedure is required before using v4 no-replace publication for a new slug.

For a replacement, a stale bind fingerprint or failed bind is resolved by
leaving the live predecessor untouched, obtaining a fresh signed inspection,
and repeating dry-run before apply. If publication outcome is ambiguous,
recover the same prepared transaction first. If a signed successful
publication later fails the binding post-state verification, use the original
transaction's exact rollback CAS to restore the retained predecessor; then
obtain a fresh signed inspection, repeat the owner-proven bind, and use a fresh
authorized publication transaction. Never infer new CAS values from the live
directory and never mix values across transactions.

## Test acceptance

The native tests cover no-replace first publication, replacement exchange,
control-directory name drift with automatic exchange reversal, post-exchange
recovery, CAS rollback, idempotent terminal receipts, privacy leaks, extra and
missing paths, symlinks, hardlinks, wrong modes, and CAS mismatch without live
mutation.

Run the exact audited artifact acceptance in addition to the ordinary native
suite:

```sh
PROPERTYQUARRY_TOUR_V4_TEST_BUNDLE=/tmp/property-f7-tour-final-v4.HUQw8lU4/ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d \
  go test ./internal/authority -run TestTourV4AuditedArtifactMatchesCompiledPermit
```
