# Single-host v2 package contract

This package is a deterministic transport artifact for the separate
`single-host-production-v2` authority. Building, verifying, or staging it does
not install an authority and does not authorize release effects. A separately
trusted root helper must verify the archive again, install every file with the
declared owner and mode, persist the manifest and signature, and emit its own
signed terminal install receipt.

## Trust boundary

`tools/package.py verify` and `stage` require an external package-authority
Ed25519 SPKI PEM. They never accept the anchor inside the package as the trust
root. The bundled anchor must byte-match that external key. The package-signing
private key is never included; the receipt-authority PKCS8 private key and its
matching SPKI public key are included because the installed root service signs
release receipts.

The signature input is:

```
"propertyquarry.release-control.single-host-package-manifest-signature.v2\0"
+ uint64-big-endian(len(manifest))
+ canonical-manifest-bytes
```

The key ID is `sha256:` followed by the lowercase SHA-256 of the SPKI DER. The
same convention is used by the Go controller.

## Deterministic archive

The archive is uncompressed USTAR. Every member is a regular file with UID/GID
zero, empty owner/group names, mtime zero, and no PAX metadata. Members are
sorted by name. Links, duplicate names, unsafe paths, extra members, trailing
archive data, and metadata or content that does not reproduce the exact archive
are rejected.

Because the transport contains the receipt-signing private key, the builder
creates the archive itself as owner-read-only (`0400`). It must remain in a
private directory and must not be published as a public release asset.

The top-level members are `manifest.v2.json` (mode `0444`) and
`manifest.v2.sig` (raw 64-byte Ed25519, mode `0444`). Every remaining member is
`payload` followed by one of these absolute install paths:

| Install path | Mode | Purpose |
| --- | ---: | --- |
| `/etc/propertyquarry-release-single-host-v2/authority.v2.json` | `0400` | signed authority profile |
| `/etc/propertyquarry-release-single-host-v2/authority.v2.sig` | `0444` | profile signature |
| `/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.json` | `0444` | package-authority-signed production materialization receipt |
| `/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.sig` | `0444` | materialization receipt signature |
| `/etc/propertyquarry-release-single-host-v2/native-build-receipt.v2.json` | `0444` | reproducible native-build receipt |
| `/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem` | `0444` | package anchor copy |
| `/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key` | `0400` | receipt signing key |
| `/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem` | `0444` | receipt verification anchor |
| `/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json` | `0444` | profile-digest-bound transaction plan |
| `/usr/lib/propertyquarry-release-runner-v2/runner.lock.json` | `0444` | pinned ephemeral-runner archive contract |
| `/usr/lib/systemd/system/propertyquarry-release-single-host-v2.socket` | `0444` | socket unit |
| `/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service` | `0444` | root connection service template |
| `/usr/lib/systemd/system/propertyquarry-release-single-host-v2-activation-canary.service` | `0444` | host-network activation authority canary |
| `/usr/lib/sysusers.d/propertyquarry-release-single-host-v2.conf` | `0444` | signed numeric runner identity |
| `/usr/lib/tmpfiles.d/propertyquarry-release-single-host-v2.conf` | `0444` | state/runtime directories |
| `/usr/libexec/propertyquarry-release-control/propertyquarry-predeploy-backup-v2` | `0755` | signed-plan-bound encrypted pre-deploy backup helper |
| `/usr/libexec/propertyquarry-release-control/propertyquarry-database-control-v2` | `0755` | signed-plan-bound database gate helper |
| `/usr/libexec/propertyquarry-release-control/provision_propertyquarry_runtime_database.py` | `0755` | sealed adjacent database role/ACL implementation loaded by the database gate helper |
| `/usr/libexec/propertyquarry-release-control/propertyquarry-runtime-isolation-v2` | `0755` | signed-plan-bound isolation and stale-runtime retirement helper |
| `/usr/libexec/propertyquarry-release-control/propertyquarry-runtime-deploy-v2` | `0755` | signed-plan-bound immutable runtime deployment helper |
| `/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2` | `0755` | static controller |
| `/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-v2` | `0555` | one-shot ephemeral runner launcher |
| `/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-lifecycle-v2` | `0555` | fixed root lifecycle for one governed ephemeral runner |
| `/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json` | `0400` | receipt-authority-signed one-shot runner launch ticket |
| `/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v3.json` | `0400` | receipt-authority-signed protected-environment approval intent |
| `/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-post-attempt.v3.json` | `0400` | receipt-authority-signed approval precommit |
| `/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v3.json` | `0400` | receipt-authority-signed successful protected-environment prerequisite proof |
| `/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json` | `0400` | receipt-authority-signed runner reservation and source-checkout binding |

## Governed Prater panorama operations

The controller accepts the closed operations `ai-panorama-install` and
`ai-panorama-closeout` for the fixed
`prater-messe-maisonette-ai-360-053ad185e1c44b2e` publication. They use only
the Compose volume `propertyquarry_governed_public_tours` (Docker name
`property_propertyquarry_governed_public_tours`) at
`/data/governed_public_property_tours`; the legacy dynamic public-tour volume
is rejected.

Installation runs fixed, rootless-application-image one-shots for volume
bootstrap, private record discovery, preflight, and apply. The bootstrap and
preflight have no network. Discovery and apply use a temporary internal
database-only bridge and the fixed scheduler database credential projection.
Every one-shot has a read-only root filesystem, no Docker log driver, an exact
image digest, a deterministic name and labels, and the minimum phase-specific
mounts and capabilities. The controller independently verifies the image,
container consumers, volume identity, closed volume inventory, canonical
stdout projection, public manifest, and database-bound terminal receipt.

Before the four AI state files are created, the signed main controller journal
durably commits their generated instance IDs, exact final and staging names,
canonical bytes and digests, modes, ownership, and control-root device/inode
identity. Each leaf is written and fsynced under its intent-derived staging
name, securely reread, hard-linked without replacement to the fixed final
name, directory-fsynced, then unlinked and directory-fsynced again. Recovery
may complete only missing intent-bound bytes or a verified staging/link
boundary. Once the signed completion exists, a missing, replaced, relinked, or
partial state leaf is terminal tamper and is never regenerated.

Closeout is a database-independent privacy kill switch bound to the prior
signed install success, installed manifest, exact governed volume identity,
and fresh closeout OIDC authority. It remains usable when the API, scheduler,
render service, current database environment, or original release run is
absent. A no-network one-shot receives only the governed volume and the fixed
mode-`0400` canonical revocation request, writes the byte-identical
root-owned mode-`0444` marker, and returns the same canonical bytes. Recovery
binds that marker to the signed closeout journal and never deletes or replaces
an authenticated revocation.

The 226 MB GitHub runner archive is intentionally not in this package. Its
official version, byte count, URL, and SHA-256 are pinned by `runner.lock.json`;
the root helper must independently verify any installed archive.

The root installer binary is also deliberately outside the package it verifies.
The bundled native-build receipt records its static-binary digest, size, mode,
and embedded package-authority key ID (or the explicit `unbound` state). An
installer that performs production installation must be independently supplied,
digest-pinned, and package-authority-bound; the package itself never substitutes
for that trust decision.

The root helper should additionally persist the verified top-level manifest and
signature at the manifest-declared paths under
`/etc/propertyquarry-release-single-host-v2/`.

## Privileged installer image

The separate static installer is built with the package-authority SPKI embedded
at link time. An `unbound` installer is useful only for tests and refuses every
install. `tools/build-installer-image.sh` accepts a bound native-build directory,
builds the scratch image twice with networking disabled, requires identical
image IDs, and emits a non-authoritative image-build receipt. It does not install
the package.

`tools/install-with-docker.sh` accepts only that exact local `sha256:` image ID,
an absolute mode-`0400` package path, the independent package-authority SPKI
PEM, and an empty private receipt directory. It forces and proves the local
`default` Docker context on `/var/run/docker.sock`, stages and verifies the
signed package with the external anchor, derives the expected installer digest
from the signed native-build receipt, and proves that the pinned scratch image
contains exactly that one installer executable before any privileged run. The
container has no network and a read-only root filesystem. It receives only the
capabilities needed to write the host bind and chroot into its recursively bound
filesystem for fixed `systemd-sysusers`, `systemd-tmpfiles`, and `systemctl`
commands. Host PID 1 is checked as systemd before activation. Inside the
container, the Go helper independently re-verifies the package signature,
canonical archive, fixed file set and purposes, native-build receipt, signed
profile and plan, host machine ID, Google identity-envelope metadata, and the
dedicated registration-mail envelope metadata before staging any target. File publication
uses same-directory `renameat2(RENAME_NOREPLACE)`, directory fsyncs, a signed
append-only install journal, and reverse-order rollback. The helper writes a
signed terminal receipt even when activation rolls back; the wrapper exits
nonzero unless the installed socket is active. Installation also creates the
fixed backup-key directory as UID/GID 1000 with mode `0700`, generates one
32-byte key with the kernel CSPRNG when the exact mode-`0600` key is absent,
fsyncs it, and thereafter refuses malformed, relinked, re-owned, or replaced
key material instead of rotating it. The signed install receipt binds the
SHA-256 ID of the decoded key and whether that invocation created it. Before
reporting success, the wrapper also re-verifies the terminal receipt's Ed25519
signature and exact
package, controller-build, installer-build, candidate, and non-authoritative
claim bindings with `tools/verify-install-receipt.py`. The runner wrapper applies
the equivalent signed binding checks to its runner-install receipt.

`tools/dispatch-tour-v4-with-docker.sh` is the separate no-input publication
lane for the one compiled generated-reconstruction v4 permit. It consumes a
distinct `single-host-tour-publication-v4` package that the ordinary runtime
installer rejects. That deterministic mode-`0400` archive has a separate
manifest/signature domain and exactly nine material members: the source-bound
controller and build receipt, canonical package anchor, signed
receipt-authority bootstrap, bootstrapped receipt key and anchor, and a
canonical-authority-signed, machine-bound tour materialization valid for
exactly 3,600 seconds. Its signed claims forbid host installation, runtime
deployment, network use, and persistent credential installation; it has no
install paths, runtime plan/helpers, runner material, or systemd/sysusers
surface. The wrapper first invokes the tour-specific external-anchor
package/image verifier, then starts the exact scratch
image with `--network none`, a read-only container root, no host PID namespace,
no Docker socket mount, all capabilities dropped except `CHOWN`,
`DAC_OVERRIDE`, `FOWNER`, and `SYS_CHROOT`, and the host root at the single
fixed `/host` mount. Both the Bash wrapper and static Go helper accept only the
exact authority-info, inspect, publish, recover, and rollback argument layouts;
the artifact path, manifest digest, and published-tree rollback digest are
compiled constants. The helper re-verifies the signed package and its own
binary binding, independently authenticates the receipt-authority bootstrap,
host/source/artifact/operation-bound materialization, host machine ID, and
receipt key from verified package memory, chroots to `/host`, and calls the v4
authority directly. It neither installs nor execs a controller, reads a GitHub
credential, deploys the runtime, nor exposes a shell or generic root command.

An upgrade first stops new socket admissions and allows existing templated
controller instances up to 315 minutes to finish before any installed file is
replaced; the enclosing workflow timeout is 360 minutes. The installer remains
in its `--network none` container. Before every activation it disables and
drains the old socket, stops and resets the canary, deletes stale canary state,
and writes a fresh 32-byte root-owned mode-`0400` challenge under the
root-owned mode-`0750` activation-canary runtime directory (grouped to the
release identity). Host systemd then runs the fixed
`Type=oneshot` canary with `PrivateNetwork=no`. Systemd supplies the GitHub
token only through `LoadCredentialEncrypted=` and supplies the challenge and
receipt key as credentials; none is placed in argv, environment, or stdout.
The canary uses direct TLS 1.2-or-newer with proxies and redirects disabled to
prove repository runner Administration(read), repository OIDC customization
Actions(read), and the exact immutable OIDC subject policy. It signs a
120-second receipt bound to the challenge digest,
profile, plan, manifest, controller, unit, runtime SHA, workflow SHA, and both
authority keys. The host child and container parent independently verify that
proof before the socket is enabled. Any start, proof, enable, or local peer
probe failure disables the socket and removes the challenge/result. The final
signed install receipt nests the full canary proof for archived verification.

`tools/fetch-runner-archive.sh` downloads the exact locked GitHub asset into a
private mode-`0400` file and verifies both byte count and SHA-256 before a
no-replace publish. `tools/install-runner-with-docker.sh` invokes the same bound
helper image with fewer capabilities; the helper re-verifies the installed
authority/receipt key and streams the archive into its root-owned mode-`0444`
target before emitting a signed runner-install receipt. This installs only the
runner payload. Registration remains one-shot and the launcher still requires
an ephemeral `pqrelease-<32 hex>` label and registration token on descriptor 8.
Reservation preparation normalizes the detached source checkout after its
no-replace publication: only exact `100644` and `100755` Git blobs are
accepted, the index must equal the committed tree, tracked files must be
single-link regular files whose content rehashes to the bound Git blob OIDs,
and their on-disk modes become `0644` or `0755` despite the caller's
restrictive umask. Source directories are fsynced at `0755`; the checkout root
and Git metadata remain private at `0700`.

The installed runner lifecycle has a separate user-callable admission wrapper,
`tools/launch-ephemeral-runner-with-docker.sh`. It accepts exactly a pinned
scratch-helper image ID, the mode-`0400` signed package, and the independent
mode-`0444` package anchor. The caller must be UID/GID 1000 and supply exactly
one administration token through a UID/GID-1000, mode-`0600`, read-only FIFO
on descriptor 8. A non-dumpable broker is the only child that inherits that
descriptor; it does not read the token until the wrapper has verified the
package, helper image, and complete stopped-container configuration.

The stopped helper container is inspected before that gate opens. It has the
fixed scratch-installer entrypoint and sole `launch-ephemeral-runner` argument,
a read-only container root, explicit bridge networking, no host PID namespace,
no direct Docker-socket mount, and all capabilities dropped except `CHOWN`,
`DAC_OVERRIDE`, `FOWNER`, and `SYS_CHROOT`. Its only mounts are the recursively
bound host root at `/host`, the verified package at the helper's fixed input
path, and one mode-`0444` fixed resolver overlay at the host stub-resolver
target. The overlay contains only Docker bridge DNS (`127.0.0.11`) so HTTPS
resolution remains inside the explicit bridge namespace after chroot; the Go
helper verifies its bytes, owner, mode, path, host symlink, and non-writable
directory chain before executing the lifecycle. The broker then relays the token through a private FIFO to container
standard input; token material is never placed in argv, environment, a regular
file, or a Docker log.

The fixed Go mode independently re-verifies the embedded package authority,
scratch-installer self binding, exact installed generation, and every installed
signed member. It validates `/host`, duplicates standard input only to
descriptor 8 with close-on-exec cleared, chroots to `/host`, and re-hashes the
installed controller plus both runner executables. It finally executes only
the signed zero-argument lifecycle path with a fixed non-secret environment.
That lifecycle immediately transfers descriptor 8 to a built-in-only gated
broker, closes it in the orchestrator, and keeps the descriptor marker unset
before any admission, filesystem, Docker, controller, or Python subprocess.
Only after the runner image is verified does the broker export the marker and
directly exec the fixed `runner-supervise` command, which performs the first
token read.
There is no shell, arbitrary executable, or generic root-command surface.

The release dispatch has no manual environment-review step. Its exact order is:

1. Run `python3 tools/prepare-runner-reservation.py prepare` and retain the
   emitted `runner_label` and `dispatch_ticket_sha256` as exact strings.
2. Dispatch `.github/workflows/smoke-runtime.yml` on `refs/heads/main` with the
   already governed security-runner receipt values and these exact inputs:
   `release_runner_label=<runner_label>`,
   `release_runner_ticket_sha256=<dispatch_ticket_sha256>`,
   `run_launch_authority=true`, and `run_activation_journey=false`. The other
   required inputs are the exact `security_runner_label` and
   `security_runner_token_expires_at` from the security-bootstrap receipt; no
   value may be synthesized or copied from an earlier run.
3. Supply the GitHub administration token only through FIFO descriptor 8 and
   invoke the autonomous protected-environment reviewer:

   ```sh
   prerequisite_fifo=/run/user/1000/propertyquarry-prerequisite-token.fifo
   mkfifo -m 0600 "$prerequisite_fifo"
   dd if="/proc/self/fd/${PROPERTYQUARRY_ADMIN_TOKEN_SOURCE_FD}" of="$prerequisite_fifo" status=none &
   PROPERTYQUARRY_RUNNER_PREREQUISITE_TOKEN_FD=8 \
     python3 tools/approve-runner-prerequisite.py approve 8<"$prerequisite_fifo"
   rm -f "$prerequisite_fifo"
   ```

   `PROPERTYQUARRY_ADMIN_TOKEN_SOURCE_FD` names an already-open private source
   descriptor; it is not the token and must not be logged. The reviewer signs
   and fsyncs its intent before the approval POST. The protected job's
   evaluated GitHub name is exactly
   `propertyquarry-protected-dispatch-inputs | <runner_label> | <dispatch_ticket_sha256>`;
   discovery and the final pre-POST observation require that exact name. The
   run-index request is also filtered by the reservation's signed workflow SHA;
   a truncated page of more than 100 matching runs still fails closed. The
   reviewer then fsyncs a signed post-attempt record binding the current
   run/attempt, jobs, pending deployment, complete target-environment review
   history, and absence of materialization before POSTing at most once. A retry
   with that record is GET-only. An exact-comment review observed before this
   signed precommit is untrusted and fails closed; it cannot create an approval
   receipt or authorize materialization.

   GitHub's pending-deployment approval endpoint is run-scoped and offers
   neither an attempt selector nor an ETag precondition. Actions write/rerun
   authority must therefore remain trusted and quiescent between the final
   observation and POST; reconciliation fails closed if the attempt drifts.
   This is an explicit operator invariant, not an atomic GitHub guarantee.
   The mutable token buffer is wiped on every exit, but the HTTPS library
   briefly requires an immutable Authorization header string. The controller
   therefore assumes a dedicated short-lived process whose memory is trusted;
   the operator must prevent dumps or inspection while it runs.

   If an audited external operator or GitHub terminates the exact run with
   cancellation/failure before a signed approval or any materialization,
   `approve-runner-prerequisite.py retire-terminal` performs a GET-only,
   double-stable terminal adoption. It binds any existing post-attempt record,
   the complete environment-review set, empty pending deployments, and an
   absent or provably inert release job. A successful prerequisite is recorded
   as observed but is never reinterpreted as an approval receipt. GitHub may
   stamp a cancelled, never-assigned job's `started_at` equal to its
   `completed_at`; that exact terminal stamp is inert only when the runner,
   runner group, and steps are all absent and both governed custom labels are
   exact. Frozen history may additionally carry the formerly explicit
   `self-hosted` label. A different start timestamp, missing custom label, or
   any execution evidence still fails closed. Then
   `prepare-runner-reservation.py abandon-terminal` signs the abandonment and
   atomically retains the reservation as an `.abandoned.v2` terminal. These
   safe retirement steps may run after reservation expiry; neither command
   sends a cancellation request. Trusted Actions write/rerun authority must
   remain quiescent from the terminal observations through terminal signing
   and abandonment; the double-stable GETs detect observed drift but cannot
   make GitHub and the local terminal transition atomic.

   Frozen v2 prerequisite records remain verifiable for historical packages
   and GET-only terminal retirement, but cannot authorize a new approval,
   materialization, or package build.
4. Run `python3 tools/materialize.py materialize --output <new-private-path>
   --final-artifact <final-artifact> --preflight-artifact <preflight-artifact>
   --attestation-verifier-root <verified-root>`. Materialization accepts only
   the release run/attempt authorized by the prerequisite record and copies the
   two exact signed wrappers without reserialization.

The signed config, plan, launch ticket, materialization receipt, preclaim,
terminal binding, and package manifest bind the raw intent digest, raw approval
digest, canonical approval-payload digest, and prerequisite job ID. The root
installer independently authenticates both wrapper signatures and their full
intent-to-approval-to-reservation chain. The installed controller secure-reads
the mode-`0400`, root-owned, single-link records and revalidates that chain plus
the launch ticket at admission, runner start, launcher start, and every workflow
request authorization; it does not cache an earlier successful result.

Before any live job observation, materialization publishes a receipt-authority-
signed, fsynced, no-replace claim keyed by the signed reservation digest. The
claim fixes the canonical release-evidence digest, canonical output path and
parent device/inode identity, reservation nonce and label, workflow/runtime,
start/deadline, and preselected deployment ID. A retry adopts an authenticated
pending or final claim without rotating its time window or deployment. A
different output or rebound evidence fails before observers run.

After the private materialization directory is atomically published, a second
receipt-authority-signed no-replace terminal fixes the claim, output directory
device/inode identity, config and signature, plan, materialization receipt and
signature, launch ticket, reservation, run ID/attempt/job, label, deployment,
workflow, and runtime digests. Only that exact output can verify. Pending
terminal publication and terminal-first active-reservation cleanup are
idempotently recoverable; copied outputs and conflicting active reservations
fail closed. Authenticated claim/bound records coexist with authenticated
expired-reservation terminals under the private authority root.

The offline builder consumes the separately produced native build directory and
the already signed profile material explicitly:

```sh
tools/package.py build \
  --binary /absolute/build/propertyquarry-release-single-host-v2 \
  --predeploy-backup-helper /absolute/source/propertyquarry-predeploy-backup-v2 \
  --database-control-helper /absolute/source/propertyquarry-database-control-v2 \
  --runtime-database-helper /absolute/source/provision_propertyquarry_runtime_database.py \
  --runtime-isolation-helper /absolute/source/propertyquarry-runtime-isolation-v2 \
  --runtime-deploy-helper /absolute/source/propertyquarry-runtime-deploy-v2 \
  --build-receipt /absolute/build/build-receipt.v2.json \
  --config /absolute/signing/authority.v2.json \
  --config-signature /absolute/signing/authority.v2.sig \
  --plan /absolute/signing/transaction-plan.v2.json \
  --materialization-receipt /absolute/signing/materialization-receipt.v2.json \
  --materialization-receipt-signature /absolute/signing/materialization-receipt.v2.sig \
  --runner-reservation /absolute/signing/runner-reservation.v2.json \
  --runner-launch-ticket /absolute/signing/runner-launch-ticket.v2.json \
  --runner-prerequisite-intent /absolute/signing/runner-prerequisite-intent.v3.json \
  --runner-prerequisite-post-attempt /absolute/signing/runner-prerequisite-post-attempt.v3.json \
  --runner-prerequisite-approval /absolute/signing/runner-prerequisite-approval.v3.json \
  --package-authority-public-key /absolute/signing/package-authority-v2.pem \
  --package-authority-private-key /absolute/signing/package-authority-v2.key \
  --receipt-authority-public-key /absolute/signing/receipt-authority-v2.pem \
  --receipt-authority-private-key /absolute/signing/receipt-authority-v2.key \
  --output /absolute/output/propertyquarry-single-host-v2.tar
```

`verify` and `stage` require `--package-authority-public-key`; this argument is
the out-of-package trust input. `stage` also requires a nonexistent absolute
output directory and never writes to host install paths.

The offline tool uses Python 3.10+ standard-library archive/filesystem support
and `cryptography` for Ed25519/PKCS8/SPKI operations. That dependency is confined
to the non-privileged build/inspection lane; the independently built root helper
must use its embedded trust anchor and must not depend on this Python process.

## Manifest schema

`manifest.v2.json` is canonical UTF-8 JSON with no trailing newline. It has the
exact keys enforced by `tools/package.py`. `files` is sorted by `package_path`;
each element has exactly `install_path`, `mode` (four octal characters),
`package_path`, `purpose`, `sha256`, and `size`.

Two values are deliberately explicit:

```
"non_authoritative_until":"independent-root-helper-reverification-and-atomic-install"
"root_helper_verification_required":true
```

## Socket and credential boundary

The socket uses `Accept=yes`, so each peer is handled by a root
`propertyquarry-release-single-host-v2@.service` instance with standard input
and output attached to that peer. The service obtains `github-api-token` with
`LoadCredentialEncrypted=` and exposes it at the controller's fixed credential
path with a read-only bind. The encrypted credential file is deliberately not
part of the package. The separate activation-canary unit is the only bridge
from the network-isolated installer to the live GitHub prerequisite check; its
per-attempt challenge prevents reuse of a prior successful oneshot result.

Initial GitHub credential provisioning has its own narrower admission path.
`tools/provision-github-credential-with-docker.sh` requires the caller to set
`PROPERTYQUARRY_GITHUB_CREDENTIAL_TOKEN_FD=8` and attach a single-use,
read-only pipe as descriptor 8. The descriptor must be a mode-`0600`,
UID/GID-1000 FIFO and its producer must write exactly one token, optionally
followed by one newline, then close it. The wrapper never consults
`/home/tibor/.config/gh`, `gh auth`, a token environment variable, argv, or a
regular token file. It starts the fixed credential broker as the first and only
child that inherits descriptor 8, closes the wrapper's copy, verifies the
signed package and pinned helper image, and only then opens the broker's
private verification gate.

The broker accepts only the `github_pat_` fine-grained token form. Classic
`ghp_`, `gho_`, `ghu_`, `ghs_`, and `ghr_` tokens and ambiguous input fail
closed. The fine-grained token must select only
`ArchonMegalon/propertyquarry` and grant only Metadata(read),
Administration(read), and Actions(read). Before any privileged container
starts, the broker uses direct TLS with proxies and redirects disabled to bind
the exact repository and immutable owner/repository numeric IDs, require that
the authenticated repository listing contains exactly that one repository
with no next page, verify the runner and OIDC read endpoints, and require the
immutable OIDC subject policy. Non-empty classic OAuth scope headers or
unexpected endpoint permission contracts are rejected.

The broker hashes the verified high-entropy token instance and sends that
non-secret `sha256:` commitment to both the wrapper and the root helper. The
broker then writes that exact in-memory token instance to the private installer
FIFO. The root helper recomputes and compares the commitment before any host
mutation, and the signed credential receipt records
`credential_instance_sha256`, `plaintext_digest_recorded:true`, and
`token_material_recorded:false`. The independent receipt verifier requires the
caller's expected commitment. A substituted FIFO writer therefore cannot
produce an accepted mutation or terminal receipt.

All three token-bearing process layers disable core materialization: the host
wrapper sets a zero core limit, the broker sets `RLIMIT_CORE=0` and
`PR_SET_DUMPABLE=0` before reading descriptor 8, and the Docker helper receives
`--ulimit core=0:0` while the Go helper independently repeats both controls
before reading or executing a credential transform.

GitHub currently provides no token-self-introspection endpoint that
cryptographically enumerates every permission granted to a fine-grained PAT.
The admission path therefore proves the fine-grained prefix, exact observed
repository visibility, required read calls, absence of classic OAuth scopes,
and exact accepted-permission headers, but it cannot prove that GitHub has not
granted an additional fine-grained permission that those read responses do not
expose. Token issuance must still enforce the exact three-permission policy
above; any future GitHub introspection API must be added as a mandatory
fail-closed check before that limitation can be removed.

The independently signed Google identity environment file
and `/docker/property/state/runtime/propertyquarry_registration_email.env` are
also not copied or read by the packager; only their signed paths, digests,
modes, UIDs, and GIDs remain in the byte-preserved profile and plan. The latter
must contain exactly these ten non-empty registration-mail variables, in this
order:

```
EMAILIT_API_KEY
PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN
PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID
EA_REGISTRATION_EMAIL_FROM
EA_REGISTRATION_EMAIL_NAME
EA_REGISTRATION_EMAIL_FROM_FALLBACK
EA_REGISTRATION_EMAIL_NAME_FALLBACK
EA_REGISTRATION_EMAIL_FORCE_FALLBACK
EA_EMAIL_DEFAULT_FROM
EA_EMAIL_DEFAULT_NAME
```

Neither envelope's values may appear in packages, journals, or receipts.
The historical root `.env` preimage may contain either the former exact
eight-key subset (without the two Cloudflare variables) or the full ten-key
set. The purge receipt binds which complete set existed: its expected removal
count is exactly eight or ten, while an idempotent retry removes zero. The
dedicated envelope and every post-purge exposure proof always remain exact-ten.

The signed deploy contract uses fixed, ordered Compose interpolation sources
for the base environment, render-only environment, database roles, admission,
Google identity, and registration mail. Compose interpolation does not itself
authorize service-level exposure: the API must not load the render/provider/
Telegram environment, and terminal isolation verification must prove that
boundary from the running containers. The independently signed Google and mail
envelopes remain exact plan bindings.

The profile, plan, and package manifest also bind the API's only permitted host
publication as `127.0.0.1:8097` to container port `8090`. The release helper and
terminal isolation receipt must use those exact values; wildcard or alternate
host bindings are not equivalent and cannot produce a production-ready receipt.
They bind `database_image` to
`postgres:16-alpine@sha256:16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229`.
Each database-control gate receives that exact value through
`--database-image`; a tag-only, alternate, or profile/plan/manifest-rebound image
is rejected before installation or release mutation.

The same three signed layers bind `pre_purge_root_env_digest` as the expected
historical digest of `/docker/property/.env` before secret removal. The field
alone does not prove the mutation input: the authenticated isolation helper
receipt must prove that the actual captured preimage had that digest and is the
rollback preimage. After a successful purge it is deliberately not a
startup-current-file invariant. The current post-purge digest comes only from
the authenticated isolation transaction state and receipt.

The mutation phase begins with the idempotent, 9,600-second
`predeploy-encrypted-backup` contract. That step is bound to the root-owned,
digest-pinned
`/usr/libexec/propertyquarry-release-control/propertyquarry-predeploy-backup-v2`
helper, the fixed private encryption-key path, the exact candidate runtime and
image digests, and a candidate-specific signed receipt path. A release cannot
continue when the helper exits nonzero or when the controller cannot
independently authenticate the signed receipt, exact nine-artifact coverage,
remote canonical manifest, and streamed ciphertext digests under the
candidate-specific pCloud directory.

The next four mutation steps are an exact ordered database-control contract:
`provision-roles` (900 seconds), `migrate-schema` (1,500 seconds),
`harden-runtime-acl` (900 seconds), and `verify-schema-readiness` (600
seconds). Each step is idempotent, expects exit code zero, uses the sealed
`propertyquarry-database-control-v2` executable, and receives only the candidate
runtime, pinned web image, exact signed database image, and its fixed
candidate-specific receipt path. The
package verifier fixes that executable to 60,449 bytes with SHA-256
`9bdebcd2bae867ef9ac4e38374e964dc81752b2a572eb8a0568f3bb45d5bfe18` and
its adjacent implementation to 50,770 bytes with SHA-256
`bc987570cfce12c734cb80b33d7e13199b346c8a8b5406f3ebce88bb15e71a63`.

Every successful database gate must publish a canonical, newline-terminated
receipt under
`/var/lib/propertyquarry-release-single-host-v2/database-receipts/<runtime>/<deployment-id>/`.
The receipt is signed by the installed receipt authority over the domain
`propertyquarry.release-control.single-host-database-receipt-signature.v2\0`,
the uint64 big-endian payload length, and the canonical payload. Before the
next step starts, the controller verifies the signature and key ID, exact
operation/runtime/web-image/database-image/host/authority/database/container/network bindings,
the mode-`0600` UID/GID-1000 database-role environment digest, the false
production-ready and secret-output claims, and the operation-specific result.
The database OID and database-role environment digest must remain identical
across all four proofs. The migration receipt additionally establishes the
current versions of `ea_kernel`, `property_search`, and
`propertyquarry_google_identity`; the hardening and readiness receipts must
report the same three versions with no missing or substituted component. The
four verified proofs and the unchanged database-role environment are rechecked
at the deploy boundary; an absent, reordered, substituted, or invalid proof
blocks deployment and enters the signed rollback path.
