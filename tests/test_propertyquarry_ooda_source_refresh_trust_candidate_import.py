from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import propertyquarry_ooda_source_refresh_claims as claims
from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_artifact_request as artifact_request,
)
from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification as artifact_notification,
)
from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import
from scripts import (
    propertyquarry_ooda_source_refresh_trust_candidate_manual_action as manual_action,
)
from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
from scripts.propertyquarry_strict_json import loads_strict_json_object


NOW = datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)


def test_manual_action_script_is_directly_invocable_from_repo_root() -> None:
    root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(
                root
                / "scripts/propertyquarry_ooda_source_refresh_trust_candidate_manual_action.py"
            ),
            "--help",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--materialize-current" in result.stdout
    assert "--inspect-current" in result.stdout


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _claim_verification(trust_registry: dict[str, object]) -> dict[str, object]:
    lanes = sorted(source_refresh._LANE_ARTIFACTS)
    return claims._integrity_bound(
        {
            "schema": claims.VERIFY_SCHEMA,
            "status": "verified",
            "claim_state": "producer_trust_unconfigured",
            "settlement_state": "awaiting_producer_trust",
            "updated_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
            "handoff_id": "pq-source-refresh-handoff-import-test",
            "request_id": "pq-source-refresh-import-test",
            "lifecycle_receipt_sha256": "a" * 64,
            "trust": {
                "status": trust_registry["status"],
                "rotation_epoch": trust_registry["rotation_epoch"],
                "trust_registry_sha256": trust_registry[
                    "trust_registry_sha256"
                ],
                "configured": False,
                "active_producer_count": 0,
                "required_lanes": lanes,
                "trusted_lanes": [],
                "missing_lanes": lanes,
                "producer_trust_ready": False,
            },
            "progress": {
                "current_evidence_verified": True,
                "handoff_binding_verified": True,
                "lifecycle_integrity_verified": True,
            },
            **claims._safety_fields(),
        }
    )


def _source_candidate(request: dict[str, object]) -> dict[str, object]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key_sha256 = hashlib.sha256(public_key).hexdigest()
    candidate = {
        "schema": trust_intake.CANDIDATE_SCHEMA,
        "producer_id": "receipt-producer.import",
        "key_id": "receipt-producer.import.2026-08",
        "algorithm": "Ed25519",
        "public_key": _b64(public_key),
        "public_key_sha256": public_key_sha256,
        "lanes": sorted(source_refresh._LANE_ARTIFACTS),
        "requested_status": "ACTIVE",
        "request_id": request["request_id"],
        "semantic_request_sha256": request["semantic_request_sha256"],
        "trust_registry_sha256": request["source_binding"][
            "trust_registry_sha256"
        ],
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        "nonce": _b64(b"candidate-import-proof-01"),
        "candidate": {
            "state": "proposed",
            "producer_controls_private_key": True,
            "out_of_band_identity_verification_required": True,
            "candidate_confers_authority": False,
            "trust_enrollment_authorized": False,
            "provider_operation_authorized": False,
            "delivery_authorized": False,
            "private_key_material_included": False,
        },
    }
    candidate["proof_signature"] = _b64(
        private_key.sign(claims._canonical(candidate))
    )
    return candidate


def _fixture(tmp_path: Path) -> dict[str, object]:
    trust_registry_path = claims.DEFAULT_TRUST_REGISTRY_PATH
    trust_registry = claims.load_producer_trust_registry(trust_registry_path)
    claim_path = tmp_path / "claim-verification.json"
    claim_path.write_bytes(
        claims._canonical(_claim_verification(trust_registry))
    )
    claim_path.chmod(0o600)
    candidate_dir = tmp_path / "candidates"
    intake_receipt = tmp_path / "intake.json"
    intake_verification = tmp_path / "intake-verification.json"
    request = trust_intake.materialize_trust_intake_bundle(
        claim_verification_path=claim_path,
        trust_registry_path=trust_registry_path,
        candidate_dir=candidate_dir,
        receipt_path=intake_receipt,
        verification_path=intake_verification,
        now=NOW,
    )
    assert request["status"] == "verified"
    source_path = tmp_path / "operator-drop" / "producer-candidate.json"
    source_path.parent.mkdir(mode=0o700)
    source_path.write_bytes(claims._canonical(_source_candidate(request)))
    source_path.chmod(0o600)
    return {
        "trust_registry_path": trust_registry_path,
        "trust_registry_sha256": trust_registry["trust_registry_sha256"],
        "claim_path": claim_path,
        "candidate_dir": candidate_dir,
        "intake_receipt": intake_receipt,
        "intake_verification": intake_verification,
        "import_dir": tmp_path / "imports",
        "source_path": source_path,
        "source_discovery_dir": source_path.parent,
        "request": request,
    }


def _readiness(values: dict[str, object]) -> dict[str, object]:
    return candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        source_path=Path(values["source_path"]),
        now=NOW,
    )


def _execute(
    values: dict[str, object],
    *,
    readiness: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    ready = readiness or _readiness(values)
    arguments: dict[str, object] = {
        "source_path": values["source_path"],
        "expected_request_id": ready["request_id"],
        "expected_semantic_request_sha256": ready[
            "semantic_request_sha256"
        ],
        "expected_intake_verification_sha256": ready[
            "intake_verification_sha256"
        ],
        "expected_source_sha256": ready["source_sha256"],
        "expected_public_key_sha256": ready["public_key_sha256"],
        "operator_id": "operator.local",
        "import_method": "authenticated_operator_session",
        "evidence_ref": "ticket:TRUST-IMPORT-1",
        "confirmation": candidate_import.CONFIRMATION,
        "claim_verification_path": values["claim_path"],
        "trust_registry_path": values["trust_registry_path"],
        "candidate_dir": values["candidate_dir"],
        "intake_receipt_path": values["intake_receipt"],
        "intake_verification_path": values["intake_verification"],
        "import_dir": values["import_dir"],
        "now": NOW,
    }
    arguments.update(overrides)
    return candidate_import.import_current_candidate(**arguments)


def test_exact_public_candidate_import_is_audited_without_granting_trust(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    source_before = Path(values["source_path"]).read_bytes()

    result = _execute(values)

    assert result["status"] == "verified"
    assert result["import_state"] == "succeeded"
    assert result["candidate_imported"] is True
    assert result["public_key_candidate_recorded"] is True
    assert result["trust_enrollment_authorized"] is False
    assert result["trust_registry_modified"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert Path(values["source_path"]).read_bytes() == source_before
    destination = Path(str(result["destination_path"]))
    assert destination.is_file()
    assert destination.stat().st_mode & 0o077 == 0
    history = candidate_import.inspect_import_history(
        import_dir=Path(values["import_dir"]),
        candidate_dir=Path(values["candidate_dir"]),
        now=NOW,
    )
    assert history["status"] == "verified"
    assert history["latest_import"]["import_state"] == "succeeded"
    assert (
        claims.load_producer_trust_registry(values["trust_registry_path"])[
            "trust_registry_sha256"
        ]
        == values["trust_registry_sha256"]
    )

    projected = candidate_import.apply_candidate_import_presentation_state(
        result,
        state_path=tmp_path / "post-import-presentation.json",
        now=NOW,
    )
    assert projected["import_state"] == "succeeded"
    assert projected["interrupt_operator"] is False
    assert projected["presentation"]["state"] == "not_required"


def test_read_only_waiting_and_source_inspection_create_nothing(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        source_discovery_dir=tmp_path / "absent-drop",
        now=NOW,
    )
    ready = _readiness(values)

    assert waiting["import_state"] == "awaiting_external_artifact"
    assert waiting["action_required"] is True
    assert waiting["interrupt_operator"] is True
    assert waiting["artifact_request"]["private_key_material_allowed"] is False
    assert ready["import_state"] == "ready_for_manual_import"
    assert ready["candidate_import_authorized"] is False
    assert not Path(values["candidate_dir"]).exists()
    assert not Path(values["import_dir"]).exists()


def test_path_free_public_artifact_request_is_persisted_and_current(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    receipt_path = tmp_path / "public-artifact-request.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )

    staged = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=receipt_path,
        now=NOW,
    )

    assert staged["status"] == "verified"
    assert staged["artifact_request_staged"] is True
    assert staged["interrupt_operator"] is False
    assert staged["producer_contacted"] is False
    assert staged["transport_delivery_attempted"] is False
    assert staged["candidate_import_attempted"] is False
    assert staged["trust_registry_modified"] is False
    assert staged["provider_quota_consumption_allowed"] is False
    assert receipt_path.stat().st_mode & 0o077 == 0
    receipt = loads_strict_json_object(
        receipt_path.read_bytes(),
        field="public_artifact_request",
    )
    rendered = artifact_request._canonical(receipt).decode("utf-8")
    public_request = receipt["public_request"]
    assert receipt["status"] == "staged"
    assert public_request["request_id"] == waiting["request_id"]
    assert public_request["requested_lanes"] == sorted(
        source_refresh._LANE_ARTIFACTS
    )
    assert public_request["proof_contract"]["signature_payload"] == (
        "canonical_strict_json_candidate_without_proof_signature"
    )
    assert public_request["submission_contract"] == {
        "format": "strict_json_object",
        "filename_pattern": "*.json",
        "operator_managed_drop_directory_mode": "0750",
        "candidate_file_mode": "0640",
        "scheduler_access": "supplemental_group_read_only",
        "group_write_allowed": False,
        "other_access_allowed": False,
        "automatic_import_allowed": False,
    }
    assert "/docker/" not in rendered
    assert str(tmp_path) not in rendered
    assert receipt["host_path_values_recorded"] is False
    assert receipt["candidate_payload_recorded"] is False
    assert receipt["public_key_material_recorded"] is False

    verified = artifact_request.inspect_candidate_artifact_request(
        waiting,
        receipt_path=receipt_path,
        now=NOW + timedelta(seconds=1),
    )
    assert verified["status"] == "verified"
    assert verified["artifact_request_receipt_sha256"] == staged[
        "artifact_request_receipt_sha256"
    ]
    assert verified["progress"]["path_free_projection_verified"] is True


def test_public_artifact_request_tombstones_when_candidate_is_ready(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    receipt_path = tmp_path / "public-artifact-request.json"
    ready = _readiness(values)

    result = artifact_request.materialize_candidate_artifact_request(
        ready,
        receipt_path=receipt_path,
        now=NOW,
    )

    receipt = loads_strict_json_object(
        receipt_path.read_bytes(),
        field="public_artifact_request_tombstone",
    )
    assert result["artifact_request_staged"] is False
    assert result["action_required"] is False
    assert result["interrupt_operator"] is False
    assert receipt["status"] == "not_required"
    assert receipt["request_state"] == "ready_for_manual_import"
    assert receipt["public_request"] == {}
    assert receipt["request_id"] == ""
    assert receipt["expires_at"] == ""
    assert receipt["candidate_import_authorized"] is False
    assert receipt["trust_registry_modified"] is False


def test_public_artifact_request_cli_inspects_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = _fixture(tmp_path)
    receipt_path = tmp_path / "public-artifact-request.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=receipt_path,
        now=NOW,
    )
    monkeypatch.setattr(
        candidate_import,
        "inspect_current_candidate_import",
        lambda **_kwargs: dict(waiting),
    )
    monkeypatch.setattr(
        artifact_request,
        "_now",
        lambda now=None: now or NOW + timedelta(seconds=1),
    )

    assert artifact_request.main(
        ["--inspect-current", "--receipt", str(receipt_path)]
    ) == 0

    projection = loads_strict_json_object(
        capsys.readouterr().out.encode(),
        field="public_artifact_request_cli_projection",
    )
    assert projection["status"] == "verified"
    assert projection["artifact_request_staged"] is True
    assert projection["producer_contacted"] is False
    assert projection["candidate_import_attempted"] is False
    assert projection["delivery_authorized"] is False


def test_path_free_manual_action_receipt_stages_only_a_novel_real_ask(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    artifact_path = tmp_path / "public-artifact-request.json"
    action_path = tmp_path / "manual-action.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=tmp_path / "presentation.json",
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=artifact_path,
        now=NOW,
    )

    verified = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=action_path,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["action_state"] == "awaiting_external_artifact"
    assert verified["operator_action_receipt_staged"] is True
    assert verified["action_required"] is True
    assert verified["interrupt_operator"] is True
    assert verified["presentation_state"] == "novel"
    assert verified["transport_delivery_authorized"] is False
    assert verified["transport_delivery_attempted"] is False
    assert verified["candidate_import_authorized"] is False
    assert verified["trust_registry_modified"] is False
    assert action_path.stat().st_mode & 0o077 == 0
    receipt = loads_strict_json_object(
        action_path.read_bytes(),
        field="manual_action",
    )
    rendered = manual_action._canonical(receipt).decode("utf-8")
    assert receipt["manual_action"]["kind"] == (
        "provide_external_public_candidate"
    )
    assert receipt["manual_action"]["required_artifact"][
        "candidate_schema"
    ] == trust_intake.CANDIDATE_SCHEMA
    assert "/docker/" not in rendered
    assert str(tmp_path) not in rendered
    assert receipt["host_path_values_recorded"] is False
    assert receipt["candidate_payload_recorded"] is False
    assert receipt["public_key_material_recorded"] is False


def test_manual_action_rejects_forged_artifact_verification_semantics(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=tmp_path / "presentation.json",
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=tmp_path / "public-artifact-request.json",
        now=NOW,
    )
    mutations = (
        ("trust_enrollment_authorized", True),
        ("candidate_payload_recorded", True),
        ("producer_dispatch_authorized", True),
        ("request_id", "pqtrustintake_" + "f" * 24),
        ("request_semantic_sha256", "f" * 64),
        ("expires_at", (NOW - timedelta(seconds=1)).isoformat()),
        ("updated_at", (NOW - timedelta(hours=1)).isoformat()),
        ("unexpected_authority", True),
    )
    for field, value in mutations:
        forged = dict(artifact)
        forged[field] = value

        with pytest.raises(
            ValueError,
            match="trust_candidate_manual_action_source_not_current",
        ):
            manual_action.build_candidate_manual_action(
                projected,
                forged,
                now=NOW,
                max_age_seconds=60,
            )

    forged_progress = dict(artifact)
    forged_progress["progress"] = {
        "current_evidence_verified": True,
        "receipt_integrity_verified": True,
    }
    with pytest.raises(
        ValueError,
        match="trust_candidate_manual_action_source_not_current",
    ):
        manual_action.build_candidate_manual_action(
            projected,
            forged_progress,
            now=NOW,
        )


def test_manual_action_receipt_deduplicates_after_presentation_is_recorded(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    artifact_path = tmp_path / "public-artifact-request.json"
    action_path = tmp_path / "manual-action.json"
    state_path = tmp_path / "presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    first = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=state_path,
        now=NOW,
    )
    candidate_import.record_candidate_import_presentation(
        first,
        state_path=state_path,
        expected_presentation_digest=first["presentation"][
            "presentation_digest"
        ],
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=artifact_path,
        now=NOW,
    )
    duplicate = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=state_path,
        now=NOW + timedelta(seconds=60),
    )

    verified = manual_action.materialize_candidate_manual_action(
        duplicate,
        artifact,
        receipt_path=action_path,
        now=NOW + timedelta(seconds=60),
    )

    assert verified["operator_action_receipt_staged"] is True
    assert verified["action_required"] is True
    assert verified["interrupt_operator"] is False
    assert verified["presentation_state"] == "already_presented"
    inspected = manual_action.inspect_candidate_manual_action(
        duplicate,
        artifact,
        receipt_path=action_path,
        now=NOW + timedelta(seconds=61),
    )
    assert inspected["status"] == "verified"
    assert inspected["presentation_digest"] == first["presentation"][
        "presentation_digest"
    ]


def test_manual_action_replay_uses_current_evidence_time(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    artifact_path = tmp_path / "public-artifact-request.json"
    action_path = tmp_path / "manual-action.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=tmp_path / "presentation.json",
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=artifact_path,
        now=NOW,
    )
    materialized = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=action_path,
        now=NOW,
    )
    refreshed_artifact = artifact_request.inspect_candidate_artifact_request(
        waiting,
        receipt_path=artifact_path,
        now=NOW + timedelta(seconds=60),
    )

    inspected = manual_action.inspect_candidate_manual_action(
        projected,
        refreshed_artifact,
        receipt_path=action_path,
        now=NOW + timedelta(seconds=60),
        max_age_seconds=120,
    )

    assert inspected["status"] == "verified"
    assert inspected["operator_action_receipt_sha256"] == materialized[
        "operator_action_receipt_sha256"
    ]
    assert inspected["interrupt_operator"] is True
    assert inspected["candidate_import_authorized"] is False
    assert inspected["trust_registry_modified"] is False


def test_manual_action_receipt_uses_hashes_not_paths_for_ready_candidate(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    ready = _readiness(values)
    projected = candidate_import.apply_candidate_import_presentation_state(
        ready,
        state_path=tmp_path / "presentation.json",
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        ready,
        receipt_path=tmp_path / "public-artifact-request.json",
        now=NOW,
    )

    verified = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=tmp_path / "manual-action.json",
        now=NOW,
    )

    receipt = loads_strict_json_object(
        (tmp_path / "manual-action.json").read_bytes(),
        field="ready_manual_action",
    )
    rendered = manual_action._canonical(receipt).decode("utf-8")
    reference = receipt["manual_action"]["candidate_reference"]
    assert verified["action_state"] == "ready_for_manual_import"
    assert verified["interrupt_operator"] is True
    assert receipt["manual_action"]["kind"] == (
        "inspect_and_import_verified_public_candidate"
    )
    assert reference["source_sha256"] == ready["source_sha256"]
    assert reference["public_key_sha256"] == ready["public_key_sha256"]
    assert str(values["source_path"]) not in rendered
    assert str(values["candidate_dir"]) not in rendered
    assert receipt["candidate_import_authorized"] is False


def test_artifact_notification_evaluates_a_novel_action_without_sending(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    presentation_path = tmp_path / "presentation.json"
    artifact_path = tmp_path / "artifact-request.json"
    action_path = tmp_path / "manual-action.json"
    notification_path = tmp_path / "artifact-notification.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=artifact_path,
        now=NOW,
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=action_path,
        now=NOW,
    )

    result = artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        presentation_state_path=presentation_path,
        manual_action_receipt_path=action_path,
        receipt_path=notification_path,
        send=False,
        now=NOW,
    )

    assert result["status"] == "action_required"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is True
    assert result["would_send"] is True
    assert result["delivery_authorized"] is False
    assert result["delivery_attempted"] is False
    assert result["sent"] is False
    assert result["presentation_recorded"] is False
    assert result["candidate_import_authorized"] is False
    assert result["trust_registry_modified"] is False
    assert not presentation_path.exists()
    assert notification_path.stat().st_mode & 0o077 == 0
    receipt = loads_strict_json_object(
        notification_path.read_bytes(),
        field="artifact_notification",
    )
    rendered = artifact_notification._canonical(receipt).decode("utf-8")
    assert "/docker/" not in rendered
    assert str(tmp_path) not in rendered
    assert "Never provide or include the producer private key" in receipt[
        "message_preview"
    ]
    verified = artifact_notification.inspect_candidate_artifact_notification(
        projected,
        artifact,
        action,
        receipt_path=notification_path,
        now=NOW + timedelta(seconds=1),
    )
    assert verified["status"] == "verified"
    assert verified["notification_status"] == "action_required"


def test_artifact_notification_deduplicates_an_already_presented_action(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    presentation_path = tmp_path / "presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    first = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW,
    )
    candidate_import.record_candidate_import_presentation(
        first,
        state_path=presentation_path,
        expected_presentation_digest=first["presentation"][
            "presentation_digest"
        ],
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW + timedelta(seconds=1),
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=tmp_path / "artifact-request.json",
        now=NOW + timedelta(seconds=1),
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=tmp_path / "manual-action.json",
        now=NOW + timedelta(seconds=1),
    )

    result = artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        receipt_path=tmp_path / "artifact-notification.json",
        send=False,
        now=NOW + timedelta(seconds=1),
    )

    assert result["status"] == "deduplicated"
    assert result["action_required"] is True
    assert result["interrupt_operator"] is False
    assert result["would_send"] is False
    assert result["delivery_attempted"] is False
    assert "message_preview" not in result


def test_artifact_notification_records_only_a_receipted_authorized_send(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    presentation_path = tmp_path / "presentation.json"
    artifact_path = tmp_path / "artifact-request.json"
    action_path = tmp_path / "manual-action.json"
    notification_path = tmp_path / "artifact-notification.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=artifact_path,
        now=NOW,
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=action_path,
        now=NOW,
    )
    delivered: dict[str, object] = {}

    def deliver(**kwargs):
        delivered.update(kwargs)
        return {"delivery_mode": "telegram", "message_ids": ["message-1"]}

    result = artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        presentation_state_path=presentation_path,
        manual_action_receipt_path=action_path,
        lock_path=tmp_path / "send.lock",
        receipt_path=notification_path,
        principal_id="propertyquarry-operator-test",
        send=True,
        now=NOW,
        deliver=deliver,
    )

    assert result["status"] == "completed"
    assert result["delivery_authorized"] is True
    assert result["delivery_attempted"] is True
    assert result["sent"] is True
    assert result["presentation_recorded"] is True
    assert result["interrupt_operator"] is False
    assert result["would_send"] is False
    assert delivered["principal_id"] == "propertyquarry-operator-test"
    assert str(tmp_path) not in str(delivered["text"])
    assert "private key" in str(delivered["text"])
    refreshed = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW + timedelta(seconds=1),
    )
    assert refreshed["interrupt_operator"] is False
    refreshed_action = manual_action.inspect_candidate_manual_action(
        refreshed,
        artifact,
        receipt_path=action_path,
        now=NOW + timedelta(seconds=1),
    )
    assert refreshed_action["interrupt_operator"] is False
    verified = artifact_notification.inspect_candidate_artifact_notification(
        refreshed,
        artifact,
        refreshed_action,
        receipt_path=notification_path,
        now=NOW + timedelta(seconds=1),
    )
    assert verified["status"] == "verified"
    assert verified["notification_status"] == "completed"


def test_artifact_notification_failure_never_records_presentation(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    presentation_path = tmp_path / "presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=tmp_path / "artifact-request.json",
        now=NOW,
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=tmp_path / "manual-action.json",
        now=NOW,
    )

    def fail(**_kwargs):
        raise RuntimeError("delivery unavailable")

    result = artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        presentation_state_path=presentation_path,
        lock_path=tmp_path / "send.lock",
        receipt_path=tmp_path / "artifact-notification.json",
        principal_id="propertyquarry-operator-test",
        send=True,
        now=NOW,
        deliver=fail,
    )

    assert result["status"] == "delivery_failed"
    assert result["delivery_authorized"] is True
    assert result["delivery_attempted"] is True
    assert result["sent"] is False
    assert result["presentation_recorded"] is False
    assert result["interrupt_operator"] is False
    assert not presentation_path.exists()


def test_recovery_required_stages_one_path_free_deduplicated_repair_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path)
    request = dict(values["request"])
    public_key_sha256 = "d" * 64
    claim_id = "pqtrustimport_" + "e" * 24
    history = {
        "schema": candidate_import.HISTORY_SCHEMA,
        "status": "verified",
        "history_state": "import_history",
        "updated_at": NOW.isoformat(),
        "blocking_reason": "",
        "latest_import": {
            "import_state": "import_failed",
            "request_id": request["request_id"],
            "claim_id": claim_id,
            "claim_sha256": "a" * 64,
            "result_sha256": "b" * 64,
            "public_key_sha256": public_key_sha256,
            "claimed_at": NOW.isoformat(),
            "completed_at": NOW.isoformat(),
            "blocking_reason": "candidate_destination_exists",
            "candidate_import_attempted": True,
            "candidate_imported": False,
            "public_key_candidate_recorded": False,
            "claim_path": str(tmp_path / "private-claim.json"),
            "result_path": str(tmp_path / "private-result.json"),
            "destination_path": str(tmp_path / "private-candidate.json"),
            "destination_sha256": "",
        },
        "imports": [],
        "progress": {
            "current_evidence_verified": True,
            "claim_count": 1,
            "result_count": 1,
        },
    }
    monkeypatch.setattr(
        candidate_import,
        "inspect_import_history",
        lambda **_kwargs: history,
    )
    presentation_path = tmp_path / "presentation.json"
    artifact_path = tmp_path / "artifact-request.json"
    action_path = tmp_path / "manual-action.json"
    notification_path = tmp_path / "artifact-notification.json"

    recovery = candidate_import.inspect_candidate_import_readiness(
        request,
        candidate_dir=Path(values["candidate_dir"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        recovery,
        state_path=presentation_path,
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        recovery,
        receipt_path=artifact_path,
        now=NOW,
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=action_path,
        now=NOW,
    )
    notification = artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        presentation_state_path=presentation_path,
        manual_action_receipt_path=action_path,
        receipt_path=notification_path,
        send=False,
        now=NOW,
    )

    assert recovery["import_state"] == "recovery_required"
    assert recovery["candidate_import_attempted"] is True
    assert projected["presentation"]["state"] == "novel"
    assert projected["interrupt_operator"] is True
    assert artifact["artifact_request_staged"] is False
    assert action["operator_action_receipt_staged"] is True
    assert action["interrupt_operator"] is True
    assert notification["status"] == "action_required"
    assert notification["would_send"] is True
    assert notification["delivery_authorized"] is False
    assert notification["delivery_attempted"] is False
    assert notification["sent"] is False
    assert claim_id in notification["message_preview"]
    assert "Do not delete the claim" in notification["message_preview"]
    action_receipt = loads_strict_json_object(
        action_path.read_bytes(),
        field="recovery_manual_action",
    )
    rendered = manual_action._canonical(action_receipt).decode("utf-8")
    recovery_action = dict(action_receipt["manual_action"])
    assert recovery_action["kind"] == "inspect_failed_candidate_import"
    assert recovery_action["automatic_retry_allowed"] is False
    assert recovery_action["claim_deletion_allowed"] is False
    assert recovery_action["candidate_overwrite_allowed"] is False
    assert str(tmp_path) not in rendered

    presentation = candidate_import.record_candidate_import_presentation(
        projected,
        state_path=presentation_path,
        expected_presentation_digest=projected["presentation"][
            "presentation_digest"
        ],
        now=NOW,
    )
    assert presentation["status"] == "recorded"
    deduplicated = candidate_import.apply_candidate_import_presentation_state(
        recovery,
        state_path=presentation_path,
        now=NOW + timedelta(seconds=1),
    )
    deduplicated_action = manual_action.materialize_candidate_manual_action(
        deduplicated,
        artifact,
        receipt_path=action_path,
        now=NOW + timedelta(seconds=1),
    )
    deduplicated_notification = (
        artifact_notification.run_candidate_artifact_notification(
            deduplicated,
            artifact,
            deduplicated_action,
            receipt_path=notification_path,
            send=False,
            now=NOW + timedelta(seconds=1),
        )
    )
    assert deduplicated["presentation"]["state"] == "already_presented"
    assert deduplicated_action["interrupt_operator"] is False
    assert deduplicated_notification["status"] == "deduplicated"
    assert deduplicated_notification["would_send"] is False


def test_private_drop_auto_discovers_one_candidate_without_copying(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    source_path = Path(values["source_path"])
    source_before = source_path.read_bytes()

    ready = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        source_discovery_dir=Path(values["source_discovery_dir"]),
        now=NOW,
    )

    assert ready["status"] == "verified"
    assert ready["import_state"] == "ready_for_manual_import"
    assert ready["source_path"] == str(source_path.absolute())
    assert ready["candidate_import_authorized"] is False
    assert ready["candidate_import_attempted"] is False
    assert ready["candidate_imported"] is False
    assert ready["trust_registry_modified"] is False
    discovery = ready["source_discovery"]
    assert discovery["discovery_state"] == "candidate_ready"
    assert discovery["read_only"] is True
    assert discovery["source_copy_attempted"] is False
    assert discovery["source_file_modified"] is False
    assert discovery["invalid_path_names_recorded"] is False
    assert discovery["progress"] == {
        "current_evidence_verified": True,
        "scanned_file_count": 1,
        "valid_candidate_count": 1,
        "invalid_candidate_count": 0,
    }
    assert source_path.read_bytes() == source_before
    assert not Path(values["candidate_dir"]).exists()
    assert not Path(values["import_dir"]).exists()


def test_private_drop_accepts_only_scheduler_group_read_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path)
    source_path = Path(values["source_path"])
    drop = Path(values["source_discovery_dir"])
    drop.chmod(0o750)
    source_path.chmod(0o640)
    monkeypatch.setattr(
        candidate_import.os,
        "getgroups",
        lambda: [drop.stat().st_gid],
    )

    report = candidate_import.inspect_candidate_import_readiness(
        values["request"],
        candidate_dir=Path(values["candidate_dir"]),
        import_dir=Path(values["import_dir"]),
        source_discovery_dir=drop,
        now=NOW,
    )

    assert report["status"] == "verified"
    assert report["import_state"] == "ready_for_manual_import"
    assert report["source_discovery"]["discovery_state"] == "candidate_ready"
    assert report["candidate_import_authorized"] is False
    assert report["candidate_import_attempted"] is False
    assert candidate_import._read_group_public_source_bytes(source_path)
    monkeypatch.setattr(candidate_import.os, "geteuid", lambda: 10001)
    candidate, _raw, _digest = candidate_import._private_object(
        source_path,
        field="simulated_scheduler_public_candidate",
        allow_group_read=True,
    )
    assert candidate["public_key_sha256"] == report["public_key_sha256"]


@pytest.mark.parametrize(
    ("directory_mode", "source_mode"),
    [(0o770, 0o640), (0o750, 0o660), (0o751, 0o640), (0o750, 0o641)],
)
def test_private_drop_rejects_group_write_or_access_for_others(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_mode: int,
    source_mode: int,
) -> None:
    values = _fixture(tmp_path)
    source_path = Path(values["source_path"])
    drop = Path(values["source_discovery_dir"])
    drop.chmod(directory_mode)
    source_path.chmod(source_mode)
    monkeypatch.setattr(
        candidate_import.os,
        "getgroups",
        lambda: [drop.stat().st_gid],
    )

    report = candidate_import.inspect_candidate_import_readiness(
        values["request"],
        candidate_dir=Path(values["candidate_dir"]),
        import_dir=Path(values["import_dir"]),
        source_discovery_dir=drop,
        now=NOW,
    )

    assert report["import_state"] == "external_artifact_not_admissible"
    assert report["candidate_import_authorized"] is False
    assert report["candidate_import_attempted"] is False


@pytest.mark.parametrize("unsafe_kind", ["mode", "symlink"])
def test_private_drop_rejects_unsafe_entries_without_recording_their_names(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    values = _fixture(tmp_path)
    original = Path(values["source_path"])
    drop = tmp_path / "unsafe-drop"
    drop.mkdir(mode=0o700)
    unsafe = drop / "do-not-record-this-name.json"
    if unsafe_kind == "mode":
        unsafe.write_bytes(original.read_bytes())
        unsafe.chmod(0o644)
    else:
        unsafe.symlink_to(original)

    report = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        source_discovery_dir=drop,
        now=NOW,
    )

    assert report["status"] == "verified"
    assert report["import_state"] == "external_artifact_not_admissible"
    assert report["action_required"] is True
    assert report["candidate_import_authorized"] is False
    assert report["candidate_import_attempted"] is False
    discovery = report["source_discovery"]
    assert discovery["discovery_state"] == "candidates_not_admissible"
    assert discovery["invalid_path_names_recorded"] is False
    assert "do-not-record-this-name.json" not in candidate_import._canonical(
        report
    ).decode("utf-8")
    assert not Path(values["candidate_dir"]).exists()
    assert not Path(values["import_dir"]).exists()


def test_private_drop_ambiguity_fails_closed_and_records_no_names(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    source = Path(values["source_path"])
    drop = tmp_path / "ambiguous-drop"
    drop.mkdir(mode=0o700)
    for name in ("candidate-a.json", "candidate-b.json"):
        target = drop / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)

    report = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        source_discovery_dir=drop,
        now=NOW,
    )

    assert report["import_state"] == "external_artifact_not_admissible"
    assert report["source_discovery"]["discovery_state"] == (
        "candidate_ambiguous"
    )
    assert report["source_discovery"]["progress"][
        "valid_candidate_count"
    ] == 2
    rendered = candidate_import._canonical(report).decode("utf-8")
    assert "candidate-a.json" not in rendered
    assert "candidate-b.json" not in rendered
    assert report["candidate_import_authorized"] is False


def test_inadmissible_drop_stages_one_specific_path_free_repair_action(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    source = Path(values["source_path"])
    drop = tmp_path / "ambiguous-drop"
    drop.mkdir(mode=0o700)
    hidden_names = ("candidate-secret-a.json", "candidate-secret-b.json")
    for name in hidden_names:
        target = drop / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    report = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        source_discovery_dir=drop,
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        report,
        state_path=tmp_path / "presentation.json",
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        report,
        receipt_path=tmp_path / "artifact-request.json",
        now=NOW,
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=tmp_path / "manual-action.json",
        now=NOW,
    )

    result = artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        receipt_path=tmp_path / "artifact-notification.json",
        send=False,
        now=NOW,
    )

    action_receipt = loads_strict_json_object(
        (tmp_path / "manual-action.json").read_bytes(),
        field="inadmissible_manual_action",
    )
    repair = action_receipt["manual_action"]["repair_context"]
    assert action["status"] == "verified"
    assert action["interrupt_operator"] is True
    assert action_receipt["manual_action"]["kind"] == (
        "replace_inadmissible_public_candidate"
    )
    assert repair == {
        "discovery_state": "candidate_ambiguous",
        "scanned_file_count": 2,
        "valid_candidate_count": 2,
        "invalid_candidate_count": 0,
        "safe_next_step": (
            "Reduce the drop to exactly one current, request-bound public "
            "candidate JSON before read-only inspection resumes."
        ),
    }
    assert result["status"] == "action_required"
    assert "drop requires repair" in result["message_preview"]
    assert "Repair state: candidate_ambiguous" in result["message_preview"]
    assert "scanned=2 valid=2 invalid=0" in result["message_preview"]
    rendered = (
        manual_action._canonical(action_receipt)
        + artifact_notification._canonical(result)
    ).decode("utf-8")
    assert str(drop) not in rendered
    assert all(name not in rendered for name in hidden_names)
    assert result["candidate_import_authorized"] is False
    assert result["trust_registry_modified"] is False
    assert result["delivery_attempted"] is False


def test_artifact_notification_verifier_binds_the_exact_action_message(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    presentation_path = tmp_path / "presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=tmp_path / "artifact-request.json",
        now=NOW,
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=tmp_path / "manual-action.json",
        now=NOW,
    )
    notification_path = tmp_path / "artifact-notification.json"
    artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        receipt_path=notification_path,
        send=False,
        now=NOW,
    )
    tampered = loads_strict_json_object(
        notification_path.read_bytes(),
        field="tampered_artifact_notification",
    )
    tampered["message_preview"] = "Unbound operator instruction"
    notification_path.write_bytes(
        artifact_notification._canonical(
            artifact_notification._with_integrity(tampered)
        )
    )

    verified = artifact_notification.inspect_candidate_artifact_notification(
        projected,
        artifact,
        action,
        receipt_path=notification_path,
        now=NOW + timedelta(seconds=1),
    )

    assert verified["status"] == "blocked"
    assert verified["blocking_reason"] == (
        "trust_candidate_artifact_notification_not_current"
    )
    assert verified["delivery_attempted"] is False


def test_artifact_notification_verifier_rejects_rehashed_unsafe_semantics(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    presentation_path = tmp_path / "presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=presentation_path,
        now=NOW,
    )
    artifact = artifact_request.materialize_candidate_artifact_request(
        waiting,
        receipt_path=tmp_path / "artifact-request.json",
        now=NOW,
    )
    action = manual_action.materialize_candidate_manual_action(
        projected,
        artifact,
        receipt_path=tmp_path / "manual-action.json",
        now=NOW,
    )
    notification_path = tmp_path / "artifact-notification.json"
    artifact_notification.run_candidate_artifact_notification(
        projected,
        artifact,
        action,
        receipt_path=notification_path,
        send=False,
        now=NOW,
    )
    receipt = loads_strict_json_object(
        notification_path.read_bytes(),
        field="artifact_notification",
    )

    mutations = (
        ("trust_enrollment_authorized", True),
        ("candidate_payload_recorded", True),
        ("public_key_material_recorded", True),
        ("secret_values_recorded", True),
        ("producer_dispatch_authorized", True),
        ("execution_mode", "send"),
        ("next_action", "import the candidate automatically"),
        ("unexpected_authority", True),
    )
    for field, value in mutations:
        tampered = dict(receipt)
        tampered[field] = value
        tampered = artifact_notification._with_integrity(tampered)

        verified = artifact_notification.verify_candidate_artifact_notification(
            tampered,
            report=projected,
            artifact_verification=artifact,
            action_verification=action,
            now=NOW + timedelta(seconds=1),
        )

        assert verified["status"] == "blocked", field
        assert verified["blocking_reason"] == (
            "trust_candidate_artifact_notification_not_current"
        )
        assert verified["interrupt_operator"] is False
        assert verified["delivery_authorized"] is False
        assert verified["candidate_import_authorized"] is False
        assert verified["trust_registry_modified"] is False


def test_artifact_request_presentation_is_deduplicated_until_reminder(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    state_path = tmp_path / "candidate-import-presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )

    first = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=state_path,
        now=NOW,
    )

    assert first["interrupt_operator"] is True
    assert first["presentation"]["state"] == "novel"
    assert not state_path.exists()
    digest = first["presentation"]["presentation_digest"]
    recorded = candidate_import.record_candidate_import_presentation(
        first,
        state_path=state_path,
        expected_presentation_digest=digest,
        now=NOW,
        reminder_after_seconds=300,
    )
    assert recorded["status"] == "recorded"
    assert state_path.stat().st_mode & 0o077 == 0

    refreshed = {
        **waiting,
        "intake_verification_sha256": "9" * 64,
        "artifact_request": {
            **dict(waiting["artifact_request"]),
            "expires_at": (NOW + timedelta(minutes=25)).isoformat(),
        },
    }
    duplicate = candidate_import.apply_candidate_import_presentation_state(
        refreshed,
        state_path=state_path,
        now=NOW + timedelta(seconds=60),
    )
    assert duplicate["interrupt_operator"] is False
    assert duplicate["presentation"]["state"] == "already_presented"
    assert duplicate["presentation"]["presentation_digest"] == digest

    reminder = candidate_import.apply_candidate_import_presentation_state(
        refreshed,
        state_path=state_path,
        now=NOW + timedelta(seconds=301),
    )
    assert reminder["interrupt_operator"] is True
    assert reminder["presentation"]["state"] == "reminder_due"


def test_scheduler_and_health_share_the_operator_presentation_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runner, scheduler_healthcheck

    values = _fixture(tmp_path)
    state_path = tmp_path / "shared-candidate-import-presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    first = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=state_path,
        now=NOW,
    )
    digest = first["presentation"]["presentation_digest"]
    assert candidate_import.record_candidate_import_presentation(
        first,
        state_path=state_path,
        expected_presentation_digest=digest,
        now=NOW,
    )["status"] == "recorded"

    monkeypatch.setattr(
        candidate_import,
        "_now",
        lambda now=None: now or NOW + timedelta(seconds=60),
    )
    runner_projection = (
        runner._project_scheduler_propertyquarry_candidate_import_presentation(
            waiting,
            state_path=state_path,
        )
    )
    assert runner_projection["interrupt_operator"] is False
    assert runner_projection["presentation"]["state"] == "already_presented"

    monkeypatch.setattr(
        candidate_import,
        "inspect_candidate_import_readiness",
        lambda *_args, **_kwargs: dict(waiting),
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_PRESENTATION_STATE_PATH",
        str(state_path),
    )
    health_projection = (
        scheduler_healthcheck._verify_propertyquarry_ooda_source_refresh_trust_candidate_import(
            {}
        )
    )
    assert health_projection["interrupt_operator"] is False
    assert health_projection["presentation"]["presentation_digest"] == digest
    assert health_projection["candidate_import_authorized"] is False
    assert health_projection["delivery_authorized"] is False


def test_tampered_artifact_request_presentation_fails_closed(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    state_path = tmp_path / "candidate-import-presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    first = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=state_path,
        now=NOW,
    )
    candidate_import.record_candidate_import_presentation(
        first,
        state_path=state_path,
        expected_presentation_digest=first["presentation"][
            "presentation_digest"
        ],
        now=NOW,
    )
    state = loads_strict_json_object(
        state_path.read_bytes(),
        field="presentation",
    )
    state["delivery_state_updated"] = True
    state_path.write_bytes(claims._canonical(state))

    with pytest.raises(
        ValueError,
        match="trust_candidate_import_presentation_not_admissible",
    ):
        candidate_import.apply_candidate_import_presentation_state(
            waiting,
            state_path=state_path,
            now=NOW + timedelta(seconds=60),
        )


def test_cli_records_exact_artifact_request_presentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = _fixture(tmp_path)
    state_path = tmp_path / "cli-presentation.json"
    waiting = candidate_import.inspect_current_candidate_import(
        claim_verification_path=Path(values["claim_path"]),
        trust_registry_path=Path(values["trust_registry_path"]),
        candidate_dir=Path(values["candidate_dir"]),
        intake_receipt_path=Path(values["intake_receipt"]),
        intake_verification_path=Path(values["intake_verification"]),
        import_dir=Path(values["import_dir"]),
        now=NOW,
    )
    monkeypatch.setattr(
        candidate_import,
        "inspect_current_candidate_import",
        lambda **_kwargs: dict(waiting),
    )

    assert candidate_import.main(
        ["--inspect", "--presentation-state", str(state_path)]
    ) == 0
    projected = loads_strict_json_object(
        capsys.readouterr().out.encode(),
        field="cli_projection",
    )
    digest = projected["presentation"]["presentation_digest"]

    assert candidate_import.main(
        [
            "--inspect",
            "--presentation-state",
            str(state_path),
            "--record-presentation",
            "--expected-presentation-digest",
            digest,
            "--presentation-reminder-seconds",
            "300",
        ]
    ) == 0
    recorded = loads_strict_json_object(
        capsys.readouterr().out.encode(),
        field="cli_recorded_projection",
    )
    assert recorded["presentation_receipt"]["status"] == "recorded"
    assert recorded["presentation_receipt"]["presentation_digest"] == digest

    assert candidate_import.main(
        ["--inspect", "--presentation-state", str(state_path)]
    ) == 0
    duplicate = loads_strict_json_object(
        capsys.readouterr().out.encode(),
        field="cli_duplicate_projection",
    )
    assert duplicate["interrupt_operator"] is False
    assert duplicate["presentation"]["state"] == "already_presented"


def test_exact_binding_mismatch_fails_before_claim_or_copy(tmp_path: Path) -> None:
    values = _fixture(tmp_path)

    result = _execute(values, expected_source_sha256="f" * 64)

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "trust_candidate_import_exact_binding_mismatch"
    )
    assert not Path(values["candidate_dir"]).exists()
    assert not Path(values["import_dir"]).exists()


def test_missing_literal_confirmation_fails_before_inspection_or_copy(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)

    result = _execute(values, confirmation="")

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "trust_candidate_import_confirmation_missing"
    )
    assert not Path(values["candidate_dir"]).exists()
    assert not Path(values["import_dir"]).exists()


def test_immutable_claim_blocks_replay(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    readiness = _readiness(values)
    assert _execute(values, readiness=readiness)["import_state"] == "succeeded"

    replay = _execute(values, readiness=readiness)

    assert replay["status"] == "blocked"
    assert replay["blocking_reason"] in {
        "trust_candidate_import_exact_binding_mismatch",
        "trust_candidate_import_already_claimed",
    }


def test_invalid_proof_source_fails_without_creating_import_state(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    source_path = Path(values["source_path"])
    candidate = loads_strict_json_object(
        source_path.read_bytes(),
        field="candidate",
    )
    candidate["proof_signature"] = _b64(b"x" * 64)
    source_path.write_bytes(claims._canonical(candidate))

    report = _readiness(values)

    assert report["status"] == "blocked"
    assert report["blocking_reason"] == (
        "trust_candidate_import_source_not_admissible"
    )
    assert not Path(values["candidate_dir"]).exists()
    assert not Path(values["import_dir"]).exists()


def test_result_persistence_failure_reports_a_real_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture(tmp_path)
    original_write = candidate_import.atomic_write_bytes

    def fail_result(path: Path, payload: bytes, *, overwrite: bool) -> None:
        if Path(path).name.startswith("result--"):
            raise RuntimeError("simulated_result_persistence_failure")
        original_write(path, payload, overwrite=overwrite)

    monkeypatch.setattr(candidate_import, "atomic_write_bytes", fail_result)
    result = _execute(values)

    assert result["status"] == "blocked"
    assert result["import_state"] == "recovery_required"
    assert result["candidate_import_attempted"] is True
    assert result["candidate_imported"] is True
    assert result["public_key_candidate_recorded"] is True
    recovery = candidate_import.inspect_candidate_import_readiness(
        dict(values["request"]),
        candidate_dir=Path(values["candidate_dir"]),
        import_dir=Path(values["import_dir"]),
        now=NOW + timedelta(seconds=1),
    )
    projected = candidate_import.apply_candidate_import_presentation_state(
        recovery,
        state_path=tmp_path / "recovery-presentation.json",
        now=NOW + timedelta(seconds=1),
    )
    assert recovery["status"] == "verified"
    assert recovery["import_state"] == "recovery_required"
    assert recovery["blocking_reason"] == (
        "trust_candidate_import_claim_without_result"
    )
    assert recovery["claim_sha256"]
    assert recovery["result_sha256"] == ""
    assert recovery["public_key_sha256"] == result["public_key_sha256"]
    assert projected["presentation"]["state"] == "novel"
    assert projected["interrupt_operator"] is True


def test_runtime_packaging_keeps_import_manual_and_exposes_exact_handoff() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "ea/Dockerfile.property-web").read_text()
    compose = (root / "docker-compose.property.yml").read_text()
    runner = (root / "ea/app/runner.py").read_text()
    healthcheck = (root / "ea/app/scheduler_healthcheck.py").read_text()
    operator_summary = (root / "scripts/operator_summary.sh").read_text()

    assert (
        "COPY --chmod=0555 scripts/propertyquarry_ooda_source_refresh_trust_candidate_import.py"
        in dockerfile
    )
    assert (
        "COPY --chmod=0444 scripts/propertyquarry_ooda_source_refresh_trust_candidate_artifact_request.py"
        in dockerfile
    )
    assert (
        "COPY --chmod=0444 scripts/propertyquarry_ooda_source_refresh_trust_candidate_manual_action.py"
        in dockerfile
    )
    assert (
        "COPY --chmod=0444 scripts/propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification.py"
        in dockerfile
    )
    assert (
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_DIR"
        in compose
    )
    assert "inspect_candidate_import_readiness(" in runner
    assert (
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_SOURCE_DIR"
        in compose
    )
    assert (
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_PRESENTATION_STATE_PATH"
        in compose
    )
    assert (
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_REQUEST_RECEIPT_PATH"
        in compose
    )
    assert (
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_MANUAL_ACTION_RECEIPT_PATH"
        in compose
    )
    assert (
        "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_NOTIFICATION_RECEIPT_PATH"
        in compose
    )
    assert "/run/propertyquarry/ooda-producer-trust-source" in compose
    assert "PROPERTYQUARRY_OODA_TRUST_CANDIDATE_DROP_DIR" in compose
    assert "PROPERTYQUARRY_OODA_TRUST_CANDIDATE_DROP_GID" in compose
    assert "group_add:" in compose
    assert "create_host_path: false" in compose
    assert "source_discovery_dir=Path(" in runner
    assert "source_discovery_dir=Path(" in healthcheck
    assert "source_trust_candidate_import.import_current_candidate(" not in runner
    assert "--inspect-source" in operator_summary
    assert "--import-source" in operator_summary
    assert "apply_candidate_import_presentation_state" in operator_summary
    assert "apply_candidate_import_presentation_state" in runner
    assert "apply_candidate_import_presentation_state" in healthcheck
    assert "materialize_candidate_artifact_request" in runner
    assert "inspect_candidate_artifact_request" in healthcheck
    assert "candidate public request receipt:" in operator_summary
    assert "materialize_candidate_manual_action" in runner
    assert "inspect_candidate_manual_action" in healthcheck
    assert "candidate manual action receipt:" in operator_summary
    assert "run_candidate_artifact_notification" in runner
    assert "inspect_candidate_artifact_notification" in healthcheck
    assert "candidate artifact alert receipt:" in operator_summary
    assert "--inspect-current" in operator_summary
    assert "record_candidate_import_presentation" in operator_summary
    assert "--record-presentation" in operator_summary
    assert "candidate watch authority: read-only" in operator_summary
    assert "candidate drop permissions: directory=0750 source=0640" in (
        operator_summary
    )
    assert "--source-discovery-dir" in operator_summary
    assert (
        "source_discovery_dir=(\n"
        "                    source_refresh_trust_candidate_source_dir\n"
        "                )"
        in operator_summary
    )
    assert (
        "source-refresh-trust-candidate-import-presentation.json"
        in operator_summary
    )
    assert "IMPORT_VERIFIED_PUBLIC_KEY_CANDIDATE" in operator_summary
    assert "never provide or copy a private key" in operator_summary


def test_discovered_candidate_is_a_novel_then_deduplicated_manual_action(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    watched_drop = tmp_path / "watched-drop"
    watched_drop.mkdir(mode=0o700)
    state_path = tmp_path / "presentation.json"
    common = {
        "claim_verification_path": Path(values["claim_path"]),
        "trust_registry_path": Path(values["trust_registry_path"]),
        "candidate_dir": Path(values["candidate_dir"]),
        "intake_receipt_path": Path(values["intake_receipt"]),
        "intake_verification_path": Path(values["intake_verification"]),
        "import_dir": Path(values["import_dir"]),
        "source_discovery_dir": watched_drop,
    }
    waiting = candidate_import.inspect_current_candidate_import(
        **common,
        now=NOW,
    )
    waiting_presented = candidate_import.apply_candidate_import_presentation_state(
        waiting,
        state_path=state_path,
        now=NOW,
    )
    waiting_digest = waiting_presented["presentation"][
        "presentation_digest"
    ]
    assert candidate_import.record_candidate_import_presentation(
        waiting_presented,
        state_path=state_path,
        expected_presentation_digest=waiting_digest,
        now=NOW,
    )["status"] == "recorded"

    discovered_path = watched_drop / "producer-public-candidate.json"
    discovered_path.write_bytes(Path(values["source_path"]).read_bytes())
    discovered_path.chmod(0o600)
    ready = candidate_import.inspect_current_candidate_import(
        **common,
        now=NOW + timedelta(seconds=1),
    )
    ready_presented = candidate_import.apply_candidate_import_presentation_state(
        ready,
        state_path=state_path,
        now=NOW + timedelta(seconds=1),
    )

    assert ready["import_state"] == "ready_for_manual_import"
    assert ready_presented["presentation"]["state"] == "novel"
    assert ready_presented["interrupt_operator"] is True
    ready_digest = ready_presented["presentation"]["presentation_digest"]
    assert ready_digest != waiting_digest
    assert candidate_import.record_candidate_import_presentation(
        ready_presented,
        state_path=state_path,
        expected_presentation_digest=ready_digest,
        now=NOW + timedelta(seconds=1),
    )["status"] == "recorded"
    repeated = candidate_import.apply_candidate_import_presentation_state(
        candidate_import.inspect_current_candidate_import(
            **common,
            now=NOW + timedelta(seconds=2),
        ),
        state_path=state_path,
        now=NOW + timedelta(seconds=2),
    )
    assert repeated["presentation"]["state"] == "already_presented"
    assert repeated["interrupt_operator"] is False
    assert not Path(values["candidate_dir"]).exists()
    assert not Path(values["import_dir"]).exists()
