# PropertyQuarry Property-Search Schema Migrations

Property-search database changes are deploy operations, not application
startup behavior. API, worker, and scheduler processes only verify the schema
ledger and required relations; they never create or alter the run, durable
queue, source-cache, delivery-outbox, or property-content ledger schema.

## Versioned contract

`app.product.property_search_schema` owns the ordered migration manifest:

1. `property_search_runs_tenant_schema`
2. `property_search_durable_work_queue`
3. `property_source_listing_cache`
4. `replica_safe_delivery_outbox`
5. `durable_property_content_job_ledger`
6. `bounded_run_delivery_projection`
7. `tenant_scoped_delivery_outbox_idempotency`
8. `property_evidence_overlay_cached_read_model`
9. `property_evidence_overlay_staged_snapshot_activation`
10. `tenant_scoped_property_research_packet_links`
11. `durable_property_search_erasure_fences`
12. `property_content_account_ownership_fence`
13. `property_content_polymorphic_authority_trigger_fix`
14. `property_research_packet_erasure_trigger_split`
15. `authoritative_distributed_ingress_admission`

Each immutable migration has a SHA-256 checksum. Applied versions are recorded
in `propertyquarry_schema_migrations` under component `property_search`. The
deploy command acquires a transaction-scoped PostgreSQL advisory lock before
creating the ledger, validates every existing name and checksum, applies all
pending migrations in order, writes their ledger rows, and commits once. A
failed statement rolls back the complete batch. A changed checksum, version
gap, or unknown future version fails closed; never repair those conditions by
editing the ledger.

## Production deploy phase

The candidate checkout has no production migration authority. Production
schema changes run only inside the independently installed release controller,
under its fixed deploy lock, canonical Compose plan, server-derived database
identity, durable role fence, signed authorization, and external monotonic
seal. The controller contains ingress and every writer before it reads
candidate evidence, commits the ordered DDL, migration ledger, plan binding,
and result digest atomically, and activates a new runtime-role epoch only after
the exact result is sealed.

An unprivileged operator first submits the externally issued signed request to
the controller's read-only disposition:

```bash
EA_RUNTIME_MODE=prod \
PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST=/run/user/$(id -u)/propertyquarry-deploy-preflight-request.json \
  ./scripts/deploy_propertyquarry.sh --preflight-only
```

The preflight request is operation-bound and cannot authorize mutation. After
reviewing a `READY` disposition, obtain a distinct, fresh `deploy-run` signed
request and use the handoff without `--preflight-only`. Do not export a
production `DATABASE_URL`,
`POSTGRES_PASSWORD`, owner/migrator credential, or traffic credential to the
checkout. Direct Compose and Python migration commands are not a production
fallback and their output is not release evidence.

### Mandatory contained cutover for schema v11 and admission schema v15

Writer contract 3 and schema v11 are deliberately not rolling-compatible.
Current schema v15 remains incompatible with contract-2 processes, and the
authoritative ingress runtime must not start before v15 is committed. The
installed controller must execute this exact fail-closed sequence when upgrading
a v9 or v10 deployment:

1. Pin one high-entropy, at-least-32-byte
   `PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET` in the external secret store.
   The migration role and every API, worker, scheduler, and publication role
   must receive the same value. Render-only processes that cannot commit a
   publication do not receive it. The database stores only its key ID; a missing or different key
   is rejected as `property_search_erasure_key_required` or
   `property_search_erasure_key_mismatch`; a shorter production secret is
   rejected as `property_search_erasure_secret_too_short`.
2. Contain ingress, stop new queue claims, drain current work, and stop every
   API, worker, scheduler, and render/publication writer. A merely healthy old
   replica is not safe evidence that the drain completed.
3. While all writers remain stopped, take the controller-governed backup and
   apply the ordered pending migrations. From live schema v9 this applies v10
   and v11 in the same migration transaction, then continues through v12 and
   v13 before that transaction commits; partial application is forbidden.
   The migration runner sets contract 2 only inside that transaction while v11
   backfills legacy principal keys, before v11 replaces the write guards with
   contract 3. This is migration authority, never runtime writer authority.
   Schema v13 then replaces the polymorphic property-content trigger so event
   rows without a `row_json` column are validated without unsafe field access.
   Schema v14 installs the table-specific packet-erasure guards, and schema v15
   installs the bounded quota, lease, and immutable capacity-counter contract.
4. Schema v11 first established the homogeneous schema-v11/contract-3 fleet
   boundary. For current schema v15, start only the immutable, homogeneous
   schema-v15/contract-3 fleet with ingress-policy contract v1. Require current
   readiness plus fresh per-instance heartbeats for the complete expected role
   manifest before reopening ingress.

Never start current contract-3 code before every migration through v15 commits,
and never restart a contract-2 binary after v11. A failed step leaves ingress and writers contained for
forward repair. Changing the erasure secret is a separately designed key
migration, not an environment-variable rotation; without that migration the
database intentionally fails closed.

## Disposable development and test targets

The standalone Compose topology includes a one-shot
`propertyquarry-migrate` service. It may be used directly only against a
disposable local development database whose credentials and containers have no
production reach:

```bash
EA_RUNTIME_MODE=dev \
POSTGRES_PASSWORD='<local-disposable-password>' \
  docker compose -f docker-compose.property.yml up -d --build
```

For an explicitly disposable, separately orchestrated development database,
run the same migration boundary before starting any application role:

```bash
PYTHONPATH=ea DATABASE_URL='<private-disposable-development-dsn>' \
  python3 scripts/migrate_property_search_storage.py
```

The command is idempotent. A successful no-op reports the current version and
`applied=none`. Do not put even a disposable database URL in shell history,
logs, receipts, or checked-in configuration.

Verify source contracts without contacting a database:

```bash
PYTHONPATH=ea env -u DATABASE_URL \
  python3 scripts/check_property_search_storage_schema.py
```

With `DATABASE_URL` deliberately supplied, the check performs read-only ledger
and relation probes. It does not migrate.

## Runtime readiness

Production API, worker, and scheduler roles require the current schema
version. `/health/ready` returns `503` with a bounded
`property_search_schema_not_ready:<reason>` until the ledger, checksums,
versions, tables, and indexes pass. Application repositories enforce the same
read-only boundary before issuing run or queue queries.

Hot readiness verifies the v15 ledger, relations, functions, enabled triggers,
fixed capacity rows, and the database-bound erasure key ID with bounded
connection, statement, and lock timeouts. It does not scan the quota or lease
tables. API startup performs the deeper physical counter reconciliation in one
PostgreSQL statement/snapshot before accepting traffic. A counter mismatch,
missing guard, or erasure-key mismatch fails startup; do not reset counters or
rotate the secret to clear it.

Production API readiness also requires the dedicated durable worker heartbeat
to be present and fresh. A missing, malformed, or stale worker heartbeat keeps
`/health/ready` at `503`; do not disable that requirement to reopen ingress.
The worker runs only the fixed `property_only` profile and is part of the
writer fleet, database fence, drain receipt, and contained cutover.

Migration 4 is also the scheduler delivery-safety boundary. Morning memo and
assistant-nudge sends are inserted under a stable daily idempotency key before
provider dispatch. A scheduler replica must atomically own the row lease and
record `dispatching` before making the outbound call. Email recovery reuses the
same provider idempotency key after a lease expires. Telegram does not expose a
provider idempotency key, so an expired `dispatching` Telegram row is moved to
`dead_lettered` for reconciliation instead of being sent again. This favors a
visible missed delivery over a duplicate message when the provider outcome is
unknown.

Migration 5 is the Property Content Studio durability boundary. Content jobs,
their ordered append-only events, and Subscribr provider event IDs live in
PostgreSQL in production. Stable job/provider idempotency keys and transaction
advisory locks make claims replica-safe; row leases permit recovery after a
worker crash, while stale owners cannot update a recovered claim. A replayed
provider event with the same ID but different canonical payload hash fails closed for
operator investigation. If a worker crashes after provider dispatch begins,
the job moves to `PROVIDER_RECONCILIATION_REQUIRED` rather than repeating an
external request whose outcome is unknown.

Memory mode and development without `DATABASE_URL` remain database-free.
The development JSON compatibility ledger uses cross-process locking, fsync,
and atomic replace; malformed data is preserved and raises a bounded
corruption error instead of being reset to an empty ledger.
Development PostgreSQL is also check-only: run the migration command against a
disposable development database first. Set
`PROPERTYQUARRY_SEARCH_SCHEMA_READINESS_REQUIRED=1` to exercise the production
readiness gate in development.

## Incident boundary

- `migration_ledger_missing` or `migration_pending`: keep traffic contained and
  require the installed controller to reconcile the fence and execute a newly
  authorized migration against the server-identified target.
- `property_search_migration_checksum_drift`: restore the exact released
  migration source and investigate; do not update the stored checksum.
- `property_search_schema_ahead`: deploy compatible application code; do not
  delete future ledger rows.
- `required_relation_missing`: treat it as schema damage. Preserve evidence and
  follow the database recovery runbook rather than recreating objects from an
  application process.

Migrations are additive and have no automatic down path. Release rollback must
use the guarded rollback procedure and a version known to tolerate the current
schema.
