# PropertyQuarry Release Security Gate

This gate covers only the PropertyQuarry Python dependency lock and the two
PropertyQuarry runtime images. It does not inspect the legacy EA Compose stack
or unrelated host images. The controller installs nothing and never pulls an
image or vulnerability database.

## Flagship controller contract

The installed v2 release controller must run this gate as a protected security
phase before it admits a candidate. The `propertyquarry-release-v2` workflow
hands the immutable GitHub candidate identity to that controller; it does not
execute scanners from the checkout. The disabled legacy
`propertyquarry-flagship-security` CI job is retained only as inert migration
history and is not release evidence.

Provision the protected security runtime with:

- `pip-audit==2.10.1` inside the authenticated release-Python tree;
- root-owned, singly linked `0755` Syft `1.44.0` and Trivy `0.72.0`
  binaries at `/opt/propertyquarry-security/bin`, with the exact source-pinned
  SHA-256 digests;
- the root-owned `/etc/propertyquarry/security-runtime.v1.json` manifest;
- root-owned, singly linked `0644` empty Syft and Trivy YAML configurations
  below `/etc/propertyquarry/security-scanners`; and
- root-owned, singly linked `0644` vulnerability and Java databases below
  `/var/lib/propertyquarry-security/trivy`;
- the exact web and render images already present in the local Docker daemon;
- database metadata whose root-manifest-bound hashes, schema versions,
  timestamps, and `NextUpdate` deadlines all validate; and
- no scanner configuration or inherited environment that can weaken the fixed
  command flags.

Scanner installation, image loading, and vulnerability-database refresh are
separate governed controller-maintenance actions. They do not occur in the
candidate checkout or release workflow. Missing tools, local images, databases,
scanner output, or valid SBOMs fail flagship mode closed and still produce an
atomic receipt where Python can start.

Configure the controller's canonical release plan with immutable image
references, not mutable tags:

```text
PROPERTYQUARRY_WEB_IMAGE=registry.example/propertyquarry-web@sha256:<64-hex>
PROPERTYQUARRY_RENDER_IMAGE=registry.example/propertyquarry-render@sha256:<64-hex>
```

The release commit is the full candidate SHA authenticated by the installed
controller. The controller-owned security receipt must pass and bind that SHA
plus both immutable image identities before migration or promotion authority
can be consumed.

## Web runtime construction

The standalone web image is a two-stage build pinned to the exact
`linux/amd64` child manifests, rather than mutable tags or multi-platform
indexes:

```text
python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464
gcr.io/distroless/cc-debian13@sha256:d0b79eb697888ecb8ef019bbb7192e4f41974830ea95f0543123eaaeb2d5fd2c
```

The Python stage installs only from the committed
`vendor/propertyquarry-wheelhouse/cp312-linux-x86_64` wheelhouse. Its 37 wheels
cover every exact package in `ea/requirements.lock` plus the pinned installer,
and `ea/requirements.wheelhouse.lock` binds each filename to one SHA-256.
Installation uses `--no-index`, `--find-links`, and `--require-hashes`; the
wheelhouse and installer are removed before the runtime stage. A forced
no-cache Docker build with `--network none --pull=false` succeeds, proving that
neither PyPI nor a previous network-enabled build cache is part of the build
contract. `tests/test_propertyquarry_wheelhouse_contract.py` authenticates the
inventory, archive safety, package metadata, target-compatible wheel tags,
hashes, modes, and Dockerfile contract.

The shipped stage is the pinned Distroless Debian 13 `cc` image and runs as
numeric UID/GID `10001:10001`. It has no shell entrypoint, package manager,
`curl`, or runtime `pip`; its default command is `python -m app.runner`, so the
Compose migration command can still replace the image command without an
entrypoint shim.

Python native modules require five Debian shared-library packages that are not
in the Distroless base: `libbz2-1.0`, `libcrypt1`, `libffi8`, `liblzma5`, and
`libsqlite3-0`. The build copies only those package-owned paths and writes each
exact package status stanza into `/var/lib/dpkg/status.d`. This is a security
boundary: copied native libraries must remain visible to Syft and Trivy.
`tests/test_propertyquarry_web_image_contract.py` fixes the stage, package
inventory, no-shell/no-package-manager, health-probe, and Compose-command
contracts.

On 2026-07-26, the authenticated local builder captured candidate
`4ed9e233b1fd5bf9e13791ad78de2e2c6d165be8` and evidence envelope
`3ef72860be567fd8ae2633272e6b8bb2ab41667f`, published the result to a
loopback-only Registry v2, observed the registry manifest through a direct
bounded `HEAD`, removed local tag authority, and pulled the image by immutable
digest:

```text
127.0.0.1:5000/propertyquarry/web@sha256:4b44b4d039cdfd5e26ae6cea9407c74d6824640c9169fb59113dc42cd61b9c1d
```

The canonical private build receipt has SHA-256
`fa8c18b31b3c599331eb6625be1f0164c60aede194360e33b6b321b547825e7a`.
It binds image configuration
`sha256:f5461296ba764f8e0981705b97ada98ffeec56d911df4a34e9ad630c4b873866`
and independently reconstructed local OCI manifest
`sha256:f99c7f4c70d7f4e0bf9656772460944e6cee003ead88bc2a5f672bfd627590df`;
the local archive manifest and compressed registry manifest are distinct
identities and are recorded separately. A read-only, capability-dropped
container created from the repository digest became Docker-healthy and returned
HTTP 200 from `/health/live`.

The pinned Syft scan of that repository digest produced a CycloneDX artifact
with SHA-256
`3824af87e16fbe43e0db0335c6eedc0fea208e61b7518a6d186f035b9e1107eb`
and 1,427 components: 2 applications, 1,369 files, 55 libraries, and 1
operating system. The pinned offline Trivy scan produced JSON with SHA-256
`83a981ce3f25ed35fa4daa499cb15ce6ad79158e42e567b7eea0d652f3b31115`
and 19 operating-system findings (9 low and 10 medium), with no high, critical,
fixable, Python, Node, or secret findings.

This is authenticated local construction and scanner-path evidence, not a
current release candidate or public launch receipt. The loopback registry is
ephemeral and is not the durable production repository. Flagship release still
requires both current-candidate images in the controller-owned repository,
pull-by-digest verification there, a controller-owned two-image security
receipt, and all other launch evidence.

## Render runtime construction

The dedicated render bridge is built from
`ea/Dockerfile.property-render`. Its assembly stage is pinned to:

```text
python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
```

The build installs FFmpeg `8.1.2-r0` from 114 committed Alpine packages
totalling 53,557,077 bytes. Every APK filename and SHA-256 is fixed by
`ea/render-system-packages.lock.json` and
`ea/render-system-packages.sha256`; repository access is removed before
`apk --no-network --no-cache add` runs. Pillow `12.3.0` is the only Python
dependency and is installed from the hash-locked CPython 3.12 musllinux wheel.
Runtime `pip`, `ensurepip`, `apk`, Playwright, Blender, and NumPy are absent.
The final stage starts from `scratch`, retains the Alpine package database for
scanner visibility, and runs as numeric UID/GID `10001:10001`.

GLB production no longer shells out to Blender. The reconstruction generator
parses its bounded OBJ/MTL output and writes deterministic GLB 2.0 bytes
in-process, with flat normals, indexed triangles, material colors, atomic
replacement, and a fail-closed receipt. Unit proofs verify byte-identical
exports and stale-output removal for invalid input.

On 2026-07-26 UTC, a forced network-disabled build produced image configuration
`sha256:70a6996a9c34f2743b82124d93b161e77409d1ee344c90cf652b904b227560c6`
at 196,788,182 bytes. A loopback-only Registry v2 returned the exact immutable
manifest:

```text
127.0.0.1:5000/propertyquarry/render@sha256:f986088c3fa3893a768b64e4acdd6b798f007d15cb8696ef5d500a226db4e633
```

The registry manifest bytes hash to that digest and bind the same image
configuration. A container using that configuration became Docker-healthy
with no network, no published ports, a read-only root filesystem, all
capabilities dropped, `no-new-privileges`, a bounded `/tmp` tmpfs, and only
the public-tour mount writable. The bridge returned `401` for an invalid
bearer token and `200` for an authenticated reconstruction. That request
generated a 6,484-byte GLB with SHA-256
`b777e1dba919c2552464105dfe416be3cf875fba5ab3ad98020542b77d40dd00`
and a 9.667-second MP4 with SHA-256
`fd4d9f0a88623c6e5469877bed6ce1b8dcc68c189d8406efdf68d9bde092428d`;
the two expected route stops were both visited.

The pinned Syft scan of the immutable repository digest produced a CycloneDX
artifact with SHA-256
`383290ce68cab32e17abe9d8cac0474601e75f7f9187cc74c1a4d2b7ad7ad18b`
and 1,430 components: 1 application, 1,189 files, 239 libraries, and 1
operating system. The pinned offline Trivy scan produced JSON with SHA-256
`60b1e01aea71c96d2272cf68898f751459c8d380c971bfb4bf1ceaa942a95625`
and zero vulnerability, fixable, Python, or secret findings across 141 Alpine
packages and the Python environment.

This is authenticated local construction and runtime evidence, not a current
release candidate or public launch receipt. The controller must still rebuild
the render image from the authenticated current-candidate archive, publish it
to the controller-owned repository, scan that exact digest alongside the web
image, and bind both results to the release SHA.

## Reproducible scanner lane

The controller audits the fully pinned `ea/requirements.lock` by invoking
`pip-audit` only as an isolated module of the authenticated release
interpreter through the authenticated release launcher, with
`--no-deps --disable-pip` and the OSV vulnerability service. The gate captures
the dependency lock once through no-follow descriptors, parses those exact
bytes, audits a private mode-`0600` snapshot of them, and reauthenticates the
source lock after scanning. It requires the audit result to cover every
normalized lock package and version exactly. It invokes Syft and Trivy only
through their pinned absolute paths, with an allowlisted environment and the
corresponding root-owned, SHA-256-pinned empty YAML configuration. It
generates one CycloneDX JSON SBOM per image from the local Docker daemon using
the explicit `docker:` Syft source and requires the SBOM root to bind the
expected immutable digest. Trivy scans each exact local image with
`trivy image --image-src docker`; database, Java-database, VEX, and version
updates stay disabled and offline vulnerability scanning is selected. Trivy
receives a new private cache root for each run. Exact, pre/post-audited `db` and
`java-db` symlinks within that root resolve only to the canonical root-owned
database directories; Trivy's writable `fanal` cache is constrained to a
private owner-only directory, regular singly linked files, fixed modes, at most
64 members, and at most 512 MiB. Substitution, an unsafe source database, an
unexpected member type, or a bound violation fails closed. Trivy must report
the same immutable image identity.

```bash
./scripts/propertyquarry_release_python.sh \
  scripts/propertyquarry_release_security_gate.py \
  --flagship \
  --release-sha '<full-40-character-git-sha>' \
  --web-image 'registry.example/propertyquarry-web@sha256:<64-hex>' \
  --render-image 'registry.example/propertyquarry-render@sha256:<64-hex>' \
  --severity-threshold HIGH \
  --waivers config/propertyquarry_security_waivers.json \
  --artifacts-dir _completion/propertyquarry_release_security/run/artifacts \
  --receipt _completion/propertyquarry_release_security/run/receipt.json
```

`LOW`, `MEDIUM`, `HIGH`, and `CRITICAL` are accepted thresholds. A finding at
or above the selected threshold blocks flagship mode unless one exact waiver
applies. `pip-audit` JSON does not provide a normalized severity, so dependency
findings are recorded as `UNKNOWN` and conservatively evaluated as
`CRITICAL`.

Without `--flagship`, unavailable scanners and blocking findings are recorded
as advisory and exit zero. This keeps ordinary local development usable; it
does not create flagship evidence and must never be substituted for the
controller-owned protected security phase.

## Evidence

The atomic `0600` receipt records:

- the full release SHA, exact image digest references, and dependency-lock
  SHA-256;
- authenticated execution prefixes, exact binary/interpreter paths, modes,
  owners, link counts, sizes, SHA-256 digests, scanner-configuration
  identities, normalized versions, and version-output hashes;
- the root runtime-manifest identity and both Trivy database/metadata
  identities, schemas, update/download times, and freshness deadlines;
- one run-consistent scanner/runtime identity set, reauthenticated after both
  image scans;
- the explicit threshold and offline/no-registry command contract;
- CycloneDX component counts plus SBOM, Trivy result, and pip-audit artifact
  hashes;
- normalized findings, conservative effective severities, exact waivers, and
  blocking counts; and
- final `pass`, `failed`, `advisory_findings`, or `advisory_unavailable` state.

Raw scanner stderr is withheld from receipts. SBOM and scanner JSON artifacts
are also atomically written with mode `0600`. Preserve the entire CI artifact,
not only the summary receipt, in the controller-owned evidence store.
Without `--overwrite-receipt`, the receipt and every fixed artifact path are
preflighted as one absent output bundle before any scanner runs; a repeated
path leaves the complete existing bundle byte-for-byte unchanged. Scanner
results are held in memory and published before the final receipt, so a
receipt never claims an artifact that the run had not finished producing.

Version strings alone are not executable attestation. The gate therefore
revalidates the isolated release-Python `pip-audit` module, both root-owned
scanner binaries, the root-owned runtime manifest, both database files, and
their exact metadata before and after scanning. A missing, changed,
peer-writable, linked, wrongly owned, wrongly versioned, or stale runtime
component fails flagship mode closed.

## Waiver format

The committed waiver file is empty by default. A waiver is an exceptional,
release-specific approval, not an ignore list. It must identify exactly one
scanner source, immutable target, vulnerability, package, and reported
severity:

```json
{
  "schema": "propertyquarry.security_waivers.v1",
  "waivers": [
    {
      "id": "PQSEC-2026-001",
      "source": "trivy:web",
      "target": "registry.example/propertyquarry-web@sha256:<64-hex>",
      "vulnerability_id": "CVE-2026-12345",
      "package": "example-package",
      "severity": "HIGH",
      "release_commit_sha": "<full-40-character-git-sha>",
      "owner": "security-owner",
      "approved_by": "release-approver",
      "reason": "Compensating control and remediation tracking reference.",
      "created_at": "2026-07-13T12:00:00Z",
      "expires_at": "2026-07-20T12:00:00Z"
    }
  ]
}
```

Allowed sources are `pip-audit`, `trivy:web`, and `trivy:render`. Their targets
must respectively equal the dependency-lock `sha256:<hash>`, the web image
reference, or the render image reference. Waivers cannot use wildcards, must
be bound to the current release SHA, require distinct security owner and
independent approver identities, cannot be created in the future, and must
expire within 30 days of creation. Expired, overlong, malformed, duplicate,
mismatched, or wrong-release waivers fail before scanning.

On a failed flagship receipt, do not loosen the threshold or reuse a waiver
from another release. Remediate and rebuild the immutable image, provision a
reviewed time-limited waiver, or stop the launch.
