# PropertyQuarry Release Manifest

This file is the concise, current release authority. Detailed dated notes are archived in [`archive/PROPERTYQUARRY_RELEASE_HISTORY_2026-07-16.md`](archive/PROPERTYQUARRY_RELEASE_HISTORY_2026-07-16.md); they never override this manifest.

## Current state

PropertyQuarry is a source/browser candidate whose production state is decided
by the current local Docker deployment receipt:

- The locally materialized candidate receipt covers `7/7` source cases, `16/16` flagship real-browser cases, and all eight required product journeys for the exact source identity recorded below. Local Compose deployment and its mode-`0600` operator receipt—not GitHub Actions—decide whether that candidate is live. This candidate keeps the normal camera walkthrough primary, exposes 3D tour only for verified Matterport or 3DVista publications, labels generated reconstruction as an AI layout preview, enforces topology-gated Matterport and multi-node 3DVista walkable claims, and preserves attributable area evidence, in-place shortlist history, Telegram delivery proof, production-mode PostgreSQL storage/browser parity, fail-closed production registration delivery, and canonical public-tour volume controls.
- Candidate/browser proof does not prove deployment, production storage, authentication, external delivery, observability, rollback, or disaster recovery.
- A prior, separately observed public-edge check returned `200` for `/` after a narrow generated-tour permission repair and received a response from `/health/ready`; the observation has no current timestamp, immutable image binding, or exact-candidate authority and is therefore historical context only. At that observation, `/version` reported an incomplete release manifest without canonical release identity. None of those checks is deployment proof for this candidate.
- Production promotion requires a passing exact-candidate local Docker receipt with distinct immutable web/render image IDs, completed migration, healthy services, and localhost readiness.
- ID Austria is optional and unconfigured. Another supported sign-in path must pass the local live activation proof.
- External notification release evidence is Telegram-only. WhatsApp is outside the current launch evidence.
- Release claims are split. **Core Gold** covers search, shortlist, property detail, provider-authentic public-tour delivery, dossier, decision, and governed delivery evidence. Core requires one walkable provider lane: either a topology-verified Matterport capture or a licensed, verified multi-node 3DVista export. Captured Matterport topology remains eligible for 30 days while live viewer availability stays provider-controlled; 3DVista counts only with PropertyQuarry-specific license/provenance evidence and multiple spatial panorama nodes. Missing or unconfigured MagicFit, Magic, OMagic, generated scene-video, or other advanced visual lanes do not block Core Gold and must remain unavailable in customer copy.
- **Advanced Visual Gold** is a separate opt-in claim scope. It fails closed unless every claimed MagicFit/Magic/OMagic lane has exact candidate-bound provider provenance, accepted playback, quota/account state, privacy, isolation, source-receipt hashes, and media-artifact hashes. Adapter configuration or a generated file alone is never Advanced Visual Gold evidence.
- The current Advanced Visual producer receipts do not yet carry source-side `release_commit_sha` + `image_digest` identities and exact verifier/source packet hashes. Therefore this candidate records Advanced Visual Gold as `unavailable_unbound_producer_receipts`. The aggregate rejects these legacy/current shapes and never relabels them from its own CLI arguments; Core Gold remains independently eligible.

## Repository authority transition

`ArchonMegalon/propertyquarry` is the sole canonical repository under
`config/release/propertyquarry_repository_role.v1.json`. The historical
`ArchonMegalon/property` repository is a noncanonical verifier only, has no
release/image/deployment authority, and is not an exact mirror.

The legacy repo's formerly recorded runtime candidate
`8b9b2dd7e7d2df6e52c51572e4c44cbdff53a8e1` and envelope head
`6e28833e06e31703b59fca11e4bed0d2c16e3cc7` are retired provenance, not
alternate release candidates. The canonical candidate is only the exact
`release_commit_sha` in the marked object below. Different canonical and legacy
heads are therefore expected; neither equality nor a second candidate identity
is required or permitted.

## Evidence-count history

No governed source case was removed. The current and archived source-backed
contract both select seven cases (`7/7`). The separate journey matrix contains
eight required product journeys, which is the historical `8/8` often quoted in
release notes. The old real-browser lane also selected eight cases; it has since
expanded to sixteen (`16/16`) without changing the seven-case source contract.
The three counts are intentionally reported separately:

- governed source cases: `7/7`;
- product journey rows: `8/8`; and
- current real-browser cases: `16/16`.

## Candidate binding

The marked JSON object is the single canonical release authority consumed by the runtime and release verifier. Its exact field set and canonical SHA-256 fail closed on missing, duplicate, unexpected, empty, or mismatched fields.

<!-- propertyquarry-release-manifest-json:start -->
```json
{
  "release_artifact_set": "propertyquarry-generated-release-artifacts-v1@sha256:c385a928cdea2c2104533abfdad3dc536ae4529b69e1d8d7a963f3138fbddc9b",
  "release_branch": "main",
  "release_candidate_status": "source-browser-candidate-pending-local-docker-receipt",
  "release_commit_sha": "4a5670b889116b53c4ed6e52519bcc9835ff793c",
  "release_deployment_id": "propertyquarry-governed-deploy-4a5670b88911",
  "release_generated_at": "2026-08-04T15:56:23Z",
  "release_label": "propertyquarry-source-browser-candidate-4a5670b88911",
  "release_manifest_schema": "propertyquarry.release_manifest.v1",
  "release_product": "PropertyQuarry",
  "release_public_origin": "https://propertyquarry.com",
  "release_repository": "ArchonMegalon/propertyquarry",
  "release_repository_origin": "https://github.com/ArchonMegalon/propertyquarry.git",
  "release_verification_commands": "./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight"
}
```
<!-- propertyquarry-release-manifest-json:end -->

The artifact-set identity covers the exact tracked bytes of the flagship release receipt, weekly product pulse, and browser workflow proof named by the verifier. The runtime SHA is the source candidate recorded by the flagship receipt; it is intentionally not a self-reference to the later documentation/envelope commit. Any missing, duplicate, empty, or mismatched authority field blocks verification.

## Launch blockers

Production stays fail-closed until every item is bound to the exact runtime candidate:

1. The authenticated local release gate bundle is green for the final runtime and receipt envelope, with no accepted skips or mutable generated state.
2. The local Docker operator deploys only the canonical Compose files, preserves the canonical public-tour volume, and writes a secret-free receipt binding image IDs, Compose hashes, service topology, migration, health, security posture, and a live raw public/private tour-volume privacy audit. Legacy tour repair requires an exact pre-mutation backup volume, atomically rebuilds public manifests through the current allowlist, retains removed values in mode-`0600` private receipts, and emits only counts and content digests.
3. The local image store contains distinct immutable `PROPERTYQUARRY_WEB_IMAGE` and `PROPERTYQUARRY_RENDER_IMAGE` IDs built from the canonical checkout.
4. Dependency, container, policy, and SBOM scans pass without stale databases or weakened gates.
5. The governed deployment preserves the canonical public-tour inventory, repairs only journaled legacy ownership, and completes; `/version` reports the approved runtime SHA and complete manifest; `/` remains healthy without generated-bundle permission errors or path disclosure.
6. A supported sign-in path, lifecycle controls, Telegram delivery, PostgreSQL durability, and the customer search-to-decision journeys pass local live verification.
7. Observability, alerting, rollback, disaster recovery, post-promotion smoke, and Cloudflare/public-origin receipts pass.
8. Billing, analytics, and Core Gold limitations are resolved with evidence or excluded from the launch claim. Advanced scene-video/provider-media limitations may be excluded only by selecting Core Gold and keeping every affected customer claim unavailable; an Advanced Visual Gold claim remains blocked.

## Gold evidence tier and claim scope

- Evidence tier (`standard|flagship|launch`) is independent from claim scope (`core|advanced_visual`). Production release always uses `launch`; standard preserves operator-summary semantics and cannot make a release claim.
- `core_gold` is a strict compatibility alias for `launch` + `core`. It requires the first-party customer operating loop, every Core launch/UX receipt, and one provider-authentic walkable tour from the `matterport|3dvista` alternative group. Its provider fields are `core_required_provider_mode_groups`, `core_required_provider_modes`, and `core_missing_provider_modes`.
- `advanced_visual_gold` is a strict compatibility alias for `launch` + `advanced_visual`. It adds governed MagicFit, Magic, and OMagic evidence plus an offline aggregate binding to the exact release SHA/image, current source receipts, provider artifact hashes, quota/account state, privacy, and isolation. Its provider fields are `advanced_visual_required_provider_modes` and `advanced_visual_missing_provider_modes`.
- Every authoritative Advanced Visual source must carry its expected schema plus source-side `release_commit_sha` and `image_digest`. Every derived verifier/status/packet must also bind the exact upstream receipt or packet SHA-256. Missing or replayed identities yield `unavailable_unbound_producer_receipts`; freshness plus aggregate CLI arguments are not release authority.
- Legacy `required_provider_modes` / `missing_provider_modes` remain a combined operator envelope. They must not be used to make a Core Gold decision; operator dashboards consume the explicit combined `operator_*` fields.
- Any customer-facing walkthrough-ready claim is fail-closed even under Core Gold when its exact provider receipt or playback binding is absent, invalid, stale, over quota, privacy-unsafe, or outside the governed render isolation boundary.

## Whole-project Gold boundary

- Evidence-map overlay source and browser UI proof is green for unavailable, stale, and verified states. Whole-project Gold remains blocked until local live authenticated source coverage and candidate-bound cache-recency, source-time/reference-period, and performance receipts cover environmental quality, heat, traffic/noise, mobility, schools, official aggregate safety context, media-attention statistics with article links, and fiber/broadband coverage.
- Rybbit remains a whole-project gold blocker until dashboard/API receipts prove the approved taxonomy across conversion, product engagement, billing, tours, support/recovery, and activation without private candidate, listing, or contact payloads.
- Production security remains a whole-project gold blocker until runtime/container hardening, reproducible supply chain, dependency/container scans, SBOM, durable RBAC/session revocation, key rotation, and disabled production override receipts are current.
- The authenticated local candidate gates and deployed/live receipts remain required before launch authority can be granted.

## Rules

- Update this file only from current candidate and production evidence.
- Treat a tracked-`main`/runtime SHA mismatch as blocked until governed deployment reconciles it.
- Store detailed machine receipts in the local mode-`0600` receipt store, not in this document.
- Never include credentials, tokens, cookies, license keys, or customer data.
- Never bypass the local Docker receipt, provenance, rollback, or disaster-recovery gates.
