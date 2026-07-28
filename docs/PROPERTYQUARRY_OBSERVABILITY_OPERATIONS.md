# PropertyQuarry Observability Operations

This runbook defines the operating contract behind
`config/monitoring/propertyquarry_flagship_operations.v1.json`. It is not live
evidence. A flagship release remains blocked until independently authenticated,
fresh receipts bind the dashboard, log, trace, and alert-delivery results to the
exact release commit, image digest, and replica set.

## Distributed admission

Correlate admission outcomes with bounded rejection reasons, route-class cost,
and high-cost in-flight work. Split results by the declared backend, operation,
and outcome labels. A missing family is an instrumentation failure, not a zero
rate. Do not weaken admission limits or relabel backend failures to make this
panel pass.

The API exporter emits
`propertyquarry_ingress_admission_operations_total{backend,operation,outcome}`
from a closed label set. Production startup requires the PostgreSQL backend and
fails if its governed schema is not ready; development may use the explicitly
process-local `memory` backend. Subjects, addresses, principal identifiers,
routes, and lease tokens are never metric labels. A
`backend_unavailable` outcome is an observed failure and must page; it must not
be rewritten as a quota rejection.

Production policy is the fixed schema-contract-v1 profile: its request/cost
limits, 60-second window, concurrency caps, and 30-second lease cannot be
overridden per replica. Its trusted immediate-proxy set is likewise fixed to
the canonical loopback networks; production rejects replica-local CIDR
overrides. Subject HMACs use a domain-separated key derived from the durable,
at-least-32-byte property-search erasure secret. Startup compares that secret's
non-secret key ID with the database key-state row, so a weak, rotated, or
replica-local key fails closed instead of partitioning quota subjects. Key
rotation requires the governed database key-migration procedure.

Metered POST/PUT/PATCH requests require one canonical `Content-Length`; DELETE
bodies are reconciled against an explicit length or zero. Transfer encoding and
any body that differs from the declared length are rejected before cost
admission and route dispatch, including routes that never read their body. The
cheap distributed IP-request admission happens before bounded body buffering.
This makes size-derived cost units authoritative without offering pre-quota
memory or bandwidth work.

The backend-unavailable objective has zero tolerance. Both the authenticated
short-window snapshots and the independently authenticated 30-day Prometheus
range must carry the complete PostgreSQL operation/outcome matrix and fail when
any `backend_unavailable` delta is non-zero.

## Admission capacity

Require one valid PostgreSQL capacity contract and bounded `lease` and `quota`
row-count/limit series. Compare each observed limit with the configured hard
limit before calculating utilization. Missing, duplicate, non-finite, negative,
or mismatched series block promotion. Do not infer empty capacity from absent
telemetry or raise a hard limit during an incident.

Schema v15 maintains the authoritative counters transactionally through
protected triggers and refuses direct capacity-row mutation or truncation. The
authenticated metrics scrape reads the current backend snapshot and emits:

- `propertyquarry_admission_capacity_contract_valid{backend}`
- `propertyquarry_admission_capacity_row_count{backend,capacity_key}`
- `propertyquarry_admission_capacity_limit{backend,capacity_key}`

For PostgreSQL the only capacity keys are `lease` and `quota`, with immutable
limits `100000` and `1000000` respectively. A failed snapshot emits
`contract_valid=0` without fabricating row counts or limits. Production
admission remains fail-closed while the backend is unavailable; it never falls
back to process-local counters.

Expired-row cleanup is cadence-limited per process and protected by a
cluster-wide PostgreSQL try-lock. It commits before admission subject/capacity
locks, preserving lock order without adding a cleanup transaction to every
request. Do not replace it with per-request cleanup or an uncoordinated replica
timer.

## Log correlation

Query the exact release and time window, then confirm every relevant event has
bounded correlation, trace, span, release, image, and replica fields. Preserve
the original query and result digest. Logs must not contain credentials,
cookies, private payloads, or unbounded provider data. Missing fields or a query
that cannot isolate the candidate release block the evidence lane.

## Trace continuity

Follow one independently selected customer request across the API, durable
search worker, and provider or render boundary. Require W3C `traceparent` v00,
one shared nonzero trace ID, distinct nonzero span IDs, the direct parent chain
`customer_api` → `durable_search_worker` → `provider_or_render_boundary`, and
exact release attributes. A star of sibling spans is correlation, not
continuity, and fails this gate. Preserve the trace-query receipt and result
digest. Synthetic spans, manually joined traces, or traces from a different
release do not satisfy this contract. Incoming reserved v00 trace-flag bits are
never propagated; outbound headers retain only the defined sampled bit.

### Optional local span evidence

The runtime defaults to a null span exporter. Operators may explicitly enable a
private, bounded JSONL diagnostic sink with:

- `PROPERTYQUARRY_LOCAL_SPAN_EXPORT_ENABLED=1`
- `PROPERTYQUARRY_LOCAL_SPAN_EXPORT_PATH` set to a normalized absolute `.jsonl`
  path whose dedicated parent directory is owned by the runtime user and has
  mode `0700`
- `PROPERTYQUARRY_LOCAL_SPAN_EXPORT_MAX_BYTES` between `4096` and `67108864`
  bytes (default `4194304`)
- `PROPERTYQUARRY_LOCAL_SPAN_EXPORT_BACKUP_COUNT` between `1` and `10`
  (default `3`)

The runtime creates the sink and its stable lock file with mode `0600`, rejects
symlinks, special files, extra hard links, and ownership or permission drift,
and uses an advisory process lock for append and rotation. Each canonical JSON
line declares schema `propertyquarry.local-span-evidence.v1`, scope
`repo_local_deterministic_only`, and `live_receipt_eligible=false`. Root spans
encode `parent_span_id` as JSON `null`; release commit, image digest, and replica
identity are derived from the runtime and revalidated before each append.
Historical records retain and validate their own release identity, so a
persistent file remains queryable across replica replacement and rolling
releases. A crash-truncated active tail is discarded back to its last complete
line before another append; failed writes are rolled back to their prior
offset. Both recovery and export failure emit bounded `ea.telemetry` log events,
and the process-local health snapshot counts failures and recoveries. The
authenticated metrics endpoint exposes those counters as
`propertyquarry_local_span_export_failures_total` and
`propertyquarry_local_span_export_recoveries_total`.

This file is local diagnostic evidence only. It is not a Tempo response, is not
authenticated or challenge-bound by the evidence authority, and does not prove
the required six-hour window or declared replica set. Never submit it as
`PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT`. Use a release-specific private
directory and treat a missing or unreadable artifact as a telemetry failure, not
as evidence of live readiness.

## Flagship live-receipt gate

The launch gate requires three private JSON inputs in addition to the existing
alert-delivery receipt:

- `PROPERTYQUARRY_DASHBOARD_RENDER_RECEIPT`
- `PROPERTYQUARRY_STRUCTURED_LOG_QUERY_RECEIPT`
- `PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT`

Each receipt is signed separately under its kind-specific domain by the fixed
external release-control authority. It must bind the active challenge,
canonical operations-policy hash, exact commit and image, the same sorted
replica set, and the same six-hour window. Gold copies the raw files into its
private immutable input set, verifies their signatures and canonical hashes,
and the final launch authority independently re-hashes the original files.
There is no policy path or hash override.

Dashboard evidence must cover every canonical panel in order. A rendered panel
with zero samples is invalid. The admission families are now emitted, but their
presence is not live evidence and does not remove the independent receipt,
topology, freshness, release-identity, or replica-coverage requirements.
Log samples and trace spans must use declared replicas, carry exact release
attributes, and join through one real trace and at least one shared span.

Adding `flagship_operations_sha256` to the canonical policy set invalidates old
challenges by design. Release control must issue a fresh challenge before live
receipts are captured. The policy's
`source_contract_status=defined_not_live_evidence` remains intentional: it says
the checked-in contract is not itself proof.

### Current production boundary

This repository currently provides the verifier and signing client, but not a
production server for `/run/propertyquarry/evidence-authority.sock` or a live
Grafana/Loki/Tempo capture producer. The active release-control-v2 supervisor is
also deliberately non-authoritative and refuse-only. Test-only Ed25519
authorities and hand-authored JSON must never be used as release evidence.

Promotion therefore remains blocked until a root-controlled, schema-aware
evidence authority allows only the three receipt domains, fixed private
observability topology is deployed, and the active controller consumes the
verified results. Extending the generic signer without a domain and payload
allowlist is not an acceptable shortcut.

## Evidence handling

Keep raw monitoring results private and immutable. Receipts must name the
bounded query, observation window, exact release identity, payload digest,
independent attestation authority, and capture time. Do not copy secrets into a
receipt or claim a panel passed when its source family, query backend, or
release binding is unavailable.
