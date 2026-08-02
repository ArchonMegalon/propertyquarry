# PropertyQuarry Schema Migrations

Property-search database changes are deploy operations, not application
startup behavior. API, worker, and scheduler processes only verify the schema
ledger and required relations; they never create or alter the run, durable
queue, source-cache, delivery-outbox, property-content ledger, or account
privacy-lifecycle schema.

The same boundary now covers the shared EA kernel schema used by runtime
repositories. `app.kernel_schema` owns the checked-in `ea/schema/*.sql`
manifest (v0.2 through v0.38), records immutable checksums in
`ea_kernel_schema_migrations`, and verifies every declared table after the
transaction commits. Production repository constructors are read/write only;
they never use `CREATE`, `ALTER`, or `DROP` as a startup fallback.

Database identities and post-migration grants are governed by
[`PROPERTYQUARRY_DATABASE_ROLE_BOUNDARIES.md`](PROPERTYQUARRY_DATABASE_ROLE_BOUNDARIES.md).
The one-shot migration DSN is never a runtime fallback. Production API
admission readiness uses a separately named, standalone two-table DSN rather
than probing admission state with the API's general runtime credential.

## Versioned contract

`app.product.propertyquarry_schema` is the one-shot combined deploy gate. It
applies and verifies the EA kernel manifest first, then applies and verifies
the property-search manifest below. A kernel failure prevents property-search
migration, and either failure leaves API, worker, scheduler, and render
services blocked by Compose's `service_completed_successfully` dependency.

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
15. `durable_property_account_privacy_lifecycle`
16. `distributed_request_admission_control`
17. `bounded_admission_capacity_state`
18. `nonpublishing_work_lease_heartbeat`
19. `bounded_storage_retention_control`

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

The closed writer trust root is
`config/release/propertyquarry_deploy_writer_topology.v1.json`, authenticated by
the compiled canonical SHA-256 pin in
`scripts/propertyquarry_deploy_writer_topology.py`. API, durable worker,
scheduler, render bridge, and migrator are database writers. The render bridge
persists distributed admission leases and quotas even when optional generation
is idle. No classified writer may therefore be omitted from drain, immediate
pre-DDL inventory revalidation, crash containment, pre-commit restoration, or
post-commit hold. A topology edit without an intentional pin rotation fails
closed.

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

### Mandatory contained cutover for schema v11

Writer contract 3 and schema v11 are deliberately not rolling-compatible.
Current schema v19 remains incompatible with contract-2 processes. The
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
   and v11 in the same migration transaction, then continues through v12-v19
   before that transaction commits; partial application is forbidden.
   The migration runner sets contract 2 only inside that transaction while v11
   backfills legacy principal keys, before v11 replaces the write guards with
   contract 3. This is migration authority, never runtime writer authority.
   Schema v13 replaces the polymorphic property-content trigger so event
   rows without a `row_json` column are validated without unsafe field access.
   Schema v14 splits the research-packet erasure guards, and schema v15 adopts
   or creates the checked, checksummed privacy-request/tombstone store. Runtime
   roles have no DDL authority for that store. Schema v16 adds the distributed
   request-quota and concurrency-lease state used by API and render admission.
   Schema v17 locks both admission tables, refuses migration above 1,000,000
   quota rows or 100,000 lease rows, initializes the exact two-row capacity
   ledger from locked counts, and installs canonical statement triggers for
   `INSERT`, `DELETE`, and `TRUNCATE`. The administration plane must first
   provision the dedicated `propertyquarry_admission_capacity_owner` `NOLOGIN`
   role and grant it to `propertyquarry_migration` exactly as specified in the
   role-boundary runbook; v17 rejects a missing, login-capable, elevated, or
   unrelated-role-member owner.
   Schema v18 permits nonpublishing lease-heartbeat updates without starving
   erasure-ordered publication. Schema v19 installs the bounded operator
   retention journal and its single-running-run fence.
4. Schema v11 first established the homogeneous schema-v11/contract-3 fleet
   boundary. For current schema v19, start only the immutable, homogeneous
   schema-v19/contract-3 fleet. Require current readiness plus fresh per-instance
   heartbeats for the complete expected role manifest before reopening ingress.

Never start current contract-3 code before every migration through v19 commits,
and never restart a contract-2 binary after v11. A failed step leaves ingress and writers contained for
forward repair. Changing the erasure secret is a separately designed key
migration, not an environment-variable rotation; without that migration the
database intentionally fails closed.

### Legacy research-packet index activation

The research-packet index remains held until the new immutable web image is
running as one coordinated writer fleet and the controller has completed all
three phases below. A source checkout, an old image, a partial writer restart,
or a backfill receipt alone cannot authorize activation or traffic.

While ingress remains contained, the installed controller must derive the
complete `api`, `worker`, and `scheduler` instance manifest from its pinned
topology and record the coordinated restart boundary. It then executes the
packaged checker inside the new database-authorized web image:

```text
/usr/local/bin/python /app/scripts/check_property_search_storage_schema.py \
  --require-live-db --phase pre-backfill \
  --heartbeat-dir /data/artifacts/propertyquarry-writer-heartbeats \
  --expected-writers <controller-derived-role-instance-manifest> \
  --not-before <controller-recorded-coordinated-restart-boundary> \
  --heartbeat-max-age-seconds 120 \
  --fleet-proof-path /data/artifacts/property-research-packet-fleet-proof.json
```

Every expected instance must have a fresh, role-correct heartbeat from the new
image. The worker heartbeat includes the closed
`property_search_work_queue` observation contract; the scheduler heartbeat
includes the closed `delivery_outbox` contract. The proof must be a private
mode-`0600` file and the controller records its canonical SHA-256.

Only a successful proof permits the controller to execute the bounded apply:

```text
/usr/local/bin/python /app/scripts/backfill_property_research_packet_links.py \
  --apply --batch-size 25 --max-batches 0 --max-batch-bytes 33554432 \
  --checkpoint-path /data/artifacts/property-research-packet-backfill.checkpoint.json \
  --receipt-path /data/artifacts/property-research-packet-backfill.receipt.json \
  --fleet-proof-path /data/artifacts/property-research-packet-fleet-proof.json
```

The index remains held unless that command exits zero and the private receipt
reports `status=complete`, `coverage_complete=true`, `scan_complete=true`, zero
compact-only rows and failures, exact source/scan and expected/verified count
equality, verified identity digest and link coverage, zero orphan counts, and a
fleet-proof SHA-256 equal to the pre-backfill proof. Partial, resumable, failed,
a proof expired before apply, or mismatched evidence leaves ingress contained.

Finally, with the identical manifest, restart boundary, heartbeat directory,
and proof path, the controller reruns the first checker command with
`--phase activate`. Only an exit-zero `status=activation_ready` receipt whose
counts and proof hash match the completed index state permits the controller to
continue release validation and reopen ingress. Direct invocation from the
checkout remains forbidden for production.

## Disposable development and test targets

The standalone Compose topology includes a one-shot
`propertyquarry-migrate` service and a long-lived property-only
`propertyquarry-worker` that consumes `property_search_work_jobs`. It may be
used directly only against a
disposable local development database whose credentials and containers have no
production reach:

```bash
EA_RUNTIME_MODE=dev docker compose \
  --env-file /run/user/$(id -u)/propertyquarry-disposable-compose.env \
  -f docker-compose.property.yml up -d --build
```

For the disposable test shorthand only, after the same mode-`0600` environment
has already been loaded into the shell, the canonical source-contract phrase is:

```bash
docker compose -f docker-compose.property.yml up -d --build
```

It remains forbidden against any database or container set with production
reach.

Create that mode-`0600` file outside the checkout. It must provide
`POSTGRES_PASSWORD`, `EA_SIGNING_SECRET`,
`PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN`, and the six scoped
`PROPERTYQUARRY_{API,API_ADMISSION,WORKER,SCHEDULER,RENDER,MIGRATION}_DATABASE_URL`
values. Use the role boundaries in the linked database-role runbook even for a
disposable full-topology rehearsal. The API admission value must use a distinct
login and must not equal the API runtime DSN. Do not put any of those values on
the command line or in shell history.

For an explicitly disposable, separately orchestrated development database,
run the same combined migration boundary before starting any application role:

```bash
PYTHONPATH=ea DATABASE_URL='<private-disposable-development-dsn>' \
  python3 -m app.product.propertyquarry_schema migrate
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

To probe both ledgers and their required relations in a disposable or
controller-contained environment, use:

```bash
PYTHONPATH=ea DATABASE_URL='<private-contained-dsn>' \
  python3 -m app.product.propertyquarry_schema check
```

## Runtime readiness

Production API, worker, and scheduler roles require the current schema
version. `/health/ready` returns `503` with a bounded
`property_search_schema_not_ready:<reason>` until the ledger, checksums,
versions, tables, and indexes pass. Application repositories enforce the same
read-only boundary before issuing run or queue queries.

The API also requires the shared worker heartbeat in production. The worker
healthcheck validates a role-correct, positive-PID heartbeat no older than the
configured 120-second ceiling. The heartbeat lives on the shared artifacts
volume, while the worker itself is read-only apart from bounded tmpfs and its
explicit persistent volumes. Missing, stale, malformed, or wrong-role evidence
makes the worker unhealthy and keeps API readiness closed.

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

Migration 15 is the account privacy-lifecycle durability boundary. Production
privacy-request reads and writes use PostgreSQL and propagate connection,
schema, and statement failures; they never continue against process memory.
The digest-only retention tombstone remains inside the governed request payload,
so request state and the backup-restoration block travel together. A legacy
table created by older runtime code is adopted only when its column and primary
key shape match the release contract; unknown shapes fail the migration.

Migration 17 is the admission-cardinality durability boundary. Its capacity
state has exactly `quota` and `lease` rows with immutable hard limits of
1,000,000 and 100,000 respectively. `AFTER ... FOR EACH STATEMENT` triggers use
transition tables so bulk and concurrent inserts/deletes serialize through the
corresponding capacity row; an over-limit insert raises SQLSTATE `54000` and
rolls the entire statement back. `TRUNCATE` resets only the matching counter
while PostgreSQL holds the table's access-exclusive lock. Runtime admission
roles cannot alter or disable these triggers, mutate capacity state, execute
their functions, create schema objects, bypass row security, or assume another
role. The three functions are `SECURITY DEFINER`, have
`search_path=pg_catalog`, and are owned by the dedicated
`propertyquarry_admission_capacity_owner` `NOLOGIN` role. The runtime probe
accepts only those exact functions, bodies, ACLs, owners, grants, trigger
arguments, event bits, and transition-table aliases; extra or drifted catalog
state keeps readiness closed.

DSAR export v2.1 does not claim a materialized cross-collection transaction
snapshot. Its signed cursor binds a canonical fingerprint of the complete
observed item set, and a mutation between pages fails closed as
`privacy_export_snapshot_changed`. Every response declares
`bounded_incomplete`, publishes the source-query limits, and leaves
`pagination.complete=false`; exhausting the observed page sequence is reported
separately from complete subject-data coverage.

Explicit `EA_STORAGE_BACKEND=memory` development and test mode remains
database-free. An implicit `auto` backend without `DATABASE_URL` is not a
privacy-lifecycle store, and memory is forbidden in production.
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
- `propertyquarry_admission_*_capacity_exceeded`: keep writers contained,
  preserve the failed v17 transaction, expire or compact admission state under
  the incident plan, and rerun the newly authorized migration. Never raise a
  limit or hand-edit the migration ledger.
- `admission_backend_capacity_*_drift`: treat the capacity table, canonical
  triggers/functions, owner, or grants as damaged authority. Keep ingress
  closed and restore the released v17 catalog contract through the controller.

Migrations are additive and have no automatic down path. Release rollback must
use the guarded rollback procedure and a version known to tolerate the current
schema.
