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
2. the wrapper verifies the signed package with the independent package anchor
   before starting a privileged container;
3. the container has no network, no shell, no generic exec, a read-only
   rootfs, a fixed host bind, and only the capabilities needed for host chroot
   and the bounded filesystem operation;
4. inside the container, the root helper re-verifies the package, its own
   binary digest/size/mode, and the shared source-manifest/static-ELF binding;
5. after chrooting onto the host, the authority independently re-verifies the
   detached signed profile, plan, host machine ID, and receipt key from package
   memory, then accepts only the exact v4 command/argument grammar.

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
artifact, all audit hashes, installed profile, runtime, workflow, deployment,
and receipt key.

## Exact production sequence

Use only the newly built package and digest-pinned installer image. Set these
three values from the verified package/image build receipts; tags are not
accepted:

```sh
PQ_TOUR_DISPATCH='/docker/property/native/propertyquarry-release-single-host-v2/tools/dispatch-tour-v4-with-docker.sh'
PQ_INSTALLER_IMAGE='sha256:<exact-64-lowercase-hex-image-id>'
PQ_SIGNED_PACKAGE='/absolute/path/propertyquarry-release-single-host-v2.tar'
PQ_PACKAGE_ANCHOR='/absolute/path/package-authority-v2.pem'
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
normalize it. Choose one fresh 32-lowercase-hex transaction ID and retain it for
publish, recovery, and rollback:

```sh
PQ_EXPECTED_OLD_TREE='absent'
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
Never retry with a new expected-old value or transaction ID after an ambiguous
failure.

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
