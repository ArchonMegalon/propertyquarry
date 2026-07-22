# PropertyQuarry runtime image publication

The protected `propertyquarry-publish-runtime-images` workflow is the only repository lane for publishing the web and render runtime candidates to GHCR. It publishes images; it does not deploy them, change protected environment variables, or grant promotion authority.

## Authority boundary

- Start the workflow manually on `main` only after ordinary CI is green and `docs/PROPERTYQUARRY_RELEASE_MANIFEST.md` names the intended runtime candidate.
- GitHub environment approval for `propertyquarry-production` is required before the lane can read source or write packages.
- The clean workflow-envelope SHA is the exact repository-root build context. The manifest runtime SHA is derived separately, must be its ancestor, and is recorded alongside the envelope SHA.
- The approved repositories are only `ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime` and `ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime`. They are first-published by `ArchonMegalon/propertyquarry` and do not inherit package authority from the former combined repository.

## Published contract

The matrix builds these exact Compose inputs for `linux/amd64`:

| Component | Compose service | Dockerfile |
| --- | --- | --- |
| Web | `propertyquarry-api` | `ea/Dockerfile.property-web` |
| Render | `propertyquarry-render-tools` | `ea/Dockerfile.property` |

Each build requests maximum BuildKit provenance and an SPDX SBOM, uses a unique tag containing both full SHAs plus the workflow run and attempt, and records the authoritative `repository@sha256:...` reference. There is no `latest` publication. OCI labels describe the built envelope and logical runtime candidate; matching PropertyQuarry labels preserve both identities explicitly. Before building, the lane renders the Compose configuration without interpolation and proves that each named service still maps the repository-root context to its expected Dockerfile.

The build job also uses the pinned official `actions/attest` action to create GitHub/Sigstore-signed SLSA provenance for the exact untagged repository name and captured image digest. The preflight confirms the reviewed repository is public, so the action uses Sigstore's public-good instance and transparency log. Its OIDC and attestation-write permissions exist only on that job. The signed provenance is pushed to GHCR, while GitHub artifact-storage records are disabled. This supplements rather than replaces the BuildKit maximum-provenance and SPDX attestations.

Before sealing evidence, the workflow retrieves the remote tag and digest manifests, recomputes their SHA-256 identities, verifies the `linux/amd64` child, checks the remote provenance/SBOM predicates, and checks the image labels. Missing or malformed digests, unexpected repositories, manifest drift, or equal web/render digests fail the run.

## Receipt and handoff

Download `propertyquarry-image-publish-receipt-<run>-<attempt>` and retain it with the protected release evidence. The JSON receipt contains:

- runtime and workflow-envelope SHAs;
- exact tagged and digest-addressed image references;
- Dockerfile, `.dockerignore`, Compose, source-tree, and tracked-source archive hashes;
- remotely verified platform manifest, labels, and BuildKit attestation predicates;
- the GitHub attestation ID and URL plus the SHA-256-bound public Sigstore bundle for each image;
- explicit statements that deployment, environment-variable mutation, and promotion did not occur.

The receipt artifact carries copies of the public Sigstore bundles and rechecks their hashes during consolidation. It does not persist OIDC tokens, registry credentials, or GitHub tokens, and it does not rely on a runner-local GitHub CLI verifier.

Release control may populate the protected web/render image inputs only from the receipt-bound immutable references so the preloaded security runner can evaluate those exact images. Setting those gate inputs is configuration evidence, not deployment or promotion authority. The existing `propertyquarry-security` and independent release-controller gates must then pass before deployment; never dispatch deployment from this workflow.
