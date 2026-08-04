# PropertyQuarry bounded-storage contract

PropertyQuarry treats storage growth as admission and lifecycle control, not as
an occasional broad delete. The safe default is observable dry-run behavior;
mutation is allowlisted, journaled, bounded, resumable, and owner-operated.

## Invariants

- Durable business records, privacy tombstones, packet evidence, and explicit
  legal holds are never part of generic TTL deletion.
- A legal hold wins over a tenant quota. If no eligible compact row can be
  evicted, the new write is rejected with backpressure instead of deleting held
  or active material. Age compaction also skips any run represented by a held
  packet membership.
- Search-run payloads are capped at 512 KiB by default. Terminal runs compact
  immediately; oversized active runs persist a bounded recovery projection.
  Independently checksummed packet rows are projected before payload bounding.
- Each principal retains at most 500 search runs by default. A new run compacts
  and evicts only the oldest terminal, non-held rows in batches. Legacy
  over-limit tenants converge without increasing their row count.
- Generic observation payloads are capped at 512 KiB. Larger observations must
  provide `raw_payload_uri`; PostgreSQL then stores only a SHA-256/size/pointer
  envelope. The property-scout completion event is a bounded projection rather
  than a duplicate full search result.
- Text artifacts are capped at 16 MiB per write and production writers stop
  growing the artifact volume below 10 GiB free. Shrinking an existing artifact
  remains possible so an operator can recover from low-water state.
- The reconstructible map-preview cache is pruned oldest-first to both 2,048
  entries and 256 MiB. Neither limit applies to durable artifact records.
- Generic DB retention accepts only seven reviewed runtime tables. Apply mode is
  recorded in `propertyquarry_retention_runs`, uses `SKIP LOCKED` batches and
  per-table ceilings, and never invokes `VACUUM FULL`.
- Workspace access sessions are not audit authority. Revoked/expired rows and
  active rows whose parseable expiry is past the configured grace window are
  eligible after 7 days by default; malformed or missing expiries fail closed.
- PostgreSQL physical space reclamation is a distinct maintenance operation.
  Ordinary deletes and compaction make pages reusable but do not promise a
  smaller relation file.

## Storage classes

| Class | Growth control | Deletion authority |
| --- | --- | --- |
| Search runs | payload bytes, per-principal rows, age compaction, batch size | runtime writer contract; legal holds excluded |
| Packet links/memberships | per-run projection; memberships age out in batches | writer contract; held links/memberships preserved |
| Observation/execution/policy events | payload admission plus reviewed TTL windows | owner-only retention command |
| Delivery/approval rows | terminal-status TTL only | owner-only retention command |
| Workspace access sessions | terminal/expired TTL after a bounded grace window | owner-only retention command; active future/unknown expiries preserved |
| Source listing cache | TTL plus fixed entry ceiling | cache writer; fully reconstructible |
| Map-preview cache | 2,048 entries and 256 MiB, oldest first | API cache writer; fully reconstructible |
| Durable text artifacts | 16 MiB/write plus 10 GiB free-space admission | no generic deletion; owner lifecycle only |
| Governed tours/media | numeric lifecycle, legal-hold contract, free-space admission, archive byte/entry caps | governed lifecycle store only |
| Docker logs | `json-file`, 10 MiB × 3 files per standalone service | Docker rotation |
| PostgreSQL WAL | `db_size.sh` high-water reporting and PostgreSQL checkpoints | PostgreSQL; no file deletion by scripts |
| Backups/DR/vendor tools | inventory and high-water reporting; immutable/recovery inputs | explicit backup/vendor lifecycle, never generic retention |
| Shared Docker build cache | reported as shared-host state | host operator only; PropertyQuarry never prunes it automatically |

## Operator commands

Target the standalone database explicitly. The scripts report the resolved
service, container, and database before doing any work and reuse an existing
container rather than trying to recreate it.

```bash
export PROPERTYQUARRY_DB_SERVICE=propertyquarry-db
export PROPERTYQUARRY_DB_CONTAINER_NAME=propertyquarry-db-live
export POSTGRES_DB=propertyquarry
export POSTGRES_USER=postgres

# Read-only diagnostics. Add EA_DB_SIZE_FAIL_ON_HIGH_WATER=1 for alerting.
bash scripts/db_size.sh
bash scripts/storage_size.sh

# Candidate counts only: no journal DDL and no data changes.
bash scripts/db_retention.sh

# Reviewed mutation; every run is journaled and batch/row limited.
bash scripts/db_retention.sh --apply
```

Useful controls are documented by each command's `--help`. Production defaults
include 500 delete rows per transaction, 10,000 rows per table/run, a 2-second
lock timeout, and a 30-second statement timeout. A ceiling hit is successful
partial progress and is reported for a later resumable run.

## Scheduling and alerts

Checked-in systemd units live under
`packaging/propertyquarry-storage-maintenance/systemd/`. Install them into the
host's systemd unit directory only through the normal host configuration lane,
then review `/etc/propertyquarry/storage-retention.env` and enable:

```bash
systemctl enable --now propertyquarry-storage-high-water.timer
systemctl enable --now propertyquarry-db-retention.timer
systemctl list-timers 'propertyquarry-*storage*' 'propertyquarry-*retention*'
```

The high-water check runs hourly and exits nonzero when configured thresholds
are crossed. Retention runs weekly with randomized delay. The runtime API,
worker, and scheduler remain non-owner processes and do not receive DDL or
generic retention authority.

## Physical reclaim after a historic growth incident

When diagnostics show a large TOAST allocation but low live payload bytes,
back up first, verify restore readiness and available scratch disk, contain
writers, and choose an exact relation. `VACUUM (ANALYZE)` only makes space
reusable. `VACUUM FULL` or `pg_repack` can return disk space but rewrites and/or
locks the relation; it is never run by the scheduled retention command.

The maintenance record must capture the target relation, pre/post sizes,
backup receipt, available bytes, expected lock window, rollback path, and the
retention journal run that removed or compacted the logical data. Do not use a
database-wide wildcard reclaim.

## Current inventory interpretation

The 2026-08-02 audit found that live PropertyQuarry growth was primarily TOAST
allocation from repeated large `property_search_runs` rewrites, followed by
duplicated `property_scout_sync_completed` observation payloads. Local state
and Docker also occupy material space, but include recovery backups, licensed
vendor applications, and governed public-tour assets. Those classes are
reported separately and intentionally excluded from generic automated
deletion.
