# Release Checklist

## Preflight

- [ ] `git status` is clean on release branch.
- [ ] Fetch the intended release upstream immediately before reconciliation and
  record its immutable commit. Cached remote-tracking refs are planning evidence
  only; they cannot establish that the candidate includes the current upstream.
  Follow `docs/PROPERTYQUARRY_CACHED_UPSTREAM_RECONCILIATION.md` and revalidate
  its pinned findings against the freshly fetched commit before resolving
  source, schema, controller, or generated-evidence conflicts.
- [ ] Regenerate
  `config/propertyquarry_release_verifier_requirements.lock` from
  `config/propertyquarry_release_verifier_requirements.in` with the recorded
  `uv pip compile --python-version 3.12 --generate-hashes` contract. Do not
  hand-edit the hash lock. Update its authenticated bootstrap/pin digests and
  rebuild the private verifier through the controlled release-environment
  recovery lane, then require the protocol suites and JSON Schema format checks
  to pass. The v3 pin binds the canonical input, its included
  `ea/requirements.lock`, and the compiled hash lock by path and SHA-256.
  Bootstrap and the authenticated launcher must reject stale base or direct-pin
  parity before environment creation, installation, or test collection. The
  current authenticated verifier includes
  `jsonschema[format-nongpl]==4.26.0`; its format checks and the 373-test
  release-protocol suite pass. The cached-upstream reconciliation probe
  produced no authoritative replacement lock, performed no download, and
  requires explicit operator approval before any networked recovery.
- [ ] For a PropertyQuarry release, do not export checkout-local database, traffic, owner, migrator, or controller credentials. The independently installed release controller owns its root-managed secret store and canonical runtime configuration.
- [ ] If separately validating the legacy EA stack, keep its `.env`, `EA_STORAGE_BACKEND`, and `DATABASE_URL` work outside the PropertyQuarry release path and do not treat it as PropertyQuarry deployment evidence.
- [ ] `PRODUCT_RELEASE_CHECKLIST.md` is fully satisfied for the current product wedge.
- [ ] `.codex-design/repo/IMPLEMENTATION_SCOPE.md`, `.codex-design/ea/START_HERE.md`, `docs/PRODUCT_BRIEF.md`, and `docs/PROPERTY_DECISION_WORKBENCH_GUIDE.md` still match the shipped public/app surface.
- [ ] `.codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md`, `.codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json`, and `.codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json` agree with the PropertyQuarry browser workflow proof and exact candidate identity.
- [ ] When refreshing release evidence is intended, explicitly run `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py materialize-release-assets-authenticated` and review the resulting diff.
- [ ] `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py verify-flagship-release-readiness-authenticated` passes as read-only validation that the already-materialized weekly pulse, browser proof, flagship receipt, and Fleet journey gate are all clear for wider release claims, and that the browser -> flagship -> weekly digest/size chain matches the canonical bytes.
- [ ] The authenticated `release-preflight` target leaves the already-materialized weekly pulse, browser proof, and flagship receipt byte-for-byte unchanged; any refresh uses the explicit materialization target above.
- [ ] Product boundary reviewed: non-core public utility routes are disabled unless intentionally required (`EA_ENABLE_PUBLIC_RESULTS`, `EA_ENABLE_PUBLIC_TOURS`).
- [ ] Select and record an evidence tier (`standard|flagship|launch`) independently from claim scope (`core|advanced_visual`). Release uses the `launch` tier; `core_gold` and `advanced_visual_gold` remain strict launch-tier aliases, never lighter profiles.
- [ ] Core scope requires the search -> shortlist -> property -> first-party 3DVista/public-tour -> dossier -> decision -> governed delivery loop and must not advertise unavailable MagicFit/Magic/OMagic or scene-video output. Advanced Visual scope adds exact candidate-bound provider provenance, accepted playback, quota/account state, privacy, isolation, source-receipt hashes, and media-artifact hashes; missing or mismatched evidence fails closed.
- [ ] Advanced Visual producers themselves emit the expected schema, `release_commit_sha`, `image_digest`, and exact upstream receipt/packet SHA links. The aggregate must never add those identities to legacy receipts; until every producer does so, record `unavailable_unbound_producer_receipts` and keep Advanced Visual Gold blocked.
- [ ] CI smoke workflow is green.
- [ ] The smoke workflow explicitly reproduces browser -> flagship -> weekly receipts after Chromium installation in its disposable canonical checkout, immediately verifies an exact `HEAD` match, and then runs the read-only authenticated CI gates. Treat this as reproduction evidence only, not publication or release authority.
- [ ] Source-tree read-only authenticated CI gate bundle (`./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ci-gates-authenticated`) is green, leaves all three canonical receipt bytes unchanged, disables repository pytest/bytecode caches, and routes nested exit-gate evidence through a cleaned private temporary directory.
- [ ] Optional read-only local parity run completed: `make ci-gates`.
- [ ] Optional local parity run including Postgres smoke completed: `make ci-gates-postgres`.
- [ ] Optional local parity run including legacy migration smoke completed: `make ci-gates-postgres-legacy`.
- [ ] Optional docs parity run completed: `make docs-verify`.
- [ ] Optional docs+usage parity run completed: `make release-docs`.
- [ ] `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py propertyquarry-release-protocol-contracts` passes. Treat this only as offline protocol and handoff conformance evidence, never as signature verification, authorization, controller attestation, or a live-release claim.
- [ ] Docs parity confirms the EA canon, flagship truth plane, gate seed, and generated receipt are present and the browser proof is still green. For PropertyQuarry, those assets must remain current, exact-candidate-bound, and scoped to the standalone product.

## Build & Deploy

- [ ] Follow `docs/PROPERTYQUARRY_SLO_RELEASE_EVIDENCE.md`; production remains blocked while the tracked external-controller manifest or digest pin is `UNCONFIGURED`. Release control—not the deploy actor—must install the root-owned fixed controller/manifest/pin, canonical Compose plan, v2 keyring, dedicated database policy, monitoring topology/tool pins, secret store, signed genesis, and external monotonic compare-and-swap authority.
- [ ] Confirm the independent controller implements `docs/PROPERTYQUARRY_RELEASE_CONTROL_PROTOCOL_V1.md`; the repository validator is a conformance aid and has no authentication or deployment authority.
- [ ] Treat source tests as fail-closed/FD-handoff evidence only. Do not claim containment, fencing, receipt, Gold, or traffic semantics until the installed native controller is independently attested in the target environment.
- [ ] Confirm the fixed controller lock is acquired and ingress, API, dedicated worker, scheduler, render, and any live migrator are contained before journal reads, host-port/provenance checks, or stale/new receipt validation.
- [ ] Confirm the dedicated target is not the default `postgres` database; control, NOLOGIN owner, migrator, and per-epoch runtime roles are distinct non-superusers, `PUBLIC CONNECT` is revoked, applications have no owner/control/migrator credentials, restart is controller-owned, and zero target backends/prepared transactions/logical writers are proved under the durable fence.
- [ ] Obtain independent signed pre-migration authorization bound to release/image, server-derived cluster/database/target identity, observed inventory, drain challenge, exact plan, actor, nonce, and TTL. Configure only the external receipt path, target ID, and actor ID.
- [ ] Confirm the external controller commits DDL, migration ledger, challenge/plan link, and migration-result digest in one transaction while the fence remains active; it must activate a new runtime-role epoch only after the exact result is sealed.
- [ ] Confirm promotion authorization binds the sealed migration result and exact candidate Gold/observability evidence, then is atomically consumed once immediately before Cloudflared promotion. Require public `/health/ready` and `/version` to expose the exact candidate SHA and image digest.
- [ ] Confirm the controller's canonical plan binds Cloudflared by immutable image digest and reviewed config hash; mutable tags such as `latest` have no release authority.
- [ ] Confirm drain-key rotation enforces monotonic epochs, activation, explicit old/new overlap, and revocation; local journal or ledger deletion/restoration must fail against the external generation/hash chain.
- [ ] On any post-migration or promotion failure, confirm public ingress and candidate API/worker/scheduler/render remain stopped. A consumed receipt cannot be reused.
- [ ] As an unprivileged operator, run read-only disposition first: `EA_RUNTIME_MODE=prod PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST=/run/user/$(id -u)/propertyquarry-deploy-preflight-request.json ./scripts/deploy_propertyquarry.sh --preflight-only`. The request file is private untrusted transport; only the installed controller authenticates it. It must bind `deploy-preflight`, cannot authorize mutation, and is never reused for deployment.
- [ ] After reviewing a `READY` disposition, obtain a distinct fresh `deploy-run` request and run the same handoff without `--preflight-only`: `EA_RUNTIME_MODE=prod PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST=/run/user/$(id -u)/propertyquarry-deploy-run-request.json ./scripts/deploy_propertyquarry.sh`. Verify the checkout replaces itself with the installed controller and performs no local Docker, database, receipt, or traffic action.

## Migrations

- [ ] Require the installed controller to perform the database fence, migration, ledger commit, runtime-role epoch activation, and post-migration verification described above; checkout-local migration scripts have no PropertyQuarry release authority.
- [ ] Retain the controller's signed, release-bound migration result and verify that it is linked to the exact promotion authorization and candidate evidence.
- [ ] Treat `make bootstrap`, `make db-status`, and their underlying scripts as legacy EA/development utilities only. Never run them against a PropertyQuarry release target or use their output as production evidence.
- [ ] From the controller-owned verification receipt, confirm the expected durable tables exist, including:
  - `execution_sessions`
  - `execution_events`
  - `observation_events`
  - `delivery_outbox`
  - `policy_decisions`

## Smoke

- [ ] Optional authenticated read-only repository preflight: `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight` (includes flagship release-readiness verification)
- [ ] `make release-smoke`
- [ ] The product proves one durable brief -> search dispatch -> ranked results -> property dossier -> shortlist or feedback -> revisit loop.
- [ ] The Gold receipt emits `evidence_tier`, `claim_scope`, `core_required_provider_modes`, `advanced_visual_required_provider_modes`, both explicit missing lists, and the combined operator list; Core scope keys only off the core provider list while retaining every launch-tier customer/UX requirement.
- [ ] A launch-tier Core smoke with verified 3DVista and absent MagicFit can pass only when every Core launch/customer-loop receipt is present; the paired Advanced Visual scope stays unavailable/blocked without its additive candidate binding.
- [ ] Any customer-visible walkthrough-ready claim without its exact accepted provider/playback receipt fails in every profile.
- [ ] Browser surface contract tests confirm no product-surface links to experimental routes in product mode.
- [ ] `make operator-help` (manual spot-check of script usage contracts)
- [ ] Optional combined read-only local mirror: `make ci-gates`
- [ ] Confirm blocked-policy path returns `403`.
- [ ] Confirm delayed, partial, failed, and offline search states retain customer-safe recovery and candidate-bound evidence without contacting a real provider during release tests.

## Observability

- [ ] For PropertyQuarry, review the installed controller's release-bound, sanitized API/database log receipt and monitoring evidence. The checkout has no Docker authority, and direct Compose logs are not PropertyQuarry release evidence.
- [ ] Only when separately diagnosing the legacy EA development stack, `docker compose logs --tail 200 ea-api ea-db` may be used as a legacy-only local check; keep its output outside the PropertyQuarry release path.
- [ ] Verify no repeated fallback warnings in postgres-required environments.
- [ ] Follow `docs/PROPERTYQUARRY_OBSERVABILITY.md`; confirm production application and Uvicorn logs parse as one-line JSON and correlation IDs appear on requests and 500s.
- [ ] Confirm an unauthenticated `/internal/metrics` scrape is rejected and a private system-token scrape returns Prometheus text with `Cache-Control: no-store`.
- [ ] Confirm readiness is `1` and every required worker/scheduler heartbeat reports `present=1` and `stale=0` before traffic promotion.
- [ ] Confirm request-error, latency, readiness, and heartbeat alerts target every API replica without routing the metrics endpoint publicly.
- [ ] Load `config/monitoring/propertyquarry_alert_rules.v1.yml` and retain the matching versioned SLO and synthetic rule-test files.
- [ ] Capture a fresh authenticated private metrics snapshot with `scripts/propertyquarry_slo_capture.py`; require `no-store`, release SHA, image digest, replica identity/count, mode `0600`, and `credential_persisted=false`, and never store the bearer credential in either artifact.
- [ ] Run `scripts/propertyquarry_slo_evidence.py --flagship --image-digest <digest>` with pinned preinstalled `promtool` and `amtool`; require syntax/config checks and synthetic injection tests for every required alert.
- [ ] Export `PROPERTYQUARRY_SLO_METRICS_SNAPSHOT` and `PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT`; require `scripts/property_release_gates.sh`, deploy gold status, and final deploy success to consume the passing receipt while it is no more than 15 minutes old.
- [ ] Export `PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT`, `PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT`, `PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE`, and `PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT`; retain the raw private response and require `propertyquarry_observability_receipts.py verify` to recompute byte/hash/matrix/config/replica/alert links.
- [ ] Require gold `--require-launch-evidence` to invoke both canonical validators from the raw artifacts; never promote from producer booleans or a copied green verification receipt.
- [ ] Require availability/error-rate, p95/p99 latency, readiness, required worker/scheduler heartbeat, provider/quota, and conditional DB/queue evidence to pass.
- [ ] Preserve the atomic SLO evidence receipt and follow `docs/PROPERTYQUARRY_SLO_ALERT_RUNBOOK.md` for any firing condition.
- [ ] Run `scripts/property_evidence_overlay_read_model.py --stage-only` from the protected release lane with the Teable HTTPS origin, bearer credential, overlay base ID, production Postgres URL, exact candidate SHA, and independently configured `PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN` plus `PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256`. Require authenticated API response/table/page digests, exact eight-layer staged coverage, cache rows no older than 48 hours, cadence-specific source evidence, three indexed candidate lookups per layer, p95 at or below the fixed 100 ms ceiling, and a mode-`0600` v3 staged receipt. Launch mode must reject `--teable-export` and prefetched fixtures.
- [ ] Run launch Gold against the staged overlay receipt while the old active pointer is unchanged. Only after staged Gold passes, run explicit receipt-bound `--activate-snapshot` with a mode-`0600` rollback token, require compare-and-switch against the receipt's prior active pointer, revalidate the active snapshot, and require the updated receipt to prove `activation.phase=active`. Arm the workflow ERR trap to run the idempotent compare-and-restore command; restore must refuse when the active pointer no longer equals the just-activated snapshot.
- [ ] Run `scripts/propertyquarry_rybbit_evidence.py` against the exact public candidate. Require the non-empty tracking script, anonymous attribute-free collector POST/2xx, authenticated site/has-data/events API confirmation, matching hashed site identity, no private payload fields, and a receipt no more than 15 minutes old.
- [ ] Export `PROPERTYQUARRY_EVIDENCE_OVERLAY_RECEIPT`, `PROPERTYQUARRY_RYBBIT_EVIDENCE_RECEIPT`, `PROPERTYQUARRY_PUBLIC_ORIGIN`, `PROPERTYQUARRY_RYBBIT_ORIGIN`, and `PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256`; launch-profile Gold must consume both receipts and fail closed on a missing or mismatched binding.

## Dependency, image, and SBOM security

- [ ] Follow `docs/PROPERTYQUARRY_IMAGE_PUBLICATION.md`; manually run the protected image-publication workflow on the clean `main` envelope, retain its machine receipt, and treat the two distinct digest references as publication evidence only—not deployment or promotion authority.
- [ ] Follow `docs/PROPERTYQUARRY_RELEASE_SECURITY.md`; use a full release SHA and digest-pinned PropertyQuarry web/render image references.
- [ ] Confirm the protected security runner has reviewed preinstalled `pip-audit`, Syft, and Trivy versions, current pre-provisioned Trivy databases, and both exact images already loaded locally.
- [ ] Require the focused `propertyquarry-flagship-security` job to pass before the live release job; missing scanners, images, databases, SBOMs, or scanner JSON must fail closed.
- [ ] Require the canonical security-runner bootstrap target-run attestation for the exact workflow run, job, SHA, one-time label, registration, cleanup, and terminal security result. Missing, stale, or mismatched bootstrap consumption blocks release even when the scanner job itself passed.
- [ ] Review the atomic receipt, dependency audit, both CycloneDX SBOMs, both Trivy results, scanner versions, artifact hashes, and explicit severity threshold.
- [ ] Treat dependency findings with unknown normalized severity as blocking-critical for flagship release decisions.
- [ ] Require every waiver to match the documented exact source/immutable target/vulnerability/package/severity/release schema and expire within 30 days; reject expired or cross-release waivers.
- [ ] Preserve the complete private CI artifact and do not replace flagship evidence with the advisory local mode.

## Backup and restore evidence

- [ ] Follow `docs/PROPERTYQUARRY_POSTGRES_DISASTER_RECOVERY.md` before production launch; v1 or incomplete DR receipts have no launch authority.
- [ ] Build the exact candidate first and record its full 40-character Git SHA plus immutable `sha256:` web-image identity.
- [ ] Require a recent encrypted v2 backup receipt whose release identity matches the candidate and whose source ledger is recorded as an exact valid ordered prefix (including explicit version 0/ledger absence), with names, checksums, and fingerprint.
- [ ] Require provider-native read-back proof for a matching encrypted off-host object: non-local provider, bucket/container, object key, immutable version ID, ETag, matching SHA-256/size, existence, checksum verification, method, and timestamp.
- [ ] Require a passing disposable v2 restore-drill receipt from that exact off-host object with `rpo_met=true`, `rto_met=true`, schema/integrity checks, non-empty required-table checks, and passing release-specific verification/readiness hooks.
- [ ] Run the candidate migration command only against the disposable restore; confirm the preserved source prefix matches between receipts and the post-migration restored ledger exactly matches the current release source contract.
- [ ] Confirm the restore target name begins `propertyquarry_restore_drill_` and is not the source database.
- [ ] Run `propertyquarry_postgres_dr.py release-gate` with `PROPERTYQUARRY_DR_BACKUP_RECEIPT`, `PROPERTYQUARRY_DR_RESTORE_RECEIPT`, `PROPERTYQUARRY_RELEASE_COMMIT_SHA`, and `PROPERTYQUARRY_RELEASE_IMAGE_DIGEST`; retain the passing `_completion/disaster_recovery/release-gate.json`.
- [ ] Require deploy preflight to pass before build and require the rebuilt web image ID/repository digest to match the DR-bound image digest before any database, migration, API, worker, or scheduler start.

## Property-search schema boundary

- [ ] Follow `docs/PROPERTYQUARRY_SCHEMA_MIGRATIONS.md`; never grant API, worker, or scheduler startup authority to create or alter the property-search schema.
- [ ] Require the one-shot `propertyquarry-migrate` deploy phase to finish before API, worker, or scheduler startup.
- [ ] Preserve the ordered migration names and SHA-256 checksums in `propertyquarry_schema_migrations`; treat checksum drift, gaps, or future versions as blocking.
- [ ] Require `/health/ready` to report the current property-search schema version before traffic promotion.
- [ ] Confirm schema v4 includes `delivery_outbox` claim/lease indexes and that runtime outbox repositories contain no `CREATE` or `ALTER` statements.
- [ ] Run deterministic scheduler replica-race, email crash/retry, Telegram ambiguous-outcome dead-letter, and bounded-attempt contracts; never exercise a real provider during release tests.
- [ ] Scrape `propertyquarry_scheduler_delivery_outbox_events_total` and alert on increasing `dead_lettered`, `failed`, or sustained `claim_conflicts` outcomes.
- [ ] Confirm schema v5 includes `property_content_jobs`, ordered `property_content_job_events`, and unique `property_content_webhook_events`; runtime content-ledger repositories must contain no DDL.
- [ ] Confirm schema v8 includes the original overlay rollups/snapshots and schema v9 adds snapshot-keyed rollups, staged/active/retired status, the singleton active pointer, and snapshot lookup/freshness indexes. The overlay ingestion and runtime repositories must fail closed when the governed migration is pending and must contain no schema DDL or destructive active-row replacement.
- [ ] Run deterministic content-job/webhook replica races, payload-conflict replay, corruption preservation, and expired-lease crash recovery contracts without contacting Subscribr.
- [ ] Scrape `propertyquarry_content_ledger_events_total`; investigate increasing `replay_conflict`, `failed`, or `corruption` outcomes before promotion.
- [ ] Run the source-only schema contract check without `DATABASE_URL`; use only an explicitly disposable test database for PostgreSQL contracts.

## Rollback

- [ ] Follow `docs/PROPERTYQUARRY_ROLLBACK.md` and record distinct immutable current/previous identifiers (full Git SHA or image digest).
- [ ] Stage and fully probe the previous release under an isolated Compose project, unique container names, and a non-public host port with Cloudflare disabled.
- [ ] Run the command-free rollback dry-run and review its private JSON receipt before requesting signed release-control authorization.
- [ ] Deliver the signed rollback authorization directly to the installed-controller service. Confirm source `propertyquarry_rollback.py --execute` fails closed; candidate-selected schema, traffic, and verification commands have no execution authority.
- [ ] Require the installed controller to own the fixed lock, external monotonic journal, containment, database fence, forward-schema compatibility proof, traffic, and verification; rejection has no local fallback.
- [ ] Preserve DB data volume; do not drop tables during rollback.
- [ ] Open incident note with failing endpoint, timestamps, and logs.

## Host reboot recovery

- [ ] Follow `docs/PROPERTYQUARRY_HOST_RECOVERY.md`; do not use the legacy EA Compose stack or a generic host-wide restart command.
- [ ] Record the immutable 40-character release SHA, dedicated `propertyquarry-*` Compose/tunnel identities, and `propertyquarry.com` route; keep tunnel/database credentials only in the installed controller's root-owned store.
- [ ] Run the command-free recovery dry-run and review its private atomic receipt before execution.
- [ ] Deliver the signed recovery authorization directly to the installed-controller service. Confirm source `propertyquarry_host_recovery.py --execute` fails before Compose inspection; require exactly six steady-state services and one declared ephemeral migration service in controller-owned evidence.
- [ ] Require the installed controller to reconcile the fixed lock, external journal, unconditional containment, database role fence, schema epoch, compatible runtime, private proofs, and public promotion; candidate Compose commands have no fallback authority.
- [ ] Preserve the `0600` receipt; on failure, do not run `up`, `down`, `restart`, `--remove-orphans`, candidate migrations, or an automatic rollback.

## Global flagship launch authority

- [ ] Treat `docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md` as the terminal acceptance contract. A source-only, browser-only, private-beta, or protected-CI checkpoint is evidence progress, not authority for a global-launch claim.
- [ ] Materialize `docs/propertyquarry_global_market_envelope.v1.json` with `scripts/propertyquarry_global_market_envelope.py`; require every claimed launch market and journey to be `launch_supported` with native content, locale behavior, browser/device, accessibility, performance, privacy, provider-rights, and production-like evidence. Do not broaden the claim from the current AT/DE private beta or CR browser-state-only envelope.
- [ ] Run `scripts/propertyquarry_incident_support_gate.py --fail-on-blocked` against fresh, independently attested live evidence bound to the exact Git SHA and immutable image digest. Require staffed primary and backup roles, real safe HTTPS endpoints, completed drills, market coverage, and approvals; never substitute placeholder contacts.
- [ ] Run `scripts/propertyquarry_global_experience_gate.py --fail-on-blocked` against the governed live receipt for the exact Git SHA and immutable image. Require native AT/DE/CR UI/content review, WCAG 2.2 AA automated and manual keyboard/screen-reader/zoom/reduced-motion evidence, Chromium/Firefox/WebKit plus contracted mobile devices, per-device field CWV p75 cohorts with the minimum sample/window, degraded-network recovery, localized SEO/hreflang, owner approvals, freshness, and independent attestation. Source-only mode must remain blocked.
- [ ] Run `scripts/propertyquarry_jurisdiction_privacy_rights_gate.py --fail-on-blocked` against fresh independently attested evidence for the exact release. Require current independent local legal approvals, localized notices and DSAR paths, privacy/residency and transfer controls, an exact per-market provider inventory, terms/rights approval for every enabled capability, technical enforcement of every prohibition, and binding to the current source contract and market-envelope digests. Source policy is not legal approval.
- [ ] Keep Core Gold independent of paid or optional Advanced Visual providers. A useful, truthful property decision path must remain available when spatial tours, generated media, WebGL, image decode, or premium-provider quotas are absent.
- [ ] Freeze one immutable candidate and re-run the complete focused, PostgreSQL, security/provenance, and Chromium/Firefox/WebKit suites on that exact tree. Earlier receipts remain historical evidence only.
- [ ] Run only `/usr/libexec/propertyquarry/propertyquarry-global-launch-terminal --manifest /run/propertyquarry/release-evidence/global-launch-core-manifest.v1.json`. Its private closed manifest must explicitly provide the exact committed SHA/image, every Gold product-data origin/hash, every Core receipt including the four global-governance gates, all six raw observability inputs, and fresh exact-release preflight, disaster-recovery, capacity, and observability-operations receipts. Require a fresh active-challenge Ed25519 controller signature over all artifact digests, product-data values, output paths, fixed Launch/Core argv contract, an independently selected exact lowercase/unprefixed canonical runtime-manifest SHA-256, independently selected canonical Chromium path/digest policy, and root-owned installed wrapper/Python/Gold/support/policy bundle digests. Require the producer to verify and explicitly launch that Chromium binary, bind both measured document responses to their serving release/manifest/replica, require `/version` and both document responses to match the controller-provided canonical manifest digest, and keep the probe secret out of app/worker/driver/browser environments. Observability operations must prove correlation-ID log ingestion/query, W3C API-to-search/provider/render trace continuity, versioned Core-SLO/queue/provider dashboards, alert delivery, and immutable digest-bound runbooks. The checkout `scripts/propertyquarry_global_launch_terminal.py` is non-authoritative developer validation only. Preserve structured blockers when any evidence is missing, stale, placeholder, unsafe-path, digest-mismatched, candidate-mismatched, unsigned, or below live-production evidence level.
- [ ] Treat protected-runner process creation as the remaining release-probe secret boundary. GitHub Actions has no native secret-to-stdin/FD binding: avoiding a step environment would require interpolating the secret into the generated runner script or introducing a separately installed privileged broker, neither of which is a safe source-only substitution. Keep the secret in the single-command protected step environment and explicitly replace inherited `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, and `GCONV_PATH` with empty values plus `BASH_ENV` and `ENV` with `/dev/null` in that same step before GitHub launches its generated shell. Start the generated step with fixed `/bin/bash -p` and immediately `exec` the hardened release gate; Bash privileged mode blocks inherited functions, and the gate then clears shell, loader, and language hooks and captures/unsets the secret before external commands. Transfer to the nested live gate only through a command-scoped environment assignment placed before `/usr/bin/env`; the utility removes `BASH_ENV`/`ENV` before launching absolute privileged Bash, and the secret must never appear as an `env` operand or process argument. The self-hosted runner service and process launcher remain trusted because they construct the final environment and inject the secret; root-level loader configuration such as `/etc/ld.so.preload`, a compromised runner, or a compromised host loader remains outside source control. Require independent runner/host attestation: these local overrides are not production authority by themselves.
- [ ] Preserve and consume only the `propertyquarry.global_launch_terminal_result.v1` wrapper receipt with `gold_invoked: true`, exact release identity, and the controller-attestation, complete artifact-map, invocation-contract, and Gold-result SHA-256 bindings. Direct Gold output is not global terminal authority.
- [ ] Request protected deployment/promotion authority only after the terminal Gold command passes. Re-prove the exact candidate at the public runtime before making or publishing a global flagship claim.
