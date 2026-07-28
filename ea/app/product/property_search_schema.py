from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Callable, Sequence

SCHEMA_COMPONENT = "property_search"
SCHEMA_LEDGER_TABLE = "propertyquarry_schema_migrations"
SCHEMA_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"propertyquarry:property_search:migrations:v1").digest()[:8],
    byteorder="big",
    signed=True,
)


@dataclass(frozen=True)
class PropertySearchMigration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        payload = (
            f"{SCHEMA_COMPONENT}\0{self.version}\0{self.name}\0{self.sql.strip()}\n"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PropertySearchSchemaStatus:
    ready: bool
    reason: str
    current_version: int
    required_version: int
    applied_versions: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "component": SCHEMA_COMPONENT,
            "ready": self.ready,
            "reason": self.reason,
            "current_version": self.current_version,
            "required_version": self.required_version,
            "applied_versions": list(self.applied_versions),
        }


@dataclass(frozen=True)
class PropertySearchMigrationResult:
    previous_version: int
    current_version: int
    applied_versions: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "component": SCHEMA_COMPONENT,
            "previous_version": self.previous_version,
            "current_version": self.current_version,
            "applied_versions": list(self.applied_versions),
        }


class PropertySearchSchemaError(RuntimeError):
    """Base failure for the governed property-search schema boundary."""


class PropertySearchSchemaDriftError(PropertySearchSchemaError):
    """The migration ledger no longer matches the immutable source contract."""


class PropertySearchSchemaNotReadyError(PropertySearchSchemaError):
    """Runtime access was attempted before the deploy migration completed."""


_RUN_SCHEMA_V1 = r"""
CREATE TABLE IF NOT EXISTS property_search_runs (
    principal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT,
    compact_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (principal_id, run_id)
);

ALTER TABLE property_search_runs ADD COLUMN IF NOT EXISTS principal_id TEXT;
ALTER TABLE property_search_runs ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE property_search_runs ADD COLUMN IF NOT EXISTS payload_json JSONB DEFAULT '{}'::jsonb;
ALTER TABLE property_search_runs ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE property_search_runs ADD COLUMN IF NOT EXISTS compact_json JSONB DEFAULT '{}'::jsonb;
ALTER TABLE property_search_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE property_search_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

UPDATE property_search_runs
SET payload_json = '{}'::jsonb
WHERE payload_json IS NULL;

UPDATE property_search_runs
SET status = COALESCE(status, payload_json->>'status', payload_json#>>'{summary,status}'),
    compact_json = CASE
        WHEN compact_json IS NULL OR compact_json = '{}'::jsonb
        THEN jsonb_strip_nulls(payload_json)
        ELSE compact_json
    END,
    created_at = COALESCE(created_at, NOW()),
    updated_at = COALESCE(updated_at, created_at, NOW());

ALTER TABLE property_search_runs ALTER COLUMN payload_json SET DEFAULT '{}'::jsonb;
ALTER TABLE property_search_runs ALTER COLUMN payload_json SET NOT NULL;
ALTER TABLE property_search_runs ALTER COLUMN compact_json SET DEFAULT '{}'::jsonb;
ALTER TABLE property_search_runs ALTER COLUMN compact_json SET NOT NULL;
ALTER TABLE property_search_runs ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE property_search_runs ALTER COLUMN updated_at SET NOT NULL;

DO $property_search_runs_primary_key$
DECLARE
    primary_key_name TEXT;
    primary_key_columns TEXT[];
BEGIN
    SELECT constraint_row.conname,
           array_agg(attribute_row.attname ORDER BY key_column.ordinality)
    INTO primary_key_name, primary_key_columns
    FROM pg_constraint AS constraint_row
    JOIN unnest(constraint_row.conkey) WITH ORDINALITY
      AS key_column(attnum, ordinality) ON TRUE
    JOIN pg_attribute AS attribute_row
      ON attribute_row.attrelid = constraint_row.conrelid
     AND attribute_row.attnum = key_column.attnum
    WHERE constraint_row.conrelid = 'property_search_runs'::regclass
      AND constraint_row.contype = 'p'
    GROUP BY constraint_row.conname;

    IF primary_key_columns IS DISTINCT FROM ARRAY['principal_id', 'run_id']::TEXT[] THEN
        IF primary_key_name IS NOT NULL THEN
            EXECUTE format(
                'ALTER TABLE property_search_runs DROP CONSTRAINT %I',
                primary_key_name
            );
        END IF;
        DELETE FROM property_search_runs AS older
        USING property_search_runs AS newer
        WHERE older.ctid < newer.ctid
          AND older.principal_id = newer.principal_id
          AND older.run_id = newer.run_id;
        ALTER TABLE property_search_runs ALTER COLUMN principal_id SET NOT NULL;
        ALTER TABLE property_search_runs ALTER COLUMN run_id SET NOT NULL;
        ALTER TABLE property_search_runs
            ADD CONSTRAINT property_search_runs_pkey PRIMARY KEY (principal_id, run_id);
    END IF;
END
$property_search_runs_primary_key$;

CREATE INDEX IF NOT EXISTS idx_property_search_runs_updated
    ON property_search_runs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_property_search_runs_principal_updated
    ON property_search_runs(principal_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_property_search_runs_status_updated
    ON property_search_runs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_property_search_runs_principal_status_updated
    ON property_search_runs(principal_id, status, updated_at DESC);
"""


_WORK_QUEUE_SCHEMA_V2 = r"""
CREATE TABLE IF NOT EXISTS property_search_work_jobs (
    job_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_property_search_work_idempotency
    ON property_search_work_jobs(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_property_search_work_principal_run
    ON property_search_work_jobs(principal_id, run_id);
CREATE INDEX IF NOT EXISTS idx_property_search_work_claim
    ON property_search_work_jobs(status, available_at, lease_expires_at, created_at);

DO $property_search_work_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_search_work_jobs'::regclass
          AND conname = 'property_search_work_jobs_status_check'
    ) THEN
        ALTER TABLE property_search_work_jobs
            ADD CONSTRAINT property_search_work_jobs_status_check
            CHECK (status IN ('queued', 'leased', 'completed', 'failed'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_search_work_jobs'::regclass
          AND conname = 'property_search_work_jobs_attempt_count_check'
    ) THEN
        ALTER TABLE property_search_work_jobs
            ADD CONSTRAINT property_search_work_jobs_attempt_count_check
            CHECK (attempt_count >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_search_work_jobs'::regclass
          AND conname = 'property_search_work_jobs_max_attempts_check'
    ) THEN
        ALTER TABLE property_search_work_jobs
            ADD CONSTRAINT property_search_work_jobs_max_attempts_check
            CHECK (max_attempts >= 1);
    END IF;
END
$property_search_work_constraints$;
"""


_SOURCE_CACHE_SCHEMA_V3 = r"""
CREATE TABLE IF NOT EXISTS property_source_listing_cache (
    cache_key TEXT PRIMARY KEY,
    source_url TEXT NOT NULL DEFAULT '',
    listing_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
    provider_filter_pushdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    stored_at_epoch DOUBLE PRECISION NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_property_source_listing_cache_stored_at
    ON property_source_listing_cache(stored_at_epoch DESC);
"""


_DELIVERY_OUTBOX_SCHEMA_V4 = r"""
CREATE TABLE IF NOT EXISTS delivery_outbox (
    delivery_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NULL,
    last_error TEXT NOT NULL DEFAULT '',
    receipt_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    dead_lettered_at TIMESTAMPTZ NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TIMESTAMPTZ NULL,
    claimed_at TIMESTAMPTZ NULL,
    dispatch_started_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS principal_id TEXT NOT NULL DEFAULT '';
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NULL;
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS receipt_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ NULL;
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS lease_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL;
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ NULL;
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS dispatch_started_at TIMESTAMPTZ NULL;
ALTER TABLE delivery_outbox ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

UPDATE delivery_outbox
SET principal_id = COALESCE(NULLIF(metadata_json->>'principal_id', ''), principal_id, ''),
    updated_at = COALESCE(updated_at, sent_at, created_at, NOW())
WHERE COALESCE(principal_id, '') = '' OR updated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_delivery_outbox_status_created
    ON delivery_outbox(status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox_principal_idempotency_unique
    ON delivery_outbox(principal_id, idempotency_key)
    WHERE idempotency_key <> '';
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_retry_schedule
    ON delivery_outbox(status, next_attempt_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_principal_status_created
    ON delivery_outbox(principal_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_claim
    ON delivery_outbox(status, next_attempt_at, lease_expires_at, created_at, delivery_id);
"""


_PROPERTY_CONTENT_LEDGER_SCHEMA_V5 = r"""
CREATE TABLE IF NOT EXISTS property_content_jobs (
    packet_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    source_packet_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_packet_sha256 CHAR(64) NOT NULL,
    row_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    version BIGINT NOT NULL DEFAULT 1,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TIMESTAMPTZ NULL,
    claimed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (packet_id <> ''),
    CHECK (idempotency_key <> ''),
    CHECK (status <> ''),
    CHECK (source_packet_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (version >= 1)
);

CREATE TABLE IF NOT EXISTS property_content_job_events (
    event_sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    packet_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (event_id <> ''),
    CHECK (packet_id <> ''),
    CHECK (event_type <> ''),
    CHECK (status <> ''),
    CHECK (idempotency_key <> '')
);

CREATE TABLE IF NOT EXISTS property_content_webhook_events (
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    row_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    version BIGINT NOT NULL DEFAULT 1,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TIMESTAMPTZ NULL,
    claimed_at TIMESTAMPTZ NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    replayed_at TIMESTAMPTZ NULL,
    processed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, provider_event_id),
    CHECK (provider <> ''),
    CHECK (provider_event_id <> ''),
    CHECK (status <> ''),
    CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_property_content_jobs_status_updated
    ON property_content_jobs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_property_content_jobs_claim
    ON property_content_jobs(status, lease_expires_at, updated_at, packet_id);
CREATE INDEX IF NOT EXISTS idx_property_content_job_events_packet_sequence
    ON property_content_job_events(packet_id, event_sequence);
CREATE INDEX IF NOT EXISTS idx_property_content_webhook_status_updated
    ON property_content_webhook_events(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_property_content_webhook_claim
    ON property_content_webhook_events(status, lease_expires_at, received_at, provider_event_id);
"""


_RUN_DELIVERY_PROJECTION_SCHEMA_V6 = r"""
ALTER TABLE property_search_runs
    ADD COLUMN IF NOT EXISTS compact_schema_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE property_search_runs
    ADD COLUMN IF NOT EXISTS delivery_pending BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE property_search_runs
    ADD COLUMN IF NOT EXISTS delivery_checked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_property_search_runs_delivery_work_updated
    ON property_search_runs(
        status,
        compact_schema_version,
        delivery_checked_at ASC NULLS FIRST,
        updated_at ASC
    )
    WHERE delivery_pending OR compact_schema_version < 2;
CREATE INDEX IF NOT EXISTS idx_property_search_runs_principal_delivery_work_updated
    ON property_search_runs(
        principal_id,
        status,
        compact_schema_version,
        delivery_checked_at ASC NULLS FIRST,
        updated_at ASC
    )
    WHERE delivery_pending OR compact_schema_version < 2;
"""


_TENANT_SCOPED_OUTBOX_IDEMPOTENCY_SCHEMA_V7 = r"""
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox_principal_idempotency_unique
    ON delivery_outbox(principal_id, idempotency_key)
    WHERE idempotency_key <> '';

DROP INDEX IF EXISTS idx_delivery_outbox_idempotency_key_unique;
"""


_EVIDENCE_OVERLAY_READ_MODEL_SCHEMA_V8 = r"""
CREATE TABLE IF NOT EXISTS property_evidence_overlay_rollups (
    layer_key TEXT NOT NULL,
    record_key CHAR(64) NOT NULL,
    match_key TEXT NOT NULL,
    match_value TEXT NOT NULL,
    teable_table TEXT NOT NULL,
    teable_record_id TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    cache_updated_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    payload_json JSONB NOT NULL,
    PRIMARY KEY (layer_key, record_key, match_key, match_value),
    CHECK (layer_key <> ''),
    CHECK (record_key ~ '^[0-9a-f]{64}$'),
    CHECK (match_key <> ''),
    CHECK (match_value <> ''),
    CHECK (teable_table <> ''),
    CHECK (teable_record_id <> ''),
    CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_property_evidence_overlay_lookup
    ON property_evidence_overlay_rollups(
        match_key,
        match_value,
        layer_key,
        cache_updated_at DESC
    );
CREATE INDEX IF NOT EXISTS idx_property_evidence_overlay_freshness
    ON property_evidence_overlay_rollups(layer_key, cache_updated_at DESC);

CREATE TABLE IF NOT EXISTS property_evidence_overlay_snapshots (
    snapshot_id CHAR(64) PRIMARY KEY,
    source_schema TEXT NOT NULL,
    source_generated_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    candidate_sha CHAR(40) NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    table_counts_json JSONB NOT NULL,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
    CHECK (source_schema <> ''),
    CHECK (candidate_sha ~ '^[0-9a-f]{40}$'),
    CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (schema_version >= 1),
    CHECK (status IN ('pass', 'retired'))
);

CREATE INDEX IF NOT EXISTS idx_property_evidence_overlay_snapshots_ingested
    ON property_evidence_overlay_snapshots(ingested_at DESC);
"""


_EVIDENCE_OVERLAY_STAGED_ACTIVATION_SCHEMA_V9 = r"""
ALTER TABLE property_evidence_overlay_rollups
    ADD COLUMN IF NOT EXISTS snapshot_id CHAR(64);

ALTER TABLE property_evidence_overlay_snapshots
    ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE property_evidence_overlay_snapshots
    DROP CONSTRAINT IF EXISTS property_evidence_overlay_snapshots_status_check;

WITH ranked_snapshots AS (
    SELECT snapshot_id,
           ROW_NUMBER() OVER (ORDER BY ingested_at DESC, snapshot_id DESC) AS rank
    FROM property_evidence_overlay_snapshots
    WHERE status = 'pass'
)
UPDATE property_evidence_overlay_snapshots AS snapshots
SET status = CASE WHEN ranked.rank = 1 THEN 'active' ELSE 'retired' END,
    activated_at = CASE
        WHEN ranked.rank = 1 THEN COALESCE(snapshots.activated_at, snapshots.ingested_at)
        ELSE snapshots.activated_at
    END
FROM ranked_snapshots AS ranked
WHERE snapshots.snapshot_id = ranked.snapshot_id;

ALTER TABLE property_evidence_overlay_snapshots
    ADD CONSTRAINT property_evidence_overlay_snapshots_status_check
    CHECK (status IN ('staged', 'active', 'retired'));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM property_evidence_overlay_rollups
        WHERE snapshot_id IS NULL
    ) AND NOT EXISTS (
        SELECT 1
        FROM property_evidence_overlay_snapshots
        WHERE status = 'active'
    ) THEN
        RAISE EXCEPTION 'property evidence overlay rows have no authoritative snapshot';
    END IF;
END
$$;

UPDATE property_evidence_overlay_rollups
SET snapshot_id = (
    SELECT snapshot_id
    FROM property_evidence_overlay_snapshots
    WHERE status = 'active'
    ORDER BY ingested_at DESC, snapshot_id DESC
    LIMIT 1
)
WHERE snapshot_id IS NULL;

ALTER TABLE property_evidence_overlay_rollups
    ALTER COLUMN snapshot_id SET NOT NULL;
ALTER TABLE property_evidence_overlay_rollups
    ADD CONSTRAINT property_evidence_overlay_rollups_snapshot_id_check
    CHECK (snapshot_id ~ '^[0-9a-f]{64}$');
ALTER TABLE property_evidence_overlay_rollups
    DROP CONSTRAINT property_evidence_overlay_rollups_pkey;
ALTER TABLE property_evidence_overlay_rollups
    ADD CONSTRAINT property_evidence_overlay_rollups_pkey
    PRIMARY KEY (snapshot_id, layer_key, record_key, match_key, match_value);
ALTER TABLE property_evidence_overlay_rollups
    ADD CONSTRAINT property_evidence_overlay_rollups_snapshot_id_fkey
    FOREIGN KEY (snapshot_id)
    REFERENCES property_evidence_overlay_snapshots(snapshot_id)
    ON DELETE CASCADE;

CREATE TABLE IF NOT EXISTS property_evidence_overlay_active_snapshot (
    pointer_key TEXT PRIMARY KEY,
    snapshot_id CHAR(64) NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL,
    CHECK (pointer_key = 'active'),
    CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
    FOREIGN KEY (snapshot_id)
        REFERENCES property_evidence_overlay_snapshots(snapshot_id)
        ON DELETE RESTRICT
);

INSERT INTO property_evidence_overlay_active_snapshot (
    pointer_key, snapshot_id, activated_at
)
SELECT 'active', snapshot_id, COALESCE(activated_at, ingested_at)
FROM property_evidence_overlay_snapshots
WHERE status = 'active'
ORDER BY ingested_at DESC, snapshot_id DESC
LIMIT 1
ON CONFLICT (pointer_key) DO NOTHING;

CREATE UNIQUE INDEX IF NOT EXISTS idx_property_evidence_overlay_single_active
    ON property_evidence_overlay_snapshots ((status))
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_property_evidence_overlay_snapshot_lookup
    ON property_evidence_overlay_rollups(
        snapshot_id,
        match_key,
        match_value,
        layer_key,
        cache_updated_at DESC
    );
CREATE INDEX IF NOT EXISTS idx_property_evidence_overlay_snapshot_freshness
    ON property_evidence_overlay_rollups(
        snapshot_id,
        layer_key,
        cache_updated_at DESC
    );
"""


_PROPERTY_RESEARCH_PACKET_LINKS_SCHEMA_V10 = r"""
CREATE TABLE IF NOT EXISTS property_research_packet_links (
    principal_id TEXT NOT NULL,
    candidate_ref TEXT NOT NULL,
    candidate_ref_algorithm TEXT NOT NULL,
    packet_json JSONB NOT NULL,
    packet_canonical_json TEXT NOT NULL,
    packet_size_bytes INTEGER NOT NULL,
    packet_schema_version INTEGER NOT NULL,
    packet_sha256 CHAR(64) NOT NULL,
    property_url_sha256 CHAR(64),
    first_run_id TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    retention_state TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (principal_id, candidate_ref),
    CHECK (principal_id <> ''),
    CHECK (char_length(candidate_ref) BETWEEN 1 AND 256),
    CHECK (candidate_ref_algorithm IN ('explicit', 'derived_v1')),
    CHECK (jsonb_typeof(packet_json) = 'object'),
    CHECK (packet_json = packet_canonical_json::jsonb),
    CHECK (packet_size_bytes = octet_length(convert_to(packet_canonical_json, 'UTF8'))),
    CHECK (packet_size_bytes BETWEEN 2 AND 262144),
    CHECK (packet_schema_version >= 1),
    CHECK (packet_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (
        property_url_sha256 IS NULL
        OR property_url_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CHECK (char_length(first_run_id) BETWEEN 1 AND 256),
    CHECK (char_length(last_run_id) BETWEEN 1 AND 256),
    CHECK (last_seen_at >= first_seen_at),
    CHECK (
        retention_state IN (
            'active',
            'quarantined',
            'deletion_pending',
            'legal_hold'
        )
    )
);

CREATE TABLE IF NOT EXISTS property_research_packet_run_memberships (
    principal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    candidate_ref TEXT NOT NULL,
    candidate_ref_algorithm TEXT NOT NULL,
    packet_json JSONB NOT NULL,
    packet_canonical_json TEXT NOT NULL,
    packet_size_bytes INTEGER NOT NULL,
    packet_schema_version INTEGER NOT NULL,
    packet_sha256 CHAR(64) NOT NULL,
    property_url_sha256 CHAR(64),
    observed_at TIMESTAMPTZ NOT NULL,
    source_rank INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (principal_id, run_id, candidate_ref),
    FOREIGN KEY (principal_id, run_id)
        REFERENCES property_search_runs(principal_id, run_id)
        ON DELETE CASCADE,
    FOREIGN KEY (principal_id, candidate_ref)
        REFERENCES property_research_packet_links(principal_id, candidate_ref)
        ON DELETE CASCADE,
    CHECK (principal_id <> ''),
    CHECK (char_length(run_id) BETWEEN 1 AND 256),
    CHECK (char_length(candidate_ref) BETWEEN 1 AND 256),
    CHECK (candidate_ref_algorithm IN ('explicit', 'derived_v1')),
    CHECK (jsonb_typeof(packet_json) = 'object'),
    CHECK (packet_json = packet_canonical_json::jsonb),
    CHECK (packet_size_bytes = octet_length(convert_to(packet_canonical_json, 'UTF8'))),
    CHECK (packet_size_bytes BETWEEN 2 AND 262144),
    CHECK (packet_schema_version >= 1),
    CHECK (packet_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (
        property_url_sha256 IS NULL
        OR property_url_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CHECK (source_rank >= 0)
);

CREATE TABLE IF NOT EXISTS property_research_packet_index_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
    coverage_status TEXT NOT NULL DEFAULT 'pending',
    writer_contract_version INTEGER NOT NULL DEFAULT 2,
    packet_schema_version INTEGER NOT NULL DEFAULT 1,
    cutoff_at TIMESTAMPTZ,
    source_run_rows BIGINT NOT NULL DEFAULT 0,
    expected_membership_rows BIGINT NOT NULL DEFAULT 0,
    expected_distinct_tenant_refs BIGINT NOT NULL DEFAULT 0,
    verified_membership_rows BIGINT NOT NULL DEFAULT 0,
    verified_distinct_tenant_refs BIGINT NOT NULL DEFAULT 0,
    zero_projection_run_rows BIGINT NOT NULL DEFAULT 0,
    fleet_proof_sha256 CHAR(64),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (singleton),
    CHECK (coverage_status IN ('pending', 'running', 'complete', 'failed')),
    CHECK (writer_contract_version >= 1),
    CHECK (packet_schema_version >= 1),
    CHECK (source_run_rows >= 0),
    CHECK (expected_membership_rows >= 0),
    CHECK (expected_distinct_tenant_refs >= 0),
    CHECK (verified_membership_rows >= 0),
    CHECK (verified_distinct_tenant_refs >= 0),
    CHECK (zero_projection_run_rows >= 0),
    CHECK (fleet_proof_sha256 IS NULL OR fleet_proof_sha256 ~ '^[0-9a-f]{64}$')
);

INSERT INTO property_research_packet_index_state (singleton)
VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_property_research_packet_links_last_seen
    ON property_research_packet_links(
        principal_id,
        last_seen_at DESC,
        candidate_ref
    );
CREATE INDEX IF NOT EXISTS idx_property_research_packet_links_property_url
    ON property_research_packet_links(
        principal_id,
        property_url_sha256,
        last_seen_at DESC,
        candidate_ref
    );
CREATE INDEX IF NOT EXISTS idx_property_research_packet_links_retention
    ON property_research_packet_links(
        retention_state,
        last_seen_at,
        principal_id,
        candidate_ref
    );
CREATE INDEX IF NOT EXISTS idx_property_research_packet_memberships_ref
    ON property_research_packet_run_memberships(
        principal_id,
        candidate_ref,
        observed_at DESC,
        run_id DESC
    );
CREATE INDEX IF NOT EXISTS idx_property_research_packet_memberships_observed
    ON property_research_packet_run_memberships(
        observed_at,
        principal_id,
        run_id,
        candidate_ref
    );

DO $property_search_runs_compact_version_guard$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'property_search_runs'::regclass
          AND conname = 'property_search_runs_compact_schema_version_match_check'
    ) THEN
        ALTER TABLE property_search_runs
            ADD CONSTRAINT property_search_runs_compact_schema_version_match_check
            CHECK (
                compact_schema_version = 0
                OR CASE
                    WHEN jsonb_typeof(
                        compact_json->'compact_schema_version'
                    ) = 'number'
                     AND compact_json->>'compact_schema_version' ~ '^[0-9]+$'
                    THEN (
                        compact_json->>'compact_schema_version'
                    )::INTEGER = compact_schema_version
                    ELSE FALSE
                END
            ) NOT VALID;
    END IF;
END
$property_search_runs_compact_version_guard$;

CREATE OR REPLACE FUNCTION property_search_runs_enforce_writer_contract()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_search_runs_writer_contract_function$
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_search_writer_contract', TRUE),
        ''
    ) <> '2' THEN
        RAISE EXCEPTION 'property_search_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$property_search_runs_writer_contract_function$;

DROP TRIGGER IF EXISTS property_search_runs_writer_contract_guard
    ON property_search_runs;
CREATE TRIGGER property_search_runs_writer_contract_guard
    BEFORE INSERT OR UPDATE OR DELETE ON property_search_runs
    FOR EACH ROW
    EXECUTE FUNCTION property_search_runs_enforce_writer_contract();
"""


_PROPERTY_SEARCH_ERASURE_FENCES_SCHEMA_V11 = r"""
ALTER TABLE property_search_runs
    ADD COLUMN IF NOT EXISTS principal_key TEXT;
ALTER TABLE property_search_work_jobs
    ADD COLUMN IF NOT EXISTS principal_key TEXT;
ALTER TABLE property_research_packet_links
    ADD COLUMN IF NOT EXISTS principal_key TEXT;
ALTER TABLE property_research_packet_run_memberships
    ADD COLUMN IF NOT EXISTS principal_key TEXT;

CREATE TABLE IF NOT EXISTS property_search_erasure_key_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
    key_id CHAR(64) NOT NULL,
    established_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (singleton),
    CHECK (key_id ~ '^[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION property_search_erasure_key_state_reject_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_search_erasure_key_state_immutable_function$
BEGIN
    RAISE EXCEPTION 'property_search_erasure_key_state_immutable'
        USING ERRCODE = '23514';
END
$property_search_erasure_key_state_immutable_function$;

DROP TRIGGER IF EXISTS property_search_erasure_key_state_immutable_guard
    ON property_search_erasure_key_state;
CREATE TRIGGER property_search_erasure_key_state_immutable_guard
    BEFORE UPDATE OR DELETE ON property_search_erasure_key_state
    FOR EACH ROW
    EXECUTE FUNCTION property_search_erasure_key_state_reject_mutation();

CREATE TABLE IF NOT EXISTS property_search_erasure_fences (
    principal_key TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    erased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (principal_key, run_id),
    CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$'),
    CHECK (char_length(run_id) <= 256)
);

CREATE OR REPLACE FUNCTION property_search_assert_erasure_key()
RETURNS VOID
LANGUAGE plpgsql
AS $property_search_erasure_key_function$
DECLARE
    configured_key_id TEXT;
BEGIN
    configured_key_id := COALESCE(
        current_setting('propertyquarry.property_search_erasure_key_id', TRUE),
        ''
    );
    IF configured_key_id !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'property_search_erasure_key_required'
            USING ERRCODE = '23514';
    END IF;
    INSERT INTO property_search_erasure_key_state (singleton, key_id)
    VALUES (TRUE, configured_key_id)
    ON CONFLICT (singleton) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1
        FROM property_search_erasure_key_state
        WHERE singleton = TRUE
          AND key_id = configured_key_id
    ) THEN
        RAISE EXCEPTION 'property_search_erasure_key_mismatch'
            USING ERRCODE = '23514';
    END IF;
END
$property_search_erasure_key_function$;

CREATE OR REPLACE FUNCTION property_search_assert_write_allowed(
    guarded_principal_key TEXT,
    guarded_run_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $property_search_erasure_fence_function$
BEGIN
    PERFORM property_search_assert_erasure_key();
    IF COALESCE(guarded_principal_key, '') !~
        '^hmac-sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'property_search_principal_key_required'
            USING ERRCODE = '23514';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'property_search_erasure:' || guarded_principal_key,
            0
        )
    );
    IF EXISTS (
        SELECT 1
        FROM property_search_erasure_fences AS fences
        WHERE fences.principal_key = guarded_principal_key
          AND (fences.run_id = '' OR fences.run_id = COALESCE(guarded_run_id, ''))
    ) THEN
        RAISE EXCEPTION 'property_search_account_erased'
            USING ERRCODE = '23514';
    END IF;
END
$property_search_erasure_fence_function$;

DO $property_search_principal_key_backfill$
DECLARE
    supplied_mapping JSONB;
BEGIN
    supplied_mapping := COALESCE(
        NULLIF(
            current_setting(
                'propertyquarry.property_search_principal_key_map',
                TRUE
            ),
            ''
        ),
        '{}'
    )::JSONB;
    IF EXISTS (
        SELECT 1
        FROM jsonb_each_text(supplied_mapping) AS supplied(principal_id, principal_key)
        WHERE supplied.principal_id = ''
           OR supplied.principal_id <> btrim(supplied.principal_id)
           OR supplied.principal_key !~ '^hmac-sha256:[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'property_search_principal_key_map_invalid'
            USING ERRCODE = '23514';
    END IF;

    UPDATE property_search_runs AS target
    SET principal_key = supplied.principal_key
    FROM jsonb_each_text(supplied_mapping)
        AS supplied(principal_id, principal_key)
    WHERE target.principal_id = supplied.principal_id
      AND target.principal_key IS NULL;

    UPDATE property_search_work_jobs AS target
    SET principal_key = supplied.principal_key
    FROM jsonb_each_text(supplied_mapping)
        AS supplied(principal_id, principal_key)
    WHERE target.principal_id = supplied.principal_id
      AND target.principal_key IS NULL;

    UPDATE property_research_packet_links AS target
    SET principal_key = supplied.principal_key
    FROM jsonb_each_text(supplied_mapping)
        AS supplied(principal_id, principal_key)
    WHERE target.principal_id = supplied.principal_id
      AND target.principal_key IS NULL;

    UPDATE property_research_packet_run_memberships AS target
    SET principal_key = supplied.principal_key
    FROM jsonb_each_text(supplied_mapping)
        AS supplied(principal_id, principal_key)
    WHERE target.principal_id = supplied.principal_id
      AND target.principal_key IS NULL;

    IF EXISTS (
        SELECT 1 FROM property_search_runs
        WHERE principal_key IS NULL
           OR principal_id = ''
           OR principal_id <> btrim(principal_id)
    ) OR EXISTS (
        SELECT 1 FROM property_search_work_jobs
        WHERE principal_key IS NULL
           OR principal_id = ''
           OR principal_id <> btrim(principal_id)
    ) OR EXISTS (
        SELECT 1 FROM property_research_packet_links
        WHERE principal_key IS NULL
           OR principal_id = ''
           OR principal_id <> btrim(principal_id)
    ) OR EXISTS (
        SELECT 1 FROM property_research_packet_run_memberships
        WHERE principal_key IS NULL
           OR principal_id = ''
           OR principal_id <> btrim(principal_id)
    ) THEN
        RAISE EXCEPTION 'property_search_principal_key_backfill_incomplete'
            USING ERRCODE = '23514';
    END IF;
END
$property_search_principal_key_backfill$;

ALTER TABLE property_search_runs
    ALTER COLUMN principal_key SET NOT NULL;
ALTER TABLE property_search_work_jobs
    ALTER COLUMN principal_key SET NOT NULL;
ALTER TABLE property_research_packet_links
    ALTER COLUMN principal_key SET NOT NULL;
ALTER TABLE property_research_packet_run_memberships
    ALTER COLUMN principal_key SET NOT NULL;

DO $property_search_principal_key_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_search_runs'::regclass
          AND conname = 'property_search_runs_principal_key_check'
    ) THEN
        ALTER TABLE property_search_runs
            ADD CONSTRAINT property_search_runs_principal_key_check
            CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_search_work_jobs'::regclass
          AND conname = 'property_search_work_jobs_principal_key_check'
    ) THEN
        ALTER TABLE property_search_work_jobs
            ADD CONSTRAINT property_search_work_jobs_principal_key_check
            CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_research_packet_links'::regclass
          AND conname = 'property_research_packet_links_principal_key_check'
    ) THEN
        ALTER TABLE property_research_packet_links
            ADD CONSTRAINT property_research_packet_links_principal_key_check
            CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_research_packet_run_memberships'::regclass
          AND conname = 'property_research_packet_memberships_principal_key_check'
    ) THEN
        ALTER TABLE property_research_packet_run_memberships
            ADD CONSTRAINT property_research_packet_memberships_principal_key_check
            CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_search_runs'::regclass
          AND conname = 'property_search_runs_principal_key_run_id_key'
    ) THEN
        ALTER TABLE property_search_runs
            ADD CONSTRAINT property_search_runs_principal_key_run_id_key
            UNIQUE (principal_key, run_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_search_work_jobs'::regclass
          AND conname = 'property_search_work_jobs_owner_run_fkey'
    ) THEN
        ALTER TABLE property_search_work_jobs
            ADD CONSTRAINT property_search_work_jobs_owner_run_fkey
            FOREIGN KEY (principal_key, run_id)
            REFERENCES property_search_runs(principal_key, run_id)
            ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'property_research_packet_run_memberships'::regclass
          AND conname = 'property_research_packet_memberships_owner_run_fkey'
    ) THEN
        ALTER TABLE property_research_packet_run_memberships
            ADD CONSTRAINT property_research_packet_memberships_owner_run_fkey
            FOREIGN KEY (principal_key, run_id)
            REFERENCES property_search_runs(principal_key, run_id)
            ON DELETE CASCADE;
    END IF;
END
$property_search_principal_key_constraints$;

CREATE INDEX IF NOT EXISTS idx_property_search_runs_run_principal_key
    ON property_search_runs(run_id, principal_key);
CREATE INDEX IF NOT EXISTS idx_property_research_packet_links_principal_key
    ON property_research_packet_links(principal_key, candidate_ref);
CREATE INDEX IF NOT EXISTS idx_property_research_packet_memberships_principal_key
    ON property_research_packet_run_memberships(
        principal_key,
        run_id,
        candidate_ref
    );

CREATE OR REPLACE FUNCTION property_search_assert_run_owner(
    guarded_principal_key TEXT,
    guarded_run_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $property_search_run_owner_function$
BEGIN
    IF COALESCE(guarded_principal_key, '') !~
        '^hmac-sha256:[0-9a-f]{64}$'
       OR COALESCE(guarded_run_id, '') = '' THEN
        RAISE EXCEPTION 'property_search_run_owner_required'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM property_search_runs
        WHERE principal_key = guarded_principal_key
          AND run_id = guarded_run_id
    ) THEN
        RAISE EXCEPTION 'property_search_run_owner_mismatch'
            USING ERRCODE = '23514';
    END IF;
END
$property_search_run_owner_function$;

CREATE OR REPLACE FUNCTION property_search_resolve_principal_key(
    guarded_principal_id TEXT,
    guarded_run_id TEXT
)
RETURNS TEXT
LANGUAGE plpgsql
AS $property_search_principal_key_resolver_function$
DECLARE
    resolved_key TEXT;
    resolved_count INTEGER;
BEGIN
    IF COALESCE(guarded_principal_id, '') = ''
       OR guarded_principal_id <> btrim(guarded_principal_id) THEN
        RAISE EXCEPTION 'property_search_principal_id_required'
            USING ERRCODE = '23514';
    END IF;
    IF COALESCE(guarded_run_id, '') <> '' THEN
        SELECT MIN(principal_key), COUNT(DISTINCT principal_key)
        INTO resolved_key, resolved_count
        FROM property_search_runs
        WHERE principal_id = guarded_principal_id
          AND run_id = guarded_run_id;
    ELSE
        SELECT MIN(principal_key), COUNT(DISTINCT principal_key)
        INTO resolved_key, resolved_count
        FROM (
            SELECT principal_key FROM property_search_runs
            WHERE principal_id = guarded_principal_id
            UNION ALL
            SELECT principal_key FROM property_search_work_jobs
            WHERE principal_id = guarded_principal_id
            UNION ALL
            SELECT principal_key FROM property_research_packet_links
            WHERE principal_id = guarded_principal_id
            UNION ALL
            SELECT principal_key FROM property_research_packet_run_memberships
            WHERE principal_id = guarded_principal_id
        ) AS observed_keys;
    END IF;
    IF COALESCE(resolved_count, 0) <> 1
       OR COALESCE(resolved_key, '') !~ '^hmac-sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'property_search_principal_owner_unresolved'
            USING ERRCODE = '23514';
    END IF;
    RETURN resolved_key;
END
$property_search_principal_key_resolver_function$;

CREATE OR REPLACE FUNCTION property_search_assert_principal_write_allowed(
    guarded_principal_id TEXT,
    guarded_run_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $property_search_principal_authority_function$
DECLARE
    resolved_key TEXT;
BEGIN
    resolved_key := property_search_resolve_principal_key(
        guarded_principal_id,
        guarded_run_id
    );
    IF COALESCE(guarded_run_id, '') <> '' THEN
        PERFORM property_search_assert_run_owner(
            resolved_key,
            guarded_run_id
        );
    END IF;
    PERFORM property_search_assert_write_allowed(
        resolved_key,
        guarded_run_id
    );
END
$property_search_principal_authority_function$;

CREATE OR REPLACE FUNCTION property_search_runs_enforce_writer_contract()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_search_runs_writer_contract_function$
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_search_writer_contract', TRUE),
        ''
    ) <> '3' THEN
        RAISE EXCEPTION 'property_search_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.principal_id IS DISTINCT FROM OLD.principal_id
        OR NEW.run_id IS DISTINCT FROM OLD.run_id
        OR NEW.principal_key IS DISTINCT FROM OLD.principal_key
    ) THEN
        RAISE EXCEPTION 'property_search_run_owner_immutable'
            USING ERRCODE = '23514';
    END IF;
    PERFORM property_search_assert_write_allowed(
        NEW.principal_key,
        NEW.run_id
    );
    RETURN NEW;
END
$property_search_runs_writer_contract_function$;

CREATE OR REPLACE FUNCTION property_search_work_jobs_enforce_erasure_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_search_work_jobs_erasure_fence_function$
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_search_writer_contract', TRUE),
        ''
    ) <> '3' THEN
        RAISE EXCEPTION 'property_search_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.principal_id IS DISTINCT FROM OLD.principal_id
        OR NEW.run_id IS DISTINCT FROM OLD.run_id
        OR NEW.principal_key IS DISTINCT FROM OLD.principal_key
    ) THEN
        RAISE EXCEPTION 'property_search_work_job_owner_immutable'
            USING ERRCODE = '23514';
    END IF;
    PERFORM property_search_assert_run_owner(
        NEW.principal_key,
        NEW.run_id
    );
    PERFORM property_search_assert_write_allowed(NEW.principal_key, NEW.run_id);
    RETURN NEW;
END
$property_search_work_jobs_erasure_fence_function$;

DROP TRIGGER IF EXISTS property_search_work_jobs_erasure_fence_guard
    ON property_search_work_jobs;
CREATE TRIGGER property_search_work_jobs_erasure_fence_guard
    BEFORE INSERT OR UPDATE OR DELETE ON property_search_work_jobs
    FOR EACH ROW
    EXECUTE FUNCTION property_search_work_jobs_enforce_erasure_fence();

CREATE OR REPLACE FUNCTION property_research_packets_enforce_erasure_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_research_packets_erasure_fence_function$
DECLARE
    guarded_run_id TEXT;
    resolved_key TEXT;
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_search_writer_contract', TRUE),
        ''
    ) <> '3' THEN
        RAISE EXCEPTION 'property_search_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.principal_id IS DISTINCT FROM OLD.principal_id
        OR NEW.principal_key IS DISTINCT FROM OLD.principal_key
        OR NEW.candidate_ref IS DISTINCT FROM OLD.candidate_ref
        OR (
            TG_TABLE_NAME = 'property_research_packet_run_memberships'
            AND NEW.run_id IS DISTINCT FROM OLD.run_id
        )
    ) THEN
        RAISE EXCEPTION 'property_research_packet_owner_immutable'
            USING ERRCODE = '23514';
    END IF;
    guarded_run_id := CASE
        WHEN TG_TABLE_NAME = 'property_research_packet_run_memberships'
        THEN NEW.run_id
        ELSE NEW.last_run_id
    END;
    resolved_key := property_search_resolve_principal_key(
        NEW.principal_id,
        guarded_run_id
    );
    IF NEW.principal_key IS NULL THEN
        NEW.principal_key := resolved_key;
    ELSIF NEW.principal_key <> resolved_key THEN
        RAISE EXCEPTION 'property_research_packet_owner_mismatch'
            USING ERRCODE = '23514';
    END IF;
    PERFORM property_search_assert_principal_write_allowed(
        NEW.principal_id,
        guarded_run_id
    );
    RETURN NEW;
END
$property_research_packets_erasure_fence_function$;

DROP TRIGGER IF EXISTS property_research_packet_links_erasure_fence_guard
    ON property_research_packet_links;
CREATE TRIGGER property_research_packet_links_erasure_fence_guard
    BEFORE INSERT OR UPDATE ON property_research_packet_links
    FOR EACH ROW
    EXECUTE FUNCTION property_research_packets_enforce_erasure_fence();

DROP TRIGGER IF EXISTS property_research_packet_memberships_erasure_fence_guard
    ON property_research_packet_run_memberships;
CREATE TRIGGER property_research_packet_memberships_erasure_fence_guard
    BEFORE INSERT OR UPDATE ON property_research_packet_run_memberships
    FOR EACH ROW
    EXECUTE FUNCTION property_research_packets_enforce_erasure_fence();

UPDATE property_research_packet_index_state
SET writer_contract_version = 3,
    coverage_status = 'pending',
    fleet_proof_sha256 = NULL,
    completed_at = NULL,
    updated_at = NOW()
WHERE writer_contract_version IS DISTINCT FROM 3;
"""


_PROPERTY_CONTENT_ACCOUNT_OWNERSHIP_SCHEMA_V12 = r"""
DO $property_content_legacy_ownership_guard$
DECLARE
    jobs_count BIGINT;
    job_events_count BIGINT;
    webhook_events_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO jobs_count FROM property_content_jobs;
    SELECT COUNT(*) INTO job_events_count FROM property_content_job_events;
    SELECT COUNT(*) INTO webhook_events_count FROM property_content_webhook_events;
    IF jobs_count > 0 OR job_events_count > 0 OR webhook_events_count > 0 THEN
        RAISE EXCEPTION
            'property_content_legacy_ownership_unresolved:jobs=%,job_events=%,webhook_events=%',
            jobs_count,
            job_events_count,
            webhook_events_count;
    END IF;
END
$property_content_legacy_ownership_guard$;

ALTER TABLE property_content_jobs
    ADD COLUMN IF NOT EXISTS principal_key TEXT;
ALTER TABLE property_content_jobs
    ADD COLUMN IF NOT EXISTS ownership_scope TEXT;
ALTER TABLE property_content_jobs
    ADD COLUMN IF NOT EXISTS search_run_id TEXT NOT NULL DEFAULT '';
ALTER TABLE property_content_job_events
    ADD COLUMN IF NOT EXISTS principal_key TEXT;
ALTER TABLE property_content_job_events
    ADD COLUMN IF NOT EXISTS ownership_scope TEXT;
ALTER TABLE property_content_job_events
    ADD COLUMN IF NOT EXISTS search_run_id TEXT NOT NULL DEFAULT '';
ALTER TABLE property_content_webhook_events
    ADD COLUMN IF NOT EXISTS principal_key TEXT;
ALTER TABLE property_content_webhook_events
    ADD COLUMN IF NOT EXISTS ownership_scope TEXT;
ALTER TABLE property_content_webhook_events
    ADD COLUMN IF NOT EXISTS search_run_id TEXT NOT NULL DEFAULT '';
ALTER TABLE property_content_webhook_events
    ADD COLUMN IF NOT EXISTS packet_id TEXT;

ALTER TABLE property_content_jobs ALTER COLUMN principal_key SET NOT NULL;
ALTER TABLE property_content_jobs ALTER COLUMN ownership_scope SET NOT NULL;
ALTER TABLE property_content_job_events ALTER COLUMN principal_key SET NOT NULL;
ALTER TABLE property_content_job_events ALTER COLUMN ownership_scope SET NOT NULL;
ALTER TABLE property_content_webhook_events ALTER COLUMN principal_key SET NOT NULL;
ALTER TABLE property_content_webhook_events ALTER COLUMN ownership_scope SET NOT NULL;
ALTER TABLE property_content_webhook_events ALTER COLUMN packet_id SET NOT NULL;

ALTER TABLE property_content_jobs
    DROP CONSTRAINT IF EXISTS property_content_jobs_pkey;
ALTER TABLE property_content_jobs
    DROP CONSTRAINT IF EXISTS property_content_jobs_idempotency_key_key;
ALTER TABLE property_content_jobs
    ADD CONSTRAINT property_content_jobs_pkey
    PRIMARY KEY (principal_key, ownership_scope, search_run_id, packet_id);
ALTER TABLE property_content_jobs
    ADD CONSTRAINT property_content_jobs_owner_idempotency_key
    UNIQUE (principal_key, ownership_scope, search_run_id, idempotency_key);

ALTER TABLE property_content_job_events
    DROP CONSTRAINT IF EXISTS property_content_job_events_event_id_key;
ALTER TABLE property_content_job_events
    DROP CONSTRAINT IF EXISTS property_content_job_events_idempotency_key_key;
ALTER TABLE property_content_job_events
    ADD CONSTRAINT property_content_job_events_owner_event_id_key
    UNIQUE (principal_key, ownership_scope, search_run_id, event_id);
ALTER TABLE property_content_job_events
    ADD CONSTRAINT property_content_job_events_owner_idempotency_key
    UNIQUE (principal_key, ownership_scope, search_run_id, idempotency_key);
ALTER TABLE property_content_job_events
    ADD CONSTRAINT property_content_job_events_owner_packet_fkey
    FOREIGN KEY (principal_key, ownership_scope, search_run_id, packet_id)
    REFERENCES property_content_jobs(
        principal_key,
        ownership_scope,
        search_run_id,
        packet_id
    )
    ON DELETE CASCADE;

ALTER TABLE property_content_webhook_events
    DROP CONSTRAINT IF EXISTS property_content_webhook_events_pkey;
ALTER TABLE property_content_webhook_events
    ADD CONSTRAINT property_content_webhook_events_pkey
    PRIMARY KEY (
        principal_key,
        ownership_scope,
        search_run_id,
        provider,
        provider_event_id
    );
ALTER TABLE property_content_webhook_events
    ADD CONSTRAINT property_content_webhook_events_owner_packet_fkey
    FOREIGN KEY (principal_key, ownership_scope, search_run_id, packet_id)
    REFERENCES property_content_jobs(
        principal_key,
        ownership_scope,
        search_run_id,
        packet_id
    )
    ON DELETE CASCADE;

ALTER TABLE property_content_jobs
    ADD CONSTRAINT property_content_jobs_principal_key_check
    CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$');
ALTER TABLE property_content_jobs
    ADD CONSTRAINT property_content_jobs_ownership_scope_check
    CHECK (ownership_scope IN ('search_run', 'system'));
ALTER TABLE property_content_jobs
    ADD CONSTRAINT property_content_jobs_search_run_id_check
    CHECK (
        char_length(search_run_id) <= 256
        AND (
            (ownership_scope = 'search_run' AND search_run_id <> '')
            OR (ownership_scope = 'system' AND search_run_id = '')
        )
    );
ALTER TABLE property_content_job_events
    ADD CONSTRAINT property_content_job_events_principal_key_check
    CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$');
ALTER TABLE property_content_job_events
    ADD CONSTRAINT property_content_job_events_ownership_scope_check
    CHECK (ownership_scope IN ('search_run', 'system'));
ALTER TABLE property_content_job_events
    ADD CONSTRAINT property_content_job_events_search_run_id_check
    CHECK (
        char_length(search_run_id) <= 256
        AND (
            (ownership_scope = 'search_run' AND search_run_id <> '')
            OR (ownership_scope = 'system' AND search_run_id = '')
        )
    );
ALTER TABLE property_content_webhook_events
    ADD CONSTRAINT property_content_webhook_events_principal_key_check
    CHECK (principal_key ~ '^hmac-sha256:[0-9a-f]{64}$');
ALTER TABLE property_content_webhook_events
    ADD CONSTRAINT property_content_webhook_events_ownership_scope_check
    CHECK (ownership_scope IN ('search_run', 'system'));
ALTER TABLE property_content_webhook_events
    ADD CONSTRAINT property_content_webhook_events_search_run_id_check
    CHECK (
        char_length(search_run_id) <= 256
        AND (
            (ownership_scope = 'search_run' AND search_run_id <> '')
            OR (ownership_scope = 'system' AND search_run_id = '')
        )
    );

CREATE INDEX IF NOT EXISTS idx_property_content_jobs_principal_updated
    ON property_content_jobs(
        principal_key,
        ownership_scope,
        search_run_id,
        updated_at DESC,
        packet_id
    );
CREATE INDEX IF NOT EXISTS idx_property_content_job_events_principal_sequence
    ON property_content_job_events(
        principal_key,
        ownership_scope,
        search_run_id,
        event_sequence
    );
CREATE INDEX IF NOT EXISTS idx_property_content_webhook_principal_updated
    ON property_content_webhook_events(
        principal_key,
        ownership_scope,
        search_run_id,
        updated_at DESC,
        provider_event_id
    );

CREATE OR REPLACE FUNCTION property_content_enforce_account_authority()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_content_account_authority_function$
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_content_writer_contract', TRUE),
        ''
    ) <> '3' THEN
        RAISE EXCEPTION 'property_content_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF COALESCE(NEW.principal_key, '') !~
        '^hmac-sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'property_content_principal_key_required'
            USING ERRCODE = '23514';
    END IF;
    IF char_length(COALESCE(NEW.search_run_id, '')) > 256 THEN
        RAISE EXCEPTION 'property_content_search_run_id_invalid'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        OLD.principal_key IS DISTINCT FROM NEW.principal_key
        OR OLD.ownership_scope IS DISTINCT FROM NEW.ownership_scope
        OR OLD.search_run_id IS DISTINCT FROM NEW.search_run_id
        OR OLD.packet_id IS DISTINCT FROM NEW.packet_id
    ) THEN
        RAISE EXCEPTION 'property_content_owner_run_immutable'
            USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME IN ('property_content_jobs', 'property_content_webhook_events')
       AND (
           COALESCE(NEW.row_json->>'principal_key', '') <> NEW.principal_key
           OR COALESCE(NEW.row_json->>'ownership_scope', '') <> NEW.ownership_scope
           OR COALESCE(NEW.row_json->>'search_run_id', '') <> NEW.search_run_id
           OR COALESCE(NEW.row_json->>'packet_id', '') <> NEW.packet_id
       ) THEN
        RAISE EXCEPTION 'property_content_row_owner_mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.ownership_scope = 'search_run' THEN
        IF NEW.search_run_id = '' THEN
            RAISE EXCEPTION 'property_content_search_run_id_required'
                USING ERRCODE = '23514';
        END IF;
        PERFORM property_search_assert_run_owner(
            NEW.principal_key,
            NEW.search_run_id
        );
    ELSIF NEW.ownership_scope = 'system' THEN
        IF NEW.search_run_id <> '' OR NEW.principal_key <> COALESCE(
            current_setting(
                'propertyquarry.property_content_system_principal_key',
                TRUE
            ),
            ''
        ) THEN
            RAISE EXCEPTION 'property_content_system_owner_required'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'property_content_ownership_scope_invalid'
            USING ERRCODE = '23514';
    END IF;
    PERFORM property_search_assert_write_allowed(
        NEW.principal_key,
        NEW.search_run_id
    );
    RETURN NEW;
END
$property_content_account_authority_function$;

DROP TRIGGER IF EXISTS property_content_jobs_account_authority_guard
    ON property_content_jobs;
CREATE TRIGGER property_content_jobs_account_authority_guard
    BEFORE INSERT OR UPDATE ON property_content_jobs
    FOR EACH ROW
    EXECUTE FUNCTION property_content_enforce_account_authority();

DROP TRIGGER IF EXISTS property_content_job_events_account_authority_guard
    ON property_content_job_events;
CREATE TRIGGER property_content_job_events_account_authority_guard
    BEFORE INSERT OR UPDATE ON property_content_job_events
    FOR EACH ROW
    EXECUTE FUNCTION property_content_enforce_account_authority();

DROP TRIGGER IF EXISTS property_content_webhook_account_authority_guard
    ON property_content_webhook_events;
CREATE TRIGGER property_content_webhook_account_authority_guard
    BEFORE INSERT OR UPDATE ON property_content_webhook_events
    FOR EACH ROW
    EXECUTE FUNCTION property_content_enforce_account_authority();
"""


_PROPERTY_CONTENT_ACCOUNT_AUTHORITY_TRIGGER_FIX_SCHEMA_V13 = r"""
CREATE OR REPLACE FUNCTION property_content_enforce_account_authority()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_content_account_authority_function$
DECLARE
    embedded_row JSONB;
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_content_writer_contract', TRUE),
        ''
    ) <> '3' THEN
        RAISE EXCEPTION 'property_content_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF COALESCE(NEW.principal_key, '') !~
        '^hmac-sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'property_content_principal_key_required'
            USING ERRCODE = '23514';
    END IF;
    IF char_length(COALESCE(NEW.search_run_id, '')) > 256 THEN
        RAISE EXCEPTION 'property_content_search_run_id_invalid'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        OLD.principal_key IS DISTINCT FROM NEW.principal_key
        OR OLD.ownership_scope IS DISTINCT FROM NEW.ownership_scope
        OR OLD.search_run_id IS DISTINCT FROM NEW.search_run_id
        OR OLD.packet_id IS DISTINCT FROM NEW.packet_id
    ) THEN
        RAISE EXCEPTION 'property_content_owner_run_immutable'
            USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME IN (
        'property_content_jobs',
        'property_content_webhook_events'
    ) THEN
        embedded_row := to_jsonb(NEW)->'row_json';
        IF COALESCE(embedded_row->>'principal_key', '') <> NEW.principal_key
           OR COALESCE(embedded_row->>'ownership_scope', '') <> NEW.ownership_scope
           OR COALESCE(embedded_row->>'search_run_id', '') <> NEW.search_run_id
           OR COALESCE(embedded_row->>'packet_id', '') <> NEW.packet_id THEN
            RAISE EXCEPTION 'property_content_row_owner_mismatch'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.ownership_scope = 'search_run' THEN
        IF NEW.search_run_id = '' THEN
            RAISE EXCEPTION 'property_content_search_run_id_required'
                USING ERRCODE = '23514';
        END IF;
        PERFORM property_search_assert_run_owner(
            NEW.principal_key,
            NEW.search_run_id
        );
    ELSIF NEW.ownership_scope = 'system' THEN
        IF NEW.search_run_id <> '' OR NEW.principal_key <> COALESCE(
            current_setting(
                'propertyquarry.property_content_system_principal_key',
                TRUE
            ),
            ''
        ) THEN
            RAISE EXCEPTION 'property_content_system_owner_required'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'property_content_ownership_scope_invalid'
            USING ERRCODE = '23514';
    END IF;
    PERFORM property_search_assert_write_allowed(
        NEW.principal_key,
        NEW.search_run_id
    );
    RETURN NEW;
END
$property_content_account_authority_function$;
"""


_PROPERTY_RESEARCH_PACKET_ERASURE_TRIGGER_SPLIT_SCHEMA_V14 = r"""
CREATE OR REPLACE FUNCTION property_research_packet_links_enforce_erasure_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_research_packet_links_erasure_fence_function$
DECLARE
    resolved_key TEXT;
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_search_writer_contract', TRUE),
        ''
    ) <> '3' THEN
        RAISE EXCEPTION 'property_search_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.principal_id IS DISTINCT FROM OLD.principal_id
        OR NEW.principal_key IS DISTINCT FROM OLD.principal_key
        OR NEW.candidate_ref IS DISTINCT FROM OLD.candidate_ref
    ) THEN
        RAISE EXCEPTION 'property_research_packet_owner_immutable'
            USING ERRCODE = '23514';
    END IF;
    resolved_key := property_search_resolve_principal_key(
        NEW.principal_id,
        NEW.last_run_id
    );
    IF NEW.principal_key IS NULL THEN
        NEW.principal_key := resolved_key;
    ELSIF NEW.principal_key IS DISTINCT FROM resolved_key THEN
        RAISE EXCEPTION 'property_search_principal_key_mismatch'
            USING ERRCODE = '23514';
    END IF;
    PERFORM property_search_assert_principal_write_allowed(
        NEW.principal_id,
        NEW.last_run_id
    );
    RETURN NEW;
END
$property_research_packet_links_erasure_fence_function$;

CREATE OR REPLACE FUNCTION property_research_packet_memberships_enforce_erasure_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $property_research_packet_memberships_erasure_fence_function$
DECLARE
    resolved_key TEXT;
BEGIN
    IF COALESCE(
        current_setting('propertyquarry.property_search_writer_contract', TRUE),
        ''
    ) <> '3' THEN
        RAISE EXCEPTION 'property_search_writer_contract_required'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.principal_id IS DISTINCT FROM OLD.principal_id
        OR NEW.principal_key IS DISTINCT FROM OLD.principal_key
        OR NEW.candidate_ref IS DISTINCT FROM OLD.candidate_ref
        OR NEW.run_id IS DISTINCT FROM OLD.run_id
    ) THEN
        RAISE EXCEPTION 'property_research_packet_owner_immutable'
            USING ERRCODE = '23514';
    END IF;
    resolved_key := property_search_resolve_principal_key(
        NEW.principal_id,
        NEW.run_id
    );
    IF NEW.principal_key IS NULL THEN
        NEW.principal_key := resolved_key;
    ELSIF NEW.principal_key IS DISTINCT FROM resolved_key THEN
        RAISE EXCEPTION 'property_search_principal_key_mismatch'
            USING ERRCODE = '23514';
    END IF;
    PERFORM property_search_assert_principal_write_allowed(
        NEW.principal_id,
        NEW.run_id
    );
    RETURN NEW;
END
$property_research_packet_memberships_erasure_fence_function$;

DROP TRIGGER IF EXISTS property_research_packet_links_erasure_fence_guard
    ON property_research_packet_links;
CREATE TRIGGER property_research_packet_links_erasure_fence_guard
    BEFORE INSERT OR UPDATE ON property_research_packet_links
    FOR EACH ROW
    EXECUTE FUNCTION property_research_packet_links_enforce_erasure_fence();

DROP TRIGGER IF EXISTS property_research_packet_memberships_erasure_fence_guard
    ON property_research_packet_run_memberships;
CREATE TRIGGER property_research_packet_memberships_erasure_fence_guard
    BEFORE INSERT OR UPDATE ON property_research_packet_run_memberships
    FOR EACH ROW
    EXECUTE FUNCTION property_research_packet_memberships_enforce_erasure_fence();

DROP FUNCTION IF EXISTS property_research_packets_enforce_erasure_fence();
"""


PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT = 100_000
PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT = 1_000_000
PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION = 1


_DISTRIBUTED_INGRESS_ADMISSION_SCHEMA_V15 = rf"""
CREATE TABLE IF NOT EXISTS propertyquarry_ingress_quota_buckets (
    quota_kind TEXT NOT NULL,
    dimension TEXT NOT NULL,
    subject_digest TEXT NOT NULL,
    window_id BIGINT NOT NULL,
    window_seconds INTEGER NOT NULL,
    used_units BIGINT NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT propertyquarry_ingress_quota_buckets_pkey
        PRIMARY KEY (quota_kind, dimension, subject_digest, window_id),
    CONSTRAINT propertyquarry_ingress_quota_kind_check
        CHECK (quota_kind IN ('request', 'cost')),
    CONSTRAINT propertyquarry_ingress_quota_dimension_check
        CHECK (dimension IN ('ip', 'account')),
    CONSTRAINT propertyquarry_ingress_quota_subject_digest_check
        CHECK (subject_digest ~ '^hmac-sha256:[0-9a-f]{{64}}$'),
    CONSTRAINT propertyquarry_ingress_quota_window_id_check
        CHECK (window_id >= 0),
    CONSTRAINT propertyquarry_ingress_quota_window_seconds_check
        CHECK (window_seconds BETWEEN 1 AND 2678400),
    CONSTRAINT propertyquarry_ingress_quota_used_units_check
        CHECK (used_units >= 0)
);

CREATE INDEX IF NOT EXISTS idx_propertyquarry_ingress_quota_expiry
    ON propertyquarry_ingress_quota_buckets(
        expires_at,
        quota_kind,
        dimension,
        subject_digest
    );

CREATE TABLE IF NOT EXISTS propertyquarry_ingress_leases (
    lease_token UUID PRIMARY KEY,
    ip_subject_digest TEXT NOT NULL,
    account_subject_digest TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT propertyquarry_ingress_lease_ip_digest_check
        CHECK (ip_subject_digest ~ '^hmac-sha256:[0-9a-f]{{64}}$'),
    CONSTRAINT propertyquarry_ingress_lease_account_digest_check
        CHECK (
            account_subject_digest IS NULL
            OR account_subject_digest ~ '^hmac-sha256:[0-9a-f]{{64}}$'
        ),
    CONSTRAINT propertyquarry_ingress_lease_heartbeat_check
        CHECK (heartbeat_at >= created_at),
    CONSTRAINT propertyquarry_ingress_lease_expiry_check
        CHECK (expires_at > created_at),
    CONSTRAINT propertyquarry_ingress_lease_horizon_check
        CHECK (
            expires_at >= heartbeat_at + INTERVAL '1 second'
            AND expires_at <= heartbeat_at + INTERVAL '3600 seconds'
        )
);

CREATE INDEX IF NOT EXISTS idx_propertyquarry_ingress_lease_expiry
    ON propertyquarry_ingress_leases(expires_at, lease_token);
CREATE INDEX IF NOT EXISTS idx_propertyquarry_ingress_lease_ip_expiry
    ON propertyquarry_ingress_leases(
        ip_subject_digest,
        expires_at,
        lease_token
    );
CREATE INDEX IF NOT EXISTS idx_propertyquarry_ingress_lease_account_expiry
    ON propertyquarry_ingress_leases(
        account_subject_digest,
        expires_at,
        lease_token
    )
    WHERE account_subject_digest IS NOT NULL;

CREATE TABLE IF NOT EXISTS propertyquarry_ingress_admission_capacity (
    capacity_key TEXT PRIMARY KEY,
    row_count BIGINT NOT NULL,
    hard_limit BIGINT NOT NULL,
    contract_version INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT propertyquarry_ingress_capacity_key_check
        CHECK (capacity_key IN ('lease', 'quota')),
    CONSTRAINT propertyquarry_ingress_capacity_count_check
        CHECK (row_count >= 0 AND row_count <= hard_limit),
    CONSTRAINT propertyquarry_ingress_capacity_limit_check
        CHECK (
            (capacity_key = 'lease'
             AND hard_limit = {PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT})
            OR
            (capacity_key = 'quota'
             AND hard_limit = {PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT})
        ),
    CONSTRAINT propertyquarry_ingress_capacity_version_check
        CHECK (
            contract_version =
            {PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION}
        )
);

INSERT INTO propertyquarry_ingress_admission_capacity (
    capacity_key,
    row_count,
    hard_limit,
    contract_version,
    updated_at
)
VALUES
    (
        'lease',
        0,
        {PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT},
        {PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION},
        NOW()
    ),
    (
        'quota',
        0,
        {PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT},
        {PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION},
        NOW()
    )
ON CONFLICT (capacity_key) DO NOTHING;

DO $propertyquarry_ingress_initial_capacity_contract$
DECLARE
    lease_rows BIGINT;
    quota_rows BIGINT;
BEGIN
    SELECT COUNT(*) INTO lease_rows
    FROM propertyquarry_ingress_leases;
    SELECT COUNT(*) INTO quota_rows
    FROM propertyquarry_ingress_quota_buckets;

    IF (
        SELECT COUNT(*)
        FROM propertyquarry_ingress_admission_capacity
    ) <> 2
       OR NOT EXISTS (
           SELECT 1
           FROM propertyquarry_ingress_admission_capacity
           WHERE capacity_key = 'lease'
             AND row_count = lease_rows
             AND hard_limit =
                 {PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT}
             AND contract_version =
                 {PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION}
       )
       OR NOT EXISTS (
           SELECT 1
           FROM propertyquarry_ingress_admission_capacity
           WHERE capacity_key = 'quota'
             AND row_count = quota_rows
             AND hard_limit =
                 {PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT}
             AND contract_version =
                 {PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION}
       ) THEN
        RAISE EXCEPTION
            'propertyquarry_ingress_capacity_contract_invalid'
            USING ERRCODE = '23514';
    END IF;
END
$propertyquarry_ingress_initial_capacity_contract$;

CREATE OR REPLACE FUNCTION
propertyquarry_ingress_admission_protect_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $propertyquarry_ingress_identity_function$
BEGIN
    IF TG_TABLE_NAME = 'propertyquarry_ingress_quota_buckets' THEN
        IF OLD.quota_kind IS DISTINCT FROM NEW.quota_kind
           OR OLD.dimension IS DISTINCT FROM NEW.dimension
           OR OLD.subject_digest IS DISTINCT FROM NEW.subject_digest
           OR OLD.window_id IS DISTINCT FROM NEW.window_id
           OR OLD.window_seconds IS DISTINCT FROM NEW.window_seconds THEN
            RAISE EXCEPTION
                'propertyquarry_ingress_quota_identity_immutable'
                USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'propertyquarry_ingress_leases' THEN
        IF OLD.lease_token IS DISTINCT FROM NEW.lease_token
           OR OLD.ip_subject_digest IS DISTINCT FROM NEW.ip_subject_digest
           OR OLD.account_subject_digest
                IS DISTINCT FROM NEW.account_subject_digest
           OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION
                'propertyquarry_ingress_lease_identity_immutable'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION
            'propertyquarry_ingress_identity_table_invalid'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$propertyquarry_ingress_identity_function$;

CREATE OR REPLACE FUNCTION
propertyquarry_ingress_admission_guard_capacity_contract()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $propertyquarry_ingress_capacity_contract_function$
BEGIN
    IF TG_OP <> 'UPDATE'
       OR pg_trigger_depth() <> 2
       OR OLD.capacity_key IS DISTINCT FROM NEW.capacity_key
       OR OLD.hard_limit IS DISTINCT FROM NEW.hard_limit
       OR OLD.contract_version IS DISTINCT FROM NEW.contract_version
       OR abs(NEW.row_count - OLD.row_count) <> 1 THEN
        RAISE EXCEPTION
            'propertyquarry_ingress_capacity_contract_immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$propertyquarry_ingress_capacity_contract_function$;

CREATE OR REPLACE FUNCTION
propertyquarry_ingress_admission_apply_capacity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $propertyquarry_ingress_capacity_function$
DECLARE
    resource_key TEXT;
    expected_limit BIGINT;
    next_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'propertyquarry_ingress_quota_buckets' THEN
        resource_key := 'quota';
        expected_limit :=
            {PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT};
    ELSIF TG_TABLE_NAME = 'propertyquarry_ingress_leases' THEN
        resource_key := 'lease';
        expected_limit :=
            {PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT};
    ELSE
        RAISE EXCEPTION
            'propertyquarry_ingress_capacity_table_invalid'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' THEN
        UPDATE propertyquarry_ingress_admission_capacity
        SET row_count = row_count + 1,
            updated_at = NOW()
        WHERE capacity_key = resource_key
          AND hard_limit = expected_limit
          AND contract_version =
              {PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION}
          AND row_count >= 0
          AND row_count < hard_limit
        RETURNING row_count INTO next_count;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE propertyquarry_ingress_admission_capacity
        SET row_count = row_count - 1,
            updated_at = NOW()
        WHERE capacity_key = resource_key
          AND hard_limit = expected_limit
          AND contract_version =
              {PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION}
          AND row_count > 0
          AND row_count <= hard_limit
        RETURNING row_count INTO next_count;
    ELSE
        RAISE EXCEPTION
            'propertyquarry_ingress_capacity_operation_invalid'
            USING ERRCODE = '23514';
    END IF;

    IF next_count IS NULL THEN
        RAISE EXCEPTION
            'propertyquarry_ingress_%_capacity_unavailable',
            resource_key
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$propertyquarry_ingress_capacity_function$;

CREATE OR REPLACE FUNCTION
propertyquarry_ingress_admission_forbid_truncate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $propertyquarry_ingress_truncate_function$
BEGIN
    RAISE EXCEPTION
        'propertyquarry_ingress_truncate_forbidden'
        USING ERRCODE = '23514';
END
$propertyquarry_ingress_truncate_function$;

DROP TRIGGER IF EXISTS propertyquarry_ingress_quota_identity_guard
    ON propertyquarry_ingress_quota_buckets;
CREATE TRIGGER propertyquarry_ingress_quota_identity_guard
    BEFORE UPDATE ON propertyquarry_ingress_quota_buckets
    FOR EACH ROW
    EXECUTE FUNCTION propertyquarry_ingress_admission_protect_identity();

DROP TRIGGER IF EXISTS propertyquarry_ingress_lease_identity_guard
    ON propertyquarry_ingress_leases;
CREATE TRIGGER propertyquarry_ingress_lease_identity_guard
    BEFORE UPDATE ON propertyquarry_ingress_leases
    FOR EACH ROW
    EXECUTE FUNCTION propertyquarry_ingress_admission_protect_identity();

DROP TRIGGER IF EXISTS propertyquarry_ingress_capacity_contract_guard
    ON propertyquarry_ingress_admission_capacity;
CREATE TRIGGER propertyquarry_ingress_capacity_contract_guard
    BEFORE INSERT OR UPDATE OR DELETE
    ON propertyquarry_ingress_admission_capacity
    FOR EACH ROW
    EXECUTE FUNCTION
        propertyquarry_ingress_admission_guard_capacity_contract();

DROP TRIGGER IF EXISTS propertyquarry_ingress_quota_capacity_guard
    ON propertyquarry_ingress_quota_buckets;
CREATE TRIGGER propertyquarry_ingress_quota_capacity_guard
    AFTER INSERT OR DELETE ON propertyquarry_ingress_quota_buckets
    FOR EACH ROW
    EXECUTE FUNCTION propertyquarry_ingress_admission_apply_capacity();

DROP TRIGGER IF EXISTS propertyquarry_ingress_lease_capacity_guard
    ON propertyquarry_ingress_leases;
CREATE TRIGGER propertyquarry_ingress_lease_capacity_guard
    AFTER INSERT OR DELETE ON propertyquarry_ingress_leases
    FOR EACH ROW
    EXECUTE FUNCTION propertyquarry_ingress_admission_apply_capacity();

DROP TRIGGER IF EXISTS propertyquarry_ingress_quota_truncate_guard
    ON propertyquarry_ingress_quota_buckets;
CREATE TRIGGER propertyquarry_ingress_quota_truncate_guard
    BEFORE TRUNCATE ON propertyquarry_ingress_quota_buckets
    FOR EACH STATEMENT
    EXECUTE FUNCTION propertyquarry_ingress_admission_forbid_truncate();

DROP TRIGGER IF EXISTS propertyquarry_ingress_lease_truncate_guard
    ON propertyquarry_ingress_leases;
CREATE TRIGGER propertyquarry_ingress_lease_truncate_guard
    BEFORE TRUNCATE ON propertyquarry_ingress_leases
    FOR EACH STATEMENT
    EXECUTE FUNCTION propertyquarry_ingress_admission_forbid_truncate();

DROP TRIGGER IF EXISTS propertyquarry_ingress_capacity_truncate_guard
    ON propertyquarry_ingress_admission_capacity;
CREATE TRIGGER propertyquarry_ingress_capacity_truncate_guard
    BEFORE TRUNCATE ON propertyquarry_ingress_admission_capacity
    FOR EACH STATEMENT
    EXECUTE FUNCTION propertyquarry_ingress_admission_forbid_truncate();
"""


PROPERTY_SEARCH_MIGRATIONS: tuple[PropertySearchMigration, ...] = (
    PropertySearchMigration(1, "property_search_runs_tenant_schema", _RUN_SCHEMA_V1),
    PropertySearchMigration(
        2, "property_search_durable_work_queue", _WORK_QUEUE_SCHEMA_V2
    ),
    PropertySearchMigration(
        3, "property_source_listing_cache", _SOURCE_CACHE_SCHEMA_V3
    ),
    PropertySearchMigration(
        4, "replica_safe_delivery_outbox", _DELIVERY_OUTBOX_SCHEMA_V4
    ),
    PropertySearchMigration(
        5, "durable_property_content_job_ledger", _PROPERTY_CONTENT_LEDGER_SCHEMA_V5
    ),
    PropertySearchMigration(
        6, "bounded_run_delivery_projection", _RUN_DELIVERY_PROJECTION_SCHEMA_V6
    ),
    PropertySearchMigration(
        7,
        "tenant_scoped_delivery_outbox_idempotency",
        _TENANT_SCOPED_OUTBOX_IDEMPOTENCY_SCHEMA_V7,
    ),
    PropertySearchMigration(
        8,
        "property_evidence_overlay_cached_read_model",
        _EVIDENCE_OVERLAY_READ_MODEL_SCHEMA_V8,
    ),
    PropertySearchMigration(
        9,
        "property_evidence_overlay_staged_snapshot_activation",
        _EVIDENCE_OVERLAY_STAGED_ACTIVATION_SCHEMA_V9,
    ),
    PropertySearchMigration(
        10,
        "tenant_scoped_property_research_packet_links",
        _PROPERTY_RESEARCH_PACKET_LINKS_SCHEMA_V10,
    ),
    PropertySearchMigration(
        11,
        "durable_property_search_erasure_fences",
        _PROPERTY_SEARCH_ERASURE_FENCES_SCHEMA_V11,
    ),
    PropertySearchMigration(
        12,
        "property_content_account_ownership_fence",
        _PROPERTY_CONTENT_ACCOUNT_OWNERSHIP_SCHEMA_V12,
    ),
    PropertySearchMigration(
        13,
        "property_content_polymorphic_authority_trigger_fix",
        _PROPERTY_CONTENT_ACCOUNT_AUTHORITY_TRIGGER_FIX_SCHEMA_V13,
    ),
    PropertySearchMigration(
        14,
        "property_research_packet_erasure_trigger_split",
        _PROPERTY_RESEARCH_PACKET_ERASURE_TRIGGER_SPLIT_SCHEMA_V14,
    ),
    PropertySearchMigration(
        15,
        "authoritative_distributed_ingress_admission",
        _DISTRIBUTED_INGRESS_ADMISSION_SCHEMA_V15,
    ),
)
LATEST_PROPERTY_SEARCH_SCHEMA_VERSION = PROPERTY_SEARCH_MIGRATIONS[-1].version

_LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_LEDGER_TABLE} (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    migration_name TEXT NOT NULL,
    checksum_sha256 CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (component, version),
    CHECK (version > 0),
    CHECK (checksum_sha256 ~ '^[0-9a-f]{{64}}$')
)
"""

_REQUIRED_RELATIONS = (
    "property_search_runs",
    "idx_property_search_runs_updated",
    "idx_property_search_runs_principal_updated",
    "idx_property_search_runs_status_updated",
    "idx_property_search_runs_principal_status_updated",
    "idx_property_search_runs_run_principal_key",
    "idx_property_search_runs_delivery_work_updated",
    "idx_property_search_runs_principal_delivery_work_updated",
    "property_search_work_jobs",
    "idx_property_search_work_idempotency",
    "idx_property_search_work_principal_run",
    "idx_property_search_work_claim",
    "property_source_listing_cache",
    "idx_property_source_listing_cache_stored_at",
    "delivery_outbox",
    "idx_delivery_outbox_status_created",
    "idx_delivery_outbox_principal_idempotency_unique",
    "idx_delivery_outbox_retry_schedule",
    "idx_delivery_outbox_principal_status_created",
    "idx_delivery_outbox_claim",
    "property_content_jobs",
    "property_content_job_events",
    "property_content_webhook_events",
    "idx_property_content_jobs_status_updated",
    "idx_property_content_jobs_claim",
    "idx_property_content_job_events_packet_sequence",
    "idx_property_content_webhook_status_updated",
    "idx_property_content_webhook_claim",
    "idx_property_content_jobs_principal_updated",
    "idx_property_content_job_events_principal_sequence",
    "idx_property_content_webhook_principal_updated",
    "property_evidence_overlay_rollups",
    "idx_property_evidence_overlay_lookup",
    "idx_property_evidence_overlay_freshness",
    "property_evidence_overlay_snapshots",
    "idx_property_evidence_overlay_snapshots_ingested",
    "property_evidence_overlay_active_snapshot",
    "idx_property_evidence_overlay_single_active",
    "idx_property_evidence_overlay_snapshot_lookup",
    "idx_property_evidence_overlay_snapshot_freshness",
    "property_research_packet_links",
    "idx_property_research_packet_links_last_seen",
    "idx_property_research_packet_links_property_url",
    "idx_property_research_packet_links_retention",
    "property_research_packet_run_memberships",
    "idx_property_research_packet_memberships_ref",
    "idx_property_research_packet_memberships_observed",
    "idx_property_research_packet_links_principal_key",
    "idx_property_research_packet_memberships_principal_key",
    "property_research_packet_index_state",
    "property_search_erasure_key_state",
    "property_search_erasure_fences",
    "propertyquarry_ingress_quota_buckets",
    "idx_propertyquarry_ingress_quota_expiry",
    "propertyquarry_ingress_leases",
    "idx_propertyquarry_ingress_lease_expiry",
    "idx_propertyquarry_ingress_lease_ip_expiry",
    "idx_propertyquarry_ingress_lease_account_expiry",
    "propertyquarry_ingress_admission_capacity",
)

_FORBIDDEN_RELATIONS = ("idx_delivery_outbox_idempotency_key_unique",)
_REQUIRED_TRIGGERS = (
    ("property_search_runs", "property_search_runs_writer_contract_guard"),
    ("property_search_work_jobs", "property_search_work_jobs_erasure_fence_guard"),
    (
        "property_research_packet_links",
        "property_research_packet_links_erasure_fence_guard",
    ),
    (
        "property_research_packet_run_memberships",
        "property_research_packet_memberships_erasure_fence_guard",
    ),
    (
        "property_search_erasure_key_state",
        "property_search_erasure_key_state_immutable_guard",
    ),
    ("property_content_jobs", "property_content_jobs_account_authority_guard"),
    (
        "property_content_job_events",
        "property_content_job_events_account_authority_guard",
    ),
    (
        "property_content_webhook_events",
        "property_content_webhook_account_authority_guard",
    ),
    (
        "propertyquarry_ingress_quota_buckets",
        "propertyquarry_ingress_quota_identity_guard",
    ),
    (
        "propertyquarry_ingress_quota_buckets",
        "propertyquarry_ingress_quota_capacity_guard",
    ),
    (
        "propertyquarry_ingress_quota_buckets",
        "propertyquarry_ingress_quota_truncate_guard",
    ),
    (
        "propertyquarry_ingress_leases",
        "propertyquarry_ingress_lease_identity_guard",
    ),
    (
        "propertyquarry_ingress_leases",
        "propertyquarry_ingress_lease_capacity_guard",
    ),
    (
        "propertyquarry_ingress_leases",
        "propertyquarry_ingress_lease_truncate_guard",
    ),
    (
        "propertyquarry_ingress_admission_capacity",
        "propertyquarry_ingress_capacity_contract_guard",
    ),
    (
        "propertyquarry_ingress_admission_capacity",
        "propertyquarry_ingress_capacity_truncate_guard",
    ),
)
_REQUIRED_FUNCTIONS = (
    "propertyquarry_ingress_admission_protect_identity()",
    "propertyquarry_ingress_admission_guard_capacity_contract()",
    "propertyquarry_ingress_admission_apply_capacity()",
    "propertyquarry_ingress_admission_forbid_truncate()",
)
_SCHEMA_READY_LOCK = threading.Lock()
_SCHEMA_READY_DATABASES: set[str] = set()


def _migration_by_version() -> dict[int, PropertySearchMigration]:
    return {migration.version: migration for migration in PROPERTY_SEARCH_MIGRATIONS}


def _validate_applied_rows(
    rows: Sequence[Sequence[object]],
) -> tuple[int, ...]:
    expected = _migration_by_version()
    observed: dict[int, tuple[str, str]] = {}
    for row in rows:
        version = int(row[0])
        name = str(row[1] or "")
        checksum = str(row[2] or "").strip().lower()
        if version in observed:
            raise PropertySearchSchemaDriftError(
                f"duplicate_property_search_migration_version:{version}"
            )
        observed[version] = (name, checksum)
    for version, (name, checksum) in sorted(observed.items()):
        migration = expected.get(version)
        if migration is None:
            raise PropertySearchSchemaDriftError(
                f"property_search_schema_ahead:{version}"
            )
        if name != migration.name or checksum != migration.checksum:
            raise PropertySearchSchemaDriftError(
                f"property_search_migration_checksum_drift:{version}"
            )
    versions = tuple(sorted(observed))
    if versions and versions != tuple(range(1, versions[-1] + 1)):
        raise PropertySearchSchemaDriftError("property_search_migration_gap")
    return versions


def _connect(database_url: str, *, autocommit: bool):  # type: ignore[no-untyped-def]
    import psycopg

    return psycopg.connect(
        database_url,
        autocommit=autocommit,
        connect_timeout=5,
    )


def migrate_property_search_schema(
    database_url: str,
    *,
    applied_by: str = "deploy",
    connect: Callable[..., object] | None = None,
) -> PropertySearchMigrationResult:
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        raise PropertySearchSchemaError("database_url_required")
    connector = connect or _connect
    conn = connector(normalized_url, autocommit=False)
    try:
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            cur.execute(_LEDGER_DDL)
            cur.execute(
                f"""
                SELECT version, migration_name, checksum_sha256
                FROM {SCHEMA_LEDGER_TABLE}
                WHERE component = %s
                ORDER BY version
                """,
                (SCHEMA_COMPONENT,),
            )
            before = _validate_applied_rows(cur.fetchall())
            before_set = set(before)
            applied: list[int] = []
            erasure_key_id = ""
            if any(version >= 11 for version in before_set) or any(
                migration.version >= 11
                and migration.version not in before_set
                for migration in PROPERTY_SEARCH_MIGRATIONS
            ):
                from app.product.property_search_storage import (
                    _property_search_erasure_key_id,
                )

                try:
                    erasure_key_id = _property_search_erasure_key_id()
                except RuntimeError as exc:
                    raise PropertySearchSchemaError(str(exc)) from exc
                cur.execute(
                    "SELECT set_config("
                    "'propertyquarry.property_search_erasure_key_id', %s, TRUE"
                    ")",
                    (erasure_key_id,),
                )
                if 11 in before_set:
                    cur.execute("SELECT property_search_assert_erasure_key()")
            for migration in PROPERTY_SEARCH_MIGRATIONS:
                if migration.version in before_set:
                    continue
                if migration.version == 11:
                    from app.product.property_search_storage import (
                        _property_search_principal_key,
                    )

                    cur.execute(
                        "SELECT set_config("
                        "'propertyquarry.property_search_writer_contract', "
                        "'2', TRUE"
                        ")"
                    )

                    cur.execute(
                        """
                        SELECT principal_id
                        FROM (
                            SELECT principal_id FROM property_search_runs
                            UNION
                            SELECT principal_id FROM property_search_work_jobs
                            UNION
                            SELECT principal_id FROM property_research_packet_links
                            UNION
                            SELECT principal_id
                            FROM property_research_packet_run_memberships
                        ) AS property_search_principals
                        ORDER BY principal_id
                        """
                    )
                    principal_key_map: dict[str, str] = {}
                    for row in cur.fetchall():
                        principal_id = str(row[0] or "")
                        if (
                            not principal_id
                            or principal_id != principal_id.strip()
                            or principal_id in principal_key_map
                        ):
                            raise PropertySearchSchemaError(
                                "property_search_principal_identity_invalid"
                            )
                        try:
                            principal_key_map[principal_id] = (
                                _property_search_principal_key(principal_id)
                            )
                        except RuntimeError as exc:
                            raise PropertySearchSchemaError(str(exc)) from exc
                    cur.execute(
                        "SELECT set_config("
                        "'propertyquarry.property_search_principal_key_map', "
                        "%s, TRUE"
                        ")",
                        (
                            json.dumps(
                                principal_key_map,
                                ensure_ascii=True,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
                elif migration.version > 11:
                    cur.execute("SELECT property_search_assert_erasure_key()")
                cur.execute(migration.sql)
                if migration.version == 11:
                    cur.execute("SELECT property_search_assert_erasure_key()")
                cur.execute(
                    f"""
                    INSERT INTO {SCHEMA_LEDGER_TABLE}
                        (component, version, migration_name, checksum_sha256, applied_by)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        SCHEMA_COMPONENT,
                        migration.version,
                        migration.name,
                        migration.checksum,
                        str(applied_by or "deploy").strip()[:120] or "deploy",
                    ),
                )
                applied.append(migration.version)
        conn.commit()  # type: ignore[attr-defined]
    except Exception:
        conn.rollback()  # type: ignore[attr-defined]
        raise
    finally:
        conn.close()  # type: ignore[attr-defined]
    with _SCHEMA_READY_LOCK:
        _SCHEMA_READY_DATABASES.discard(normalized_url)
    return PropertySearchMigrationResult(
        previous_version=before[-1] if before else 0,
        current_version=LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
        applied_versions=tuple(applied),
    )


def inspect_property_search_schema_cursor(  # type: ignore[no-untyped-def]
    cur,
    *,
    verify_capacity_counts: bool = True,
) -> PropertySearchSchemaStatus:
    cur.execute("SELECT to_regclass(%s)", (SCHEMA_LEDGER_TABLE,))
    ledger_row = cur.fetchone()
    if not ledger_row or ledger_row[0] is None:
        return PropertySearchSchemaStatus(
            False,
            "migration_ledger_missing",
            0,
            LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            (),
        )
    cur.execute(
        f"""
        SELECT version, migration_name, checksum_sha256
        FROM {SCHEMA_LEDGER_TABLE}
        WHERE component = %s
        ORDER BY version
        """,
        (SCHEMA_COMPONENT,),
    )
    try:
        versions = _validate_applied_rows(cur.fetchall())
    except PropertySearchSchemaDriftError as exc:
        return PropertySearchSchemaStatus(
            False,
            str(exc),
            0,
            LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            (),
        )
    current = versions[-1] if versions else 0
    if current != LATEST_PROPERTY_SEARCH_SCHEMA_VERSION:
        return PropertySearchSchemaStatus(
            False,
            "migration_pending",
            current,
            LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            versions,
        )
    for relation in _REQUIRED_RELATIONS:
        cur.execute("SELECT to_regclass(%s)", (relation,))
        relation_row = cur.fetchone()
        if not relation_row or relation_row[0] is None:
            return PropertySearchSchemaStatus(
                False,
                f"required_relation_missing:{relation}",
                current,
                LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
                versions,
            )
    for relation in _FORBIDDEN_RELATIONS:
        cur.execute("SELECT to_regclass(%s)", (relation,))
        relation_row = cur.fetchone()
        if relation_row and relation_row[0] is not None:
            return PropertySearchSchemaStatus(
                False,
                f"forbidden_relation_present:{relation}",
                current,
                LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
                versions,
            )
    for relation, trigger in _REQUIRED_TRIGGERS:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_trigger
                WHERE tgrelid = %s::regclass
                  AND tgname = %s
                  AND NOT tgisinternal
                  AND tgenabled IN ('O', 'A')
            )
            """,
            (relation, trigger),
        )
        trigger_row = cur.fetchone()
        if not trigger_row or not trigger_row[0]:
            return PropertySearchSchemaStatus(
                False,
                f"required_trigger_missing:{trigger}",
                current,
                LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
                versions,
            )
    for function in _REQUIRED_FUNCTIONS:
        cur.execute("SELECT to_regprocedure(%s)", (function,))
        function_row = cur.fetchone()
        if not function_row or function_row[0] is None:
            return PropertySearchSchemaStatus(
                False,
                f"required_function_missing:{function}",
                current,
                LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
                versions,
            )
    capacity_count_projection = (
        """
        ,
        CASE capacity_key
            WHEN 'lease' THEN (
                SELECT COUNT(*) FROM propertyquarry_ingress_leases
            )
            WHEN 'quota' THEN (
                SELECT COUNT(*) FROM propertyquarry_ingress_quota_buckets
            )
        END AS actual_row_count
        """
        if verify_capacity_counts
        else ""
    )
    cur.execute(
        """
        SELECT capacity_key, row_count, hard_limit, contract_version
        """
        + capacity_count_projection
        + """
        FROM propertyquarry_ingress_admission_capacity
        ORDER BY capacity_key
        """
    )
    capacity_rows = cur.fetchall()
    expected_capacity = (
        (
            "lease",
            PROPERTYQUARRY_INGRESS_LEASE_CAPACITY_LIMIT,
            PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION,
        ),
        (
            "quota",
            PROPERTYQUARRY_INGRESS_QUOTA_CAPACITY_LIMIT,
            PROPERTYQUARRY_INGRESS_CAPACITY_CONTRACT_VERSION,
        ),
    )
    if len(capacity_rows) != len(expected_capacity):
        return PropertySearchSchemaStatus(
            False,
            "ingress_admission_capacity_contract_invalid",
            current,
            LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            versions,
        )
    for row, (expected_key, expected_limit, expected_version) in zip(
        capacity_rows,
        expected_capacity,
        strict=True,
    ):
        observed_key = str(row[0] or "")
        observed_count = int(row[1])
        observed_limit = int(row[2])
        observed_version = int(row[3])
        if (
            observed_key != expected_key
            or observed_count < 0
            or observed_count > expected_limit
            or observed_limit != expected_limit
            or observed_version != expected_version
        ):
            return PropertySearchSchemaStatus(
                False,
                "ingress_admission_capacity_contract_invalid",
                current,
                LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
                versions,
            )
        if verify_capacity_counts and (
            len(row) != 5 or observed_count != int(row[4])
        ):
            return PropertySearchSchemaStatus(
                False,
                f"ingress_admission_capacity_count_mismatch:{observed_key}",
                current,
                LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
                versions,
            )
    return PropertySearchSchemaStatus(
        True,
        "schema_ready",
        current,
        LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
        versions,
    )


def inspect_property_search_schema(
    database_url: str,
    *,
    connect: Callable[..., object] | None = None,
) -> PropertySearchSchemaStatus:
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        return PropertySearchSchemaStatus(
            False,
            "database_url_missing",
            0,
            LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            (),
        )
    connector = connect or _connect
    try:
        conn = connector(normalized_url, autocommit=True)
        try:
            with conn.cursor() as cur:  # type: ignore[attr-defined]
                return inspect_property_search_schema_cursor(cur)
        finally:
            conn.close()  # type: ignore[attr-defined]
    except Exception as exc:
        return PropertySearchSchemaStatus(
            False,
            f"schema_probe_failed:{exc.__class__.__name__}",
            0,
            LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
            (),
        )


def require_property_search_schema_ready(
    database_url: str,
    *,
    connect: Callable[..., object] | None = None,
) -> None:
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        raise PropertySearchSchemaNotReadyError("database_url_missing")
    if connect is None:
        with _SCHEMA_READY_LOCK:
            if normalized_url in _SCHEMA_READY_DATABASES:
                return
    status = inspect_property_search_schema(normalized_url, connect=connect)
    if not status.ready:
        raise PropertySearchSchemaNotReadyError(
            f"property_search_schema_not_ready:{status.reason}"
        )
    if connect is None:
        with _SCHEMA_READY_LOCK:
            _SCHEMA_READY_DATABASES.add(normalized_url)


def property_search_schema_readiness_required(
    *,
    runtime_mode: str,
    role: str,
    explicit: str = "",
) -> bool:
    normalized_mode = str(runtime_mode or "dev").strip().lower()
    normalized_role = str(role or "api").strip().lower()
    if normalized_mode == "prod" and normalized_role in {"api", "worker", "scheduler"}:
        return True
    normalized_explicit = str(explicit or "").strip().lower()
    return normalized_explicit in {"1", "true", "yes", "on"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Governed PropertyQuarry property-search schema boundary."
    )
    parser.add_argument("operation", choices=("migrate", "check"))
    parser.add_argument("--database-url", default="")
    parser.add_argument("--applied-by", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_url = str(
        args.database_url or os.environ.get("DATABASE_URL") or ""
    ).strip()
    if not database_url:
        print(json.dumps({"status": "failed", "reason": "database_url_missing"}))
        return 2
    if args.operation == "migrate":
        try:
            result = migrate_property_search_schema(
                database_url,
                applied_by=(
                    str(args.applied_by or "").strip()
                    or str(
                        os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or ""
                    ).strip()
                    or str(os.environ.get("EA_ROLE") or "deploy").strip()
                    or "deploy"
                ),
            )
        except PropertySearchSchemaError as exc:
            print(json.dumps({"status": "failed", "reason": str(exc)}))
            return 2
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "reason": f"migration_failed:{exc.__class__.__name__}",
                    }
                )
            )
            return 2
        print(json.dumps({"status": "migrated", **result.as_dict()}, sort_keys=True))
        return 0
    status = inspect_property_search_schema(database_url)
    print(
        json.dumps(
            {"status": "ready" if status.ready else "not_ready", **status.as_dict()},
            sort_keys=True,
        )
    )
    return 0 if status.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
