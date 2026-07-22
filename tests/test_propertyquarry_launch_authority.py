from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import property_evidence_overlay_read_model as overlay_read_model
from scripts import propertyquarry_launch_authority as launch_authority
from scripts import propertyquarry_rybbit_evidence as rybbit_evidence


CANDIDATE_SHA = "a" * 40
HEAD_SHA = "b" * 40
RUN_ID = "73921"
RUN_ATTEMPT = "2"
TEABLE_ORIGIN = "https://app.teable.io"
TEABLE_BASE_ID_SHA256 = "1" * 64
RYBBIT_PUBLIC_ORIGIN = "https://propertyquarry.com"
RYBBIT_ANALYTICS_ORIGIN = "https://app.rybbit.io"
RYBBIT_SITE_ID = "propertyquarry-production"
RYBBIT_SITE_ID_SHA256 = hashlib.sha256(RYBBIT_SITE_ID.encode()).hexdigest()
GENERATED_AT = datetime(2026, 7, 16, 17, 0, tzinfo=timezone.utc)


def test_launch_authority_requires_temporal_overlay_receipt_v3() -> None:
    assert (
        launch_authority.OVERLAY_SCHEMA
        == "propertyquarry.evidence_overlay_read_model_receipt.v3"
    )


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _security_payload() -> dict[str, object]:
    return {
        "schema": launch_authority.SECURITY_SCHEMA,
        "mode": "flagship",
        "status": "pass",
        "gate_passed": True,
        "severity_threshold": "HIGH",
        "identities": {"release_commit_sha": CANDIDATE_SHA},
        "summary": {"blocking": 0},
    }


def _security_binding_payload(
    *,
    candidate_sha: str = CANDIDATE_SHA,
    head_sha: str = HEAD_SHA,
    run_id: str = RUN_ID,
    run_attempt: str = RUN_ATTEMPT,
) -> dict[str, object]:
    return {
        "contract_name": launch_authority.SECURITY_BINDING_CONTRACT,
        "version": 1,
        "product": "PropertyQuarry",
        "runtime_commit_sha": candidate_sha,
        "workflow_head_sha": head_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
    }


def _activation_payload() -> dict[str, object]:
    required_checks = (
        "protected_live_configuration",
        "idempotent_run_reservation",
        "activation_step_matrix_complete",
        "safe_cleanup_complete",
    )
    return {
        "generated_at": GENERATED_AT.isoformat(),
        "status": "pass",
        "failed_count": 0,
        "candidate_sha": CANDIDATE_SHA,
        "proof_mode": "deployed_playwright",
        "run_key": "launch-activation-current-run",
        "live_contract": {
            "deployed_playwright_runner": True,
            "local_execution_forbidden": True,
            "provider_response_mocking_forbidden": True,
            "principal_headers_forbidden": True,
            "session_injection_forbidden": True,
        },
        "checks": [{"name": name, "ok": True} for name in required_checks],
        "steps": [
            {"name": "account_create_or_reopen", "ok": True},
            {"name": "real_provider_results", "ok": True},
            {"name": "safe_cleanup", "ok": True},
        ],
    }


def _overlay_export(registry: dict[str, object]) -> dict[str, object]:
    generated_at = GENERATED_AT.isoformat()
    tables: dict[str, list[dict[str, object]]] = {}
    source_tables: dict[str, dict[str, object]] = {}
    for index, layer in enumerate(overlay_read_model._registry_layers(registry)):
        layer_key = str(layer["layer_key"])
        table_name = str(layer["teable_table"])
        fields: dict[str, object] = {
            "match": {"postal_code": f"10{index:02d}"},
            "summary": f"Verified {layer_key} context",
            "source_name": "Launch source",
            "source_url": f"https://source.example.com/{layer_key}",
            "source_updated_at": generated_at,
            "cache_updated_at": generated_at,
            "uncertainty_label": "area-level context",
            "ui_state": "verified",
        }
        temporalities = set(layer["allowed_source_temporalities"])
        if "current_feed" in temporalities:
            fields["source_temporality"] = "current_feed"
            fields["source_checked_at"] = generated_at
        elif "live" in temporalities:
            fields["source_temporality"] = "live"
        else:
            fields["source_temporality"] = "reference"
            fields["reference_period"] = "2025"
        if layer_key == "media_attention":
            fields.update(
                {
                    "article_url": "https://news.example.com/article",
                    "media_source_class": "independent_press",
                    "independent_press": True,
                }
            )
        if layer_key == "official_safety_context":
            fields.update(
                {
                    "geographic_scope": "district_aggregate",
                    "rights_caveat": "Reuse subject to the official source terms.",
                    "property_scoring": False,
                    "person_scoring": False,
                }
            )
        tables[table_name] = [{"id": f"record-{index}", "fields": fields}]
        source_tables[table_name] = {
            "table_id_sha256": f"{index + 1:064x}",
            "record_count": 1,
            "page_count": 1,
            "pages": [
                {
                    "status_code": 200,
                    "response_sha256": f"{index + 101:064x}",
                    "size_bytes": 128,
                }
            ],
        }
    return {
        "schema": overlay_read_model.EXPORT_SCHEMA,
        "generated_at": generated_at,
        "tables": tables,
        "source_evidence": {
            "mode": "authenticated_teable_api",
            "auth_kind": "bearer_api_key",
            "secret_in_export": False,
            "base_origin": TEABLE_ORIGIN,
            "base_id_sha256": TEABLE_BASE_ID_SHA256,
            "redirects_followed": False,
            "table_discovery": {
                "status_code": 200,
                "response_sha256": "e" * 64,
                "size_bytes": 256,
            },
            "tables": source_tables,
        },
    }


class _OverlayRepository:
    def __init__(self, plan: dict[str, object]) -> None:
        self.plan = plan
        self.active_id = "d" * 64

    def ensure_schema(self) -> None:
        return None

    def active_snapshot_id(self) -> str:
        return self.active_id

    def stage_snapshot(self, **_kwargs: object) -> None:
        return None

    def _coverage(self) -> list[dict[str, object]]:
        return [
            {
                "layer_key": row["layer_key"],
                "teable_table": row["teable_table"],
                "record_count": 1,
                "latest_cache_updated_at": row["cache_updated_at"],
                "latest_ingested_at": GENERATED_AT.isoformat(),
            }
            for row in (dict(record) for record in list(self.plan["records"]))
        ]

    def coverage(self, *, snapshot_id: str = "") -> list[dict[str, object]]:
        del snapshot_id
        return self._coverage()

    def lookup(
        self,
        lookup_values: dict[str, str],
        *,
        snapshot_id: str = "",
    ) -> list[dict[str, object]]:
        del snapshot_id
        for row in (dict(record) for record in list(self.plan["records"])):
            match = dict(row["match"])
            if any(match.get(key) == value for key, value in lookup_values.items()):
                return [{"layer_key": row["layer_key"]}]
        return []

    def benchmark_samples(
        self,
        *,
        snapshot_id: str,
    ) -> list[tuple[str, dict[str, str]]]:
        del snapshot_id
        return [
            (str(row["layer_key"]), dict(row["match"]))
            for row in (dict(record) for record in list(self.plan["records"]))
        ]

    def discard_staged_snapshot(self, snapshot_id: str) -> None:
        del snapshot_id

    def activate_snapshot(
        self,
        *,
        snapshot_id: str,
        activated_at: str,
        expected_previous_snapshot_id: str,
    ) -> str:
        del activated_at
        assert expected_previous_snapshot_id == self.active_id
        previous = self.active_id
        self.active_id = snapshot_id
        return previous

    def restore_active_snapshot(
        self,
        *,
        failed_snapshot_id: str,
        restore_snapshot_id: str,
        restored_at: str,
    ) -> bool:
        del failed_snapshot_id, restored_at
        self.active_id = restore_snapshot_id
        return True


def _staged_overlay_receipt() -> tuple[dict[str, object], _OverlayRepository]:
    registry = json.loads(
        overlay_read_model.REGISTRY_PATH.read_text(encoding="utf-8")
    )
    plan = overlay_read_model.build_ingestion_plan(
        export=_overlay_export(registry),
        registry=registry,
        candidate_sha=CANDIDATE_SHA,
        max_age_hours=launch_authority.MAX_OVERLAY_RECEIPT_AGE_HOURS,
        expected_teable_origin=TEABLE_ORIGIN,
        expected_teable_base_id_sha256=TEABLE_BASE_ID_SHA256,
        now=GENERATED_AT,
    )
    assert plan["status"] == "pass", plan["failures"]
    repository = _OverlayRepository(plan)
    receipt = overlay_read_model.execute_ingestion(
        plan=plan,
        repository=repository,
        candidate_sha=CANDIDATE_SHA,
        max_query_ms=100.0,
        stage_only=True,
        observed_at=GENERATED_AT,
    )
    assert receipt["status"] == "pass", receipt["failures"]
    return receipt, repository


def _overlay_payload() -> dict[str, object]:
    staged, repository = _staged_overlay_receipt()
    active = overlay_read_model.activate_staged_receipt(
        receipt=staged,
        repository=repository,
        snapshot_id=str(staged["snapshot_id"]),
        expected_candidate_sha=CANDIDATE_SHA,
        max_age_hours=launch_authority.MAX_OVERLAY_RECEIPT_AGE_HOURS,
        expected_teable_origin=TEABLE_ORIGIN,
        expected_teable_base_id_sha256=TEABLE_BASE_ID_SHA256,
        activation_authority_sha256="0" * 64,
        staged_receipt_sha256="f" * 64,
        authorized_workflow={
            "head_sha": HEAD_SHA,
            "run_id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
        },
        now=GENERATED_AT,
    )
    assert active["status"] == "pass", active["failures"]
    return active


def _staged_overlay_payload() -> dict[str, object]:
    staged, _repository = _staged_overlay_receipt()
    return staged


def _activation_authority_payload(
    staged_receipt_sha256: str,
    *,
    snapshot_id: str,
) -> dict[str, object]:
    return {
        "schema": launch_authority.SCHEMA,
        "status": "pass",
        "generated_at": GENERATED_AT.isoformat(),
        "authority_phase": "preactivation",
        "candidate_sha": CANDIDATE_SHA,
        "workflow": {
            "head_sha": HEAD_SHA,
            "run_id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
        },
        "teable_authority": {
            "origin": TEABLE_ORIGIN,
            "base_id_sha256": TEABLE_BASE_ID_SHA256,
            "supplied_independently": True,
        },
        "rybbit_authority": {
            "public_origin": RYBBIT_PUBLIC_ORIGIN,
            "analytics_origin": RYBBIT_ANALYTICS_ORIGIN,
            "site_id_sha256": RYBBIT_SITE_ID_SHA256,
            "supplied_independently": True,
        },
        "activation_scope": {
            "snapshot_id": snapshot_id,
            "staged_overlay_receipt_sha256": staged_receipt_sha256,
            "activation_authority_sha256": "",
        },
        "inputs": {"overlay": {"sha256": staged_receipt_sha256}},
        "checks": [{"name": "staged_current_run_evidence", "ok": True}],
        "failures": [],
        "activation_authorized": True,
        "launch_authorized": False,
        "notification_authorized": False,
    }


def _rybbit_api_provenance(digest_digit: str) -> dict[str, object]:
    digest = digest_digit * 64
    return {
        "response_sha256": digest,
        "response_size_bytes": 120,
        "response_limit_bytes": rybbit_evidence.MAX_RYBBIT_API_RESPONSE_BYTES,
        "content_type": "application/json",
        "requested_url_origin": RYBBIT_ANALYTICS_ORIGIN,
        "final_url_origin": RYBBIT_ANALYTICS_ORIGIN,
        "requested_url_sha256": digest,
        "final_url_sha256": digest,
        "same_request_url": True,
        "redirected": False,
    }


def _rybbit_payload() -> dict[str, object]:
    sent_at = GENERATED_AT - timedelta(seconds=5)
    observed_at = GENERATED_AT - timedelta(seconds=4)
    browser: dict[str, object] = {
        "script": {
            "url": f"{RYBBIT_ANALYTICS_ORIGIN}/api/script.js",
            "status_code": 200,
            "sha256": "1" * 64,
            "size_bytes": 42_000,
            "site_id_bound": True,
        },
        "collector": {
            "url_origin": RYBBIT_ANALYTICS_ORIGIN,
            "url_path": rybbit_evidence.RYBBIT_COLLECTOR_PATH,
            "url_sha256": "2" * 64,
            "method": "POST",
            "status_code": 204,
            "response_sha256": "3" * 64,
            "size_bytes": 0,
            "request_payload_sha256": "7" * 64,
            "request_payload_size_bytes": 74,
            "event_name_bound": True,
            "observed_at": observed_at.isoformat(),
        },
        "event": {
            "name": rybbit_evidence.PROBE_EVENT_NAME,
            "sent_at": sent_at.isoformat(),
            "anonymous": True,
            "attribute_count": 0,
        },
        "privacy": {
            check: True for check in rybbit_evidence.REQUIRED_PRIVACY_CHECKS
        },
    }
    api: dict[str, object] = {
        "auth": {"kind": "bearer_api_key", "secret_in_receipt": False},
        "site": {
            "status_code": 200,
            **_rybbit_api_provenance("4"),
            "site_id_bound": True,
        },
        "has_data": {
            "status_code": 200,
            **_rybbit_api_provenance("5"),
            "has_data": True,
        },
        "events": {
            "status_code": 200,
            **_rybbit_api_provenance("6"),
            "event_name": rybbit_evidence.PROBE_EVENT_NAME,
            "event_count": 1,
            "last_seen_at": observed_at.isoformat(),
            "observed_after_probe": True,
        },
    }
    return rybbit_evidence.build_receipt(
        candidate_sha=CANDIDATE_SHA,
        public_origin=RYBBIT_PUBLIC_ORIGIN,
        analytics_origin=RYBBIT_ANALYTICS_ORIGIN,
        site_id=RYBBIT_SITE_ID,
        browser=browser,
        api=api,
        generated_at=GENERATED_AT,
    )


def _live_payload(*, security_sha: str, binding_sha: str) -> dict[str, object]:
    release = {
        "release_repository": "ArchonMegalon/propertyquarry",
        "release_public_origin": "https://propertyquarry.com",
        "release_branch": "main",
        "release_commit_sha": CANDIDATE_SHA,
        "release_deployment_id": "propertyquarry-governed-deploy-aaaaaaaaaaaa",
        "release_artifact_set": "propertyquarry-runtime-v8",
        "release_label": "propertyquarry-flagship",
        "release_generated_at": GENERATED_AT.isoformat(),
        "release_image_digest": "sha256:" + "e" * 64,
        "replica_id": "propertyquarry-api-1",
    }
    return {
        "contract_name": "propertyquarry.live_release_provenance.v2",
        "generated_at": GENERATED_AT.isoformat(),
        "status": "pass",
        "failed_count": 0,
        "expected": release,
        "actual": deepcopy(release),
        "checks": [{"name": "all_current_release_fields_match", "ok": True}],
        "security_receipt_binding": {
            "verified": True,
            "receipt_sha256": security_sha,
            "workflow_binding_sha256": binding_sha,
            "release_commit_sha": CANDIDATE_SHA,
            "release_image_digest": "sha256:" + "e" * 64,
            "workflow_head_sha": HEAD_SHA,
            "workflow_run_id": RUN_ID,
            "workflow_run_attempt": RUN_ATTEMPT,
        },
    }


def _gold_payload(
    *,
    activation_path: Path,
    overlay_path: Path,
    overlay_snapshot_id: str,
    staged_overlay_receipt_sha256: str,
    rybbit_path: Path,
) -> dict[str, object]:
    return {
        "generated_at": GENERATED_AT.isoformat(),
        "status": "pass",
        "ready_for_notification": True,
        "readiness_profile": "launch",
        "blockers": [],
        "next_required_actions": [],
        "flagship_customer_ux_evidence": {
            "required": True,
            "ready": True,
            "missing_receipts": [],
        },
        "launch_product_data_evidence": {
            "required": True,
            "ready": True,
            "evidence_overlay_read_model": {
                "status": "pass",
                "candidate_sha": CANDIDATE_SHA,
                "snapshot_id": overlay_snapshot_id,
                "receipt_sha256": staged_overlay_receipt_sha256,
                "activation_phase": "staged",
                "source_evidence": {
                    "base_origin": TEABLE_ORIGIN,
                    "base_id_sha256": TEABLE_BASE_ID_SHA256,
                },
                "source_authority": {
                    "expected_origin": TEABLE_ORIGIN,
                    "expected_base_id_sha256": TEABLE_BASE_ID_SHA256,
                    "bound_independently": True,
                },
            },
            "rybbit_delivery": {
                "status": "pass",
                "candidate_sha": CANDIDATE_SHA,
            },
        },
        "canonical_launch_evidence": {
            "required": True,
            "status": "pass",
            "validation_errors": [],
            "slo": {"status": "pass"},
            "observability": {
                "status": "pass",
                "cross_receipt_links_verified": True,
            },
        },
        "activation_to_value": {
            "status": "pass",
            "flagship_proof_ok": True,
            "proof_mode": "deployed_playwright",
        },
        "release_hygiene": {
            "status": "pass",
            "manifest_runtime_commit": CANDIDATE_SHA,
            "head_commit": HEAD_SHA,
            "parent_commit": CANDIDATE_SHA,
            "manifest_descendant_paths": [],
            "manifest_metadata_only_ancestor": False,
        },
        "slo_evidence": {
            "status": "pass",
            "release_commit_sha": CANDIDATE_SHA,
        },
        "pass_areas": [
            {
                "area": "release_hygiene",
                "status": "pass",
                "manifest_runtime_commit": CANDIDATE_SHA,
                "head_commit": HEAD_SHA,
                "parent_commit": CANDIDATE_SHA,
                "manifest_descendant_paths": [],
                "manifest_metadata_only_ancestor": False,
            },
            {
                "area": "slo_evidence",
                "status": "pass",
                "release_commit_sha": CANDIDATE_SHA,
            },
            {
                "area": "evidence_overlay_read_model",
                "status": "pass",
                "candidate_sha": CANDIDATE_SHA,
                "snapshot_id": overlay_snapshot_id,
                "receipt_sha256": staged_overlay_receipt_sha256,
                "receipt_path": str(overlay_path),
            },
            {
                "area": "rybbit_delivery",
                "status": "pass",
                "candidate_sha": CANDIDATE_SHA,
                "receipt_path": str(rybbit_path),
            },
            {
                "area": "activation_to_value",
                "status": "pass",
                "candidate_sha": CANDIDATE_SHA,
                "receipt_path": str(activation_path),
            },
            {"area": "canonical_launch_evidence", "status": "pass"},
        ],
    }


def _inputs(tmp_path: Path) -> dict[str, object]:
    security = _write_json(tmp_path / "security.json", _security_payload())
    binding = _write_json(tmp_path / "security-binding.json", _security_binding_payload())
    activation = _write_json(tmp_path / "activation.json", _activation_payload())
    overlay_payload = _overlay_payload()
    overlay_snapshot_id = str(overlay_payload["snapshot_id"])
    activation_authority = _write_json(
        tmp_path / "activation-authority.json",
        _activation_authority_payload(
            "f" * 64,
            snapshot_id=overlay_snapshot_id,
        ),
    )
    overlay_activation = overlay_payload["activation"]
    assert isinstance(overlay_activation, dict)
    overlay_activation["activation_authority_sha256"] = _digest(
        activation_authority
    )
    overlay = _write_json(tmp_path / "overlay.json", overlay_payload)
    rybbit = _write_json(tmp_path / "rybbit.json", _rybbit_payload())
    live = _write_json(
        tmp_path / "live.json",
        _live_payload(security_sha=_digest(security), binding_sha=_digest(binding)),
    )
    gold = _write_json(
        tmp_path / "gold.json",
        _gold_payload(
            activation_path=activation,
            overlay_path=overlay,
            overlay_snapshot_id=overlay_snapshot_id,
            staged_overlay_receipt_sha256="f" * 64,
            rybbit_path=rybbit,
        ),
    )
    controller = tmp_path / "propertyquarry-release-controller-v1.tar.zst"
    controller.write_bytes(b"protected-controller-bundle\0v1")
    controller.chmod(0o600)
    return {
        "candidate_sha": CANDIDATE_SHA,
        "workflow_head_sha": HEAD_SHA,
        "workflow_run_id": RUN_ID,
        "workflow_run_attempt": RUN_ATTEMPT,
        "expected_teable_origin": TEABLE_ORIGIN,
        "expected_teable_base_id_sha256": TEABLE_BASE_ID_SHA256,
        "expected_rybbit_public_origin": RYBBIT_PUBLIC_ORIGIN,
        "expected_rybbit_analytics_origin": RYBBIT_ANALYTICS_ORIGIN,
        "expected_rybbit_site_id_sha256": RYBBIT_SITE_ID_SHA256,
        "gold_status_path": gold,
        "live_provenance_path": live,
        "activation_receipt_path": activation,
        "overlay_receipt_path": overlay,
        "rybbit_receipt_path": rybbit,
        "security_receipt_path": security,
        "security_workflow_binding_path": binding,
        "controller_bundle_path": controller,
        "expected_controller_bundle_sha256": _digest(controller),
        "activation_authority_path": activation_authority,
        "generated_at": GENERATED_AT,
    }


def _build(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    inputs = _inputs(tmp_path)
    return launch_authority.build_launch_authority_envelope(**inputs), inputs


def _preactivation_inputs(tmp_path: Path) -> dict[str, object]:
    inputs = _inputs(tmp_path)
    overlay_path = inputs["overlay_receipt_path"]
    assert isinstance(overlay_path, Path)
    _rewrite(overlay_path, _staged_overlay_payload())
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold = _read_payload(gold_path)
    staged_sha256 = _digest(overlay_path)
    launch_product_data = gold["launch_product_data_evidence"]
    assert isinstance(launch_product_data, dict)
    top_overlay = launch_product_data["evidence_overlay_read_model"]
    assert isinstance(top_overlay, dict)
    top_overlay["receipt_sha256"] = staged_sha256
    top_overlay["snapshot_id"] = _read_payload(overlay_path)["snapshot_id"]
    pass_areas = gold["pass_areas"]
    assert isinstance(pass_areas, list)
    overlay_area = next(
        row
        for row in pass_areas
        if isinstance(row, dict) and row.get("area") == "evidence_overlay_read_model"
    )
    overlay_area["receipt_sha256"] = staged_sha256
    overlay_area["snapshot_id"] = _read_payload(overlay_path)["snapshot_id"]
    _rewrite(gold_path, gold)
    inputs["authority_phase"] = "preactivation"
    inputs.pop("activation_authority_path")
    return inputs


def _read_payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _rewrite(path: Path, payload: dict[str, object]) -> None:
    _write_json(path, payload)


def test_launch_authority_binds_every_exact_input_and_current_workflow_run(
    tmp_path: Path,
) -> None:
    envelope, inputs = _build(tmp_path)

    assert envelope["status"] == "pass"
    assert envelope["authority_phase"] == "final"
    assert envelope["activation_authorized"] is True
    assert envelope["launch_authorized"] is True
    assert envelope["notification_authorized"] is True
    assert envelope["failures"] == []
    assert envelope["workflow"] == {
        "head_sha": HEAD_SHA,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
    }
    assert envelope["release_hygiene_binding"] == {
        "manifest_runtime_commit": CANDIDATE_SHA,
        "head_commit": HEAD_SHA,
        "parent_commit": CANDIDATE_SHA,
        "manifest_descendant_paths": [],
        "manifest_metadata_only_ancestor": False,
    }
    assert envelope["teable_authority"] == {
        "origin": TEABLE_ORIGIN,
        "base_id_sha256": TEABLE_BASE_ID_SHA256,
        "supplied_independently": True,
    }
    assert envelope["rybbit_authority"] == {
        "public_origin": RYBBIT_PUBLIC_ORIGIN,
        "analytics_origin": RYBBIT_ANALYTICS_ORIGIN,
        "site_id_sha256": RYBBIT_SITE_ID_SHA256,
        "supplied_independently": True,
    }
    expected_names = {
        "gold_status",
        "live_provenance",
        "activation",
        "overlay",
        "rybbit",
        "security",
        "security_workflow_binding",
        "controller_bundle",
        "activation_authority",
    }
    identities = envelope["inputs"]
    assert isinstance(identities, dict)
    assert set(identities) == expected_names
    path_by_name = {
        "gold_status": inputs["gold_status_path"],
        "live_provenance": inputs["live_provenance_path"],
        "activation": inputs["activation_receipt_path"],
        "overlay": inputs["overlay_receipt_path"],
        "rybbit": inputs["rybbit_receipt_path"],
        "security": inputs["security_receipt_path"],
        "security_workflow_binding": inputs["security_workflow_binding_path"],
        "controller_bundle": inputs["controller_bundle_path"],
        "activation_authority": inputs["activation_authority_path"],
    }
    for name, source in path_by_name.items():
        assert isinstance(source, Path)
        identity = identities[name]
        assert identity["sha256"] == _digest(source)
        assert identity["size_bytes"] == source.stat().st_size
    controller = inputs["controller_bundle_path"]
    assert isinstance(controller, Path)
    assert envelope["controller_bundle_sha256"] == _digest(controller)
    assert all(row["ok"] is True for row in envelope["checks"])


def test_preactivation_authority_binds_exact_staged_overlay_without_launching(
    tmp_path: Path,
) -> None:
    inputs = _preactivation_inputs(tmp_path)
    overlay_path = inputs["overlay_receipt_path"]
    assert isinstance(overlay_path, Path)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "pass"
    assert envelope["authority_phase"] == "preactivation"
    assert envelope["activation_authorized"] is True
    assert envelope["launch_authorized"] is False
    assert envelope["notification_authorized"] is False
    assert envelope["activation_scope"] == {
        "snapshot_id": _read_payload(overlay_path)["snapshot_id"],
        "staged_overlay_receipt_sha256": _digest(overlay_path),
        "activation_authority_sha256": "",
    }
    assert "activation_authority" not in envelope["inputs"]


def test_preactivation_authority_rejects_same_path_overlay_byte_tamper_after_gold(
    tmp_path: Path,
) -> None:
    inputs = _preactivation_inputs(tmp_path)
    overlay_path = inputs["overlay_receipt_path"]
    assert isinstance(overlay_path, Path)
    overlay_path.write_bytes(overlay_path.read_bytes() + b"\n")
    overlay_path.chmod(0o600)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_overlay_receipt_sha256_mismatch" in envelope["failures"]


@pytest.mark.parametrize("authority_case", ["missing", "wrong", "stale"])
def test_final_authority_refuses_missing_wrong_or_stale_preauthority(
    tmp_path: Path,
    authority_case: str,
) -> None:
    inputs = _inputs(tmp_path)
    authority_path = inputs["activation_authority_path"]
    overlay_path = inputs["overlay_receipt_path"]
    assert isinstance(authority_path, Path)
    assert isinstance(overlay_path, Path)
    if authority_case == "missing":
        inputs["activation_authority_path"] = None
    else:
        authority = _read_payload(authority_path)
        if authority_case == "wrong":
            authority["workflow"]["run_id"] = "73920"  # type: ignore[index]
        else:
            authority["generated_at"] = (
                GENERATED_AT
                - timedelta(
                    seconds=launch_authority.MAX_ACTIVATION_AUTHORITY_AGE_SECONDS + 1
                )
            ).isoformat()
        _rewrite(authority_path, authority)
        overlay = _read_payload(overlay_path)
        overlay["activation"]["activation_authority_sha256"] = _digest(  # type: ignore[index]
            authority_path
        )
        _rewrite(overlay_path, overlay)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert envelope["activation_authorized"] is False
    assert envelope["launch_authorized"] is False
    expected_failure = (
        "activation_authority_missing"
        if authority_case == "missing"
        else "activation_authority_mismatch"
    )
    assert expected_failure in envelope["failures"]


def test_launch_authority_rejects_gold_envelope_head_as_runtime_candidate(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold = _read_payload(gold_path)
    gold["release_hygiene"]["head_commit"] = CANDIDATE_SHA  # type: ignore[index]
    _rewrite(gold_path, gold)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_pass_area_candidate_mismatch" in envelope["failures"]


def test_launch_authority_preserves_valid_metadata_only_ancestry_projection(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold = _read_payload(gold_path)
    projection = {
        "parent_commit": "d" * 40,
        "manifest_descendant_paths": [
            "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"
        ],
        "manifest_metadata_only_ancestor": True,
    }
    gold["release_hygiene"].update(projection)  # type: ignore[union-attr]
    gold["pass_areas"][0].update(projection)  # type: ignore[index,union-attr]
    _rewrite(gold_path, gold)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "pass"
    assert envelope["release_hygiene_binding"] == {
        "manifest_runtime_commit": CANDIDATE_SHA,
        "head_commit": HEAD_SHA,
        **projection,
    }


def test_launch_authority_rejects_missing_release_hygiene_ancestry_projection(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold = _read_payload(gold_path)
    ancestry_fields = {
        "parent_commit",
        "manifest_descendant_paths",
        "manifest_metadata_only_ancestor",
    }
    for projection in (gold["release_hygiene"], gold["pass_areas"][0]):  # type: ignore[index]
        assert isinstance(projection, dict)
        for field in ancestry_fields:
            projection.pop(field, None)
    _rewrite(gold_path, gold)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_pass_area_candidate_mismatch" in envelope["failures"]


def test_launch_authority_rejects_disallowed_metadata_ancestry_projection(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold = _read_payload(gold_path)
    gold["release_hygiene"].update(  # type: ignore[union-attr]
        {
            "parent_commit": "d" * 40,
            "manifest_descendant_paths": ["ea/app/main.py"],
            "manifest_metadata_only_ancestor": True,
        }
    )
    _rewrite(gold_path, gold)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_pass_area_candidate_mismatch" in envelope["failures"]


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    [
        ("gold_status", "gold_launch_top_level_not_pass"),
        ("gold_customer_ux", "gold_customer_ux_not_ready"),
        ("gold_product_data", "gold_product_data_not_ready"),
        ("gold_canonical", "gold_canonical_launch_not_pass"),
        ("gold_activation", "gold_activation_not_deployed"),
        ("gold_area_candidate", "gold_pass_area_candidate_mismatch"),
        ("gold_overlay_snapshot", "gold_overlay_snapshot_mismatch"),
        ("gold_area_path", "gold_pass_area_input_path_mismatch"),
        ("activation_candidate", "activation_receipt_not_candidate_deployed_pass"),
        ("activation_local", "activation_receipt_not_candidate_deployed_pass"),
        ("overlay_candidate", "overlay_receipt_not_candidate_pass"),
        ("overlay_staged", "overlay_receipt_not_candidate_pass"),
        ("rybbit_candidate", "rybbit_receipt_not_candidate_pass"),
    ],
)
def test_launch_authority_withholds_on_gold_or_product_proof_tamper(
    tmp_path: Path,
    mutation: str,
    expected_failure: str,
) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    activation_path = inputs["activation_receipt_path"]
    overlay_path = inputs["overlay_receipt_path"]
    rybbit_path = inputs["rybbit_receipt_path"]
    assert all(isinstance(path, Path) for path in (gold_path, activation_path, overlay_path, rybbit_path))
    gold = _read_payload(gold_path)
    activation = _read_payload(activation_path)
    overlay = _read_payload(overlay_path)
    rybbit = _read_payload(rybbit_path)

    if mutation == "gold_status":
        gold["status"] = "blocked"
    elif mutation == "gold_customer_ux":
        gold["flagship_customer_ux_evidence"]["ready"] = False  # type: ignore[index]
    elif mutation == "gold_product_data":
        gold["launch_product_data_evidence"]["ready"] = False  # type: ignore[index]
    elif mutation == "gold_canonical":
        gold["canonical_launch_evidence"]["status"] = "blocked"  # type: ignore[index]
    elif mutation == "gold_activation":
        gold["activation_to_value"]["proof_mode"] = "loopback"  # type: ignore[index]
    elif mutation == "gold_area_candidate":
        gold["pass_areas"][1]["release_commit_sha"] = "f" * 40  # type: ignore[index]
    elif mutation == "gold_overlay_snapshot":
        gold["pass_areas"][2]["snapshot_id"] = "f" * 64  # type: ignore[index]
    elif mutation == "gold_area_path":
        gold["pass_areas"][2]["receipt_path"] = str(tmp_path / "other.json")  # type: ignore[index]
    elif mutation == "activation_candidate":
        activation["candidate_sha"] = "f" * 40
    elif mutation == "activation_local":
        activation["proof_mode"] = "loopback"
    elif mutation == "overlay_candidate":
        overlay["candidate_sha"] = "f" * 40
    elif mutation == "overlay_staged":
        overlay["activation"]["phase"] = "staged"  # type: ignore[index]
    elif mutation == "rybbit_candidate":
        rybbit["candidate_sha"] = "f" * 40

    _rewrite(gold_path, gold)
    _rewrite(activation_path, activation)
    _rewrite(overlay_path, overlay)
    _rewrite(rybbit_path, rybbit)
    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert envelope["launch_authorized"] is False
    assert expected_failure in envelope["failures"]


@pytest.mark.parametrize(
    "mutation",
    ["delete_temporal_evidence", "change_claim_safety"],
)
def test_launch_authority_canonically_rejects_same_path_post_gold_overlay_tamper(
    tmp_path: Path,
    mutation: str,
) -> None:
    inputs = _inputs(tmp_path)
    overlay_path = inputs["overlay_receipt_path"]
    assert isinstance(overlay_path, Path)
    receipt = _read_payload(overlay_path)
    original_snapshot_id = receipt["snapshot_id"]

    if mutation == "delete_temporal_evidence":
        receipt.pop("temporal_evidence")
    else:
        claim_safety = receipt["claim_safety"]
        assert isinstance(claim_safety, dict)
        claim_safety["municipal_rss_is_independent_press"] = True
    _rewrite(overlay_path, receipt)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert receipt["snapshot_id"] == original_snapshot_id
    assert envelope["status"] == "withheld"
    assert envelope["launch_authorized"] is False
    assert "overlay_receipt_not_candidate_pass" in envelope["failures"]
    checks = {
        str(row["name"]): row["ok"]
        for row in envelope["checks"]
        if isinstance(row, dict)
    }
    assert checks["gold_overlay_staged_receipt_sha256_bound"] is True


def test_launch_authority_rejects_gold_staged_overlay_digest_tamper(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold = _read_payload(gold_path)
    launch_product_data = gold["launch_product_data_evidence"]
    assert isinstance(launch_product_data, dict)
    top_overlay = launch_product_data["evidence_overlay_read_model"]
    assert isinstance(top_overlay, dict)
    top_overlay["receipt_sha256"] = "e" * 64
    pass_areas = gold["pass_areas"]
    assert isinstance(pass_areas, list)
    overlay_area = next(
        row
        for row in pass_areas
        if isinstance(row, dict) and row.get("area") == "evidence_overlay_read_model"
    )
    overlay_area["receipt_sha256"] = "e" * 64
    _rewrite(gold_path, gold)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_overlay_receipt_sha256_mismatch" in envelope["failures"]


def test_launch_authority_rejects_duplicate_gold_pass_area(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold = _read_payload(gold_path)
    pass_areas = gold["pass_areas"]
    assert isinstance(pass_areas, list)
    pass_areas.append(deepcopy(pass_areas[0]))
    _rewrite(gold_path, gold)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_pass_areas_incomplete_or_duplicate" in envelope["failures"]


def test_launch_authority_rejects_coherent_prior_workflow_run(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    binding_path = inputs["security_workflow_binding_path"]
    live_path = inputs["live_provenance_path"]
    assert isinstance(binding_path, Path)
    assert isinstance(live_path, Path)
    prior_run = "73920"
    binding = _security_binding_payload(run_id=prior_run)
    _rewrite(binding_path, binding)
    live = _read_payload(live_path)
    security_binding = live["security_receipt_binding"]
    assert isinstance(security_binding, dict)
    security_binding["workflow_run_id"] = prior_run
    security_binding["workflow_binding_sha256"] = _digest(binding_path)
    _rewrite(live_path, live)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "security_workflow_binding_mismatch" in envelope["failures"]
    assert "live_provenance_not_current_run_pass" in envelope["failures"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workflow_head_sha", "c" * 40),
        ("workflow_run_id", "73922"),
        ("workflow_run_attempt", "3"),
    ],
)
def test_launch_authority_rejects_expected_cross_run_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    inputs = _inputs(tmp_path)
    inputs[field] = value

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "security_workflow_binding_mismatch" in envelope["failures"]


def test_launch_authority_revalidates_security_even_when_live_hash_is_rebound(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    security_path = inputs["security_receipt_path"]
    live_path = inputs["live_provenance_path"]
    assert isinstance(security_path, Path)
    assert isinstance(live_path, Path)
    security = _read_payload(security_path)
    security["status"] = "fail"
    _rewrite(security_path, security)
    live = _read_payload(live_path)
    binding = live["security_receipt_binding"]
    assert isinstance(binding, dict)
    binding["receipt_sha256"] = _digest(security_path)
    _rewrite(live_path, live)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "security_receipt_not_candidate_pass" in envelope["failures"]


def test_launch_authority_rejects_security_content_tamper_against_live_hash(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    security_path = inputs["security_receipt_path"]
    assert isinstance(security_path, Path)
    security_path.write_bytes(security_path.read_bytes() + b"\n")
    security_path.chmod(0o600)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "live_provenance_not_current_run_pass" in envelope["failures"]


def test_launch_authority_rejects_wrong_controller_bundle_digest(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    inputs["expected_controller_bundle_sha256"] = "f" * 64

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "controller_bundle_sha256_mismatch" in envelope["failures"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phase", "staged"),
        ("candidate_snapshot_id", "f" * 64),
        ("activated_snapshot_id", "f" * 64),
        ("candidate_staged", False),
        ("activation_performed", False),
        ("active_snapshot_unchanged", True),
        ("active_pointer_switch", "non_atomic"),
        ("active_revalidation_performed", False),
        ("active_revalidation_query_sample_count", 23),
        ("active_revalidation_query_p95_ms", 101.0),
    ],
)
def test_launch_authority_requires_final_active_overlay_pointer_switch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    inputs = _inputs(tmp_path)
    overlay_path = inputs["overlay_receipt_path"]
    assert isinstance(overlay_path, Path)
    overlay = _read_payload(overlay_path)
    activation = overlay["activation"]
    assert isinstance(activation, dict)
    activation[field] = value
    _rewrite(overlay_path, overlay)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "overlay_receipt_not_candidate_pass" in envelope["failures"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_teable_origin", "http://app.teable.io"),
        ("expected_teable_origin", "https://app.teable.io/"),
        ("expected_teable_origin", "https://user@app.teable.io"),
        ("expected_teable_origin", "https://APP.TEABLE.IO"),
        ("expected_teable_base_id_sha256", "A" * 64),
        ("expected_teable_base_id_sha256", "z" * 64),
        ("expected_teable_base_id_sha256", "1" * 63),
    ],
)
def test_launch_authority_rejects_invalid_independent_teable_authority(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    inputs = _inputs(tmp_path)
    inputs[field] = value

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "expected_teable_authority_invalid" in envelope["failures"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_rybbit_public_origin", "http://propertyquarry.com"),
        ("expected_rybbit_public_origin", "https://propertyquarry.com/"),
        ("expected_rybbit_analytics_origin", "https://user@app.rybbit.io"),
        ("expected_rybbit_analytics_origin", "https://APP.RYBBIT.IO"),
        ("expected_rybbit_site_id_sha256", "A" * 64),
        ("expected_rybbit_site_id_sha256", "1" * 63),
    ],
)
def test_launch_authority_rejects_invalid_independent_rybbit_authority(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    inputs = _inputs(tmp_path)
    inputs[field] = value

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "expected_rybbit_authority_invalid" in envelope["failures"]


@pytest.mark.parametrize(
    "mutation",
    [
        "public_origin",
        "analytics_origin",
        "site_id",
        "privacy",
        "api_auth",
        "api_site",
        "freshness",
    ],
)
def test_launch_authority_canonically_revalidates_rybbit_receipt(
    tmp_path: Path,
    mutation: str,
) -> None:
    inputs = _inputs(tmp_path)
    rybbit_path = inputs["rybbit_receipt_path"]
    assert isinstance(rybbit_path, Path)
    receipt = _read_payload(rybbit_path)

    if mutation == "public_origin":
        receipt["public_origin"] = "https://other.invalid"
    elif mutation == "analytics_origin":
        receipt["analytics_origin"] = "https://other.invalid"
    elif mutation == "site_id":
        receipt["site_id_sha256"] = "f" * 64
    elif mutation == "privacy":
        browser = receipt["browser"]
        assert isinstance(browser, dict)
        privacy = browser["privacy"]
        assert isinstance(privacy, dict)
        privacy["no_principal"] = False
    elif mutation == "api_auth":
        api = receipt["api"]
        assert isinstance(api, dict)
        auth = api["auth"]
        assert isinstance(auth, dict)
        auth["secret_in_receipt"] = True
    elif mutation == "api_site":
        api = receipt["api"]
        assert isinstance(api, dict)
        site = api["site"]
        assert isinstance(site, dict)
        site["status_code"] = 500
    elif mutation == "freshness":
        receipt["generated_at"] = (
            GENERATED_AT - timedelta(minutes=16)
        ).isoformat()

    _rewrite(rybbit_path, receipt)
    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "rybbit_receipt_not_candidate_pass" in envelope["failures"]


@pytest.mark.parametrize(
    ("target", "section", "field", "value", "expected_failure"),
    [
        (
            "overlay",
            "source_evidence",
            "base_origin",
            "https://other.teable.io",
            "overlay_receipt_not_candidate_pass",
        ),
        (
            "overlay",
            "source_authority",
            "expected_base_id_sha256",
            "f" * 64,
            "overlay_receipt_not_candidate_pass",
        ),
        (
            "gold",
            "source_evidence",
            "base_origin",
            "https://other.teable.io",
            "gold_product_data_not_ready",
        ),
        (
            "gold",
            "source_authority",
            "expected_base_id_sha256",
            "f" * 64,
            "gold_product_data_not_ready",
        ),
    ],
)
def test_launch_authority_cross_checks_independent_teable_authority_everywhere(
    tmp_path: Path,
    target: str,
    section: str,
    field: str,
    value: str,
    expected_failure: str,
) -> None:
    inputs = _inputs(tmp_path)
    overlay_path = inputs["overlay_receipt_path"]
    gold_path = inputs["gold_status_path"]
    assert isinstance(overlay_path, Path)
    assert isinstance(gold_path, Path)
    if target == "overlay":
        payload = _read_payload(overlay_path)
        section_payload = payload[section]
        assert isinstance(section_payload, dict)
        section_payload[field] = value
        _rewrite(overlay_path, payload)
    else:
        payload = _read_payload(gold_path)
        product_data = payload["launch_product_data_evidence"]
        assert isinstance(product_data, dict)
        overlay_details = product_data["evidence_overlay_read_model"]
        assert isinstance(overlay_details, dict)
        section_payload = overlay_details[section]
        assert isinstance(section_payload, dict)
        section_payload[field] = value
        _rewrite(gold_path, payload)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert expected_failure in envelope["failures"]


@pytest.mark.parametrize("target", ["overlay", "gold"])
def test_launch_authority_rejects_raw_teable_base_id_in_receipts(
    tmp_path: Path,
    target: str,
) -> None:
    inputs = _inputs(tmp_path)
    path_key = "overlay_receipt_path" if target == "overlay" else "gold_status_path"
    path = inputs[path_key]
    assert isinstance(path, Path)
    payload = _read_payload(path)
    if target == "overlay":
        source_evidence = payload["source_evidence"]
    else:
        product_data = payload["launch_product_data_evidence"]
        assert isinstance(product_data, dict)
        overlay_details = product_data["evidence_overlay_read_model"]
        assert isinstance(overlay_details, dict)
        source_evidence = overlay_details["source_evidence"]
    assert isinstance(source_evidence, dict)
    source_evidence["base_id"] = "raw-base-id-must-not-be-recorded"
    _rewrite(path, payload)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"


def test_launch_authority_rejects_duplicate_key_json(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    gold_path = inputs["gold_status_path"]
    assert isinstance(gold_path, Path)
    gold_path.write_text('{"status":"pass","status":"blocked"}', encoding="utf-8")
    gold_path.chmod(0o600)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_status_duplicate_key" in envelope["failures"]


def test_launch_authority_rejects_symlinked_json_input(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    activation_path = inputs["activation_receipt_path"]
    assert isinstance(activation_path, Path)
    real_activation = tmp_path / "activation-real.json"
    activation_path.replace(real_activation)
    activation_path.symlink_to(real_activation)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "activation_not_regular" in envelope["failures"]


def test_launch_authority_rejects_nonregular_controller_bundle(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    controller_path = inputs["controller_bundle_path"]
    assert isinstance(controller_path, Path)
    controller_path.unlink()
    controller_path.mkdir()

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "controller_bundle_not_regular" in envelope["failures"]


def test_controller_bundle_digest_is_streamed_without_retaining_bundle_bytes(
    tmp_path: Path,
) -> None:
    controller = tmp_path / "controller.tar.zst"
    controller.write_bytes(b"controller-stream" * 1024)
    controller.chmod(0o600)

    raw, size_bytes, digest = launch_authority._stable_regular_snapshot(
        controller,
        max_bytes=controller.stat().st_size,
        error_code="controller_bundle",
        retain_bytes=False,
    )

    assert raw is None
    assert size_bytes == controller.stat().st_size
    assert digest == _digest(controller)


def test_launch_authority_rejects_oversized_controller_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(launch_authority, "MAX_CONTROLLER_BUNDLE_BYTES", 8)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "controller_bundle_size_invalid" in envelope["failures"]


def test_launch_authority_rejects_oversized_json_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(launch_authority, "MAX_JSON_INPUT_BYTES", 32)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "gold_status_size_invalid" in envelope["failures"]


def test_launch_authority_cli_writes_atomic_mode_600_and_exits_nonzero_when_withheld(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    output_path = tmp_path / "launch-authority.json"
    argv = [
        "propertyquarry_launch_authority.py",
        "--candidate-sha",
        str(inputs["candidate_sha"]),
        "--workflow-head-sha",
        str(inputs["workflow_head_sha"]),
        "--workflow-run-id",
        str(inputs["workflow_run_id"]),
        "--workflow-run-attempt",
        str(inputs["workflow_run_attempt"]),
        "--expected-teable-origin",
        str(inputs["expected_teable_origin"]),
        "--expected-teable-base-id-sha256",
        str(inputs["expected_teable_base_id_sha256"]),
        "--expected-rybbit-public-origin",
        str(inputs["expected_rybbit_public_origin"]),
        "--expected-rybbit-analytics-origin",
        str(inputs["expected_rybbit_analytics_origin"]),
        "--expected-rybbit-site-id-sha256",
        str(inputs["expected_rybbit_site_id_sha256"]),
        "--gold-status",
        str(inputs["gold_status_path"]),
        "--live-provenance",
        str(inputs["live_provenance_path"]),
        "--activation-receipt",
        str(inputs["activation_receipt_path"]),
        "--overlay-receipt",
        str(inputs["overlay_receipt_path"]),
        "--rybbit-receipt",
        str(inputs["rybbit_receipt_path"]),
        "--security-receipt",
        str(inputs["security_receipt_path"]),
        "--security-workflow-binding",
        str(inputs["security_workflow_binding_path"]),
        "--controller-bundle",
        str(inputs["controller_bundle_path"]),
        "--expected-controller-bundle-sha256",
        str(inputs["expected_controller_bundle_sha256"]),
        "--activation-authority",
        str(inputs["activation_authority_path"]),
        "--write",
        str(output_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(launch_authority, "_utc_now", lambda: GENERATED_AT)

    assert launch_authority.main() == 0
    assert _read_payload(output_path)["status"] == "pass"
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600

    activation_path = inputs["activation_receipt_path"]
    assert isinstance(activation_path, Path)
    activation = _read_payload(activation_path)
    activation["status"] = "fail"
    _rewrite(activation_path, activation)

    assert launch_authority.main() == 1
    assert _read_payload(output_path)["status"] == "withheld"
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600


def test_launch_authority_rejects_group_writable_input(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    overlay_path = inputs["overlay_receipt_path"]
    assert isinstance(overlay_path, Path)
    os.chmod(overlay_path, 0o620)

    envelope = launch_authority.build_launch_authority_envelope(**inputs)

    assert envelope["status"] == "withheld"
    assert "overlay_writable_by_group_or_other" in envelope["failures"]
