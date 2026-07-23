# PropertyQuarry Release Manifest

This file is the concise, current release authority. Detailed dated notes are archived in [`archive/PROPERTYQUARRY_RELEASE_HISTORY_2026-07-16.md`](archive/PROPERTYQUARRY_RELEASE_HISTORY_2026-07-16.md); they never override this manifest.

## Current state

PropertyQuarry is a source/browser candidate, not a production launch:

- The locally materialized candidate receipt covers `7/7` source cases, `16/16` real-browser cases, and all eight required product journeys for the exact source identity recorded below. It has not been published or accepted by protected release authority. This candidate includes explicit hosted/generated/blocked/expired 3D-tour truth, attributable unavailable/stale/verified area-evidence states, in-place shortlist history, protected Telegram delivery proof, production-mode PostgreSQL storage/browser parity with internal-only CI session provisioning, immutable CI actions, fail-closed production registration delivery, and a signed controller profile that preserves the canonical public-tour volume and permits only journaled ownership repair.
- Candidate/browser proof does not prove deployment, production storage, authentication, external delivery, observability, rollback, or disaster recovery.
- A prior, separately observed public-edge check returned `200` for `/` after a narrow generated-tour permission repair and received a response from `/health/ready`; the observation has no current timestamp, immutable image binding, or exact-candidate authority and is therefore historical context only. At that observation, `/version` reported an incomplete release manifest without canonical release identity. None of those checks is deployment proof for this candidate.
- Production promotion remains blocked on the independent release-controller artifact, an approved `propertyquarry-security` runner, and distinct digest-pinned web/render images.
- ID Austria is optional and unconfigured. Another supported sign-in path still needs protected live activation proof.
- External notification release evidence is Telegram-only. WhatsApp is outside the current launch evidence.
- Release claims are split. **Core Gold** covers search, shortlist, property detail, first-party 3DVista/public-tour delivery, dossier, decision, and governed delivery evidence. Missing or unconfigured MagicFit, Magic, OMagic, generated scene-video, or other advanced visual lanes do not block Core Gold and must remain unavailable in customer copy.
- **Advanced Visual Gold** is a separate opt-in claim scope. It fails closed unless every claimed MagicFit/Magic/OMagic lane has exact candidate-bound provider provenance, accepted playback, quota/account state, privacy, isolation, source-receipt hashes, and media-artifact hashes. Adapter configuration or a generated file alone is never Advanced Visual Gold evidence.
- The current Advanced Visual producer receipts do not yet carry source-side `release_commit_sha` + `image_digest` identities and exact verifier/source packet hashes. Therefore this candidate records Advanced Visual Gold as `unavailable_unbound_producer_receipts`. The aggregate rejects these legacy/current shapes and never relabels them from its own CLI arguments; Core Gold remains independently eligible.

## Candidate binding

The marked JSON object is the single canonical release authority consumed by the runtime and release verifier. Its exact field set and canonical SHA-256 fail closed on missing, duplicate, unexpected, empty, or mismatched fields.

<!-- propertyquarry-release-manifest-json:start -->
```json
{
  "release_artifact_set": "propertyquarry-generated-release-artifacts-v1@sha256:85fcf46a985c2ce6bcce8669d10481e717172b7f47218e427af890c071ac83f7",
  "release_branch": "main",
  "release_candidate_status": "source-browser-candidate-pending-protected-live-evidence",
  "release_commit_sha": "1649fbab01578c87c11b5ac8aac7a24d764c70ba",
  "release_deployment_id": "propertyquarry-governed-deploy-1649fbab0157",
  "release_generated_at": "2026-07-23T22:49:36Z",
  "release_label": "propertyquarry-source-browser-candidate-1649fbab0157",
  "release_manifest_schema": "propertyquarry.release_manifest.v1",
  "release_product": "PropertyQuarry",
  "release_public_origin": "https://propertyquarry.com",
  "release_repository": "ArchonMegalon/propertyquarry",
  "release_repository_origin": "https://github.com/ArchonMegalon/propertyquarry.git",
  "release_verification_commands": "bash scripts/verify_release_assets.sh && python3 scripts/verify_flagship_release_readiness.py && python3 scripts/verify_generated_release_artifacts_clean.py"
}
```
<!-- propertyquarry-release-manifest-json:end -->

The artifact-set identity covers the exact tracked bytes of the flagship release receipt, weekly product pulse, and browser workflow proof named by the verifier. The runtime SHA is the source candidate recorded by the flagship receipt; it is intentionally not a self-reference to the later documentation/envelope commit. Any missing, duplicate, empty, or mismatched authority field blocks verification.

## Launch blockers

Production stays fail-closed until every item is bound to the exact runtime candidate:

1. Both ordinary hosted CI lanes are terminal and green for the final runtime and receipt envelope.
2. The independently produced and authenticated `propertyquarry-release-controller-v2` authority is installed at `/usr/libexec/propertyquarry-release-control/propertyquarry-release-supervisor-v2`, registered on the protected `propertyquarry-release-controller-v2` runner label, and enforces `PROPERTYQUARRY_PUBLIC_TOUR_VOLUME_PROFILE_V1.md`, including canonical-volume identity, pre/post manifests, ownership-only repair, mode preservation, and rollback evidence. The repository-native non-authoritative bootstrap binary is not a substitute.
3. The protected environment has an approved `propertyquarry-security` runner and distinct digest-pinned `PROPERTYQUARRY_WEB_IMAGE` and `PROPERTYQUARRY_RENDER_IMAGE` inputs.
4. Dependency, container, policy, and SBOM scans pass without stale databases or weakened gates.
5. The governed deployment preserves the canonical public-tour inventory, repairs only journaled legacy ownership, and completes; `/version` reports the approved runtime SHA and complete manifest; `/` remains healthy without generated-bundle permission errors or path disclosure.
6. A supported sign-in path, lifecycle controls, Telegram delivery, PostgreSQL durability, and the customer search-to-decision journeys pass protected live verification.
7. Observability, alerting, rollback, disaster recovery, post-promotion smoke, and Cloudflare/public-origin receipts pass.
8. Billing, analytics, and Core Gold limitations are resolved with evidence or excluded from the launch claim. Advanced scene-video/provider-media limitations may be excluded only by selecting Core Gold and keeping every affected customer claim unavailable; an Advanced Visual Gold claim remains blocked.

## Gold evidence tier and claim scope

- Evidence tier (`standard|flagship|launch`) is independent from claim scope (`core|advanced_visual`). Production release always uses `launch`; standard preserves operator-summary semantics and cannot make a release claim.
- `core_gold` is a strict compatibility alias for `launch` + `core`. It requires the first-party customer operating loop, every Core launch/UX receipt, and verified 3DVista/public-tour evidence. Its provider fields are `core_required_provider_modes` and `core_missing_provider_modes`.
- `advanced_visual_gold` is a strict compatibility alias for `launch` + `advanced_visual`. It adds governed MagicFit, Magic, and OMagic evidence plus an offline aggregate binding to the exact release SHA/image, current source receipts, provider artifact hashes, quota/account state, privacy, and isolation. Its provider fields are `advanced_visual_required_provider_modes` and `advanced_visual_missing_provider_modes`.
- Every authoritative Advanced Visual source must carry its expected schema plus source-side `release_commit_sha` and `image_digest`. Every derived verifier/status/packet must also bind the exact upstream receipt or packet SHA-256. Missing or replayed identities yield `unavailable_unbound_producer_receipts`; freshness plus aggregate CLI arguments are not release authority.
- Legacy `required_provider_modes` / `missing_provider_modes` remain a combined operator envelope. They must not be used to make a Core Gold decision; operator dashboards consume the explicit combined `operator_*` fields.
- Any customer-facing walkthrough-ready claim is fail-closed even under Core Gold when its exact provider receipt or playback binding is absent, invalid, stale, over quota, privacy-unsafe, or outside the governed render isolation boundary.

## Whole-project Gold boundary

- Evidence-map overlay source and browser UI proof is green for unavailable, stale, and verified states. Whole-project Gold remains blocked until protected live authenticated source coverage and candidate-bound cache-recency, source-time/reference-period, and performance receipts cover environmental quality, heat, traffic/noise, mobility, schools, official aggregate safety context, media-attention statistics with article links, and fiber/broadband coverage.
- Rybbit remains a whole-project gold blocker until dashboard/API receipts prove the approved taxonomy across conversion, product engagement, billing, tours, support/recovery, and activation without private candidate, listing, or contact payloads.
- Production security remains a whole-project gold blocker until runtime/container hardening, reproducible supply chain, dependency/container scans, SBOM, durable RBAC/session revocation, key rotation, and disabled production override receipts are current.
- Remote candidate CI and deployed/live receipts remain required before launch authority can be granted.

## Rules

- Update this file only from current candidate and production evidence.
- Treat a tracked-`main`/runtime SHA mismatch as blocked until governed deployment reconciles it.
- Store detailed machine receipts in completion artifacts or CI, not in this document.
- Never include credentials, tokens, cookies, license keys, or customer data.
- Never bypass the release controller, security runner, provenance, rollback, or disaster-recovery gates.
