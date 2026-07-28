# PropertyQuarry

PropertyQuarry is a standalone property discovery product: cross-platform search, ranking, research packets, hosted review pages, feedback learning, and paid research tiers.

This repository now contains the runnable product runtime that had previously lived inside the broader EA codebase. The goal of this repo is not a docs mirror. It is the source of truth for the PropertyQuarry app, tests, deployment scripts, and branded public surfaces.

## Repository authority

[`ArchonMegalon/propertyquarry`](https://github.com/ArchonMegalon/propertyquarry) is the sole canonical PropertyQuarry source and release repository. PropertyQuarry builds, tests, publishes images, and deploys from this repository without release authority, commits, or runtime dependencies from MyExternalBrain or `ArchonMegalon/property`.

The canonical release graph structurally gates `propertyquarry-release-v2` on the same-run `propertyquarry-security-bootstrap-attestation` job. The release job requires that dependency to succeed and binds its attestation SHA-256, bootstrap run ID, and artifact digest before requesting protected preflight. The separate manual bootstrap workflow additionally records exact protected-runner consumption, but neither a bootstrap artifact nor its consumption receipt is standalone release authority; production remains blocked until every other candidate-bound launch and protected-live gate also passes.

## What is in this repo

- public product surface for `propertyquarry.com`
- onboarding, sign-in, and authenticated property workspace
- property search runs across supported providers and countries
- shortlist ranking, hosted review packets, and 360 tour links
- feedback learning loop and preference profile updates
- PayPal plan upgrades and Emailit-based client notifications
- PayFunnels bootstrap helper: `python3 scripts/bootstrap_payfunnels_propertyquarry.py --help`
- Emailit bootstrap helper: `python3 scripts/bootstrap_emailit_propertyquarry.py --help`
- Docker runtime, smoke scripts, and property-facing tests

Emailit requires the sender domain to be verified before `property@propertyquarry.com` can deliver successfully.

## Product entrypoints

- landing page: `/`
- onboarding: `/register`
- sign-in: `/sign-in`
- property desk: `/app/properties`

The repo defaults to the PropertyQuarry brand even on non-production hostnames.

## EA Release Governance Notes

This runtime still carries the EA flagship release-readiness and operator gate contracts. Reference points:

- EA product surface canon: `.codex-design/ea/START_HERE.md` and `.codex-design/ea/SURFACE_DESIGN_SYSTEM.md`
- EA flagship truth plane: `.codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md`
- EA flagship gate seed: `.codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json`
- EA flagship generated receipt: `.codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json`
- Materializer: `scripts/materialize_ea_flagship_release_gate.py`

Operator parity and release gate shortcuts:

- `make materialize-release-assets` to explicitly refresh the three generated
  release receipts with the selected development Python
- `make verify-flagship-release-readiness` for non-authoritative, read-only
  flagship release-readiness verification of the already-materialized receipts
- `make verify-generated-release-artifacts-clean` for non-authoritative,
  read-only generated release artifact cleanliness against `HEAD`
- `make runtime-hard-exit-gates`
- `make hard-exit-gates`
- `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ltd-release-gates`
- `make verify-ltd-critical-entries`
- `make verify-ltd-flagship-subset`

These hard-exit and LTD verifier scripts remain part of the operator contract even while this repo defaults to the PropertyQuarry product surface.

Release preflight now keys off the EA flagship truth plane, gate seed, generated release receipt, and weekly pulse; `MILESTONE.json` remains supporting delivery history.
The weekly pulse lives at `.codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json` and is refreshed with `scripts/materialize_weekly_product_pulse.py`.
Release preflight checklist includes the EA flagship truth-plane contract in `RELEASE_CHECKLIST.md`.
Recommended sequencing: run `make release-docs` before dispatching the
authenticated `release-preflight` target.
Local and CI-parity targets use the selected development Python. Before an
authenticated repository preflight, use the real `/docker/property` checkout
on the pinned Linux host, run
`./scripts/bootstrap_propertyquarry_release_python.sh`, and then run
`./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight`.
The v3 runtime pin authenticates both the requirements input and compiled lock.
If their exact direct pins disagree, bootstrap and the authenticated launcher
stop before environment creation, package installation, or test collection.
The current authenticated requirements input and compiled lock include
`jsonschema[format-nongpl]==4.26.0`; bootstrap verifies that exact pin before
the release interpreter can collect tests.
That authenticated preflight validates the already-materialized generated
receipts without refreshing or restoring their canonical files.
When a standalone authenticated evidence refresh is intentional, dispatch
`materialize-release-assets-authenticated` explicitly before dispatching the
read-only `verify-flagship-release-readiness-authenticated` target.
The closed dispatcher constructs the only accepted Make invocation; direct
Make targets remain non-authoritative developer facades because GNU Make
parses caller-provided startup files, eval expressions, and alternate
makefiles before this checkout's Makefile can inspect them.
`scripts/version_info.sh` still prints milestone capability-status counts and release tags from `MILESTONE.json` as delivery history, but EA flagship release claims now come from `EA_FLAGSHIP_TRUTH_PLANE.md`, `EA_FLAGSHIP_RELEASE_GATE.json`, and `EA_FLAGSHIP_RELEASE_GATE.generated.json`.

The inherited operator command surface remains available for release and support parity:

- `make operator-help` lists the supported operator scripts.
- `bash scripts/smoke_help.sh --help` validates the help-contract entrypoint.
- `make release-smoke` runs the release smoke bundle.
- `make smoke-postgres-legacy` exercises the legacy Postgres migration fixture.
- `make test-postgres-contracts` runs the focused Postgres contract matrix.
- `make ci-gates-postgres-legacy` keeps local parity with the legacy Postgres CI job.
- `make docs-verify` is the non-authoritative local
  documentation-verification alias.
- `make release-docs` runs the documentation and usage checks that precede release preflight.
- `make all-local` is the lighter local readiness pass with a configurable
  interpreter; the dispatcher's `release-preflight` mode is the authenticated
  repository preflight. It does not make a live-runtime or disaster-recovery claim.
- `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py propertyquarry-release-preflight`
  is the explicit full operator gate. It runs the source/local preflight and
  then the PropertyQuarry release gates, which require release-bound DR
  receipts and authenticated live-runtime inputs and write verification
  receipts under `_completion/`.
- `bash scripts/operator_summary.sh --help` documents smoke, readiness, CI parity, release/support, and task-archive shortcuts.

Endpoint/version/OpenAPI helper scripts also expose `--help`: `scripts/list_endpoints.sh`, `scripts/version_info.sh`, `scripts/export_openapi.sh`, `scripts/diff_openapi.sh`, and `scripts/prune_openapi.sh`.
`EA_LEDGER_BACKEND` remains a temporary backward-compatible alias for the canonical `EA_STORAGE_BACKEND` setting.

Policy and principal-scope contracts remain fail closed. A disallowed tool reports `policy_denied:tool_not_allowed`.
Principal-scoped rewrite/session/artifact/receipt/run-cost, plan-compile, connector, human-task, and memory routes treat body/query `principal_id` as compatibility input only; mismatches against the request principal fail with `403 principal_scope_mismatch`.

Support bundles include live `ea-db` mount inspection output unless disabled with `SUPPORT_INCLUDE_DB_VOLUME=0`.

For a standalone PropertyQuarry runtime, you can extend the runtime-only hard-exit bundle with live product probes:

```bash
PROPERTYQUARRY_RUNTIME_GATES=1 \
EA_API_TOKEN=... \
PROPERTYQUARRY_LIVE_SMOKE_BASE_URL=http://localhost:8097 \
make runtime-hard-exit-gates
```

That optional branch runs the public runtime smoke plus the authenticated, seeded all-surface mobile, and provider-catalog smokes against the deployed PropertyQuarry service.

## Disposable local development

Direct Compose commands are for a disposable local development target only.
They do not produce release evidence and must never point at production
databases, credentials, containers, or traffic:

```bash
cp .env.example .env
# Fill .env with local-only values, including POSTGRES_PASSWORD,
# EA_SIGNING_SECRET, EA_API_TOKEN or local access settings, and a random
# PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN.
EA_RUNTIME_MODE=dev docker compose -f docker-compose.property.yml up -d --build
```

The env template uses the literal non-secret sentinel
`REVIEW_ONLY_NOT_A_SECRET_REPLACE_BEFORE_DEPLOY` for the render-bridge token so
reviewers can render the Compose model with a disposable `POSTGRES_PASSWORD` and
no production secret. Replace that sentinel before starting even a local
service. `PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS=1860` keeps the container stop
budget above the reconstruction generation ceiling; adjust both budgets together
if that ceiling changes.

The disposable topology runs `propertyquarry-migrate` as an ephemeral phase,
then starts `propertyquarry-api`, the property-only durable-search consumer
`propertyquarry-worker`, `propertyquarry-scheduler`,
`propertyquarry-render-tools`, and `propertyquarry-db`. The migration container
must exit `0`; it is never counted as a running or healthy runtime service. See
`docs/PROPERTYQUARRY_SCHEMA_MIGRATIONS.md`.
The API, worker, and scheduler use `ea/Dockerfile.property-web`, a lightweight
web runtime without Blender, COLMAP, MeshLab, or bundled Playwright browser
payloads. The API readiness contract requires a fresh role-correct worker
heartbeat, so a missing or wedged durable consumer fails closed instead of
accepting searches that cannot progress. The worker is fixed to the
`property_only` profile, does not load the advanced-visual env bundle, and does
not join the render network; Core Gold does not depend on paid visual tooling.
Native 3D reconstruction and vendor tooling stay in the explicit `render-tools` profile, which builds `ea/Dockerfile.property`.
Browser-backed PDF/render fallbacks must use MarkupGo or an explicit helper/render lane rather than adding Chromium to the request-serving image.
Both images omit Docker CLI tooling and run the app process as the non-root `ea` user.

For the disposable local topology, open:

- `http://localhost:8090/`
- `http://localhost:8090/register`
- `http://localhost:8090/app/properties`

## Production release handoff

Production deploy, recovery, and traffic authority belongs to the independently
installed release controller. The checkout is only an unprivileged handoff
client. Obtain a short-lived signed request from release control, place it in an
invoking-user-owned, single-link, mode-`0400` file outside the checkout, and run
the read-only disposition first:

```bash
EA_RUNTIME_MODE=prod \
PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST=/run/user/$(id -u)/propertyquarry-deploy-preflight-request.json \
  ./scripts/deploy_propertyquarry.sh --preflight-only
```

A preflight request is operation-bound and non-authorizing; it cannot be reused
to deploy. After reviewing a `READY` disposition, obtain a distinct, fresh
`deploy-run` signed request from release control and invoke the handoff without
`--preflight-only`:

```bash
EA_RUNTIME_MODE=prod \
PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST=/run/user/$(id -u)/propertyquarry-deploy-run-request.json \
  ./scripts/deploy_propertyquarry.sh
```

The caller must remain unprivileged, have no Docker daemon authority, and must
not export database or traffic credentials. The handoff rejects caller-selected
Compose files, Docker contexts, database URLs, tunnel tokens, verifier paths,
receipt outputs, and trust/key paths. Host ports, project/container identities,
provider matrices, migrations, health gates, rollback, and traffic selection
come only from the signed request and the controller's root-managed canonical
configuration; checkout environment overrides have no production authority.

The closed v1 wire contract is documented in
[`docs/PROPERTYQUARRY_RELEASE_CONTROL_PROTOCOL_V1.md`](docs/PROPERTYQUARRY_RELEASE_CONTROL_PROTOCOL_V1.md).
Run
`./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py propertyquarry-release-protocol-contracts`
for its offline schema, binding, and handoff checks. The validator proves
document conformance only; it
does not verify signatures, establish trust, authorize an operation, or contact
the release controller.

Until the fixed controller, manifest, digest pin, Compose plan, database fence,
keyring, gateway trust, monitoring topology/tools, signer, and external
monotonic authority are independently provisioned and attested, production
deployment remains blocked. There is no local Compose fallback.

The inherited EA mega-stack deploy script remains in the repo for disposable
migration and compatibility work. It has no PropertyQuarry production release
authority:

```bash
PROPERTYQUARRY_USE_LEGACY_STACK=1 bash scripts/deploy.sh
```

## Runtime modes

PropertyQuarry keeps the inherited runtime-mode contract because deploy and smoke gates depend on it:

- `EA_RUNTIME_MODE=dev|test|prod`
- `EA_RUNTIME_MODE=prod` must fail fast when durable runtime prerequisites are missing
- `bash scripts/smoke_postgres.sh` verifies the Postgres-backed path and the prod fail-fast behavior

Runtime and environment details live in:

- [ENVIRONMENT_MATRIX.md](ENVIRONMENT_MATRIX.md)
- [HTTP_EXAMPLES.http](HTTP_EXAMPLES.http)
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)

Operator scripts can be pointed at non-default compose service names with:

- `PROPERTYQUARRY_API_SERVICE`
- `PROPERTYQUARRY_SCHEDULER_SERVICE`
- `PROPERTYQUARRY_DB_SERVICE`
- `PROPERTYQUARRY_API_CONTAINER_NAME`
- `PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME`
- `PROPERTYQUARRY_DB_CONTAINER_NAME`
- `PROPERTYQUARRY_RENDER_CONTAINER_NAME`

This alias layer also applies to support exports such as `bash scripts/support_bundle.sh`.

Support export baseline:

- `SUPPORT_INCLUDE_DB_VOLUME=0 bash scripts/support_bundle.sh`
- support bundles can include `ea-db mount/volume attribution`
- expected runtime volume remains `ea_pgdata`
- expected container mount remains `/var/lib/postgresql/data`

## DB operator lane

Runtime DB visibility and retention helpers remain part of the standalone release surface:

- `bash scripts/db_bootstrap.sh`
- `bash scripts/db_status.sh`
- `bash scripts/db_size.sh`
- `bash scripts/db_retention.sh`

Supported controls include:

- `EA_RETENTION_PROFILE=aggressive|standard|conservative`
- `EA_RETENTION_TABLES`
- `EA_RETENTION_SKIP_TABLES`
- `EA_DB_SIZE_SCHEMA=<schema>`
- `EA_DB_SIZE_SORT_KEY=total|table|index`
- `EA_DB_SIZE_TABLE_PREFIX=<prefix>`
- `EA_DB_SIZE_MIN_MB=<n>`
- `SUPPORT_INCLUDE_DB_SIZE=0`
- `SUPPORT_DB_SIZE_LIMIT=<n>`

## Property release gates

Use the product-only release bundle when validating the standalone PropertyQuarry surface:

- `make property-release-gates`
- `./scripts/property_release_gates.sh`

This bundle includes docs links, runtime security posture, repo-isolation checks, browser contracts, and property run/catalog contracts.

## Key docs

- product brief: [docs/PRODUCT_BRIEF.md](docs/PRODUCT_BRIEF.md)
- architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- repo isolation: [docs/REPO_ISOLATION.md](docs/REPO_ISOLATION.md)
- greenfield redesign plan: [docs/GREENFIELD_REDESIGN_PLAN.md](docs/GREENFIELD_REDESIGN_PLAN.md)
- decision workbench implementation guide: [docs/PROPERTY_DECISION_WORKBENCH_GUIDE.md](docs/PROPERTY_DECISION_WORKBENCH_GUIDE.md)
- brand: [docs/BRAND.md](docs/BRAND.md)
- pricing: [docs/PRICING.md](docs/PRICING.md)
- release-control wire protocol: [docs/PROPERTYQUARRY_RELEASE_CONTROL_PROTOCOL_V1.md](docs/PROPERTYQUARRY_RELEASE_CONTROL_PROTOCOL_V1.md)
- domain rollout: [docs/DOMAIN_ROLLOUT.md](docs/DOMAIN_ROLLOUT.md)
- runbook: [RUNBOOK.md](RUNBOOK.md)

## Migration status

This repo now includes:

- `ea/` application runtime
- `scripts/` operator and deploy scripts
- `tests/` runtime and product contract coverage
- `docker-compose*.yml` deployment stack
- config, provider templates, and VPN overlay support

The active migration principle is simple: new PropertyQuarry work lands here first. The old EA repo is no longer the intended home for this product surface.

## Operator Contract Appendix

Runtime storage and deploy notes:

- `ea_pgdata` is the expected Postgres volume mounted at `/var/lib/postgresql/data`; the durable DB volume is disk-backed and not RAM.
- `docker-compose.cloudflared.yml` is the optional dedicated PropertyQuarry Cloudflare tunnel overlay.
- `docker-compose.property-legacy-edge.yml` is the optional legacy edge override that restores the old `ea-api` network alias when you intentionally still need it.
- `docker-compose.host-tools.yml` is the explicit opt-in host-tools profile. The default API, worker, scheduler, and property runtime must not mount `/var/run/docker.sock` or the host repository.
- If you deploy through `scripts/deploy.sh`, keep the overlay explicit with `EA_ENABLE_FASTESTVPN=1`.
- Operator alias envs include `PROPERTYQUARRY_API_SERVICE`, `PROPERTYQUARRY_DB_SERVICE`, `PROPERTYQUARRY_SCHEDULER_SERVICE`, and `scripts/support_bundle.sh`.
- Support exports and DB helpers document `SUPPORT_INCLUDE_DB_VOLUME=0`, `ea-db mount/volume attribution`, `SUPPORT_INCLUDE_DB_SIZE=0`, `SUPPORT_DB_SIZE_LIMIT=<n>`, `EA_RETENTION_PROFILE=aggressive|standard|conservative`, `EA_RETENTION_TABLES`, `EA_RETENTION_SKIP_TABLES`, `EA_DB_SIZE_SCHEMA=<schema>`, `EA_DB_SIZE_SORT_KEY=total|table|index`, `EA_DB_SIZE_TABLE_PREFIX=<prefix>`, and `EA_DB_SIZE_MIN_MB=<n>`.
- Pay/bootstrap helpers include `bootstrap_payfunnels_propertyquarry.py` and `bootstrap_emailit_propertyquarry.py`.

Provider health and runtime hints:

- `/v1/responses/_provider_health` and `/v1/codex/profiles` expose account-attributed credit estimates including `estimated_remaining_credits_total`, `remaining_percent_of_max`, `estimated_burn_credits_per_hour`, and `observed_consumed_credits`.
- Runtime provider routing also documents `provider-hint`, `provider_hint=BrowserAct`, and provider policy details for BrowserAct / 1min skill routing.

Workflow templates and skills:

- Task contracts support `workflow_template`, `artifact_then_dispatch`, `artifact_then_packs`, `post_artifact_packs`, `artifact_then_memory_candidate`, `browseract_extract_then_artifact`, `workflow_template=tool_then_artifact`, and `artifact_then_dispatch_then_memory_candidate`.
- Queue shapes include `step_input_prepare -> step_policy_evaluate -> step_artifact_save -> step_memory_candidate_stage`, `step_input_prepare -> step_browseract_extract -> step_artifact_save`, `step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage`, and `step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage`.
- Registry validation fails fast on `unknown_workflow_template:<value>`.
- `/v1/skills*` and `SKILLS.md` describe the skill catalog, including `ltd_inventory_refresh`, `browseract_bootstrap_manager`, `resolved \`skill_key\``, and `intent_skill_key`.
- `POST /v1/plans/compile` and `POST /v1/plans/execute` accept either `task_key` or `skill_key`.
- LTD refresh helpers include `refresh_ltds_from_inventory.sh` and `refresh_ltds_via_api.sh`.

Evidence, memory, and artifact envelopes:

- `artifact_output_template=evidence_pack` enables first-class evidence envelopes and memory-candidate staging.
- `/v1/evidence/objects` and `/v1/evidence/objects*` expose the evidence ledger.
- `/v1/memory/candidates`, `/v1/memory/stakeholders`, `/v1/memory/interruption-budgets`, and `/v1/memory/context-pack` are the principal-scoped memory seed APIs; the context-pack route injects synthesized `context_pack` payloads from principal-scoped memory reasoning.
- Artifact envelopes expose explicit `principal_id` ownership, `preview_text`, `storage_handle`, `mime_type`, and `body_ref`.

Human-task and queue operations:

- `/v1/human/tasks` handles human task packets, `resume_session_on_return=true`, and `human_task_returned`.
- `/v1/human/tasks/backlog` and `/v1/human/tasks/unassigned` expose the operator backlog and ownerless queue slices.
- Human-review metadata includes `human_review_role`, `human_review_priority`, `human_review_sla_minutes`, `human_review_desired_output_json`, `human_review_authority_required`, `human_review_why_human`, `human_review_quality_rubric_json`, and `human_review_auto_assign_if_unique`.
- Operator routing uses `/v1/human/tasks/operators`, `skill-tag`, `routing_hints_json`, `auto_assign_operator_id`, and `/v1/human/tasks/{human_task_id}/assign`; assignment rows track `assignment_source`, `assigned_at`, `assigned_by_actor_id`, and may omit `operator_id` when auto-assigned.
- `/v1/human/tasks/{human_task_id}/assignment-history` and `human_task_assignment_history` expose task-scoped ownership transitions; assignment-history rows, inline human-task assignment-history rows, and inline human-task packet rows now carry originating task identity.
- Queue state fields include `assigned_operator_id`, `assigned_by_actor_id`, `last_transition_event_name`, `last_transition_operator_id`, and `last_transition_by_actor_id`.
- Queue filters and sorts include `sort=created_asc`, `sort=priority_desc_created_asc`, `sort=last_transition_desc`, `sort=sla_due_at_asc`, `sort=sla_due_at_asc_last_transition_desc`, with fall back to oldest-created ordering for tasks without `sla_due_at`.
- Backlog filters accept `priority=<level>`, comma-separated values like `priority=urgent,high`, `queue views now also accept \`assignment_source=<source>\``, `assignment_source=none`, `human_task_assignment_source=none`, `assignment_state=unassigned&assignment_source=none`, `assignment_state=unassigned&assignment_source=none&sort=created_asc`, `assignment_state=unassigned&assignment_source=none&sort=last_transition_desc`, `assignment_source=none&sort=created_asc`, `assignment_source=none&sort=last_transition_desc`, `status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc`, `status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc`, `session_id=<id>&assignment_source=none&sort=created_asc`, `session_id=<id>&assignment_source=none&sort=last_transition_desc`, and `session_id=<id>&assignment_source=<source>`.
- Operator summary routes include `GET /v1/human/tasks/priority-summary`; it can also accept `assigned_operator_id`, `operator_id`, and `assignment_source`.
- Mixed-source queue guarantees are documented as `rechecked after extra ownerless rows are added`, `manual and auto-preselected work`, `manual and auto-preselected neighbors`, `manual and auto-preselected neighbors too`, `ownerless \`priority-summary?assignment_state=unassigned&assignment_source=none\` slice is now explicitly covered after mixed-source churn`, `unsorted ownerless \`assignment_source=none\` list, backlog, and unassigned slices are now also explicitly covered after mixed-source churn`, `unsorted session-scoped \`session_id=<id>&assignment_source=none\` slice is now also explicitly covered after mixed-source churn`, and `mixed-source session-detail ownerless slice is now also explicitly count-checked`.
- Assignment-history filters also accept `event_name`, `assigned_operator_id`, `assigned_by_actor_id`, and `assignment_source`; session detail also accepts `human_task_assignment_source`.

Principal and execution semantics:

- Request scoping uses `X-EA-Principal-ID`, `EA_DEFAULT_PRINCIPAL_ID`, and rejects mismatches as `principal_scope_mismatch`.
- Principal scope applies across `rewrite/session/artifact/receipt/run-cost, plan-compile/execute`.
- Session-bound human task create/list requests now also enforce the linked execution session principal.
- `/v1/plans/execute` supports non-`rewrite_text` artifact flows with structured `input_json` plus `context_refs`.
- Planner validation validates duplicate step keys, unknown dependency keys, and dependency cycles before queue execution starts.
- Queue runtime only merges declared dependency inputs and validates declared step outputs before completion; multi-prerequisite join steps stay parentless.
- The generic execution plane keeps the same first-class `202 awaiting_approval` and `202 awaiting_human` async contract plus first-class `202 queued` async acceptance.
- Direct execution proof records, approval projections now carry the originating task identity, queue/detail payloads now also carry the originating task identity, and inline artifact/proof rows now carry originating task identity and originating task key and deliverable type.
- `failure_strategy=retry`, `zero-backoff retries now keep draining same-session queue work inline`, `budget_policy_json.artifact_failure_strategy|artifact_max_attempts|artifact_retry_backoff_seconds`, and `dispatch_failure_strategy|max_attempts|retry_backoff_seconds` remain part of the contract.
- `/v1/rewrite/artifacts/{artifact_id}`, `/v1/rewrite/receipts/{receipt_id}`, and `/v1/rewrite/run-costs/{cost_id}` stay operator-visible lookup routes.
- `set_session_status(...)`, `/v1/policy/evaluate`, `step_kind`, `step_input_prepare`, `step_policy_evaluate`, `step_artifact_save`, `step_human_review`, `owner`, `authority_class`, `review_class`, `failure_strategy`, `timeout_budget_seconds`, `max_attempts`, and `retry_backoff_seconds` remain part of the compiled-plan/runtime surface.
- Returned review artifacts can surface `returned_payload_json.final_text`.
- `EA_RUNTIME_MODE=dev|test|prod` is supported, and `EA_RUNTIME_MODE=prod` is the durable release posture.

Tool execution:

- `ToolExecutionService` is the registry-backed execution plane and emits `tool.v1`; it self-heals missing built-in tool definitions.
- `/v1/tools/execute` covers `connector.dispatch`, `browseract.extract_account_inventory`, and `browseract.extract_account_facts`.
- Connector dispatch requires an enabled connector binding.
- Approval-backed routes return `202 Accepted` with `awaiting_approval`.
- Typed runtime policy models, `artifact_retry`, and `skill_catalog` remain first-class runtime concepts.

Additional exact contract phrases pinned by operator tests:

- account_hints_json
- resolved `skill_key`
- accepts either `task_key` or `skill_key`
- resumes execution inline
- rewrite execution now persists durable `execution_queue` rows and drains them inline for API requests before returning
- omits `operator_id`
- assignment-history` exposes task-scoped ownership transitions, now carries originating task identity too
- inline human-task assignment-history rows now carry originating task identity
- accept `priority=<level>` filters
- also accepts `assigned_operator_id`
- also accepts `operator_id`
- also accepts `assignment_source`
- queue views now also accept `assignment_source=<source>`
- ownerless `priority-summary?assignment_state=unassigned&assignment_source=none` slice is now explicitly covered after mixed-source churn
- unsorted ownerless `assignment_source=none` list, backlog, and unassigned slices are now also explicitly covered after mixed-source churn
- unsorted session-scoped `session_id=<id>&assignment_source=none` slice is now also explicitly covered after mixed-source churn
- assignment-history` also accepts `event_name`, `assigned_operator_id`, `assigned_by_actor_id`, and `assignment_source`
- current matrix covers artifacts, channel runtime, approvals, policy decisions, and task contracts
- session-bound human task create/list requests now also enforce the linked execution session principal
- step_artifact_save.state=waiting_approval
- blocked_dependency_keys=["step_human_review"]
- direct execution proof records
- queue advancement now enqueues every currently ready step from satisfied dependency edges
- policy_decision` is now recorded by the queued `step_policy_evaluate` handler after `input_prepared`
- compiled human-review steps now merge dependency outputs into the created packet input
- queued step execution now only merges declared dependency inputs and validates declared step outputs before completion
- `POST /v1/plans/compile` now exposes explicit plan-step dependencies plus declared input/output keys
- typed runtime policy models
