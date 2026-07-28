# PropertyQuarry local Docker authority v2

This authority is the authentication root for the repository-owned local
Docker deployment only. It is deliberately not an external production
authority, public-launch authority, or authorization to perform release
effects.

## Trust-on-first-use bootstrap

Bootstrap is an explicit, one-time operation. The target and its temporary
siblings must be under a caller-owned parent that is not group- or
world-writable. Publication uses Linux `renameat2(RENAME_NOREPLACE)`; an
existing or partially occupied target fails closed and is never repaired in
place.

```sh
python3 scripts/propertyquarry_release_local_identity.py bootstrap \
  --state-root /docker/property/state/runtime/propertyquarry-release-authority-v2 \
  --candidate-sha CANDIDATE_40_HEX \
  --workflow-sha WORKFLOW_40_HEX

python3 scripts/propertyquarry_release_local_identity.py verify \
  --state-root /docker/property/state/runtime/propertyquarry-release-authority-v2
```

The mode-`0700` state contains six independent Ed25519 PKCS#8 private keys,
six matching SPKI anchors, an exact nine-file `package-input/` bundle, and an
evidence-key-signed bootstrap receipt. Every file is mode `0600`. The bundle
is accepted unchanged by the existing exact-19-role payload assembler. Its
reserved `propertyquarry-local-authority.invalid` HTTPS endpoints satisfy the
closed configuration schemas while remaining deliberately unreachable until
a governed local service supplies those interfaces.

The signed receipt binds the SHA-256 digest of every private key, external
anchor, and package-input file. Verification reads the exact state through a
pinned directory descriptor, requires each canonical private key to match its
external and package-input public anchors and signed key ID, and reconstructs
the controller, watchdog, and policy bytes from the signed candidate and
workflow identities. The same audit runs immediately before and after the
publication rename. A late failure quarantines and removes only the exact
published state inode; it never deletes a replacement path.

Private keys must remain inside this state. Bootstrap receipt data, public
anchors, authenticated packages, and logs must never contain private-key
bytes. Rotation means provisioning a new empty state root and deliberately
switching trust; replacing files inside a trusted state is not rotation.

## Authenticated package wrapper

The unsigned payload remains byte- and mode-identical under `payload/`. A
detached authentication document and raw signature are added beside it:

```text
authenticated-wrapper/
  payload/                    # unchanged 21-file, 19-role payload
  authentication.v2.json      # canonical ASCII JSON, mode 0644, no newline
  authentication.v2.sig       # raw 64-byte Ed25519 signature, mode 0644
```

```sh
python3 scripts/propertyquarry_release_authenticated_package.py sign \
  --payload /absolute/unsigned-payload \
  --private-key /absolute/package-authority-v2.key \
  --external-anchor /absolute/package-authority-v2.pem \
  --output /absolute/authenticated-wrapper

python3 scripts/propertyquarry_release_authenticated_package.py verify \
  --wrapper /absolute/authenticated-wrapper \
  --external-anchor /absolute/package-authority-v2.pem
```

Verification requires the external anchor, the payload-contained package
anchor, and the signer key ID to agree. It binds the exact payload tree,
installation manifest, unsigned payload receipt, and native build receipt.
The signed message is:

```text
"propertyquarry.release-control.local-package-authentication.v2\0"
|| uint64be(authentication_json_length)
|| canonical_authentication_json
```

The tree digest uses the same framing with the
`propertyquarry.release-control.payload-tree.v2\0` domain and canonical sorted
file/directory entries. The wrapper is assembled privately, reverified, and
published with no-replace semantics. The external anchor is not copied into
the wrapper as an independent trust source.

## Interoperability vector

The fixed-seed vector is pinned by
`test_fixed_seed_cross_language_authentication_vector`:

- seed: `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f`
- key ID: `sha256:a050837d85070582ccf7394b0988847cc312cb88259b894899f6f239cf1791a5`
- framed tree digest: `sha256:8d534078cd1a91ef61a7e6a4cd7010fe482f2d76ea21941903f3c512fa66d8f6`
- authentication JSON digest: `sha256:c221a605da6cd350b18372821ff7fb924455e94db6f2598fa50d2e9efc25c109`
- framed signed-message digest: `sha256:7fdace265c86646a01652f653484a1aa12c870f2b40e97d3112846df85e5134a`

Run the focused gates with:

```sh
PYTHONDONTWRITEBYTECODE=1 pytest -q \
  tests/test_propertyquarry_release_local_identity.py \
  tests/test_propertyquarry_release_authenticated_package.py
```
