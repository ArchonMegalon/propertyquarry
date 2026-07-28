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
- [ ] Run `scripts/property_evidence_overlay_read_model.py --stage-only` from the protected release lane with the Teable HTTPS origin, bearer credential, overlay base ID, production Postgres URL, exact candidate SHA, and independently configured `PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN` plus `PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256`. Require authenticated API response/table/page digests, exact eight-layer staged coverage, fresh source rows, three indexed candidate lookups per layer, p95 at or below the fixed 100 ms ceiling, and a mode-`0600` v2 staged receipt. Launch mode must reject `--teable-export` and prefetched fixtures.
- [ ] Run launch Gold against the staged overlay receipt while the old active pointer is unchanged. Only after staged Gold passes, run explicit receipt-bound `--activate-snapshot` with a mode-`0600` rollback token, require compare-and-switch against the receipt's prior active pointer, revalidate the active snapshot, and require the updated receipt to prove `activation.phase=active`. Arm the workflow ERR trap to run the idempotent compare-and-restore command; restore must refuse when the active pointer no longer equals the just-activated snapshot.
- [ ] Run `scripts/propertyquarry_rybbit_evidence.py` against the exact public candidate. Require the non-empty tracking script, anonymous attribute-free collector POST/2xx, authenticated site/has-data/events API confirmation, matching hashed site identity, no private payload fields, and a receipt no more than 15 minutes old.
- [ ] Export `PROPERTYQUARRY_EVIDENCE_OVERLAY_RECEIPT`, `PROPERTYQUARRY_RYBBIT_EVIDENCE_RECEIPT`, `PROPERTYQUARRY_PUBLIC_ORIGIN`, `PROPERTYQUARRY_RYBBIT_ORIGIN`, and `PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256`; launch-profile Gold must consume both receipts and fail closed on a missing or mismatched binding.

## Dependency, image, and SBOM security

- [ ] Follow `docs/PROPERTYQUARRY_RELEASE_SECURITY.md`; use a full release SHA and digest-pinned PropertyQuarry web/render image references.
- [ ] Build the web image from its two immutable stages and require
  `tests/test_propertyquarry_web_image_contract.py` to pass. Confirm both bases
  are pinned to reviewed exact `linux/amd64` child manifests, the shipped
  Distroless stage runs as `10001:10001`, has no shell entrypoint, `apt`, `curl`,
  or `pip`, and exposes every copied native-library package through
  `/var/lib/dpkg/status.d`.
- [ ] Require `tests/test_propertyquarry_wheelhouse_contract.py` to pass.
  Confirm the exact 37-file CPython 3.12 Linux x86-64 wheelhouse matches
  `ea/requirements.lock` plus the pinned installer and
  `ea/requirements.wheelhouse.lock`, and prove a forced no-cache build succeeds
  with `--network none --pull=false`.
- [ ] Authenticate candidate construction before publication: bind the full
  source SHA, release-manifest semantic hash, base manifests, Dockerfile,
  context inventory, local OCI manifest, image configuration, and rootfs
  diff-IDs in a private canonical receipt. Require a direct Registry v2 digest
  observation, pull by repository digest, post-pull identity match, and runtime
  health proof.
- [ ] Treat a `propertyquarry.loopback_registry_publication.v1` receipt as local
  construction-path evidence only. It does not establish durable production
  publication, controller admission, deployment, traffic promotion, or launch
  authority. Require the current web and render images to repeat immutable
  publication and pull verification in the controller-owned release registry.
- [ ] Confirm the protected lane uses `pip-audit==2.10.1` from the authenticated release-Python tree; root-owned checksum-pinned Syft `1.44.0` and Trivy `0.72.0` binaries; root-owned hash-pinned empty YAML scanner configurations; the root-owned security runtime manifest; fresh hash-bound vulnerability and Java databases; and both exact images already loaded locally.
- [ ] Confirm every Trivy invocation uses a fresh private cache root whose
  audited `db` and `java-db` symlinks point only to the root-owned canonical
  databases, and whose writable `fanal` cache satisfies the fixed ownership,
  type, mode, link-count, member-count, and aggregate-size bounds before and
  after scanning.
- [ ] Require the controller-run flagship security phase and its atomic receipt to pass before promotion; missing scanners, images, databases, SBOMs, or scanner JSON must fail closed. The disabled legacy `propertyquarry-flagship-security` workflow job is inert migration history and has no release authority.
- [ ] Review the atomic receipt, authenticated scanner execution paths/digests, run-consistent pre/post runtime-manifest and database identities/freshness, the descriptor-captured dependency-lock identity and private audit snapshot contract, both CycloneDX SBOMs, both Trivy results, artifact hashes, and explicit severity threshold. Without explicit overwrite, require output-bundle preflight to preserve every prior receipt and artifact byte.
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
