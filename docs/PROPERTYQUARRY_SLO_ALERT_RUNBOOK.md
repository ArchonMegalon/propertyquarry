# PropertyQuarry SLO Alert Runbook

These procedures correspond to the `runbook` annotation in
`config/monitoring/propertyquarry_alert_rules.v1.yml`. Preserve the alert
labels, evaluation time, candidate release SHA, SLO-evidence receipt, metrics
snapshot, and correlation IDs before changing state. Use
`docs/PROPERTYQUARRY_ROLLBACK.md` for a release switch and
`docs/PROPERTYQUARRY_HOST_RECOVERY.md` for reboot recovery.

## Metrics missing

Confirm the private scrape target, authentication, API container health, and
`/internal/metrics` status without opening the endpoint publicly. If the API
is ready but scraping fails, repair only the private scrape credential or
target. Treat a missing metrics family as an instrumentation regression and
stop launch promotion.

## Replica coverage

Compare every `up{job="propertyquarry"}` target with
`propertyquarry_expected_api_replicas`. Identify the exact missing or failing
`instance`; do not treat one healthy replica as proof that the service is
fully covered. Confirm the direct per-replica file-SD target document and the
secret-backed bearer credential before changing replica count. A missing
expected-replica gauge or disagreement between replicas is a release identity
fault, not evidence that fewer replicas are acceptable. Keep an unready
replica out of traffic and stop promotion whenever discovered, exported, or
expected coverage is short.

<a id="availability-error-rate"></a>

## Availability / error rate

Compare 5xx rate by bounded route template with the release timestamp and
structured error logs using correlation IDs. Contain a single failing
provider or route before scaling broadly. If the new release introduced the
error budget burn, follow the guarded rollback runbook; do not mask the alert
or reclassify 5xx responses.

## Ingress admission backend

Group `propertyquarry_ingress_admission_operations_total` only by its bounded
`backend`, `operation`, and `outcome` labels. For `backend_unavailable`, confirm
the affected operation and PostgreSQL readiness before changing traffic.
Preserve correlation IDs and database errors, stop promotion, and keep the
admission path fail closed. Do not switch to process-local admission or reset
quota or lease state to clear the alert.

## Ingress admission capacity

The PostgreSQL contract must emit exactly one valid contract and both closed
capacity keys per ready API replica: `lease` with limit `100000`, and `quota`
with limit `1000000`. Treat a missing series, a zero contract-valid gauge, or
any different limit as schema or exporter drift and stop promotion. At 80
percent utilization, investigate expired-row cleanup and sustained producers;
at 95 percent, contain new admission load before capacity is exhausted. Never
delete governed rows, alter the fixed limits, or bypass the database capacity
trigger during recovery.

## Latency

Check p95 and p99 together, then split histogram data by bounded route,
method, and replica. Inspect database latency, provider timeouts, CPU pressure,
and queue backlog. Prefer reversible traffic or concurrency reduction. Do not
raise the thresholds during an incident.

## Readiness

Read the private `/health/ready` reason and correlate it with database and
runtime heartbeat state. Keep the failed replica out of traffic. Use the
dedicated recovery lane if the host rebooted; do not use a generic Compose
restart or bypass readiness.

## Runtime heartbeats

Act only on roles where `propertyquarry_runtime_heartbeat_required{role}=1`.
Confirm heartbeat presence, age, stale state, process/container health, and
the configured maximum age. The dedicated PropertyQuarry durable worker and
standalone scheduler are both required baseline roles. A missing or stale
worker heartbeat also keeps API readiness closed. Do not fabricate or touch
heartbeat files.

## Database saturation

This alert is active only when both pool capacity and in-use series are
exposed. Check connection leaks, slow queries, database limits, and replica
pressure before increasing capacity. Preserve the database and use the
disaster-recovery runbook for integrity or restore concerns.

## Queue backlog

Treat `propertyquarry_queue_observed{queue="property_search"}=0` with a fresh
required worker heartbeat as an instrumentation or database-snapshot failure,
not as an empty queue. Preserve the worker heartbeat and error logs, verify the
authoritative queue query, and stop promotion until the observed gauge returns
to `1`. When observed, identify the oldest governed work class, scheduler
health, provider availability, and retry loops. Stop uncontrolled producers or
retries before adding consumers. Preserve queued work and idempotency evidence.

## Provider and quota failures

Inspect bounded provider, quota, and balance route errors plus provider-ledger
receipts. Distinguish exhausted quota, authentication, provider outage, and
policy denial. Fail closed to the product's documented safe fallback; do not
rotate credentials, buy quota, or switch a provider without the appropriate
operator authority.

## Delivery outbox integrity

For dead-lettered or failed outcomes, preserve the outbox row, attempt
history, correlation ID, and scheduler heartbeat before retrying. For claim
conflicts, confirm that only the intended scheduler replicas and lease owners
are active. Do not delete, replay, or manually mark delivery work sent; use
the governed idempotent recovery path after the ownership fault is understood.

## Content ledger integrity

Treat replay conflicts, failed writes, and corruption as data-integrity
incidents. Stop the affected content-writing lane, preserve the ledger row and
request hash, and compare the immutable payload identity with its prior
receipt. Never overwrite a conflicting ledger entry or bypass its claim; use
the documented database recovery and rollback boundaries.

Close the incident only after the alert resolves, readiness and required
heartbeats pass, the immutable `/version` identity is correct, and a fresh SLO
evidence receipt passes.
