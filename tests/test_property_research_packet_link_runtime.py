from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from app import runner
from app.api.routes import landing
from app.product import privacy_lifecycle, property_search_schema
from app.product import service as product_service
from app.product.property_research_packet_links import (
    PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
    PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
)
from app.product.property_search_storage import _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION
from scripts import backfill_property_research_packet_links as backfill
from scripts import check_property_search_storage_schema as schema_check


def _writer_heartbeat_payload(
    *,
    role: str,
    instance_id: str,
    observed_at: datetime,
    started_at: datetime | None = None,
    status: str | None = None,
) -> dict[str, object]:
    observed_at = observed_at.astimezone(timezone.utc).replace(microsecond=0)
    started_at = (started_at or observed_at - timedelta(seconds=5)).astimezone(
        timezone.utc
    )
    payload: dict[str, object] = {
        "instance_id": instance_id,
        "started_at_epoch": started_at.timestamp(),
        "role": role,
        "status": status or ("serving" if role == "api" else "loop"),
        "writer_ready": True,
        "epoch": observed_at.timestamp(),
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "pid": 123,
        "profile": "test" if role == "scheduler" else "",
        "property_search_writer_contract": runner._property_search_writer_contract(),
    }
    if role == "scheduler":
        payload["delivery_outbox"] = {
            "queued": 0,
            "claimed": 0,
            "claim_conflicts": 0,
            "sent": 0,
            "retried": 0,
            "dead_lettered": 0,
            "failed": 0,
        }
    if role == "worker":
        payload["property_search_work_queue"] = {"observed": False}
    return payload


def test_resolver_uses_packet_index_before_twelve_run_scan() -> None:
    class _IndexedProduct:
        def get_property_research_packet_link(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["principal_id"] == "tenant-a"
            assert kwargs["candidate_ref"] == "candidate-from-run-99"
            return {
                "candidate": {
                    "candidate_ref": "candidate-from-run-99",
                    "title": "Survives history window",
                    "packet_source_run_id": "run-99",
                },
                "last_run_id": "run-99",
            }

        def list_property_search_runs(self, **_kwargs: object) -> object:
            raise AssertionError("indexed resolution must not scan the latest 12 runs")

    candidate, run_id = landing._property_lookup_candidate_across_runs(
        _IndexedProduct(),
        principal_id="tenant-a",
        candidate_ref="candidate-from-run-99",
        max_runs=12,
    )

    assert candidate and candidate["title"] == "Survives history window"
    assert run_id == "run-99"


def test_product_service_packet_lookup_only_queries_authorized_tenant_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queried: list[tuple[str, str]] = []

    def _load(*, principal_id: str, candidate_ref: str) -> dict[str, object] | None:
        queried.append((principal_id, candidate_ref))
        if principal_id == "tenant-authorized-2":
            return {"candidate": {"candidate_ref": candidate_ref}}
        return None

    monkeypatch.setattr(product_service, "_load_property_research_packet_link_storage", _load)
    instance = SimpleNamespace(
        _property_search_run_principal_ids=lambda **_kwargs: (
            "tenant-authorized-1",
            "tenant-authorized-2",
        )
    )

    result = product_service.ProductService.get_property_research_packet_link(
        instance,
        principal_id="external-principal",
        candidate_ref="ref-a",
    )

    assert queried == [
        ("tenant-authorized-1", "ref-a"),
        ("tenant-authorized-2", "ref-a"),
    ]
    assert result and result["principal_id"] == "tenant-authorized-2"


def test_product_service_dsar_and_erasure_are_limited_to_authorized_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported: list[str] = []
    erased: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        product_service,
        "_export_property_research_packet_data_storage",
        lambda *, principal_id: exported.append(principal_id)
        or ({"principal_id": principal_id, "candidate_ref": f"ref-{principal_id}"},),
    )
    monkeypatch.setattr(
        product_service,
        "_erase_property_search_account_data_storage",
        lambda *, principal_ids: erased.append(tuple(principal_ids))
        or {
            "runs_deleted": 2,
            "work_jobs_deleted": 1,
            "packet_links_deleted": 4,
            "packet_links_legal_hold_retained": 1,
        },
    )
    instance = SimpleNamespace(
        _property_search_run_principal_ids=lambda **_kwargs: ("tenant-a", "tenant-b")
    )

    dsar = product_service.ProductService.export_property_research_packet_data(
        instance,
        principal_id="external",
    )
    result = product_service.ProductService.erase_property_search_account_data(
        instance,
        principal_id="external",
    )

    assert exported == ["tenant-a", "tenant-b"]
    assert erased == [("tenant-a", "tenant-b")]
    assert {row["principal_id"] for row in dsar} == {"tenant-a", "tenant-b"}
    assert result == {
        "principal_count": 2,
        "runs_deleted": 2,
        "work_jobs_deleted": 1,
        "packet_links_deleted": 4,
        "packet_links_legal_hold_retained": 1,
    }


def test_account_erasure_purges_event_snapshot_rejected_by_durable_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "erasure-race-run"
    principal_id = "tenant-erasure-race"
    snapshot_taken = threading.Event()
    erasure_committed = threading.Event()
    writer_errors: list[BaseException] = []
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
        "run_id": run_id,
        "principal_id": principal_id,
        "summary": {},
    }
    monkeypatch.setattr(
        product_service,
        "_state_property_search_run_apply_event",
        lambda **kwargs: {
            **dict(kwargs["state"]),
            "status": kwargs["status"],
            "summary": dict(kwargs.get("summary_updates") or {}),
        },
    )

    def _delayed_store(_record: dict[str, object]) -> bool:
        snapshot_taken.set()
        assert erasure_committed.wait(timeout=5)
        return False

    monkeypatch.setattr(
        product_service,
        "_store_property_search_run_record",
        _delayed_store,
    )

    def _erase_storage(*, principal_ids: tuple[str, ...]) -> dict[str, int]:
        assert principal_ids == (principal_id,)
        erasure_committed.set()
        return {
            "runs_deleted": 1,
            "work_jobs_deleted": 1,
            "packet_links_deleted": 0,
            "packet_links_legal_hold_retained": 0,
        }

    monkeypatch.setattr(
        product_service,
        "_erase_property_search_account_data_storage",
        _erase_storage,
    )
    instance = SimpleNamespace(
        _apply_property_search_run_repair_receipts=lambda **kwargs: dict(
            kwargs["summary"]
        ),
        _property_search_run_principal_ids=lambda **_kwargs: (principal_id,),
    )

    def _write_event() -> None:
        try:
            product_service.ProductService._record_property_search_run_event(
                instance,
                run_id=run_id,
                principal_id=principal_id,
                step="late-worker-event",
                message="Late worker event.",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)

    writer = threading.Thread(target=_write_event, daemon=True)
    writer.start()
    assert snapshot_taken.wait(timeout=5)
    result = product_service.ProductService.erase_property_search_account_data(
        instance,
        principal_id=principal_id,
    )
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert run_id not in product_service._PROPERTY_SEARCH_RUN_REGISTRY
    assert result == {
        "principal_count": 1,
        "runs_deleted": 1,
        "work_jobs_deleted": 1,
        "packet_links_deleted": 0,
        "packet_links_legal_hold_retained": 0,
    }


def test_record_run_event_propagates_unexpected_mutation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        product_service,
        "_property_fact_mutate_run_record",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("database_unavailable")),
    )

    with pytest.raises(RuntimeError, match="database_unavailable"):
        product_service.ProductService._record_property_search_run_event(
            SimpleNamespace(),
            run_id="unexpected-mutation-failure",
            principal_id="tenant-unexpected-mutation-failure",
            step="source_started",
            message="Source started.",
        )


def test_privacy_export_includes_validated_research_packets_and_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []
    packet = {
        "candidate_ref": "candidate-a",
        "packet_sha256": "sha256:packet",
        "run_memberships": [{"run_id": "run-a", "source_rank": 1}],
    }
    product = SimpleNamespace(
        list_property_search_runs=lambda **_kwargs: (),
        export_property_research_packet_data=lambda **kwargs: observed.append(dict(kwargs)) or (packet,),
        list_workspace_access_sessions=lambda **_kwargs: (),
        list_property_saved_shortlist_candidates=lambda **_kwargs: (),
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(database_url="", api_token="privacy-test-secret", runtime_mode="dev"),
        onboarding=SimpleNamespace(status=lambda **_kwargs: {}),
        preference_profiles=SimpleNamespace(export_principal=lambda _principal_id: {}),
        provider_registry=SimpleNamespace(list_persisted_binding_records=lambda **_kwargs: ()),
        channel_runtime=SimpleNamespace(
            list_observations_for_principal=lambda *_args, **_kwargs: (),
            list_delivery_records=lambda *_args, **_kwargs: (),
        ),
    )
    monkeypatch.setattr(privacy_lifecycle, "build_product_service", lambda _container: product)
    monkeypatch.setattr(
        privacy_lifecycle,
        "build_fliplink_packet_service",
        lambda _container: SimpleNamespace(export_principal_data=lambda **_kwargs: {}),
    )
    monkeypatch.setattr(privacy_lifecycle, "list_hosted_property_tours_for_principal", lambda **_kwargs: ())
    monkeypatch.setattr(privacy_lifecycle, "list_privacy_request_records", lambda **_kwargs: ())

    collections, _legacy = privacy_lifecycle._export_collections(
        container=container,
        principal_id="tenant-a",
        account_email="owner@example.test",
    )

    assert observed == [{"principal_id": "tenant-a", "account_email": "owner@example.test"}]
    assert collections["research_packets"] == [packet]


def test_privacy_erasure_uses_exact_search_packet_account_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    erased: list[dict[str, object]] = []
    operation_order: list[str] = []
    product = SimpleNamespace(
        list_workspace_access_sessions=lambda **_kwargs: (),
        erase_property_search_account_data=lambda **kwargs: operation_order.append(
            "durable_search_fence"
        )
        or erased.append(dict(kwargs))
        or {
            "principal_count": 2,
            "runs_deleted": 3,
            "work_jobs_deleted": 5,
            "packet_links_deleted": 4,
            "packet_links_legal_hold_retained": 6,
        },
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(database_url="", api_token="privacy-test-secret", runtime_mode="dev"),
        preference_profiles=SimpleNamespace(erase_principal=lambda _principal_id: {"profiles": 1}),
        onboarding=SimpleNamespace(erase_principal=lambda _principal_id: True),
        provider_registry=SimpleNamespace(list_persisted_binding_records=lambda **_kwargs: ()),
        channel_runtime=SimpleNamespace(erase_principal_data=lambda _principal_id: {"events": 0}),
    )
    monkeypatch.setattr(privacy_lifecycle, "build_product_service", lambda _container: product)
    monkeypatch.setattr(
        privacy_lifecycle,
        "build_fliplink_packet_service",
        lambda _container: SimpleNamespace(erase_principal_data=lambda **_kwargs: {"packets": 0}),
    )
    monkeypatch.setattr(
        privacy_lifecycle,
        "list_hosted_property_tours_for_principal",
        lambda **_kwargs: operation_order.append("tour_revocation_sweep") or (),
    )
    lifecycle = privacy_lifecycle.PropertyAccountPrivacyLifecycle(container)
    record: dict[str, object] = {
        "request_id": "erase-a",
        "status": "awaiting_confirmation",
        "confirmation_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "phases": [],
        "provider_deletion_receipts": [],
        "local_deletion_receipts": {},
        "retention_tombstone": {},
        "subject_ref_digest": "sha256:subject",
    }
    monkeypatch.setattr(lifecycle, "_load", lambda **_kwargs: record)
    monkeypatch.setattr(lifecycle, "_save", lambda value: value)

    result = lifecycle.confirm_and_erase(
        principal_id="tenant-a",
        request_id="erase-a",
        confirmation_phrase="DELETE",
        account_email="owner@example.test",
    )

    assert result["status"] == "completed"
    assert erased == [{"principal_id": "tenant-a", "account_email": "owner@example.test"}]
    assert operation_order == ["durable_search_fence", "tour_revocation_sweep"]
    receipt = dict(dict(result["local_deletion_receipts"])["search_and_preferences"])
    assert receipt["search_runs_deleted"] == 3
    assert receipt["search_work_jobs_deleted"] == 5
    assert receipt["research_packet_links_deleted"] == 4
    assert receipt["research_packet_links_legal_hold_retained"] == 6
    assert receipt["search_principals_erased"] == 2
    phase = next(
        item
        for item in list(result["phases"])
        if dict(item).get("name") == "searches_shortlists_and_preferences"
    )
    assert "Retained 6 research packet link(s) exclusively as explicit legal-hold evidence." in str(
        dict(phase).get("detail") or ""
    )


def test_privacy_erasure_retry_reasserts_fence_before_tour_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_order: list[str] = []
    prior_search_receipt = {"search_runs_deleted": 7}
    product = SimpleNamespace(
        list_workspace_access_sessions=lambda **_kwargs: (),
        erase_property_search_account_data=lambda **_kwargs: operation_order.append(
            "durable_search_fence"
        )
        or {
            "principal_count": 1,
            "runs_deleted": 0,
            "work_jobs_deleted": 0,
            "packet_links_deleted": 0,
            "packet_links_legal_hold_retained": 0,
        },
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(
            database_url="",
            api_token="privacy-test-secret",
            runtime_mode="dev",
        ),
        preference_profiles=SimpleNamespace(
            erase_principal=lambda _principal_id: pytest.fail(
                "completed preference receipt must not be replayed"
            )
        ),
        onboarding=SimpleNamespace(
            erase_principal=lambda _principal_id: pytest.fail(
                "completed onboarding receipt must not be replayed"
            )
        ),
        provider_registry=SimpleNamespace(
            list_persisted_binding_records=lambda **_kwargs: ()
        ),
        channel_runtime=SimpleNamespace(
            erase_principal_data=lambda _principal_id: {"events": 0}
        ),
    )
    monkeypatch.setattr(
        privacy_lifecycle,
        "build_product_service",
        lambda _container: product,
    )
    monkeypatch.setattr(
        privacy_lifecycle,
        "build_fliplink_packet_service",
        lambda _container: SimpleNamespace(
            erase_principal_data=lambda **_kwargs: {"packets": 0}
        ),
    )
    monkeypatch.setattr(
        privacy_lifecycle,
        "list_hosted_property_tours_for_principal",
        lambda **_kwargs: operation_order.append("tour_revocation_sweep") or (),
    )
    lifecycle = privacy_lifecycle.PropertyAccountPrivacyLifecycle(container)
    record: dict[str, object] = {
        "request_id": "erase-retry",
        "status": "failed",
        "phases": [],
        "provider_deletion_receipts": [],
        "local_deletion_receipts": {
            "session_revocation": {"revoked_count": 0},
            "search_and_preferences": prior_search_receipt,
        },
        "retention_tombstone": {},
        "subject_ref_digest": "sha256:subject",
    }
    monkeypatch.setattr(lifecycle, "_load", lambda **_kwargs: record)
    monkeypatch.setattr(lifecycle, "_save", lambda value: value)

    result = lifecycle.confirm_and_erase(
        principal_id="tenant-a",
        request_id="erase-retry",
        confirmation_phrase="DELETE",
        account_email="owner@example.test",
    )

    assert result["status"] == "completed"
    assert operation_order == ["durable_search_fence", "tour_revocation_sweep"]
    assert dict(result["local_deletion_receipts"])["search_and_preferences"] == prior_search_receipt


class _AuditCursor:
    def __init__(self, connection: "_AuditConnection") -> None:
        self.connection = connection
        self.current_rows: list[tuple[object, ...]] = []
        self.current_row: tuple[object, ...] | None = None
        self.rowcount = 1

    def __enter__(self) -> "_AuditCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.connection.executed.append((sql, params))
        normalized = " ".join(sql.split())
        self.current_rows = []
        self.current_row = None
        if "pg_try_advisory_lock" in normalized:
            self.current_row = (True,)
        elif "pg_advisory_unlock" in normalized:
            self.current_row = (True,)
        elif "SELECT memberships.principal_id, memberships.run_id" in normalized:
            self.current_rows = list(self.connection.verification_identities)
        elif "memberships_without_links" in normalized:
            self.current_row = self.connection.link_coverage
        elif "FROM property_research_packet_run_memberships AS memberships" in normalized:
            self.current_row = self.connection.verification_count
        elif "SELECT COUNT(*)" in normalized:
            self.current_row = (self.connection.source_count,)
        elif "FROM property_search_runs" in normalized:
            self.current_rows = self.connection.batches.pop(0) if self.connection.batches else []

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.current_rows)

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        rows = self.current_rows[:size]
        self.current_rows = self.current_rows[size:]
        return rows

    def fetchone(self) -> tuple[int] | None:
        return self.current_row


class _AuditConnection:
    def __init__(self, batches: list[list[tuple[object, ...]]]) -> None:
        self.batches = list(batches)
        self.source_count = sum(len(batch) for batch in batches)
        self.verification_count = (0, 0)
        self.verification_identities: list[tuple[object, ...]] = []
        self.link_coverage = (0, 0)
        self.executed: list[tuple[str, object]] = []

    def cursor(self, name: str | None = None) -> _AuditCursor:
        del name
        return _AuditCursor(self)

    def transaction(self):
        return contextlib.nullcontext()


def _backfill_row(index: int) -> tuple[object, ...]:
    principal_id = f"tenant-{index}"
    run_id = f"run-{index}"
    timestamp = "2026-07-17T09:00:00+00:00"
    return (
        principal_id,
        run_id,
        {
            "principal_id": principal_id,
            "run_id": run_id,
            "updated_at": timestamp,
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": f"candidate-{index}",
                        "property_url": f"https://example.test/{index}",
                    }
                ]
            },
        },
        timestamp,
        timestamp,
    )


def _verification_identity(index: int) -> tuple[object, ...]:
    row = _backfill_row(index)
    link = backfill.project_property_research_packet_links(backfill._run_payload(row))[0]
    return (
        f"tenant-{index}",
        f"run-{index}",
        f"candidate-{index}",
        link["packet_sha256"],
        link["packet_size_bytes"],
    )


def _fleet_proof(*, instances: list[dict[str, object]] | None = None) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    observed_instances = instances or [
        {
            "role": "api",
            "instance_id": "api-1",
            "started_at_epoch": (now - timedelta(seconds=5)).timestamp(),
        }
    ]
    return {
        "contract": backfill.FLEET_PROOF_CONTRACT,
        "status": "ready",
        "writer_contract_version": PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
        "packet_schema_version": PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        "property_search_schema_version": property_search_schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
        "expected_instances": observed_instances,
        "rollout_not_before": (now - timedelta(minutes=1)).isoformat(),
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
    }


def test_backfill_dry_run_is_bounded_and_emits_resumable_noncoverage_receipt() -> None:
    connection = _AuditConnection([[_backfill_row(1), _backfill_row(2)]])

    receipt = backfill.run_backfill(
        connection,
        apply=False,
        batch_size=2,
        max_batches=1,
    )

    assert receipt["contract"] == backfill.BACKFILL_RECEIPT_CONTRACT
    assert receipt["mode"] == "dry_run"
    assert receipt["status"] == "partial"
    assert receipt["coverage_complete"] is False
    assert receipt["run_rows_scanned"] == 2
    assert receipt["expected_membership_rows"] == 2
    # Distinct global refs are assigned only after the bounded exact membership
    # stream is verified; partial scans never retain an unbounded ref set.
    assert receipt["expected_distinct_tenant_refs"] == 0
    assert receipt["links_upserted"] == 0
    assert len(str(dict(receipt["resume_token"])["keyset_digest"])) == 24
    assert receipt["source_run_rows_at_cutoff"] == 2
    executed_sql = next(
        " ".join(sql.split())
        for sql, _params in connection.executed
        if "FROM property_search_runs" in sql and "COUNT(*)" not in sql
    )
    assert "updated_at <= %s::timestamptz" in executed_sql
    assert "LIMIT %s FOR SHARE" in executed_sql
    assert "SKIP LOCKED" not in executed_sql
    assert "INSERT" not in executed_sql
    assert "DELETE" not in executed_sql
    assert "UPDATE" not in executed_sql


def test_backfill_batch_bound_and_apply_claim_contract() -> None:
    with pytest.raises(ValueError, match="batch_size_out_of_range"):
        backfill.run_backfill(_AuditConnection([]), apply=False, batch_size=101)

    apply_sql = " ".join(backfill.BACKFILL_APPLY_BATCH_SQL.split())
    assert "LIMIT %s FOR UPDATE" in apply_sql
    assert "SKIP LOCKED" not in apply_sql
    assert "ORDER BY principal_id ASC, run_id ASC" in apply_sql
    assert "DELETE" not in apply_sql
    coverage_sql = " ".join(backfill.BACKFILL_LINK_COVERAGE_SQL.split())
    assert coverage_sql.count("NOT EXISTS") == 2
    assert "retention_state <> 'legal_hold'" in coverage_sql


def test_backfill_receipt_fails_closed_for_source_count_gap() -> None:
    connection = _AuditConnection([[_backfill_row(1)], []])
    connection.source_count = 2

    receipt = backfill.run_backfill(connection, apply=False, batch_size=1)

    assert receipt["scan_complete"] is True
    assert receipt["run_rows_scanned"] == 1
    assert receipt["source_run_rows_at_cutoff"] == 2
    assert receipt["coverage_complete"] is False
    assert receipt["status"] == "failed"


def test_backfill_checkpoint_is_private_resumable_and_public_token_is_deidentified(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "packet-backfill.checkpoint.json"
    first = backfill.run_backfill(
        _AuditConnection([[_backfill_row(1)]]),
        apply=False,
        batch_size=1,
        max_batches=1,
        checkpoint_path=checkpoint_path,
    )

    assert checkpoint_path.stat().st_mode & 0o077 == 0
    checkpoint = backfill._load_private_json(checkpoint_path)
    assert checkpoint["boundary_principal_id"] == "tenant-1"
    assert "expected_ref_digests" not in checkpoint
    assert len(str(checkpoint["expected_ref_digest_sha256"])) == 64
    assert "tenant-1" not in json.dumps(first["resume_token"], sort_keys=True)

    resumed_connection = _AuditConnection([[]])
    resumed_connection.source_count = 1
    resumed = backfill.run_backfill(
        resumed_connection,
        apply=False,
        checkpoint=checkpoint,
    )

    assert resumed["scan_complete"] is True
    assert resumed["run_rows_scanned"] == 1


def test_backfill_many_ref_checkpoint_state_stays_fixed_size(tmp_path: Path) -> None:
    one_path = tmp_path / "one.checkpoint.json"
    many_path = tmp_path / "many.checkpoint.json"
    backfill.run_backfill(
        _AuditConnection([[_backfill_row(1)]]),
        apply=False,
        batch_size=1,
        max_batches=1,
        checkpoint_path=one_path,
    )
    backfill.run_backfill(
        _AuditConnection([[_backfill_row(index) for index in range(1, 101)]]),
        apply=False,
        batch_size=100,
        max_batches=1,
        checkpoint_path=many_path,
    )

    one = backfill._load_private_json(one_path)
    many = backfill._load_private_json(many_path)
    assert set(one) == set(many)
    assert "expected_ref_digests" not in many
    assert len(str(many["expected_ref_digest_sha256"])) == 64
    assert many_path.stat().st_size < 1_500
    assert abs(many_path.stat().st_size - one_path.stat().st_size) < 64


def test_backfill_checkpoint_cannot_cross_from_audit_to_apply(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "packet-backfill.checkpoint.json"
    backfill.run_backfill(
        _AuditConnection([[_backfill_row(1)]]),
        apply=False,
        max_batches=1,
        checkpoint_path=checkpoint_path,
    )

    receipt = backfill.run_backfill(
        _AuditConnection([]),
        apply=True,
        checkpoint=backfill._load_private_json(checkpoint_path),
    )

    assert receipt["status"] == "failed"
    assert receipt["coverage_complete"] is False
    assert receipt["error_code"] == "checkpoint_mode_mismatch"


def test_apply_backfill_requires_fleet_proof_and_activates_only_after_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_proof = backfill.run_backfill(_AuditConnection([]), apply=True)
    assert missing_proof["status"] == "failed"
    assert missing_proof["coverage_complete"] is False
    assert missing_proof["error_code"] == "fleet_proof_missing_or_invalid"

    connection = _AuditConnection([[_backfill_row(1)], []])
    connection.verification_count = (1, 1)
    connection.verification_identities = [_verification_identity(1)]
    monkeypatch.setattr(backfill, "upsert_property_research_packet_links", lambda _cursor, links: len(links))
    monkeypatch.setattr(backfill, "sync_property_research_packet_run_memberships", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(backfill, "_verify_run_memberships", lambda *_args, **_kwargs: True)
    proof = _fleet_proof()

    receipt = backfill.run_backfill(connection, apply=True, fleet_proof=proof)

    assert receipt["status"] == "complete"
    assert receipt["coverage_complete"] is True
    assert receipt["idempotent_verified"] is True
    assert receipt["expected_membership_rows"] == 1
    assert receipt["verified_membership_rows"] == 1
    assert receipt["ref_digest_set_verified"] is True
    assert receipt["expected_ref_digest_sha256"] == receipt["verified_ref_digest_sha256"]


def test_backfill_projects_reported_legacy_f412_packet_for_indexed_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_ref = "f41235f024333ab5"
    principal_id = "tenant-legacy-property"
    run_id = "legacy-run-outside-latest-twelve"
    timestamp = "2026-07-17T09:00:00+00:00"
    row = (
        principal_id,
        run_id,
        {
            "principal_id": principal_id,
            "run_id": run_id,
            "updated_at": timestamp,
            "summary": {
                "ranked_candidates": [
                    {
                        "candidate_ref": candidate_ref,
                        "title": "Reported legacy property",
                        "property_url": "https://example.test/legacy-property",
                    }
                ]
            },
        },
        timestamp,
        timestamp,
    )
    expected_link = backfill.project_property_research_packet_links(
        backfill._run_payload(row)
    )[0]
    connection = _AuditConnection([[row], []])
    connection.verification_count = (1, 1)
    connection.verification_identities = [
        (
            principal_id,
            run_id,
            candidate_ref,
            expected_link["packet_sha256"],
            expected_link["packet_size_bytes"],
        )
    ]
    captured_links: list[dict[str, object]] = []

    def _capture_links(_cursor: object, links: object) -> int:
        rows = [dict(link) for link in links]  # type: ignore[arg-type]
        captured_links.extend(rows)
        return len(rows)

    monkeypatch.setattr(backfill, "upsert_property_research_packet_links", _capture_links)
    monkeypatch.setattr(
        backfill,
        "sync_property_research_packet_run_memberships",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        backfill,
        "_verify_run_memberships",
        lambda *_args, **_kwargs: True,
    )

    receipt = backfill.run_backfill(
        connection,
        apply=True,
        fleet_proof=_fleet_proof(),
    )

    assert receipt["status"] == "complete"
    assert receipt["coverage_complete"] is True
    indexed_link = next(
        link for link in captured_links if link["candidate_ref"] == candidate_ref
    )

    class _IndexedProduct:
        def get_property_research_packet_link(
            self,
            **kwargs: object,
        ) -> dict[str, object]:
            assert kwargs["principal_id"] == principal_id
            assert kwargs["candidate_ref"] == candidate_ref
            return {
                "candidate": dict(indexed_link["packet_json"]),
                "last_run_id": indexed_link["last_run_id"],
            }

        def list_property_search_runs(self, **_kwargs: object) -> object:
            raise AssertionError("the indexed legacy packet must avoid the latest-12 scan")

    candidate, resolved_run_id = landing._property_lookup_candidate_across_runs(
        _IndexedProduct(),
        principal_id=principal_id,
        candidate_ref=candidate_ref,
        max_runs=12,
    )
    assert candidate and candidate["candidate_ref"] == candidate_ref
    assert resolved_run_id == run_id


def test_apply_backfill_rejects_equal_counts_for_the_wrong_tenant_ref_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _AuditConnection([[_backfill_row(1)], []])
    connection.verification_count = (1, 1)
    wrong_identity = list(_verification_identity(1))
    wrong_identity[0] = "tenant-other"
    wrong_identity[2] = "candidate-other"
    connection.verification_identities = [tuple(wrong_identity)]
    monkeypatch.setattr(backfill, "upsert_property_research_packet_links", lambda _cursor, links: len(links))
    monkeypatch.setattr(backfill, "sync_property_research_packet_run_memberships", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(backfill, "_verify_run_memberships", lambda *_args, **_kwargs: True)
    proof = _fleet_proof()

    receipt = backfill.run_backfill(connection, apply=True, fleet_proof=proof)

    assert receipt["verified_membership_rows"] == receipt["expected_membership_rows"] == 1
    assert receipt["verified_distinct_tenant_refs"] == 1
    assert receipt["expected_distinct_tenant_refs"] == 0
    assert receipt["ref_digest_set_verified"] is False
    assert receipt["idempotent_verified"] is False
    assert receipt["coverage_complete"] is False
    assert receipt["status"] == "failed"


def test_backfill_fleet_proof_requires_closed_schema_and_timestamped_instances() -> None:
    proof = _fleet_proof()
    proof["forged_authority"] = True
    with pytest.raises(ValueError, match="fleet_proof_schema_invalid"):
        backfill._validate_fleet_proof(proof)

    proof = _fleet_proof()
    proof["expected_instances"] = [{"role": "api", "instance_id": "api-1"}]
    with pytest.raises(ValueError, match="fleet_proof_instance_schema_invalid"):
        backfill._validate_fleet_proof(proof)


def test_runner_heartbeat_receipts_current_writer_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    heartbeat_path = tmp_path / "scheduler-heartbeat.json"
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv(
        "EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR",
        str(tmp_path / "writer-fleet"),
    )
    monkeypatch.setattr(runner, "_propertyquarry_scheduler_profile", lambda: "test")

    runner._write_scheduler_heartbeat(role="scheduler", status="loop")

    instance_path = runner._execution_role_heartbeat_path("scheduler")
    assert instance_path is not None and instance_path != heartbeat_path
    payload = json.loads(instance_path.read_text(encoding="utf-8"))
    assert json.loads(heartbeat_path.read_text(encoding="utf-8")) == payload
    assert payload["instance_id"] == runner._EXECUTION_INSTANCE_ID
    assert payload["status"] == "loop"
    assert payload["writer_ready"] is True
    assert payload["property_search_writer_contract"] == {
        "compact_schema_version": _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "research_packet_schema_version": PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        "writer_contract_version": PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
        "property_search_schema_version": property_search_schema.LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
    }
    assert runner._property_search_writer_contract() == payload["property_search_writer_contract"]


def test_checker_accepts_actual_worker_heartbeat_with_queue_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started_at = now - timedelta(seconds=5)
    instance_id = "worker-current-runner-payload"
    heartbeat_dir = tmp_path / "writer-fleet"
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_PATH", str(tmp_path / "worker.json"))
    monkeypatch.setenv("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR", str(heartbeat_dir))
    monkeypatch.setattr(runner, "_EXECUTION_INSTANCE_ID", instance_id)
    monkeypatch.setattr(runner, "_EXECUTION_STARTED_AT_EPOCH", started_at.timestamp())
    monkeypatch.setattr(runner, "_PROPERTY_SEARCH_QUEUE_METRICS", {"observed": False})

    assert runner._record_property_search_queue_metrics(
        SimpleNamespace(depth=7, oldest_item_age_seconds=12.5)
    ) is True
    runner._write_scheduler_heartbeat(role="worker", status="loop")

    instance_path = runner._execution_role_heartbeat_path("worker")
    assert instance_path is not None
    payload = json.loads(instance_path.read_text(encoding="utf-8"))
    assert payload["property_search_work_queue"] == {
        "observed": True,
        "depth": 7,
        "oldest_item_age_seconds": 12.5,
    }
    assert schema_check._validate_writer_fleet(
        heartbeat_dir=heartbeat_dir,
        expected_instances=(("worker", instance_id),),
        not_before=started_at - timedelta(seconds=1),
        max_age_seconds=120,
    ) == [
        {
            "role": "worker",
            "instance_id": instance_id,
            "started_at_epoch": started_at.timestamp(),
        }
    ]


def test_checker_fails_closed_when_live_database_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="database_url_required_for_live_schema_check"):
        schema_check.main(["--require-live-db"])


def test_checker_requires_exact_fresh_per_instance_writer_manifest(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expected = (("api", "api-1"), ("worker", "worker-1"))
    for role, instance_id in expected:
        (tmp_path / f"{role}-{instance_id}.json").write_text(
            json.dumps(
                _writer_heartbeat_payload(
                    role=role,
                    instance_id=instance_id,
                    observed_at=now,
                )
            ),
            encoding="utf-8",
        )

    observed = schema_check._validate_writer_fleet(
        heartbeat_dir=tmp_path,
        expected_instances=expected,
        not_before=now - timedelta(seconds=10),
        max_age_seconds=120,
    )

    assert [(row["role"], row["instance_id"]) for row in observed] == list(expected)
    unexpected = tmp_path / "scheduler-old.json"
    unexpected.write_text(
        json.dumps(
            _writer_heartbeat_payload(
                role="scheduler",
                instance_id="old-writer",
                observed_at=now,
                started_at=now,
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="unexpected_live_writer_instance"):
        schema_check._validate_writer_fleet(
            heartbeat_dir=tmp_path,
            expected_instances=expected,
            not_before=now - timedelta(seconds=10),
            max_age_seconds=120,
        )


@pytest.mark.parametrize(
    ("case", "expected_error"),
    (
        ("startup_status", "writer_heartbeat_not_live"),
        ("arbitrary_running_status", "writer_heartbeat_not_live"),
        ("future_observed_at", "writer_heartbeat_timestamp_in_future"),
        ("extra_key", "writer_heartbeat_schema_invalid"),
        ("string_started_at", "writer_heartbeat_epoch_invalid"),
    ),
)
def test_checker_rejects_noncanonical_or_nonready_writer_heartbeats(
    tmp_path: Path,
    case: str,
    expected_error: str,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = _writer_heartbeat_payload(
        role="api",
        instance_id="api-1",
        observed_at=now,
    )
    if case == "startup_status":
        payload["status"] = "startup"
    elif case == "arbitrary_running_status":
        payload["status"] = "forged_step_running"
    elif case == "future_observed_at":
        future = now + timedelta(seconds=10)
        payload["observed_at"] = future.isoformat().replace("+00:00", "Z")
        payload["epoch"] = future.timestamp()
    elif case == "extra_key":
        payload["unexpected"] = True
    elif case == "string_started_at":
        payload["started_at_epoch"] = str(payload["started_at_epoch"])
    else:  # pragma: no cover - parametrization is closed above.
        raise AssertionError(case)
    (tmp_path / "api-api-1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match=expected_error):
        schema_check._validate_writer_fleet(
            heartbeat_dir=tmp_path,
            expected_instances=(("api", "api-1"),),
            not_before=now - timedelta(seconds=10),
            max_age_seconds=120,
        )


def test_checker_rejects_noncanonical_scheduler_metrics_and_contract_types(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = _writer_heartbeat_payload(
        role="scheduler",
        instance_id="scheduler-1",
        observed_at=now,
    )
    delivery_outbox = dict(payload["delivery_outbox"])
    delivery_outbox["forged"] = 1
    payload["delivery_outbox"] = delivery_outbox
    heartbeat = tmp_path / "scheduler-1.json"
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="writer_heartbeat_delivery_outbox_invalid"):
        schema_check._validate_writer_fleet(
            heartbeat_dir=tmp_path,
            expected_instances=(("scheduler", "scheduler-1"),),
            not_before=now - timedelta(seconds=10),
            max_age_seconds=120,
        )

    payload = _writer_heartbeat_payload(
        role="scheduler",
        instance_id="scheduler-1",
        observed_at=now,
    )
    writer_contract = dict(payload["property_search_writer_contract"])
    writer_contract["research_packet_schema_version"] = True
    payload["property_search_writer_contract"] = writer_contract
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="writer_contract_mismatch"):
        schema_check._validate_writer_fleet(
            heartbeat_dir=tmp_path,
            expected_instances=(("scheduler", "scheduler-1"),),
            not_before=now - timedelta(seconds=10),
            max_age_seconds=120,
        )


@pytest.mark.parametrize(
    ("role", "case", "expected_error"),
    (
        ("worker", "missing_role_payload", "writer_heartbeat_schema_invalid"),
        (
            "worker",
            "unknown_queue_key",
            "writer_heartbeat_property_search_work_queue_invalid",
        ),
        (
            "worker",
            "unobserved_with_metrics",
            "writer_heartbeat_property_search_work_queue_invalid",
        ),
        (
            "worker",
            "invalid_observed_metrics",
            "writer_heartbeat_property_search_work_queue_invalid",
        ),
        ("scheduler", "cross_role_payload", "writer_heartbeat_schema_invalid"),
        ("api", "cross_role_payload", "writer_heartbeat_schema_invalid"),
    ),
)
def test_checker_enforces_closed_role_specific_heartbeat_schemas(
    tmp_path: Path,
    role: str,
    case: str,
    expected_error: str,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    instance_id = f"{role}-1"
    payload = _writer_heartbeat_payload(
        role=role,
        instance_id=instance_id,
        observed_at=now,
    )
    if case == "missing_role_payload":
        payload.pop("property_search_work_queue")
    elif case == "unknown_queue_key":
        queue_metrics = dict(payload["property_search_work_queue"])
        queue_metrics["forged"] = 1
        payload["property_search_work_queue"] = queue_metrics
    elif case == "unobserved_with_metrics":
        payload["property_search_work_queue"] = {
            "observed": False,
            "depth": 0,
            "oldest_item_age_seconds": 0.0,
        }
    elif case == "invalid_observed_metrics":
        payload["property_search_work_queue"] = {
            "observed": True,
            "depth": True,
            "oldest_item_age_seconds": -1.0,
        }
    elif case == "cross_role_payload":
        payload["property_search_work_queue"] = {"observed": False}
    else:  # pragma: no cover - parametrization is closed above.
        raise AssertionError(case)
    (tmp_path / f"{role}-{instance_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=expected_error):
        schema_check._validate_writer_fleet(
            heartbeat_dir=tmp_path,
            expected_instances=((role, instance_id),),
            not_before=now - timedelta(seconds=10),
            max_age_seconds=120,
        )


def test_checker_activation_binds_proof_to_current_fleet_and_rollout_boundary() -> None:
    proof = _fleet_proof()
    observed = list(proof["expected_instances"])
    not_before = schema_check._parse_timestamp(proof["rollout_not_before"])
    assert not_before is not None

    assert schema_check._validate_activation_fleet_proof(
        proof,
        observed_instances=observed,
        not_before=not_before,
    ) == proof

    with pytest.raises(RuntimeError, match="fleet_proof_current_instances_mismatch"):
        schema_check._validate_activation_fleet_proof(
            proof,
            observed_instances=[
                {
                    "role": "api",
                    "instance_id": "api-2",
                    "started_at_epoch": observed[0]["started_at_epoch"],
                }
            ],
            not_before=not_before,
        )
    with pytest.raises(RuntimeError, match="fleet_proof_rollout_not_before_mismatch"):
        schema_check._validate_activation_fleet_proof(
            proof,
            observed_instances=observed,
            not_before=not_before - timedelta(seconds=1),
        )


def test_storage_source_check_tracks_governed_v19_order_and_checksum() -> None:
    schema_source = schema_check.SCHEMA_SOURCE.read_text(encoding="utf-8")
    checker_source = Path(schema_check.__file__).read_text(encoding="utf-8")

    schema_check._check_source_contracts()

    assert "proof = validate_property_research_packet_fleet_proof(" in checker_source

    contracts = schema_check._declared_migration_contracts(schema_source)
    assert contracts == tuple(
        (migration.version, migration.name)
        for migration in property_search_schema.PROPERTY_SEARCH_MIGRATIONS
    )
    assert contracts[-1] == (19, "bounded_storage_retention_control")
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[9].checksum == (
        "83f07c1d91968753e454c79972110881259a01953a6755cfef020adf55e92bc4"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[10].checksum == (
        "83f78ac907ccfb82f8cd4c61eddb4e5437dfc13f7e66143250f6e6bbdd2e2d47"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[11].checksum == (
        "92901d215583a8c41854e3c3236417aca61fa21f03460777f15e5cec7626d25f"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[12].checksum == (
        "192d605e9a96e73bde817c51f28317b491313ebe3cb61f1b4c617256dbb2f8cf"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[13].checksum == (
        "0e89b189e06f2fbaaed1639e80951f87780d4102704d3371bbfc6d48bd124d0b"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[14].checksum == (
        "2f20534f4d824d1bceb763c6016358d2266c1f7e70fda60267005f50b2b53629"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[15].checksum == (
        "11069fd9275f1150beb57cc95d911ce9b2a9ae6bc09793d25ccd4ca8732f4140"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[16].checksum == (
        "25a1fcfc28060abc309f7c767889964b23e694c3ae88209105b23a6ca33ac797"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[17].checksum == (
        "eae758d281c5447d984f15e7d367e3fb181d06f79b6f0b5d277d6b91a2d455e8"
    )
    assert property_search_schema.PROPERTY_SEARCH_MIGRATIONS[18].checksum == (
        "3cee796e77912373a948ee9d9f4613ace502374cad74e753b5073f46366aa96a"
    )
