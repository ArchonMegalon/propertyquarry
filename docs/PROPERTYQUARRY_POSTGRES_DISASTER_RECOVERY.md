# PropertyQuarry Postgres disaster recovery

`scripts/propertyquarry_postgres_dr.py` is the fail-closed backup, disposable restore-drill, and release-evidence lane. Version 3 receipts bind one immutable evidence chain: full release Git SHA, web-image SHA-256 identity, one exported repeatable-read source snapshot shared by `pg_dump` and every source-evidence query, exact source migration names/checksums, the schema-version-governed critical-table set and its bounded Merkle evidence, encrypted off-host object version, provider-native retrieval of that exact version, and the exact restored migration and data evidence. Older or incomplete receipts have no launch authority.

The script never creates a target database. An operator must provision an empty disposable database first, with a name beginning `propertyquarry_restore_drill_`.

## Backup

Provide database URLs through the environment or an operator secret store; do not put credentials in receipts or shell history.

```bash
export PROPERTYQUARRY_BACKUP_DATABASE_URL='postgresql://...'
export PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT='<recovery-public-key-fingerprint>'
export PROPERTYQUARRY_RELEASE_COMMIT_SHA='<full-40-character-release-sha>'
export PROPERTYQUARRY_RELEASE_IMAGE_DIGEST='sha256:<64-hex-web-image-identity>'
export AWS_REGION='eu-central-1'
export PROPERTYQUARRY_DR_S3_BUCKET='<approved-off-host-bucket>'
export PROPERTYQUARRY_DR_S3_KEY_PREFIX='prod/propertyquarry'
export PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS='30'
export PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_ENV_KEYS='PROPERTYQUARRY_DR_S3_BUCKET,PROPERTYQUARRY_DR_S3_KEY_PREFIX,PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS'
export PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_COMMAND='/opt/propertyquarry/scripts/propertyquarry_s3_backup_verify.py'
python3 scripts/propertyquarry_postgres_dr.py backup \
  --artifact state/private_backups/propertyquarry-YYYYMMDDTHHMMSS.dump.gpg \
  --receipt state/private_backups/propertyquarry-YYYYMMDDTHHMMSS.backup.json
```

The command opens a read-only `REPEATABLE READ` transaction, exports its MVCC snapshot, keeps the exporting transaction alive, and passes that exact snapshot to `pg_dump`, the migration-ledger queries, and the row-bound preflight plus Merkle query for every schema-governed critical table. The validated source ledger selects the table set: versions before v15 use the six preserved legacy tables; v15-v16 require the seventh `property_account_privacy_requests` relation with canonical identity `(principal_key, request_id)`; and v17 additionally governs quota buckets, leases, and the two-row capacity state. Schema-v17 evidence fails closed above 1,000,000 quota rows or 100,000 lease rows, or unless capacity state has exactly two rows. A pre-v15 privacy relation may be absent or may already exist, but it is outside that prefix's governed set and is not queried; at v15 or later, a missing privacy relation makes the backup fail closed. Admission relations are likewise outside pre-v17 prefixes but mandatory at v17. The receipt records the governing schema version, table-set version, privacy and admission-capacity requirements, exact ordered table contracts, contract fingerprint, and snapshot-bound query count. It also stores SHA-256 identities for the exported snapshot and a recomputable binding to the plaintext dump SHA-256 rather than treating separately timed reads as one source state. After all snapshot consumers finish, the command validates the custom-format dump with `pg_restore --list`, encrypts it with GPG, and sets artifact and receipt permissions to `0600`. A pre-migration database is recorded honestly as a valid ordered prefix (including an explicitly absent ledger at version 0); it is never relabeled as current. `EA_RUNTIME_MODE=prod` fails closed unless encryption, a full release SHA, an immutable image digest, and a remote verifier command are configured.

Migration-ledger reads and critical-data contract v4 reference only explicitly quoted `"public"."table"` relations; critical identity columns are quoted as well. Before any canonical JSON serialization, sort, or hash, each governed table gets a cheap `SELECT 1 ... LIMIT 67,108,865` count preflight. An over-limit result raises `critical_data_scale_bound_exceeded` and the full Merkle query is never executed. When the preflight passes, the full query independently applies `LIMIT 67,108,864` in its first materialized source CTE, and its observed row count must equal the preflight result. Both queries import the same exported snapshot, so the bound proves the exact source view that is hashed rather than a separately timed estimate.

Within that proven bound, each canonical JSON row is capped at 4 MiB and hashed independently. Rows sort by the release-controlled primary identity columns (text identities use `COLLATE "C"`) followed by the canonical row digest as a deterministic tie-breaker. At most 1,024 ordered row hashes enter one bounded chunk aggregate. The database-side chunk set and receipt are both capped at 65,536 chunks (67,108,864 rows per table). The receipt carries contiguous chunk counts, per-chunk row and maximum-row-size evidence, chunk hashes, and a recomputable domain-separated Merkle root.

Copy the encrypted artifact to independently retained off-host storage before the verifier returns. The release-provided `scripts/propertyquarry_s3_backup_verify.py` is the approved verifier implementation. Install it and `propertyquarry_postgres_dr.py` together in a root-owned release tree that also contains the tracked `config/propertyquarry/aws_cli_release_pin.json`; keep the verifier canonical, non-symlinked, and executable without arguments. The bucket must have S3 Versioning and Object Lock enabled before use. The configured retention must be 30-3,650 days, and the helper uploads with `AES256`, Object Lock `COMPLIANCE`, and a content-derived object key.

The helper receives these artifact variables from the backup process:

- `PROPERTYQUARRY_BACKUP_ARTIFACT_PATH`
- `PROPERTYQUARRY_BACKUP_ARTIFACT_SHA256`
- `PROPERTYQUARRY_BACKUP_ARTIFACT_SIZE_BYTES`
- `PROPERTYQUARRY_BACKUP_ARTIFACT_ENCRYPTED`

Its provider configuration comes from `AWS_REGION` (or `AWS_DEFAULT_REGION`), `PROPERTYQUARRY_DR_S3_BUCKET`, `PROPERTYQUARRY_DR_S3_KEY_PREFIX`, and `PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS`. The three `PROPERTYQUARRY_DR_S3_*` names are deliberately forwarded through `PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_ENV_KEYS`; region and direct AWS credential variables already belong to the fixed provider allowlist. Supply short-lived direct credentials through the operator secret store; never commit them. The manifest-pinned AWS CLI is attested by canonical path, exact version, SHA-256, owner, mode, link count, and opened-file identity before use. Configured endpoint overrides, profile files, metadata-service credentials, caller `PATH`, noncanonical provider identities or prefixes, plaintext artifacts, and mutable or unversioned objects fail closed.

The helper uploads the exact opened artifact descriptor, performs provider-native `head-object`, downloads that exact version with its ETag precondition, and independently streams the downloaded descriptor through SHA-256. Both provider responses must preserve the exact version, ETag, length, checksum, `AES256`, digest metadata, `COMPLIANCE` mode, and the requested retain-until time. Its final stdout line must be one JSON object containing `provider=s3`, `backend=aws_s3api`, AWS `region`, `bucket`, `object_key`, immutable `version_id`, hexadecimal `etag`, `sha256`, `size_bytes`, `encrypted=true`, `off_host=true`, `object_exists=true`, `checksum_verified=true`, `immutability_verified=true`, `object_lock_mode=COMPLIANCE`, `object_lock_retain_until`, both provider request identities, `verified_at`, and `verification_method=aws_s3api_head_and_get_object_version_sha256_v1`. At backup-receipt validation time, retention must still extend at least seven days into the future. A local/file provider, local-looking bucket, invalid region, path/traversal key, `latest`, unversioned object, generic verification method, hand-written assertion, or upload without read-back verification does not qualify. Add any other required secret by naming its environment key in `PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_ENV_KEYS`; hook argv is forbidden rather than heuristically inspected.

A receipt without its exact immutable off-host object version is not a recoverable launch backup.

## Disposable restore drill

Provision an empty local or isolated drill database. Never point the target variable at a live database. Remote targets additionally require `PROPERTYQUARRY_RESTORE_ALLOW_REMOTE_TARGET=1`.

```bash
export PROPERTYQUARRY_RESTORE_DATABASE_URL='postgresql://.../propertyquarry_restore_drill_YYYYMMDD'
export PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM='YES_DESTROY_DISPOSABLE_TARGET'
export PROPERTYQUARRY_BACKUP_MAX_AGE_SECONDS=86400
export PROPERTYQUARRY_RESTORE_MAX_DURATION_SECONDS=1800
export PROPERTYQUARRY_RESTORE_REQUIRED_TABLES='execution_sessions,artifacts'
export PROPERTYQUARRY_RESTORE_REQUIRED_NON_EMPTY_TABLES='artifacts'
export PROPERTYQUARRY_RESTORE_INTEGRITY_SQL='<release-specific scalar integrity query>'
export PROPERTYQUARRY_RESTORE_INTEGRITY_EXPECTED_VALUE='1'
export AWS_REGION='<approved-s3-bucket-region>'
export PROPERTYQUARRY_RELEASE_COMMIT_SHA='<same-full-release-sha>'
export PROPERTYQUARRY_RELEASE_IMAGE_DIGEST='sha256:<same-web-image-identity>'
# Set these to canonical absolute, argument-free executables that use DATABASE_URL from the drill process.
# export PROPERTYQUARRY_RESTORE_MIGRATION_COMMAND='/opt/propertyquarry/bin/migrate-restored-schema'
# export PROPERTYQUARRY_RESTORE_VERIFY_COMMAND='/opt/propertyquarry/bin/verify-restored-data'
# export PROPERTYQUARRY_RESTORE_READINESS_COMMAND='/opt/propertyquarry/bin/probe-restored-readiness'
install -d -m 0700 state/private_restore_drills
python3 scripts/propertyquarry_postgres_dr.py restore-drill \
  --artifact state/private_restore_drills/propertyquarry-YYYYMMDDTHHMMSS.retrieved.dump.gpg \
  --backup-receipt state/private_backups/propertyquarry-YYYYMMDDTHHMMSS.backup.json \
  --receipt state/private_backups/propertyquarry-YYYYMMDDTHHMMSS.restore-drill.json
```

Before a recovery proof can run, a reviewer must replace the explicit `UNCONFIGURED` sentinel in the tracked [`config/propertyquarry/aws_cli_release_pin.json`](../config/propertyquarry/aws_cli_release_pin.json) manifest with `status: CONFIGURED`, the canonical absolute non-symlink AWS CLI path, its exact semantic version, and its 64-hex SHA-256, then commit that reviewed manifest with the release. The restore drill and release gate read only that fixed repository path. AWS CLI path, version, and SHA selection through environment variables, command-line arguments, `PATH`, `which`, or arbitrary caller parameters is forbidden. The exact raw manifest blob SHA-256 is included in the AWS CLI attestation and therefore bound to the restore receipt, candidate release identity, and release-gate receipt. A missing, changed, malformed, partially configured, or still-`UNCONFIGURED` manifest fails closed.

`--artifact` is a new local retrieval destination and must not already exist. Its canonical parent directory must be process-owned mode `0700`; the destination is created mode `0600` with exclusive and no-follow flags. The drill never accepts an existing local file as recovery proof. Retrieval opens the manifest-pinned AWS CLI regular file with no-follow semantics, requires trusted ownership, one hard link, owner execute permission, and no group/world write or set-ID bits, hashes the opened descriptor against the manifest SHA-256, and executes that same descriptor. The reported semantic version must exactly match the manifest version. Device, inode, mtime, size, mode, owner, exact version, executable SHA-256, fixed manifest path, and raw manifest SHA-256 are rechecked or compared around provider calls and bound into the restore and release-gate receipts.

The attested CLI issues `s3api head-object` followed by `s3api get-object` for the receipt-bound region, bucket, key, version ID, and balanced-quote-normalized ETag. The endpoint is constructed as the official regional AWS S3 HTTPS endpoint; custom retrieval commands, binary overrides, configured endpoint overrides, local providers, and local-file fallbacks are forbidden. The CLI writes through the already-open destination descriptor; path or inode replacement is rejected before any result can pass. Both provider responses must independently match the immutable version, ETag, content length, exact `AES256` server-side encryption, `COMPLIANCE` mode, receipt-bound retention, and provider request identity. The process independently hashes and sizes that descriptor, passes the same still-open descriptor to GPG, and keeps it open through plaintext checksum and `pg_restore --list` archive validation. It never reopens the retrieved input by pathname after validation, so a later name or symlink swap cannot substitute bytes into GPG. Only a fixed minimal environment plus allowlisted direct AWS credentials, region, and certificate variables reaches provider commands; database, `HOME`, operator `PATH`, profile/config files, and unrelated application secrets do not.

Only configure hook executables installed in the active operator environment. Each hook must be a canonical absolute, non-symlink, trusted-owner regular file with owner execute permission, no group/world write or set-ID bits, and no arguments. Migration, verification, and readiness hooks receive the disposable target as `DATABASE_URL` and `PROPERTYQUARRY_RESTORE_DRILL=1`. They do not inherit the operator environment. If a hook needs another key, name it explicitly in `PROPERTYQUARRY_RESTORE_MIGRATION_ENV_KEYS`, `PROPERTYQUARRY_RESTORE_VERIFY_ENV_KEYS`, or `PROPERTYQUARRY_RESTORE_READINESS_ENV_KEYS`. Database URLs and fixed process-environment keys cannot be re-imported through those lists. Put secrets only in protected, explicitly named environment keys; all hook argv is rejected without heuristic classification.

The drill rejects checksum changes, missing or changed exported-snapshot evidence, missing release identity, a different release/image, missing or mutable off-host identity, failure to retrieve the claimed immutable provider version, stale backups, unencrypted artifacts, missing confirmation, source/target equality, non-disposable database names, unexpected connected database identity, empty restored schemas, migration-name/checksum drift, governed table-set or schema-version drift, critical-data row-count, chunk, row-bound, or Merkle-root drift, failed supplementary checks, failed hooks, and RTO overruns. The release-controlled contract requires retained data in `property_search_runs`. It allows legitimate zero rows in `property_search_work_jobs`, `delivery_outbox`, the content job/event/webhook ledgers, and—for schema v15 or later—`property_account_privacy_requests`. For schema v17 or later it also allows zero quota-bucket and lease rows, but requires both admission relations and exactly two rows in `propertyquarry_admission_capacity_state`: `quota` with limit `1000000` and `lease` with limit `100000`, with each restored count in bounds. Zero rows in an optional ledger are valid; absence of a relation required by the governing schema is not. `PROPERTYQUARRY_RESTORE_REQUIRED_TABLES`, `PROPERTYQUARRY_RESTORE_REQUIRED_NON_EMPTY_TABLES`, and the scalar integrity SQL remain optional operator diagnostics; changing them cannot replace or weaken the canonical data contract and they have no independent launch authority. After `pg_restore`, the release-specific migration hook upgrades the disposable target to the current release. The drill then re-queries exactly the source-ledger-governed table set, even when a preserved older source has been migrated forward, and requires every canonical count, chunk list, and recomputed Merkle root to match the snapshot-bound source. This preserves honest prefix restores without permitting an operator to add, remove, or relabel governed tables. `pg_restore` uses `--clean --if-exists --single-transaction` against the guarded target and never uses `--create`. RTO timing begins before provider retrieval and therefore includes retrieval, decryption, archive validation, restore, migrations, data checks, and readiness verification.

A production host recovery is a separate release-control operation. After the
database and one-shot migration phase succeed, it restores the API, dedicated
durable worker, scheduler, and render dependency in the canonical order and
keeps ingress closed until API readiness proves a fresh worker heartbeat.
Database restore-drill evidence alone does not authorize those runtime starts
or traffic.

## Launch evidence

A flagship release requires:

- a recent passing encrypted backup receipt and provider-verified immutable off-host object version;
- a passing disposable restore receipt whose provider request, exact version identity, and independently verified downloaded bytes prove recovery from that off-host object rather than a local copy;
- an AWS CLI attestation matching the tracked canonical pin manifest, including its fixed repository path and exact raw blob SHA-256, plus the approved executable path, semantic version, SHA-256, ownership/mode, and runtime inode identity;
- the same full Git SHA and web-image SHA-256 identity in backup, restore, and candidate release;
- an honest source-ledger prefix in backup and restore receipts, plus the exact current ordered migration versions, names, checksums, and fingerprint after the candidate migration hook;
- `rpo_met=true` and `rto_met=true`;
- exported-snapshot contract version 2 and the exact hashed snapshot identity in backup and restore receipts, proving `pg_dump`, schema evidence, and both critical-data queries for every source-ledger-governed table shared one live MVCC snapshot;
- critical-data contract/evidence version 4, the governing schema version, exact table-set version and ordered table names, privacy- and admission-capacity-relation requirements, fixed bounds, and contract fingerprint, plus exact source/restored row and chunk counts and recomputed Merkle roots; for v17+ the capacity relation must contain exactly two in-bounds rows with the fixed quota and lease limits, optional ledgers may be empty, and `property_search_runs` must contain retained data;
- migration, verification, and readiness-hook evidence appropriate to the release (operator-defined table and scalar SQL checks are supplementary only);
- operator review that the receipt contains the expected source and disposable target identities.

Host recovery must restore the complete pinned steady-state role set, including
`propertyquarry-worker`, only after the one-shot migration exits successfully.
The installed controller must wait for the worker's fail-closed heartbeat
health before reopening ingress. Recovery or migration containment that omits
the worker is not valid evidence because it can leave a durable queue claimant
running across DDL or leave accepted search work without a consumer.

Run the release gate with the exact candidate identity:

```bash
export PROPERTYQUARRY_DR_BACKUP_RECEIPT='state/private_backups/propertyquarry.backup.json'
export PROPERTYQUARRY_DR_RESTORE_RECEIPT='state/private_backups/propertyquarry.restore-drill.json'
export PROPERTYQUARRY_RELEASE_COMMIT_SHA='<full-40-character-release-sha>'
export PROPERTYQUARRY_RELEASE_IMAGE_DIGEST='sha256:<64-hex-web-image-identity>'
python3 scripts/propertyquarry_postgres_dr.py release-gate \
  --backup-receipt "$PROPERTYQUARRY_DR_BACKUP_RECEIPT" \
  --restore-receipt "$PROPERTYQUARRY_DR_RESTORE_RECEIPT" \
  --release-commit-sha "$PROPERTYQUARRY_RELEASE_COMMIT_SHA" \
  --image-digest "$PROPERTYQUARRY_RELEASE_IMAGE_DIGEST" \
  --receipt _completion/disaster_recovery/release-gate.json
```

`scripts/property_release_gates.sh` and every production `scripts/deploy_propertyquarry.sh --preflight-only` run this check and fail when any input is missing, stale, or mismatched. A real production deploy repeats it after building and requires the built web image ID or repository digest to equal `PROPERTYQUARRY_RELEASE_IMAGE_DIGEST` before starting the database or migration service. Non-production deploys can opt into the same boundary with `PROPERTYQUARRY_REQUIRE_DR_RELEASE_EVIDENCE=1`; they never carry launch authority when it is disabled. The default evidence age limit is 24 hours; set `PROPERTYQUARRY_DR_RELEASE_MAX_AGE_SECONDS` only to an explicitly reviewed tighter operational value.

Delete the disposable database after retaining the receipt. Never delete the encrypted backup as part of the drill.
