# Runtime Runbook

All runtime scripts that call HTTP endpoints resolve host port in this order:
1. `EA_HOST_PORT` from current shell env
2. `EA_HOST_PORT` from `.env`
3. fallback `8090`

## Flagship Gates

The runtime runbook still tracks the EA product canon and flagship gate receipts:

- `.codex-design/ea/START_HERE.md`
- `.codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md`
- `.codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json`
- `scripts/materialize_ea_flagship_release_gate.py`

Recommended operator gate commands:

- `make materialize-release-assets` to explicitly refresh generated release
  receipts
- `make verify-flagship-release-readiness` to validate already-materialized
  receipts without rewriting them
- `make verify-generated-release-artifacts-clean`
- `make runtime-hard-exit-gates`
- `make hard-exit-gates`
- `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ltd-release-gates`

The first three Make commands are non-authoritative local parity. The
standalone readiness and generated-cleanliness targets are read-only; only the
explicit materialization target can refresh generated receipts. Authenticated
repository verification must run after the pinned bootstrap through
`propertyquarry_release_make_dispatch.py`; direct Make invocations cannot be
trusted against caller-controlled GNU Make startup or parser inputs.

The runbook also expects the hard-exit and LTD verifier scripts, including `verify_ltd_critical_entries.py` and `verify_ltd_flagship_subset.py`, to stay wired into operator release practice.

## API Contract Summary

| Method | Route | Success | Error contracts |
|---|---|---|---|
| GET | `/health` | `200` | n/a |
| GET | `/health/live` | `200` | n/a |
| GET | `/health/ready` | `200` | `503 not_ready:*` |
| GET | `/version` | `200` | n/a |
| POST | `/v1/rewrite/artifact` | `200`, `202 awaiting_approval`, `202 awaiting_human`, `202 queued` | `400 text is required`, `403 principal_scope_mismatch`, `403 policy_denied:*` (including `tool_not_allowed`) |
| GET | `/v1/rewrite/artifacts/{artifact_id}` | `200` | `404 artifact_not_found`, `403 principal_scope_mismatch` (returns artifact content plus explicit `principal_id` ownership, originating `task_key`/`deliverable_type`, `mime_type`, `preview_text`, `storage_handle`, `body_ref`, and structured attachment metadata) |
| GET | `/v1/rewrite/receipts/{receipt_id}` | `200` | `404 receipt_not_found`, `403 principal_scope_mismatch` (returns proof metadata plus originating `task_key`/`deliverable_type`) |
| GET | `/v1/rewrite/run-costs/{cost_id}` | `200` | `404 run_cost_not_found`, `403 principal_scope_mismatch` (returns cost metadata plus originating `task_key`/`deliverable_type`) |
| GET | `/v1/rewrite/sessions/{session_id}` | `200` | `404 session not found`, `403 principal_scope_mismatch` (returns events + steps + queue items + receipts + artifacts + costs + human task packets, inline human task assignment history, `intent_skill_key`, self-describing artifact/proof task identity, resolved `skill_key`, human-task packet task identity, inline assignment-history task identity, `plan_compiled` event, computed reviewer routing hints, and supports `human_task_assignment_source`) |
| POST | `/v1/human/tasks` | `200` | `400 step_id_required`, `404 session_not_found`, `404 step_not_found`, `403 principal_scope_mismatch` (supports `resume_session_on_return=true` to move a linked step into `waiting_human`) |
| GET | `/v1/human/tasks/priority-summary` | `200` | validation `422`, `403 principal_scope_mismatch` (supports `status`, `role_required`, `operator_id`, `assigned_operator_id`, `assignment_state`, `assignment_source`, and `overdue_only`) |
| GET | `/v1/human/tasks` | `200` | validation `422`, `403 principal_scope_mismatch` (supports `role_required`, `priority`, `assigned_operator_id`, `assignment_state`, `assignment_source`, `overdue_only`, and `sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc`) |
| GET | `/v1/human/tasks/backlog` | `200` | validation `422` (supports `priority`, `assignment_state`, `assignment_source` and `sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc`) |
| GET | `/v1/human/tasks/unassigned` | `200` | validation `422` (supports `priority`, `assignment_source` and `sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc`) |
| GET | `/v1/human/tasks/mine` | `200` | validation `422` (supports `priority`, `assignment_source` and `sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc`) |
| POST | `/v1/human/tasks/operators` | `200` | validation `422`, `403 principal_scope_mismatch` |
| GET | `/v1/human/tasks/operators` | `200` | validation `422`, `403 principal_scope_mismatch` |
| GET | `/v1/human/tasks/operators/{operator_id}` | `200` | `404 operator_profile_not_found` |
| POST | `/v1/human/tasks/{human_task_id}/assign` | `200` | `404 human_task_not_found`, `409 human_task_not_assignable` |
| GET | `/v1/human/tasks/{human_task_id}/assignment-history` | `200` | `404 human_task_not_found`, validation `422` (supports `event_name`, `assigned_operator_id`, `assigned_by_actor_id`, `assignment_source`, and returns originating `task_key`/`deliverable_type`) |
| GET | `/v1/human/tasks/{human_task_id}` | `200` | `404 human_task_not_found` |
| POST | `/v1/human/tasks/{human_task_id}/claim` | `200` | `404 human_task_not_found`, `409 human_task_not_claimable` |
| POST | `/v1/human/tasks/{human_task_id}/return` | `200` | `404 human_task_not_found`, `409 human_task_not_returnable` |
| GET | `/v1/policy/decisions/recent` | `200` | `403 principal_scope_mismatch`, `404 session_not_found` when `session_id` is scoped to another principal |
| POST | `/v1/policy/evaluate` | `200` | validation `422`, `403 principal_scope_mismatch` |
| GET | `/v1/policy/approvals/pending` | `200` | n/a (pending rows include originating `task_key`/`deliverable_type` and stay scoped to the effective request principal) |
| GET | `/v1/policy/approvals/history` | `200` | `403 principal_scope_mismatch`, `404 session_not_found` when `session_id` is scoped to another principal (history rows include originating `task_key`/`deliverable_type`) |
| POST | `/v1/policy/approvals/{approval_id}/approve` | `200` | `404 approval_not_found`, `403 principal_scope_mismatch` (decision row includes originating `task_key`/`deliverable_type`) |
| POST | `/v1/policy/approvals/{approval_id}/deny` | `200` | `404 approval_not_found`, `403 principal_scope_mismatch` (decision row includes originating `task_key`/`deliverable_type`) |
| POST | `/v1/policy/approvals/{approval_id}/expire` | `200` | `404 approval_not_found`, `403 principal_scope_mismatch` (decision row includes originating `task_key`/`deliverable_type`) |
| POST | `/v1/observations/ingest` | `200` | validation `422` (supports source/external/dedupe/auth/raw payload pointers) |
| GET | `/v1/observations/recent` | `200` | validation `422` |
| POST | `/v1/delivery/outbox` | `200` | validation `422` (supports idempotency keys) |
| GET | `/v1/delivery/outbox/pending` | `200` | validation `422` |
| POST | `/v1/delivery/outbox/{delivery_id}/sent` | `200` | `404 delivery_not_found` |
| POST | `/v1/delivery/outbox/{delivery_id}/failed` | `200` | `404 delivery_not_found` |
| POST | `/v1/channels/telegram/ingest` | `200` | validation `422` |
| POST | `/v1/tools/registry` | `200` | validation `422` |
| GET | `/v1/tools/registry` | `200` | validation `422` |
| GET | `/v1/tools/registry/{tool_name}` | `200` | `404 tool_not_found` |
| POST | `/v1/tools/execute` | `200` | `404 tool_not_registered:*`, `409 tool_execution_failed` |
| POST | `/v1/connectors/bindings` | `200` | validation `422` |
| GET | `/v1/connectors/bindings` | `200` | validation `422` |
| POST | `/v1/connectors/bindings/{binding_id}/status` | `200` | `404 binding_not_found` |
| POST | `/v1/tasks/contracts` | `200` | validation `422` |
| GET | `/v1/tasks/contracts` | `200` | validation `422` |
| GET | `/v1/tasks/contracts/{task_key}` | `200` | `404 task_contract_not_found` |
| POST | `/v1/skills` | `200` | validation `422` |
| GET | `/v1/skills` | `200` | validation `422` (supports `provider_hint=<value>` to filter the catalog by LTD-backed provider hints such as `BrowserAct` or `1min.AI`) |
| GET | `/v1/skills/{skill_key}` | `200` | `404 skill_not_found` |
| POST | `/v1/plans/compile` | `200` | validation `422`, `403 principal_scope_mismatch` (accepts either `task_key` or `skill_key`) |
| POST | `/v1/plans/execute` | `200`, `202 awaiting_approval`, `202 awaiting_human`, `202 queued` | validation `422`, `403 principal_scope_mismatch`, `403 policy_denied:*` (accepts either `task_key` or `skill_key`) |
| GET | `/v1/evidence/objects` | `200` | validation `422`, `403 principal_scope_mismatch` (supports `artifact_id`, `session_id`, and `evidence_ref`; returns stable `citation_handle` values for evidence-pack artifacts) |
| GET | `/v1/evidence/objects/{evidence_id}` | `200` | `404 evidence_object_not_found` |
| POST | `/v1/evidence/merge` | `200` | `400 evidence_ids_required`, `404 evidence_object_not_found:*`, `403 principal_scope_mismatch` |
| POST | `/v1/memory/candidates` | `200` | validation `422` |
| GET | `/v1/memory/candidates` | `200` | validation `422` |
| POST | `/v1/memory/candidates/{candidate_id}/promote` | `200` | `404 memory_candidate_not_found` |
| POST | `/v1/memory/candidates/{candidate_id}/reject` | `200` | `404 memory_candidate_not_found` |
| GET | `/v1/memory/items` | `200` | validation `422` |
| GET | `/v1/memory/items/{item_id}` | `200` | `404 memory_item_not_found` |
| POST | `/v1/memory/entities` | `200` | validation `422` |
| GET | `/v1/memory/entities` | `200` | validation `422` |
| GET | `/v1/memory/entities/{entity_id}` | `200` | `404 entity_not_found` |
| POST | `/v1/memory/relationships` | `200` | validation `422` |
| GET | `/v1/memory/relationships` | `200` | validation `422` |
| GET | `/v1/memory/relationships/{relationship_id}` | `200` | `404 relationship_not_found` |
| POST | `/v1/memory/commitments` | `200` | validation `422` |
| GET | `/v1/memory/commitments` | `200` | validation `422` |
| GET | `/v1/memory/commitments/{commitment_id}` | `200` | `404 commitment_not_found` |
| POST | `/v1/memory/authority-bindings` | `200` | validation `422` |
| GET | `/v1/memory/authority-bindings` | `200` | validation `422` |
| GET | `/v1/memory/authority-bindings/{binding_id}` | `200` | `404 authority_binding_not_found` |
| POST | `/v1/memory/delivery-preferences` | `200` | validation `422` |
| GET | `/v1/memory/delivery-preferences` | `200` | validation `422` |
| GET | `/v1/memory/delivery-preferences/{preference_id}` | `200` | `404 delivery_preference_not_found` |
| POST | `/v1/memory/follow-ups` | `200` | validation `422` |
| GET | `/v1/memory/follow-ups` | `200` | validation `422` |
| GET | `/v1/memory/follow-ups/{follow_up_id}` | `200` | `404 follow_up_not_found` |
| POST | `/v1/memory/deadline-windows` | `200` | validation `422` |
| GET | `/v1/memory/deadline-windows` | `200` | validation `422` |
| GET | `/v1/memory/deadline-windows/{window_id}` | `200` | `404 deadline_window_not_found` |
| POST | `/v1/memory/stakeholders` | `200` | validation `422` |
| GET | `/v1/memory/stakeholders` | `200` | validation `422` |
| GET | `/v1/memory/stakeholders/{stakeholder_id}` | `200` | `404 stakeholder_not_found` |
| POST | `/v1/memory/decision-windows` | `200` | validation `422` |
| GET | `/v1/memory/decision-windows` | `200` | validation `422` |
| GET | `/v1/memory/decision-windows/{decision_window_id}` | `200` | `404 decision_window_not_found` |
| POST | `/v1/memory/communication-policies` | `200` | validation `422` |
| GET | `/v1/memory/communication-policies` | `200` | validation `422` |
| GET | `/v1/memory/communication-policies/{policy_id}` | `200` | `404 communication_policy_not_found` |
| POST | `/v1/memory/follow-up-rules` | `200` | validation `422` |
| GET | `/v1/memory/follow-up-rules` | `200` | validation `422` |
| GET | `/v1/memory/follow-up-rules/{rule_id}` | `200` | `404 follow_up_rule_not_found` |
| POST | `/v1/memory/interruption-budgets` | `200` | validation `422` |
| GET | `/v1/memory/interruption-budgets` | `200` | validation `422` |
| GET | `/v1/memory/interruption-budgets/{budget_id}` | `200` | `404 interruption_budget_not_found` |
| POST | `/v1/memory/context-pack` | `200` | validation `422`, `403 principal_scope_mismatch` |

Error envelope for failures:
- `{ "error": { "code": "...", "message": "...", "details": ..., "correlation_id": "..." } }`

Auth:
- Set `EA_API_TOKEN=<token>` to require auth for all non-health routes.
- Use `Authorization: Bearer <token>` or `X-API-Token: <token>`.
- Use `X-EA-Principal-ID: <principal>` for principal-scoped rewrite/session/artifact/receipt/run-cost, plan-compile/execute, connector, human-task, and memory routes; if omitted, `EA_DEFAULT_PRINCIPAL_ID` (default `local-user`) is used.
- On those routes, body/query `principal_id` remains a compatibility field only and mismatches fail with `403 principal_scope_mismatch`.
- `GET /v1/models` returns both the public EA aliases and the currently configured upstream model IDs, so Codex can target concrete provider models when needed.
- `GET /v1/responses/_provider_health` and `GET /v1/codex/profiles` expose account-name-only provider attribution plus 1min owner metadata matched by hash or stable slot/account identifiers, latest explicit probe evidence, 1min.AI depletion, observed per-slot consumption (`observed_consumed_credits`, `observed_success_count`), rolling burn-rate, and deleted-key telemetry (`remaining_credits`, `required_credits`, `estimated_remaining_credits_total`, `remaining_percent_of_max`, `estimated_burn_credits_per_hour`, `estimated_hours_remaining_at_current_pace`) without leaking raw API keys.
- `POST /v1/providers/onemin/probe-all` sends one live low-volume request to each selected 1min slot, records `last_probe_result`, and updates deleted/depleted/rate-limited evidence immediately instead of waiting for incidental runtime traffic.
- `python3 scripts/sync_onemin_owner_ledger.py --write` re-hashes the current `ONEMIN_AI_API_KEY*` values plus any `ONEMIN_DIRECT_API_KEYS_JSON(_FILE)` manifest entries into `config/onemin_slot_owners.json` and carries owner labels/emails forward by slot/account when the runtime key set rotates.
- The template-backed 1min BrowserAct refresh lane now accepts a generic rotating proxy through `EA_UI_BROWSER_PROXY_SERVER`, `EA_UI_BROWSER_PROXY_USERNAME`, `EA_UI_BROWSER_PROXY_PASSWORD`, and `EA_UI_BROWSER_PROXY_BYPASS`; use `ONEMIN_BROWSERACT_MAX_ACCOUNTS_PER_REFRESH` plus `EA_ONEMIN_BILLING_REFRESH_MIN_INTERVAL_SECONDS` when you want one operator-triggered refresh cycle to sweep the full slot set without the old cadence throttle.
- FastestVPN can back that lane through [docker-compose.fastestvpn.yml](/docker/property/docker-compose.fastestvpn.yml): place FastestVPN OpenVPN profiles under [vpn/fastestvpn/README.md](/docker/property/vpn/fastestvpn/README.md), or fetch them with [bootstrap_fastestvpn_configs.sh](/docker/property/scripts/bootstrap_fastestvpn_configs.sh), then deploy with `EA_ENABLE_FASTESTVPN=1 bash scripts/deploy.sh --compose-override docker-compose.fastestvpn.yml`. If you use `scripts/deploy.sh`, keep that overlay explicit with `EA_ENABLE_FASTESTVPN=1`. Use [rotate_fastestvpn_proxy.sh](/docker/property/scripts/rotate_fastestvpn_proxy.sh) to recreate the proxy on a fresh FastestVPN exit profile before a full 1min BrowserAct refresh.
- `EA_RESPONSES_ONEMIN_INCLUDED_CREDITS_PER_KEY`, `EA_RESPONSES_ONEMIN_BONUS_CREDITS_PER_KEY`, `EA_RESPONSES_ONEMIN_DELETED_KEY_QUARANTINE_SECONDS`, `EA_RESPONSES_ONEMIN_OWNER_LEDGER_PATH`, `EA_RESPONSES_ONEMIN_PROBE_MODEL`, and `EA_RESPONSES_ONEMIN_PROBE_TIMEOUT_SECONDS` tune those credit, owner-ledger, and explicit-probe diagnostics.
- `EA_RESPONSES_MAGICX_HEALTH_CHECK`, `EA_RESPONSES_MAGICX_HEALTH_INTERVAL_SECONDS`, and `EA_RESPONSES_MAGICX_HEALTH_TIMEOUT_SECONDS` enable and tune the live Magicx fallback probe so provider health reflects a real upstream readiness check.
- After a BrowserAct inventory refresh, `bash scripts/refresh_ltds_from_inventory.sh --input <inventory.json> --write` can rewrite the `## Discovery Tracking` section in [LTDs.md](/docker/property/LTDs.md) from the structured inventory artifact/output instead of editing the markdown table by hand.
- When the local API is already running, `bash scripts/refresh_ltds_via_api.sh --binding-id <browseract-binding-id> --service-name BrowserAct --service-name Teable --write` can execute the BrowserAct-backed `ltd_inventory_refresh` skill via `/v1/plans/execute`, save the raw inventory payload if requested, and update [LTDs.md](/docker/property/LTDs.md) in one pass.
- `python3 scripts/verify_ltd_critical_entries.py` is the hard verifier for the currently depended-on LTD lanes. It fails closed if [LTDs.md](/docker/property/LTDs.md) or the live env drift away from the required `1min.AI`, `Prompt Architects`, BrowserAct, and Teable facts.
- `python3 scripts/verify_ltd_flagship_subset.py` is the broader release gate for the current flagship verified subset. It intentionally covers a named subset instead of pretending all `manual_seeded` or `missing` LTD rows are already proven.
- `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ltd-release-gates` runs both authenticated LTD release verifiers together.
- `make verify-ltd-critical-entries` and `make verify-ltd-flagship-subset` are the corresponding operator entrypoints and are included by `make hard-exit-gates`.

Runtime mode:
- Set `EA_RUNTIME_MODE=prod` for durable environments; the app will fail fast instead of falling back from `EA_STORAGE_BACKEND=auto` or `memory` to in-process storage.
- For the standalone durable PropertyQuarry profile, set `POSTGRES_PASSWORD` and run `docker compose -f docker-compose.property.yml up -d --build`.
- Use `PROPERTYQUARRY_USE_LEGACY_STACK=1 bash scripts/deploy.sh` only for migration work that intentionally needs inherited EA services.

Policy notes:
- Rewrite policy denies empty input, oversized input, and disallowed tool usage.
- Rewrite policy requires approval for explicit approval classes, long inputs, and high-risk/high-budget or external-send actions.
- `POST /v1/policy/evaluate` provides a direct HTTP path for previewing external-send approval requirements, including the step/authority/review metadata that now drives the queued policy step, and its body `principal_id` is only a compatibility filter against the effective request principal.
- Approving a paused rewrite resumes execution immediately on the current scaffold, so the session should move from `awaiting_approval` to `completed` with an artifact, receipt, and run-cost row.
- Approval-required rewrites now return `202` with `session_id`, `approval_id`, `status=awaiting_approval`, and `next_action=poll_or_subscribe` instead of a `409` error contract.
- Allowed and approved rewrites now pass through durable `execution_queue` rows first; the current API path drains that queue inline, while non-API runner roles can drain it as workers.
- The current rewrite scaffold now executes as three explicit queued steps: `step_input_prepare`, `step_policy_evaluate`, and `step_artifact_save`.
- `policy_decision` is now emitted from the queued `step_policy_evaluate` handler after `input_prepared`, so the ledger order matches runtime execution before approval or block transitions are recorded.
- `POST /v1/plans/compile` exposes `depends_on`, `input_keys`, and `output_keys`, and queue advancement now enqueues every currently ready step from satisfied dependency edges instead of parent-linked step order while paused sessions stop further leasing.
- Planner/orchestrator startup now validates duplicate step keys, unknown dependency keys, and dependency cycles before any session rows are started, so invalid plan graphs fail before the queue runtime can lease them.
- The queue runtime now only merges declared dependency inputs and fails missing declared outputs before a step can complete, so `input_keys` / `output_keys` contracts are enforced instead of remaining descriptive metadata only.
- Session step `parent_step_id` now mirrors only real single-dependency edges; multi-prerequisite join steps stay parentless and rely on `dependency_keys` plus `dependency_states` for graph truth.
- `POST /v1/plans/compile` and the queued session step input payloads now also expose `owner`, `authority_class`, `review_class`, `failure_strategy`, `timeout_budget_seconds`, `max_attempts`, and `retry_backoff_seconds`, so operator tooling can see who owns each step and what runtime posture it expects before deeper graph execution lands.
- `POST /v1/plans/execute` now reuses that same compiled task-contract runtime for non-`rewrite_text` artifact flows, accepts structured `input_json` plus `context_refs` in addition to the legacy `text` convenience field, injects synthesized `context_pack` payloads from principal-scoped memory reasoning, and lets stakeholder briefings and similar executive contracts run through the queue-backed graph without a rewrite-only entrypoint.
- `POST /v1/memory/context-pack` exposes the same memory reasoning envelope directly for operators, including promoted-memory signals, conflict rows, commitment-risk rows, and unresolved refs for the request principal.
- `POST /v1/plans/execute` now also returns the same first-class `202 awaiting_approval` and `202 awaiting_human` workflow contract as rewrite execution, and those generic task sessions resume through the shared approval and human-task endpoints.
- Rewrite creation and `POST /v1/plans/execute` now also return `202 queued` with `next_action=poll_or_subscribe` when a retryable step reschedules itself into the future (`retry_backoff_seconds>0`), so delayed queue retries stay async instead of surfacing `queued task did not execute`.
- Those paused generic task sessions keep graph-aware dependency metadata in `GET /v1/rewrite/sessions/{session_id}` too: approval-backed runs hold `step_artifact_save` in `waiting_approval` with satisfied dependencies, while human-review-backed runs leave downstream save steps queued behind `blocked_dependency_keys=["step_human_review"]` until the operator returns the packet.
- Tool and system step failures can now also honor `failure_strategy=retry`: the queue runtime reuses the same queue row, keeps `attempt_count` monotonic, records `step_retry_scheduled`, and delays the next lease by `retry_backoff_seconds` until `max_attempts` is exhausted.
- If that retried queue row is immediately eligible (`retry_backoff_seconds=0`), the inline create/approve/return paths now keep draining the same session until it reaches completion, pause, or terminal failure instead of surfacing `queued task did not execute` for a retry that was already ready to run.
- The execution ledger now uses an explicit `set_session_status(...)` transition API for nonterminal states (`queued`, `running`, `blocked`, `awaiting_approval`, `awaiting_human`, `failed`), so retry and pause/resume flows no longer look like session completion in code or metrics.
- Task-contract metadata can now override the built-in artifact and dispatch retry posture too: `budget_policy_json.artifact_failure_strategy|artifact_max_attempts|artifact_retry_backoff_seconds` tune `step_artifact_save`, while `dispatch_failure_strategy|dispatch_max_attempts|dispatch_retry_backoff_seconds` tune `step_connector_dispatch`.
- That same task-contract and skill metadata is now normalized into typed runtime policy models (`artifact_retry`, `dispatch_retry`, `human_review`, `memory_candidate`, `artifact_output`, and `skill_catalog`) before planner/runtime execution, so the branch-baseline workflow logic reads one canonical policy shape instead of unpacking ad-hoc `budget_policy_json` keys at each call site.
- Approval and human-task queue/detail payloads now stay self-describing for non-rewrite async work by returning the originating `task_key` and `deliverable_type` before the workflow completes.
- Rewrite creation and session/artifact/receipt/run-cost fetches now enforce the same request principal contract as the rest of the scoped surface, so foreign-principal fetch attempts fail with `403 principal_scope_mismatch` instead of exposing another execution thread.
- Session-bound `POST /v1/human/tasks` and `GET /v1/human/tasks?session_id=...` now enforce that the request principal matches the linked execution session principal too, so one principal cannot stitch packets onto or enumerate another principal's session thread via `session_id`.
- Task-contract metadata can now add a projected `step_human_review` branch by setting `budget_policy_json.human_review_role`, `human_review_priority`, `human_review_sla_minutes`, `human_review_auto_assign_if_unique`, `human_review_desired_output_json`, `human_review_authority_required`, `human_review_why_human`, and `human_review_quality_rubric_json`; the rewrite runtime auto-creates the linked human task packet with those routing and review-contract fields when that step executes, auto-assigns a unique exact reviewer when the flag is enabled, and a returned `final_text` payload now overrides the downstream artifact-save input.
- Task contracts can now also switch the compiled workflow skeleton with `budget_policy_json.workflow_template`; the built-in `artifact_then_dispatch` template emits `step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch`, persists the artifact before approval, and resumes into `connector.dispatch` only after the approval-backed delivery gate is approved.
- Task contracts can now also use the generic `workflow_template=tool_then_artifact` macro plus `budget_policy_json.pre_artifact_tool_name=<tool>` to compile a reusable pre-artifact tool branch, and the supported BrowserAct slices prove both `browseract.extract_account_facts` and `browseract.extract_account_inventory` can run through `step_input_prepare -> ... -> step_artifact_save` without another one-off planner path.
- Task contracts can now also switch to `workflow_template=browseract_extract_then_artifact`, compiling `step_input_prepare -> step_browseract_extract -> step_artifact_save` so BrowserAct-backed account discovery can extract tier/email/status facts and persist them as a structured artifact in one queue-backed pass.
- `/v1/skills` now exposes a first-class executive skill catalog on top of those task contracts, preserving product metadata such as memory reads/writes, authority/tool/human/provider policy, evaluation cases, and workflow-template selection in the existing task-contract store; [SKILLS.md](/docker/property/SKILLS.md) tracks the current catalog and now includes the Gemini Vortex-backed `chummer6_public_writer` for audience-safe guide copy, the Gemini Vortex-backed `chummer6_visual_director` with style-epoch and scene-ledger memory, the BrowserAct-backed `browseract_bootstrap_manager` for prompt-tool and page-extract templates, and the BrowserAct-backed `ltd_inventory_refresh` inventory skill alongside `meeting_prep`.
- `/v1/skills?provider_hint=<value>` now filters that catalog against nested `provider_hints_json`, so operator tooling can answer questions like “which skills rely on BrowserAct or 1min.AI?” without maintaining a second provider map outside the task-contract store.
- The runtime now also keeps typed read projections for task-contract policy (`TaskContractPolicyRecord`), product-facing skill metadata (`SkillCatalogRecord`), and provider posture (`ProviderBindingState`) so planner/catalog/provider code reads structured records instead of unpacking raw JSON blobs at every boundary.
- `/v1/plans/compile` and `/v1/plans/execute` now also project the resolved `skill_key`, so operator tooling can render the product-facing executive capability name without reverse-mapping every `task_key` client-side.
- `POST /v1/plans/compile` and `POST /v1/plans/execute` now also accept `skill_key` directly, so product-facing clients can compile or execute a skill without first resolving its backing `task_key`.
- `/v1/rewrite/sessions/{session_id}` plus direct artifact/receipt/run-cost reads now also project that same `skill_key`, so queue/runtime inspection stays aligned with the product-facing skill catalog once work has been executed.
- Chummer6 guide text generation is now intentionally EA-only on the planner side. If the Gemini Vortex lane is missing, the worker hard-fails instead of drifting into Codex fallback.
- `python3 scripts/generate_browseract_content_templates.py` writes ready-to-edit BrowserAct packet and workflow JSON for the Economist, The Atlantic, NYTimes, ApproveThis, and MetaSurvey reader templates into `/mnt/pcloud/EA/browseract_templates`.
- Task contracts can now also use `workflow_template=artifact_then_packs` plus `budget_policy_json.post_artifact_packs=[...]` to compose shared post-artifact planner branches (currently `dispatch` and `memory_candidate`) without adding another one-off named workflow template for every combination.
- The built-in `artifact_then_memory_candidate` workflow template now emits `step_input_prepare -> step_policy_evaluate -> step_artifact_save -> step_memory_candidate_stage`, persists the artifact, then stages a pending principal-scoped memory candidate through the same queue runtime so task contracts can write reviewable memory without adding a second API-side post-process.
- Task contracts can now also set `budget_policy_json.artifact_output_template=evidence_pack`, so `step_input_prepare` emits structured `claims`, `evidence_refs`, `open_questions`, and `confidence` fields that persist through `step_artifact_save` as a first-class evidence envelope and carry forward into downstream memory-candidate staging instead of only plain text.
- Those `evidence_pack` artifact saves now also materialize first-class evidence rows behind `/v1/evidence/objects`, carry stable `citation_handle` values plus `evidence_object_id` step-output metadata, and `POST /v1/evidence/merge` can recombine selected rows into a reusable evidence pack without reparsing the original artifact JSON.
- The built-in `artifact_then_dispatch_then_memory_candidate` workflow template now emits `step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage`, so approval-backed external sends can complete first and only then stage a pending memory candidate with delivery context from the finished workflow.
- That same `artifact_then_dispatch_then_memory_candidate` template can also combine with `budget_policy_json.human_review_role`, compiling `step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage` so sensitive send workflows pause first for human review and still stage post-dispatch memory only after approval-backed delivery completes.
- That same `artifact_then_dispatch` template can also combine with `budget_policy_json.human_review_role`, compiling `step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch` so sensitive send flows pause first for human review, then later for approval-backed dispatch.
- That review-then-dispatch branch now also preserves compiled `dispatch_failure_strategy|dispatch_max_attempts|dispatch_retry_backoff_seconds` metadata on `step_connector_dispatch`, so an approval-resumed send step can requeue itself into the future and keep the session `queued` instead of surfacing a runtime error after human review has already cleared; the HTTP smoke path now proves that queued post-approval send contract as well.
- Unknown `budget_policy_json.workflow_template` values now fail fast with `422 unknown_workflow_template:<value>` during plan compilation and task execution, so contract mistakes stop at the boundary instead of silently falling back to the rewrite-shaped skeleton.
- That queued human-review step now also merges dependency outputs into the packet input, so `normalized_text`, `source_text`, and `text_length` follow the satisfied dependency graph instead of depending only on one parent link.
- Tool-call steps now flow through a registry-backed `ToolExecutionService`; the built-in `artifact_repository` handler emits normalized `tool.v1` receipt metadata, `tool_execution_completed` events, and self-heals its registry definition if the runtime starts with an empty built-in tool registry.
- `POST /v1/tools/execute` now exposes the same execution plane directly for built-in handlers; `connector.dispatch` self-heals its built-in registry definition the same way, queues a delivery outbox row, and returns normalized `tool.v1` receipt metadata.
- `connector.dispatch` execution now requires a real enabled connector binding in the caller's principal scope; foreign-principal or missing bindings fail before any outbox row is queued.
- `POST /v1/tools/execute` also exposes `browseract.extract_account_facts` and `browseract.extract_account_inventory`, which self-heal their built-in registry definitions, resolve BrowserAct-backed single-service or multi-service account facts from a principal-scoped connector binding, and can optionally call a live BrowserAct `run_url` with caller-supplied `instructions` / `account_hints_json` while projecting those live-discovery hints back through the structured output envelope for auditability.
- Human review/work packets can now be attached to a session with `POST /v1/human/tasks`, claimed by an operator, and returned with structured payload/provenance while emitting `human_task_created`, `human_task_claimed`, and `human_task_returned` ledger events.
- If `resume_session_on_return=true` is set on human task creation, the linked step reopens into `waiting_human`, the session becomes `awaiting_human`, and returning the packet resumes the step back to `completed`.
- Operator queue views can filter pending human tasks by `role_required`, `assigned_operator_id`, and `overdue_only=true` so reviewers can work from targeted SLA backlogs.
- Operator queue views can also pass `sort=created_asc` so the oldest-created backlog stays pinned first for manual FIFO triage, `sort=priority_desc_created_asc` so urgent and high work stays ahead of normal packets while each priority band remains FIFO, `sort=last_transition_desc` so the most recently reassigned, claimed, or returned ownership change surfaces first, `sort=sla_due_at_asc` so the earliest pending SLA surfaces first, or `sort=sla_due_at_asc_last_transition_desc` so same-SLA work stays ordered by the freshest ownership churn in general list and backlog views.
- Operator queue views can also pass `priority=urgent|high|normal|low` to isolate one priority band before applying any of the queue sort modes above.
- Operator queue views can also pass comma-separated filters like `priority=urgent,high` to pull a combined action queue without client-side set merging.
- Operator queue views can also pass `assignment_source=manual|recommended|auto_preselected` to open just one pending ownership slice instead of filtering manual or planner-preselected rows client-side after fetch.
- Manual and planner auto-preselected `priority-summary?assignment_source=...` slices are now also rechecked after extra ownerless rows are added, so mixed-source churn does not leak ownerless work into non-ownerless summary counts.
- `GET /v1/human/tasks/unassigned?assignment_source=none` isolates ownerless pending packets directly, so UIs do not need empty-string query conventions to open the unassigned-only backlog.
- `GET /v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none` exposes that same ownerless slice through the general pending backlog endpoint, so direct backlog and unassigned-only queues share one contract.
- `GET /v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=created_asc` keeps that ownerless slice in explicit FIFO order, so reviewer triage can pull oldest untouched ownerless work first.
- `GET /v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=last_transition_desc` now also proves the untouched ownerless slice sorts newest-first under freshest-ownership ordering, even when every row only has the initial `human_task_created` transition.
- `GET /v1/human/tasks/unassigned?assignment_source=none&sort=created_asc` now mirrors that FIFO ordering on the direct unassigned queue, so the dedicated ownerless view stays aligned with the backlog slice.
- `GET /v1/human/tasks/unassigned?assignment_source=none&sort=last_transition_desc` now mirrors that newest-first behavior on the direct unassigned queue, so the dedicated ownerless view and the general backlog stay aligned.
- `GET /v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc` now mirrors that FIFO ordering on the general pending list too, keeping list, backlog, and unassigned ownerless triage on the same oldest-first contract.
- `GET /v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc` now mirrors that newest-first ordering on the general pending list too, so list, backlog, and unassigned ownerless triage all share the same freshest-transition contract.
- Those ownerless backlog, unassigned, and general pending `assignment_source=none` sorted queue slices are now also covered with manual and auto-preselected neighbors present, so mixed-source queues keep non-ownerless rows out of both `sort=created_asc` and `sort=last_transition_desc`.
- `GET /v1/human/tasks?session_id=<id>&assignment_source=none&sort=created_asc` now mirrors that FIFO ordering on the session-scoped ownerless slice too, so per-session triage stays aligned with the global queue views.
- `GET /v1/human/tasks?session_id=<id>&assignment_source=none&sort=last_transition_desc` now mirrors that newest-first ordering on the session-scoped ownerless slice too, so per-session triage shares the same freshest-transition contract as the global queue views.
- Those same session-scoped `assignment_source=none` sorted queue slices are now also covered with manual and auto-preselected neighbors present, so mixed-source sessions keep non-ownerless rows out of both `sort=created_asc` and `sort=last_transition_desc`.
- `GET /v1/rewrite/sessions/{session_id}?human_task_assignment_source=none` now also has explicit multi-task ownerless projection coverage, so the filtered `human_tasks` array and inline `human_task_assignment_history` both stay oldest-first for stable one-fetch session audit views.
- That filtered `human_task_assignment_source=none` session-detail projection is now also covered with manual and auto-preselected neighbors present, so mixed-source sessions keep current `human_tasks` ownerless-only while inline empty-source creation history stays oldest-first for one-fetch audit.
- That same mixed-source session-detail ownerless projection is now also count-checked, so the current `human_tasks` block stays at two ownerless rows while inline empty-source history still exposes a longer audit trail.
- `GET /v1/human/tasks?session_id=<id>&assignment_source=<source>` applies that same ownership-source filtering inside one session-scoped queue fetch, so operators can inspect only the manual or planner-preselected packets linked to a single execution thread.
- `GET /v1/human/tasks/priority-summary` returns queue counts per priority band (`urgent`, `high`, `normal`, `low`) plus `total` and `highest_priority`, so operators can choose the right priority filter before opening a backlog view.
- `GET /v1/human/tasks/priority-summary?assigned_operator_id=<id>` scopes those priority-band counts down to one assigned reviewer queue, so “mine” views can expose their own urgent/high load without fetching the full packet list first.
- `GET /v1/human/tasks/priority-summary?operator_id=<id>` scopes those priority-band counts down to only the exact backlog packets that match one operator profile’s role, rubric-derived skill tags, and trust tier, so pre-claim reviewer routing can size the candidate queue before anyone claims work.
- `GET /v1/human/tasks/priority-summary?assignment_source=manual|recommended|auto_preselected` scopes those priority-band counts down to one pending ownership source, so dashboards can distinguish planner-preselected packets from route-level recommended or manually assigned work before claim.
- `GET /v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&assignment_source=none` returns only ownerless pending counts, so the unassigned backlog can be sized without special empty-string filters.
- That same ownerless `priority-summary?status=pending&assignment_state=unassigned&assignment_source=none` slice is now also covered after mixed-source churn, so totals and low-priority counts stay ownerless-only even while manual and auto-preselected work coexists.
- The unsorted ownerless `assignment_source=none` list, backlog, and unassigned slices are now also covered after mixed-source churn, so multi-row queue fetches still contain only ownerless packets even while manual and auto-preselected work coexists.
- The unsorted session-scoped `session_id=<id>&assignment_source=none` slice is now also covered after mixed-source churn, so multi-row per-session queue fetches still contain only ownerless packets even while manual and auto-preselected work coexists.
- Both SLA-oriented queue sorts also fall back to oldest-created ordering for tasks without `sla_due_at`, so unscheduled pending work does not get reshuffled by ownership churn.
- `POST /v1/human/tasks/operators` now persists reviewer specialization profiles (`roles`, `skill_tags`, `trust_tier`), and `GET /v1/human/tasks/backlog?operator_id=<id>` filters pending work against that metadata plus human-task review contracts.
- Human task/session payloads now compute `routing_hints_json` from active operator profiles, rubric-derived skill tags, and trust-tier requirements, including `suggested_operator_ids`, `recommended_operator_id`, and `auto_assign_operator_id` when a single exact reviewer match is available.
- `GET /v1/human/tasks/backlog` is the direct pending-queue view, while `GET /v1/human/tasks/mine?operator_id=<id>` exposes the current operator assignment queue without rebuilding filters manually.
- `POST /v1/human/tasks/{human_task_id}/assign` sets `assigned_operator_id` while the task remains `pending`, emits `human_task_assigned`, and lets operators be pre-assigned before `claim` moves the packet into active work; if the caller omits `operator_id`, the route now uses `routing_hints_json.auto_assign_operator_id` when a single exact reviewer match is available.
- `GET /v1/human/tasks/{human_task_id}/assignment-history` filters the linked execution ledger down to `human_task_created`, `human_task_assigned`, `human_task_claimed`, and `human_task_returned` transitions so reassignment provenance is queryable without scanning the entire session event list, and those direct history rows now also carry originating `task_key`/`deliverable_type`.
- `GET /v1/human/tasks/{human_task_id}/assignment-history` also accepts `event_name`, `assigned_operator_id`, `assigned_by_actor_id`, and `assignment_source` to narrow that transition chain to specific reassignment, claim, return, recommended/manual/auto-preselected ownership views, or ownerless creation-only slices for operator tooling.
- `GET /v1/rewrite/sessions/{session_id}` now also includes `human_task_assignment_history`, mirroring those task-scoped ownership transitions inline with the broader session detail payload for one-fetch operator views, and inline assignment-history rows now also carry originating `task_key`/`deliverable_type`.
- `GET /v1/rewrite/sessions/{session_id}` inline `human_tasks` rows now also carry originating `task_key`/`deliverable_type`, so the paused packet detail itself stays self-describing for non-rewrite task contracts.
- Rewrite artifact reads, inline session artifact rows, and generic task execution responses now also expose explicit `principal_id` ownership alongside `mime_type`, `preview_text`, `storage_handle`, durable `body_ref`, and `structured_output_json` / `attachments_json`, so artifact consumers can start treating them as real metadata-plus-handle envelopes without dropping the current inline `content` contract.
- `GET /v1/rewrite/sessions/{session_id}?human_task_assignment_source=<source>` narrows the session-linked `human_tasks` array and inline `human_task_assignment_history` to one ownership source; `human_task_assignment_source=none` now exposes current ownerless packets plus empty-source creation history without special client-side handling.
- `GET /v1/human/tasks/unassigned` and `assignment_state=assigned|unassigned` make pre-assigned pending work distinct from ownerless pending work in the backlog view.
- Human task payloads now expose `assignment_state` directly (`unassigned`, `assigned`, `claimed`, `returned`) so session projections and operator queues do not have to infer assignment from `status` plus `assigned_operator_id`.
- Human task payloads now also persist `assignment_source` so operators can tell whether ownership came from a manual choice, a route-level recommended assignment, or planner-time auto-preselection even after later claim/return transitions.
- Human task payloads now also persist `assigned_at` and `assigned_by_actor_id` so current reviewer ownership is timestamped and the last assigning actor remains visible across assignment, claim, return, and planner auto-preselection.
- Human task list/detail/session rows now also expose compact `last_transition_event_name`, `last_transition_at`, `last_transition_assignment_state`, `last_transition_operator_id`, `last_transition_assignment_source`, and `last_transition_by_actor_id` fields so operators can see the most recent ownership event (`created`, `assigned`, `claimed`, or `returned`) plus its actor/source metadata without fetching the full assignment-history chain first.

## Operator Script Help Index

## Runtime Contract Appendix

- `ea_pgdata` is mounted at `/var/lib/postgresql/data`; durable volume accounting is not RAM.
- `/v1/responses/_provider_health`, `/v1/codex/profiles`, and `/v1/providers/onemin/probe-all` expose `estimated_remaining_credits_total`, `estimated_hours_remaining_at_current_pace`, `observed_consumed_credits`, and `EA_RESPONSES_MAGICX_HEALTH_CHECK`.
- If you use `scripts/deploy.sh`, keep that overlay explicit with `EA_ENABLE_FASTESTVPN=1`.
- `SUPPORT_INCLUDE_DB_VOLUME=0 bash scripts/support_bundle.sh`, `EA_RETENTION_PROFILE=aggressive bash scripts/db_retention.sh`, `EA_RETENTION_TABLES=execution_events,delivery_outbox bash scripts/db_retention.sh`, `EA_RETENTION_SKIP_TABLES=observation_events,policy_decisions bash scripts/db_retention.sh`, `EA_DB_SIZE_SCHEMA=public bash scripts/db_size.sh`, `EA_DB_SIZE_SORT_KEY=index bash scripts/db_size.sh`, `EA_DB_SIZE_TABLE_PREFIX=execution_ bash scripts/db_size.sh`, `EA_DB_SIZE_MIN_MB=25 bash scripts/db_size.sh`, and `SUPPORT_DB_SIZE_LIMIT=15 bash scripts/support_bundle.sh` remain the operator examples.
- Alias/service variables include `PROPERTYQUARRY_API_SERVICE`, `PROPERTYQUARRY_DB_SERVICE`, `/docker/property/docker-compose.fastestvpn.yml`, `/docker/property/vpn/fastestvpn/README.md`, `/docker/property/scripts/bootstrap_fastestvpn_configs.sh`, `/docker/property/scripts/rotate_fastestvpn_proxy.sh`, `/docker/property/LTDs.md`, and `/docker/property/SKILLS.md`.
- Workflow templates include `workflow_template`, `artifact_then_dispatch`, `artifact_then_packs`, `post_artifact_packs`, `artifact_then_memory_candidate`, `browseract_extract_then_artifact`, `workflow_template=tool_then_artifact`, and `artifact_then_dispatch_then_memory_candidate`.
- Queue shapes include `step_input_prepare -> step_policy_evaluate -> step_artifact_save -> step_memory_candidate_stage`, `step_input_prepare -> step_browseract_extract -> step_artifact_save`, `step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage`, and `step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage`.
- `/v1/skills`, `ltd_inventory_refresh`, `chummer6_visual_director`, `chummer6_public_writer`, `browseract_bootstrap_manager`, provider policy, `provider_hint=<value>`, `resolved \`skill_key\``, `intent_skill_key`, and refresh helpers `refresh_ltds_from_inventory.sh` and `refresh_ltds_via_api.sh` remain part of runtime operations.
- Evidence and memory lanes cover `artifact_output_template=evidence_pack`, memory-candidate staging, `/v1/evidence/objects`, `/v1/memory/candidates`, `/v1/memory/stakeholders`, `/v1/memory/interruption-budgets`, and `/v1/memory/context-pack`, including promoted-memory signals, conflict rows, commitment-risk rows, and unresolved refs.
- Principal guardrails use `X-EA-Principal-ID`, `EA_DEFAULT_PRINCIPAL_ID`, and `403 principal_scope_mismatch`; the scoped surfaces include `/v1/rewrite/sessions/{session_id}`, `/v1/plans/compile`, and `/v1/plans/execute`.
- `/v1/plans/execute` remains the generic task runtime for stakeholder briefings and other non-rewrite flows, with structured `input_json` plus `context_refs`, same first-class `202 awaiting_approval` and `202 awaiting_human` workflow contract, and accepts either `task_key` or `skill_key`.
- Planner/runtime rules include duplicate step keys, unknown dependency keys, and dependency cycles before any session rows are started; only merges declared dependency inputs and fails missing declared outputs before a step can complete; `POST /v1/plans/compile` exposes `depends_on`, `input_keys`, and `output_keys`; and `owner`, `authority_class`, `review_class`, `failure_strategy`, `timeout_budget_seconds`, `max_attempts`, and `retry_backoff_seconds` remain projected.
- Human-task operations cover `/v1/human/tasks`, `human_task_created`, `human_task_returned`, `awaiting_human`, `human_review_role`, `human_review_priority`, `human_review_sla_minutes`, `human_review_desired_output_json`, `human_review_authority_required`, `human_review_why_human`, `human_review_quality_rubric_json`, and `human_review_auto_assign_if_unique`.
- Reviewer routing covers `/v1/human/tasks/operators`, `operator_id=<id>`, `routing_hints_json`, `auto_assign_operator_id`, `assignment_source`, `assigned_at`, `assigned_by_actor_id`, `/v1/human/tasks/{human_task_id}/assignment-history`, assignment-history rows now also carry originating `task_key`/`deliverable_type`, inline `human_tasks` rows now also carry originating `task_key`/`deliverable_type`, `last_transition_event_name`, `last_transition_operator_id`, and `last_transition_by_actor_id`.
- Queue filtering/sorting covers `priority=urgent|high|normal|low`, `priority=urgent,high`, `assignment_source=manual|recommended|auto_preselected`, `assignment_source=none`, `human_task_assignment_source=none`, `assignment_state=unassigned&assignment_source=none`, `assignment_state=unassigned&assignment_source=none&sort=created_asc`, `assignment_state=unassigned&assignment_source=none&sort=last_transition_desc`, `assignment_source=none&sort=created_asc`, `assignment_source=none&sort=last_transition_desc`, `status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc`, `status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc`, `session_id=<id>&assignment_source=none&sort=created_asc`, `session_id=<id>&assignment_source=none&sort=last_transition_desc`, and `session_id=<id>&assignment_source=<source>`.
- Queue sort enums include `sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc`; they fall back to oldest-created ordering for tasks without `sla_due_at`.
- Mixed-source guarantees include `manual and auto-preselected neighbors present`, ownerless `priority-summary?status=pending&assignment_state=unassigned&assignment_source=none` slice is now also covered after mixed-source churn, unsorted ownerless `assignment_source=none` list, backlog, and unassigned slices are now also covered after mixed-source churn, unsorted session-scoped `session_id=<id>&assignment_source=none` slice is now also covered after mixed-source churn, and mixed-source session-detail ownerless projection is now also count-checked.
- Queue examples also document `Current \`scripts/test_postgres_contracts.sh\` coverage includes artifacts, channel runtime, approvals, policy decisions, and task contracts.`
- Artifact/proof lookups expose `preview_text`, `storage_handle`, `mime_type`, `body_ref`, `originating \`task_key\`/\`deliverable_type\``, and self-describing artifact/proof task identity.
- Tool execution remains `ToolExecutionService`, `tool.v1`, self-heals its registry definition, `/v1/tools/execute`, `connector.dispatch`, `browseract.extract_account_inventory`, `browseract.extract_account_facts`, principal scope, `202 awaiting_approval`, `poll_or_subscribe`, typed runtime policy models, `artifact_retry`, and `skill_catalog`.

Use `--help` (or `-h`) on key scripts to print usage contracts quickly:

| Script | Help Command | Purpose |
|---|---|---|
| `scripts/deploy.sh` | `bash scripts/deploy.sh --help` | Legacy inherited EA deploy wrapper; property deploys use `make deploy` |
| `scripts/db_bootstrap.sh` | `bash scripts/db_bootstrap.sh --help` | Apply kernel DB migrations |
| `scripts/db_status.sh` | `bash scripts/db_status.sh --help` | Check kernel table presence/counts |
| `scripts/db_size.sh` | `bash scripts/db_size.sh --help` | Inspect table/index/total DB size footprint |
| `scripts/db_retention.sh` | `bash scripts/db_retention.sh --help` | Dry-run/apply runtime retention pruning |
| `scripts/smoke_api.sh` | `bash scripts/smoke_api.sh --help` | Run API smoke contracts |
| `scripts/smoke_help.sh` | `bash scripts/smoke_help.sh --help` | Verify `--help` usage contracts for operator scripts |
| `scripts/smoke_postgres.sh` | `bash scripts/smoke_postgres.sh --help` | Run end-to-end Postgres-backed smoke contract |
| `scripts/test_postgres_contracts.sh` | `bash scripts/test_postgres_contracts.sh --help` | Run isolated Postgres-backed repository contract tests |
| `scripts/hard_exit_gates.sh` | `./scripts/hard_exit_gates.sh --help` | Run the full flagship hard-exit bundle |
| `scripts/runtime_hard_exit_gates.sh` | `bash scripts/runtime_hard_exit_gates.sh --help` | Run the deploy-safe runtime hard-exit bundle |
| `scripts/property_release_gates.sh` | `./scripts/property_release_gates.sh --help` | Run the focused PropertyQuarry release bundle |
| `scripts/verify_ltd_critical_entries.py` | `python3 scripts/verify_ltd_critical_entries.py --help` | Fail closed on runtime-critical LTD drift |
| `scripts/verify_ltd_flagship_subset.py` | `python3 scripts/verify_ltd_flagship_subset.py --help` | Fail closed on the named flagship LTD subset |
| `scripts/list_endpoints.sh` | `bash scripts/list_endpoints.sh --help` | Print live endpoint inventory from OpenAPI |
| `scripts/version_info.sh` | `bash scripts/version_info.sh --help` | Print git and milestone/version fingerprint |
| `scripts/export_openapi.sh` | `bash scripts/export_openapi.sh --help` | Export timestamped OpenAPI snapshot |
| `scripts/diff_openapi.sh` | `bash scripts/diff_openapi.sh --help` | Diff OpenAPI snapshots |
| `scripts/prune_openapi.sh` | `bash scripts/prune_openapi.sh --help` | Prune old OpenAPI snapshots |
| `scripts/operator_summary.sh` | `bash scripts/operator_summary.sh --help` | Print compact operator command inventory |
| `scripts/support_bundle.sh` | `bash scripts/support_bundle.sh --help` | Build operator support bundle |
| `scripts/archive_tasks.sh` | `bash scripts/archive_tasks.sh --help` | Archive/prune local task log Done rows |
| `scripts/bootstrap_payfunnels_propertyquarry.py` | `python3 scripts/bootstrap_payfunnels_propertyquarry.py --help` | Prepare PayFunnels webhook/runtime config for PropertyQuarry |
| `scripts/bootstrap_emailit_propertyquarry.py` | `python3 scripts/bootstrap_emailit_propertyquarry.py --help` | Prepare and inspect the PropertyQuarry Emailit sending domain |
| `scripts/verify_release_assets.sh` | `./scripts/verify_release_assets.sh --help` | Verify release artifact completeness |

Combined index:

```bash
make operator-help
```

`bash scripts/version_info.sh` still prints milestone capability-status counts and release tags from `MILESTONE.json` as delivery history, but EA flagship release claims now come from `EA_FLAGSHIP_TRUTH_PLANE.md`, `EA_FLAGSHIP_RELEASE_GATE.json`, and `EA_FLAGSHIP_RELEASE_GATE.generated.json`.
Refresh the machine-readable receipt with `python3 scripts/materialize_ea_flagship_release_gate.py`.
Refresh the weekly pulse in `WEEKLY_PRODUCT_PULSE.generated.json` with `python3 scripts/materialize_weekly_product_pulse.py`.
EA product canon for those claims now lives in `.codex-design/ea/START_HERE.md`.

## CI Gate Summary

`smoke-runtime` workflow currently enforces:

- API gate bundle job:
  - `./scripts/bootstrap_propertyquarry_release_python.sh`
  - `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ci-gates-authenticated`
  - The authenticated bundle includes these non-authoritative local component
    checks:
    - `make verify-flagship-release-readiness`
    - `make verify-generated-release-artifacts-clean`
  - `make runtime-hard-exit-gates`
  - `make hard-exit-gates`
- Postgres smoke jobs:
  - `bash scripts/smoke_postgres.sh`
  - `bash scripts/test_postgres_contracts.sh`
  - `bash scripts/smoke_postgres.sh --legacy-fixture`

Milestone tracking linkage remains historical, but EA flagship release claims now key off `EA_FLAGSHIP_TRUTH_PLANE.md`, `EA_FLAGSHIP_RELEASE_GATE.json`, and the generated receipt instead of treating `MILESTONE.json` as the oracle.

Local mirror command:

```bash
make ci-gates
```

Authenticated repository mirror on the canonical checkout:

```bash
./scripts/bootstrap_propertyquarry_release_python.sh
./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ci-gates-authenticated
```

The bootstrap and authenticated launcher authenticate the v3 pin's canonical
requirements input and compiled lock from stable no-follow snapshots, then
check exact direct-pin parity before creating a verifier environment or
collecting tests. The current lock intentionally stops there with a concise
`jsonschema[format-nongpl]==4.26.0 is missing from the compiled requirements
lock` diagnostic. Recover the lock only through the approved registry lane;
do not hand-edit it or bypass this blocker with an ambient Python environment.

Local mirror including Postgres smoke:

```bash
make ci-gates-postgres
```

Aggregate LTD release verification:

```bash
./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py ltd-release-gates
```

Isolated Postgres repository-contract run:

```bash
make test-postgres-contracts
```

Current `scripts/test_postgres_contracts.sh` coverage includes artifacts, channel runtime, approvals, policy decisions, and task contracts.
The principal-scoped memory seed APIs are covered in-process by `tests/smoke_runtime_api.py` and over HTTP by the approved `scripts/smoke_api.sh` path that `scripts/smoke_postgres.sh` invokes. That Postgres smoke script now also force-recreates `ea-api` on rebuild so host smoke validates the current container image instead of a stale running process.

Local mirror including legacy migration-regression smoke:

```bash
make ci-gates-postgres-legacy
```

Release ops linkage: `RELEASE_CHECKLIST.md` includes `make ci-gates` and `make ci-gates-postgres-legacy` as optional local parity commands.

## 1) Start Services

```bash
make deploy
# or
docker compose -f docker-compose.property.yml up -d --build
```

Set `POSTGRES_PASSWORD`, `EA_SIGNING_SECRET`, and either `EA_API_TOKEN` or Cloudflare Access before starting a production-like PropertyQuarry stack. The standalone property compose file defaults `EA_RUNTIME_MODE=prod`, disables inherited public side surfaces, and uses the property-only scheduler profile.

Memory-only local mode (API without DB dependency):

```bash
make deploy-memory
```

`make deploy-memory` intentionally opts into the inherited legacy deploy wrapper for local migration/debugging only.

With schema bootstrap in one step:

```bash
make deploy-bootstrap
```

`make deploy-bootstrap` is also a legacy-stack helper. For the standalone property compose path, run migrations explicitly once the database is reachable.

## 2) Apply Kernel Migrations Manually

```bash
bash scripts/db_bootstrap.sh
# or
make bootstrap
```

Applies:
- `ea/schema/20260305_v0_2_execution_ledger_kernel.sql`
- `ea/schema/20260305_v0_3_channel_runtime_kernel.sql`
- `ea/schema/20260305_v0_4_policy_decisions_kernel.sql`
- `ea/schema/20260305_v0_5_artifacts_kernel.sql`
- `ea/schema/20260305_v0_6_execution_ledger_v2.sql`
- `ea/schema/20260305_v0_7_approvals_kernel.sql`
- `ea/schema/20260305_v0_8_channel_runtime_reliability.sql`
- `ea/schema/20260305_v0_9_tool_connector_kernel.sql`
- `ea/schema/20260305_v0_10_task_contracts_kernel.sql`
- `ea/schema/20260305_v0_11_memory_kernel.sql`
- `ea/schema/20260305_v0_12_entities_relationships_kernel.sql`
- `ea/schema/20260305_v0_13_commitments_kernel.sql`
- `ea/schema/20260305_v0_14_authority_bindings_kernel.sql`
- `ea/schema/20260305_v0_15_delivery_preferences_kernel.sql`
- `ea/schema/20260305_v0_16_follow_ups_kernel.sql`
- `ea/schema/20260305_v0_17_deadline_windows_kernel.sql`
- `ea/schema/20260305_v0_18_stakeholders_kernel.sql`
- `ea/schema/20260305_v0_19_decision_windows_kernel.sql`
- `ea/schema/20260305_v0_20_communication_policies_kernel.sql`
- `ea/schema/20260305_v0_21_follow_up_rules_kernel.sql`
- `ea/schema/20260305_v0_22_interruption_budgets_kernel.sql`
- `ea/schema/20260305_v0_23_execution_queue_kernel.sql`
- `ea/schema/20260305_v0_24_human_tasks_kernel.sql`
- `ea/schema/20260305_v0_25_human_task_resume_kernel.sql`
- `ea/schema/20260305_v0_26_human_task_assignment_state.sql`
- `ea/schema/20260305_v0_27_human_task_review_contract.sql`
- `ea/schema/20260305_v0_28_operator_profiles_kernel.sql`
- `ea/schema/20260305_v0_29_human_task_assignment_source.sql`
- `ea/schema/20260305_v0_30_human_task_assignment_provenance.sql`

Check table presence/counts:

```bash
bash scripts/db_status.sh
# or
make db-status
```

Check table/index size footprint:

```bash
bash scripts/db_size.sh
# or
make db-size

# all local-state, generated-media, project-volume, and shared Docker summaries
bash scripts/storage_size.sh
# or
make storage-size

# optional table-prefix filter
EA_DB_SIZE_TABLE_PREFIX=execution_ bash scripts/db_size.sh

# optional schema filter
EA_DB_SIZE_SCHEMA=public bash scripts/db_size.sh

# optional sort key (total|table|index|toast|dead)
EA_DB_SIZE_SORT_KEY=index bash scripts/db_size.sh

# optional minimum table size filter (MB)
EA_DB_SIZE_MIN_MB=25 bash scripts/db_size.sh
```

The Compose Postgres volume is `ea_pgdata`, mounted at `/var/lib/postgresql/data` inside `ea-db`.
If `/var/lib/docker/volumes/.../ea_pgdata` is large on the host, that is on-disk Postgres runtime state
(ledger, outbox, observations, memory tables, indexes), not RAM. Use `bash scripts/db_size.sh`
to attribute the footprint by table/index size before pruning or moving data.
`bash scripts/support_bundle.sh` now captures the expected volume name/mount and live `ea-db` mount inspection
by default, so support bundles can answer which host path backs `/var/lib/postgresql/data`.

Retention dry-run (default) and apply mode:

```bash
bash scripts/db_retention.sh
# or
make db-retention

# optional retention profile
EA_RETENTION_PROFILE=aggressive bash scripts/db_retention.sh

# optional per-table override
EA_RETENTION_DELIVERY_SENT_DAYS=14 bash scripts/db_retention.sh

# optional expired/revoked workspace-session grace window
EA_RETENTION_WORKSPACE_ACCESS_SESSIONS_DAYS=7 bash scripts/db_retention.sh

# optional table allowlist (CSV)
EA_RETENTION_TABLES=execution_events,delivery_outbox bash scripts/db_retention.sh

# optional table skip list (CSV)
EA_RETENTION_SKIP_TABLES=observation_events,policy_decisions bash scripts/db_retention.sh

# apply deletions
bash scripts/db_retention.sh --apply
```

Always export `PROPERTYQUARRY_DB_SERVICE=propertyquarry-db`,
`PROPERTYQUARRY_DB_CONTAINER_NAME=propertyquarry-db-live`,
`POSTGRES_DB=propertyquarry`, and `POSTGRES_USER=postgres` when operating the
standalone runtime. Both commands print the resolved target before querying or
mutating. Apply mode rejects unknown table names, journals the run, uses
`EA_RETENTION_BATCH_SIZE` and `EA_RETENTION_MAX_ROWS_PER_TABLE`, and can be
rerun safely after a ceiling or failure. Ordinary `VACUUM (ANALYZE)` is opt-in;
physical reclaim is a separate backup-first maintenance operation.

Install and enable the checked-in
`packaging/propertyquarry-storage-maintenance/systemd/` units through the host
configuration lane for hourly high-water alerts and weekly bounded retention.
The complete classification, legal-hold invariants, quota/backpressure policy,
and exact reclaim boundary are in
`docs/PROPERTYQUARRY_STORAGE_RETENTION.md`.

## 3) Health Check

```bash
curl -fsS http://localhost:${EA_HOST_PORT:-8090}/health
curl -fsS http://localhost:${EA_HOST_PORT:-8090}/health/live
curl -fsS http://localhost:${EA_HOST_PORT:-8090}/health/ready
curl -fsS http://localhost:${EA_HOST_PORT:-8090}/version
```

## 4) Rewrite + Session Audit Smoke

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/rewrite/artifact \
  -H 'content-type: application/json' \
  -d '{"text":"runbook smoke"}'
```

Use returned `artifact_id` and `execution_session_id`:

```bash
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/rewrite/artifacts/<artifact_id>"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/rewrite/receipts/<receipt_id>"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/rewrite/run-costs/<cost_id>"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/rewrite/sessions/<session_id>"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/policy/decisions/recent?session_id=<session_id>&limit=5"
```

External-send policy preview:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/policy/evaluate \
  -H 'content-type: application/json' \
  -d '{"content":"Send the board update to the distribution list.","tool_name":"connector.dispatch","action_kind":"delivery.send","channel":"email"}'
```

## 5) Observation + Delivery Smoke

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/observations/ingest \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","channel":"email","event_type":"thread.opened","payload":{"subject":"Board prep"}}'
```

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/delivery/outbox \
  -H 'content-type: application/json' \
  -d '{"channel":"slack","recipient":"U1","content":"Draft ready","metadata":{"priority":"high"}}'
```

```bash
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/observations/recent?limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/delivery/outbox/pending?limit=10"
```

## 6) Telegram Adapter Smoke

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/channels/telegram/ingest \
  -H 'content-type: application/json' \
  -d '{"update":{"message":{"chat":{"id":42},"text":"hello","message_id":7,"date":123}}}'
```

## 7) Full Smoke Script

```bash
bash scripts/smoke_api.sh
# or
make smoke-api
# or (includes help-smoke + API smoke)
make release-smoke
# postgres-backed smoke path
bash scripts/smoke_postgres.sh
# or
make smoke-postgres
# optional isolated DB name override
EA_SMOKE_DB=ea_smoke_runtime bash scripts/smoke_postgres.sh
# legacy migration-regression mode (skips API smoke)
bash scripts/smoke_postgres.sh --legacy-fixture
# or
make smoke-postgres-legacy
```

The smoke script now includes external-send policy evaluation plus a blocked-policy assertion (`403` on oversized rewrite input) and runs against an isolated smoke DB so legacy runtime data is not mutated.

## 8) Memory Candidate Promotion Smoke

For every principal-scoped connector or memory example below, send `X-EA-Principal-ID: exec-1` (or your chosen principal). If you also pass `principal_id`, it must match that request header or the runtime will return `403 principal_scope_mismatch`.

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/candidates \
  -H "X-EA-Principal-ID: exec-1" \
  -H 'content-type: application/json' \
  -d '{"category":"stakeholder_pref","summary":"CEO prefers concise updates","fact_json":{"tone":"concise"}}'
```

Promote using the returned `candidate_id`:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/candidates/<candidate_id>/promote \
  -H "X-EA-Principal-ID: exec-1" \
  -H 'content-type: application/json' \
  -d '{"reviewer":"operator","sharing_policy":"private"}'
curl -fsS -H "X-EA-Principal-ID: exec-1" "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/items?limit=10"
```

Seed semantic entities/relationships:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/entities \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","entity_type":"person","canonical_name":"Alex Executive","attributes_json":{"role":"executive"}}'
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/relationships \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","from_entity_id":"<entity_a>","to_entity_id":"<entity_b>","relationship_type":"reports_to"}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/entities?limit=10&principal_id=exec-1"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/relationships?limit=10&principal_id=exec-1"
```

Principal-scoped commitments:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/commitments \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","title":"Send board follow-up","details":"Draft by Friday","status":"open","priority":"high"}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/commitments?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/commitments/<commitment_id>?principal_id=exec-1"
```

Principal-scoped authority bindings:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/authority-bindings \
  -H "X-EA-Principal-ID: exec-1" \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","subject_ref":"assistant","action_scope":"calendar.write","approval_level":"manager","channel_scope":["email","slack"],"policy_json":{"quiet_hours_enforced":true},"status":"active"}'
curl -fsS -H "X-EA-Principal-ID: exec-1" "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/authority-bindings?principal_id=exec-1&limit=10"
curl -fsS -H "X-EA-Principal-ID: exec-1" "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/authority-bindings/<binding_id>?principal_id=exec-1"
```

If the request principal and a supplied `principal_id` disagree, the runtime now returns `403 principal_scope_mismatch` instead of silently reading another principal scope.

Principal-scoped delivery preferences:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/delivery-preferences \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","channel":"email","recipient_ref":"ceo@example.com","cadence":"urgent_only","quiet_hours_json":{"start":"22:00","end":"07:00"},"format_json":{"style":"concise"},"status":"active"}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/delivery-preferences?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/delivery-preferences/<preference_id>?principal_id=exec-1"
```

Principal-scoped follow-ups:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/follow-ups \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","stakeholder_ref":"ceo@example.com","topic":"Board follow-up","status":"open","due_at":"2026-03-07T09:00:00+00:00","channel_hint":"email","notes":"Send summary after prep call","source_json":{"source":"manual"}}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/follow-ups?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/follow-ups/<follow_up_id>?principal_id=exec-1"
```

Principal-scoped deadline windows:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/deadline-windows \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","title":"Board prep delivery window","start_at":"2026-03-07T08:30:00+00:00","end_at":"2026-03-07T10:00:00+00:00","status":"open","priority":"high","notes":"Draft must be ready before board sync","source_json":{"source":"manual"}}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/deadline-windows?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/deadline-windows/<window_id>?principal_id=exec-1"
```

Principal-scoped stakeholders:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/stakeholders \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","display_name":"Sam Stakeholder","channel_ref":"email:sam@example.com","authority_level":"approver","importance":"high","response_cadence":"fast","tone_pref":"diplomatic","sensitivity":"confidential","escalation_policy":"notify_exec","open_loops_json":{"board_follow_up":"open"},"friction_points_json":{"scheduling":"tight"},"last_interaction_at":"2026-03-06T15:30:00+00:00","status":"active","notes":"Needs concise summaries"}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/stakeholders?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/stakeholders/<stakeholder_id>?principal_id=exec-1"
```

Principal-scoped decision windows:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/decision-windows \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","title":"Board response decision","context":"Choose timing and channel for reply","opens_at":"2026-03-06T08:00:00+00:00","closes_at":"2026-03-06T12:00:00+00:00","urgency":"high","authority_required":"exec","status":"open","notes":"Needs decision before board prep","source_json":{"source":"manual"}}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/decision-windows?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/decision-windows/<decision_window_id>?principal_id=exec-1"
```

Principal-scoped communication policies:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/communication-policies \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","scope":"board_threads","preferred_channel":"email","tone":"concise_diplomatic","max_length":1200,"quiet_hours_json":{"start":"22:00","end":"07:00"},"escalation_json":{"on_high_urgency":"notify_exec"},"status":"active","notes":"Board-facing communication defaults"}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/communication-policies?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/communication-policies/<policy_id>?principal_id=exec-1"
```

Principal-scoped follow-up rules:

```bash
curl -fsS -X POST http://localhost:${EA_HOST_PORT:-8090}/v1/memory/follow-up-rules \
  -H 'content-type: application/json' \
  -d '{"principal_id":"exec-1","name":"Board reminder escalation","trigger_kind":"deadline_risk","channel_scope":["email","slack"],"delay_minutes":120,"max_attempts":3,"escalation_policy":"notify_exec","conditions_json":{"priority":"high"},"action_json":{"action":"draft_follow_up"},"status":"active","notes":"Escalate if follow-up is late"}'
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/follow-up-rules?principal_id=exec-1&limit=10"
curl -fsS "http://localhost:${EA_HOST_PORT:-8090}/v1/memory/follow-up-rules/<rule_id>?principal_id=exec-1"
```
## 9) Script Help Smoke

```bash
bash scripts/smoke_help.sh
# or
make smoke-help
```

## 10) Export OpenAPI Snapshot

```bash
bash scripts/export_openapi.sh
# or
make openapi-export
```

Compare the latest two snapshots:

```bash
bash scripts/diff_openapi.sh
# or
make openapi-diff
```

Prune old snapshots (default keep=20):

```bash
bash scripts/prune_openapi.sh
# keep 50
bash scripts/prune_openapi.sh 50
# or
make openapi-prune
```

## 11) Optional Local Pre-Commit Hook

```bash
mkdir -p .githooks
cp .githooks/pre-commit.example .githooks/pre-commit
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
```

## 12) Print Endpoint Inventory

```bash
bash scripts/list_endpoints.sh
# or
make endpoints
```

## 13) Print Version Fingerprint

```bash
bash scripts/version_info.sh
# or
make version-info
```

## 14) Print Operator Summary

```bash
bash scripts/operator_summary.sh
# or
make operator-summary
```

The operator summary includes release smoke/readiness commands plus legacy smoke/parity shortcuts, release/support commands such as the authenticated release-preflight dispatcher and `make support-bundle`, and task-archive shortcuts.
It also includes the authenticated aggregate LTD release-gate dispatcher.

## 15) Generate Support Bundle

```bash
bash scripts/support_bundle.sh
# optional log tail length
SUPPORT_LOG_TAIL_LINES=500 bash scripts/support_bundle.sh
# optional: skip DB logs
SUPPORT_INCLUDE_DB=0 bash scripts/support_bundle.sh
# optional: skip API logs
SUPPORT_INCLUDE_API=0 bash scripts/support_bundle.sh
# optional: skip DB volume attribution
SUPPORT_INCLUDE_DB_VOLUME=0 bash scripts/support_bundle.sh
# optional: skip DB size snapshot
SUPPORT_INCLUDE_DB_SIZE=0 bash scripts/support_bundle.sh
# optional: DB size snapshot top-table limit
SUPPORT_DB_SIZE_LIMIT=15 bash scripts/support_bundle.sh
# optional: skip queue snapshot
SUPPORT_INCLUDE_QUEUE=0 bash scripts/support_bundle.sh
# optional: custom filename prefix
SUPPORT_BUNDLE_PREFIX=incident_42 bash scripts/support_bundle.sh
# optional: custom timestamp format
SUPPORT_BUNDLE_TIMESTAMP_FMT=%Y-%m-%dT%H%M%SZ bash scripts/support_bundle.sh
# or
make support-bundle
```

`support_bundle.sh` applies baseline redaction patterns for common secret/token/password forms.

## 16) Archive Completed Task Rows

```bash
# append Done rows to TASKS_ARCHIVE.md
bash scripts/archive_tasks.sh
# preview archive rows only
bash scripts/archive_tasks.sh --dry-run
# append + prune Done rows in the local TASKS_WORK_LOG.md
bash scripts/archive_tasks.sh --prune-done
# or
make tasks-archive
make tasks-archive-dry-run
make tasks-archive-prune
```

## 17) Verify Release Assets

```bash
# non-authoritative local parity
make verify-release-assets
# docs-focused alias
make docs-verify
# docs + operator-help bundle
make release-docs

# authenticated repository verification on the canonical Linux checkout
./scripts/bootstrap_propertyquarry_release_python.sh
./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py verify-release-assets-authenticated
# equivalent direct authority boundary after bootstrap
./scripts/verify_release_assets.sh
```

Use `make release-docs` as a pre-smoke documentation/usage pass before
dispatching the authenticated `release-preflight` target.

Combined local readiness check:

```bash
make all-local
```

`make all-local` is a lightweight readiness pass that remains
non-authoritative and still
checks release assets, flagship release readiness, and generated release
artifact cleanliness with the selected development Python. The authenticated
lane requires a real `/docker/property` checkout, the pinned Linux CPython
runtime, and `./scripts/bootstrap_propertyquarry_release_python.sh`. Use the
dispatcher's `release-preflight` target for that release-stage repository proof
and operator checks. `make all-local`, the release-asset verifiers, the
generated-cleanliness verifiers, and authenticated `release-preflight` are
read-only with respect to the three canonical receipts. Only the explicitly
named materialization targets refresh them. Both `ci-gates` lanes are also
source-tree read-only: bytecode is compiled into a private temporary cache,
pytest's repository cache is disabled, and nested exit-gate evidence is routed
to a private temporary receipt directory that is removed after the test
bundle. They validate the evidence already in the checkout. The official smoke
workflow explicitly reproduces the browser, flagship, and weekly receipts in
its disposable canonical checkout after Chromium installation, immediately
requires an exact-`HEAD` match, and only then runs `ci-gates-authenticated`.
That workflow action is current-runtime reproduction evidence, not publication
or release authority.

Deploys now default to a runtime hard-exit pass after the stack reports healthy. `scripts/deploy.sh` will run `bash scripts/runtime_hard_exit_gates.sh` unless `EA_RUN_RUNTIME_HARD_EXIT_GATES=0`. The runtime bundle is deploy-safe and excludes the deeper `smoke_api_tibor.sh` contract lane; that lane remains part of `make hard-exit-gates`.

Read-only release preflight aggregate (asset checks + flagship
release-readiness verification + generated release artifact cleanliness +
operator help + release smoke):

```bash
./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py release-preflight
```

For an intentional standalone authenticated refresh followed by read-only
readiness validation:

```bash
./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py materialize-release-assets-authenticated
./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py verify-flagship-release-readiness-authenticated
```

`RELEASE_CHECKLIST.md` now includes explicit EA flagship truth-plane and release-readiness preflight lines to validate the browser proof, release gate seed, weekly pulse, and Fleet journey gate.

## Smoke Exit Codes

`scripts/smoke_api.sh` uses these explicit non-zero codes for contract failures:

- `11`: rewrite response missing `execution_session_id`
- `12`: policy contract mismatch (`/v1/policy/evaluate` or blocked-policy assertion)
- `13`: runtime response missing an expected resource id (delivery or memory flow)

Other transport failures (for example `curl`) return their native non-zero exit codes.
PropertyQuarry operator scripts default to the standalone compose service keys (`propertyquarry-api`, `propertyquarry-scheduler`, `propertyquarry-db`). You can override them per environment with:

- `PROPERTYQUARRY_API_SERVICE`
- `PROPERTYQUARRY_WORKER_SERVICE`
- `PROPERTYQUARRY_SCHEDULER_SERVICE`
- `PROPERTYQUARRY_DB_SERVICE`

`PROPERTYQUARRY_WORKER_SERVICE` is left empty in `.env.example` because `docker-compose.property.yml` does not start a standalone worker by default. That same alias layer is honored by `scripts/support_bundle.sh`, so support exports follow the standalone compose service mapping too.
