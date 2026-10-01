from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_authorization_request as authorization
from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_runtime_review as review


NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)


def _local_merge_deployment_environment(root: Path) -> dict[str, object]:
    for relative in review._LOCAL_RUNTIME_BINDING_PATHS.values():
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
    required = sorted(
        set().union(
            *(
                set(contract["keys"])
                for contract in review._DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES
            )
        )
    )
    (root / "docker-compose.property.yml").write_text(
        "services:\n  api:\n    environment:\n"
        + "".join(f"      {key}: ${{{key}:?required}}\n" for key in required),
        encoding="utf-8",
    )
    (root / "docker-compose.cloudflared.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    present = sorted(set(required) - set(review._LOCAL_CANDIDATE_KEYS))
    environment_path = root / ".env"
    environment_path.write_text(
        "".join(f"{key}=present-{index}\n" for index, key in enumerate(present)),
        encoding="utf-8",
    )
    environment_path.chmod(0o600)
    candidate = review.materialize_local_runtime_binding_candidate(root=root)
    assert candidate["status"] == "ready_for_merge_review"
    posture = review.inspect_deployment_environment(root=root)
    assert posture["intake_plan"]["local_merge_review_required"] is True
    return posture


def _review_packet(
    *,
    now: datetime = NOW,
    source_generated_at: str = "",
    probe_origin: str = "http://127.0.0.1:8090",
    reason: str = "live_runtime_host_admission_rejected",
    release_worktree_fingerprint_sha256: str = "d" * 64,
    deployment_environment_posture: dict[str, object] | None = None,
) -> dict[str, object]:
    source_generated_at = source_generated_at or (
        now - timedelta(minutes=10)
    ).isoformat()
    action = {
        "lane": "gold_live_runtime",
        "reason": reason,
        "source_generated_at": source_generated_at,
        "safe_next_action": operator_status._GOLD_ACTIONS[reason],
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
        "provider_quota_consumption_allowed": False,
    }
    status = {
        "schema": operator_status.SCHEMA,
        "status": "action_required",
        "action_required": True,
        "interrupt_operator": True,
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
        "actions": [action],
    }
    source_evidence = {
        name: {
            "sha256": character * 64,
            "bytes": 1024,
            "generated_at": (now - timedelta(minutes=5)).isoformat(),
        }
        for name, character in (("cycle_receipt", "a"), ("approval_manifest", "b"))
    }
    runtime_rows: list[dict[str, str]] = []
    runtime = {
        "source": "local_docker",
        "observed_at": now.isoformat(),
        "query_status": "pass",
        "project": "property",
        "container_count": 0,
        "services": runtime_rows,
        "fingerprint_sha256": review._sha256(
            review._canonical({"project": "property", "services": runtime_rows})
        ),
    }
    release = {
        "source": "local_git",
        "observed_at": now.isoformat(),
        "head_sha": "c" * 40,
        "worktree_clean": False,
        "changed_path_count": 17,
        "worktree_fingerprint_sha256": release_worktree_fingerprint_sha256,
        "changed_paths_recorded": False,
    }
    probe_values: dict[str, object] = (
        {
            "source": "public_https_live_mobile_receipt",
            "source_sha256": "e" * 64,
            "source_bytes": 2406,
            "source_generated_at": source_generated_at,
            "status": "blocked",
            "base_origin": "https://propertyquarry.com",
            "host_header": "propertyquarry.com",
            "failure_code": "cloudflare_1033_tunnel_unavailable",
            "edge_provider": "cloudflare",
            "edge_error_code": "1033",
            "http_status": 530,
            "failure_reason": "tunnel_unavailable",
            "secret_values_recorded": False,
        }
        if reason == "live_runtime_tunnel_unavailable"
        else {
            "source": "local_private_live_mobile_receipt",
            "source_sha256": "e" * 64,
            "source_bytes": 1406,
            "source_generated_at": source_generated_at,
            "status": "blocked",
            "base_origin": probe_origin,
            "host_header": "propertyquarry.com",
            "failure_code": "http_421_misdirected_request",
            "secret_values_recorded": False,
        }
    )
    probe_values["fingerprint_sha256"] = review._sha256(
        review._canonical(review._probe_fingerprint_payload(probe_values))
    )
    release_authority = {
        "source": "repository_release_manifest",
        "source_sha256": "f" * 64,
        "source_bytes": 4096,
        "release_repository": "ArchonMegalon/propertyquarry",
        "release_public_origin": "https://propertyquarry.com",
        "release_public_host": "propertyquarry.com",
        "release_deployment_id": "propertyquarry-governed-deploy-test",
        "release_generated_at": (now - timedelta(days=9)).isoformat(),
        "secret_values_recorded": False,
    }
    return review.build_review_packet(
        status=status,
        snapshot_evidence=source_evidence,
        runtime_posture=runtime,
        release_posture=release,
        probe_posture=probe_values,
        release_authority=release_authority,
        deployment_environment_posture=deployment_environment_posture,
        now=now,
    )


def _request() -> dict[str, object]:
    packet = _review_packet()
    return authorization.build_authorization_request(
        review_packet=packet,
        review_packet_sha256=authorization._sha256(authorization._canonical(packet)),
        now=NOW,
    )


def _as_previous_request(request: dict[str, object]) -> dict[str, object]:
    previous = json.loads(json.dumps(request))
    previous["schema"] = authorization.PREVIOUS_SCHEMA
    previous["request_id"] = authorization._request_identifier(
        previous["binding"],
        previous["scope"],
    )
    previous.pop("integrity")
    previous["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": authorization._sha256(
            authorization._canonical(previous)
        ),
    }
    return previous


def test_authorization_request_is_exact_scope_pending_and_non_executing() -> None:
    packet = _review_packet()
    request = authorization.build_authorization_request(
        review_packet=packet,
        review_packet_sha256=authorization._sha256(authorization._canonical(packet)),
        now=NOW,
    )

    verification = authorization.verify_authorization_request(
        request,
        review_packet=packet,
        now=NOW,
    )

    assert verification["status"] == "verified"
    assert request["schema"] == authorization.SCHEMA
    assert request["request_id"] == authorization._request_instance_identifier(
        request["binding"],
        request["scope"],
        generated_at=request["generated_at"],
    )
    assert request["scope"] == {
        "operation": "runtime_configuration_change",
        "change_id": "live_probe_origin",
        "current_value": "http://127.0.0.1:8090",
        "proposed_value": "http://127.0.0.1:8097",
        "public_host": "propertyquarry.com",
        "public_origin": "https://propertyquarry.com",
        "compose_project": "property",
    }
    assert request["authorization"]["recorded"] is False
    assert request["execution_authorized"] is False
    assert request["deployment_or_restart_authorized"] is False
    assert request["excluded_operations"] == [
        "deployment_or_restart",
        "provider_account_change",
        "provider_quota_consumption",
        "external_delivery",
    ]
    assert request["provider_quota_consumption_allowed"] is False
    assert request["delivery_authorized"] is False
    assert "must-not-be-recorded" not in json.dumps(request)


def test_authorization_request_rejects_tamper_even_with_recomputed_integrity() -> None:
    packet = _review_packet()
    request = _request()
    request["authorization"]["recorded"] = True

    integrity_failure = authorization.verify_authorization_request(
        request,
        review_packet=packet,
        now=NOW,
    )
    assert integrity_failure["blocking_reason"] == "authorization_request_integrity_invalid"

    normalized = dict(request)
    normalized.pop("integrity")
    request["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": authorization._sha256(
            authorization._canonical(normalized)
        ),
    }
    contract_failure = authorization.verify_authorization_request(
        request,
        review_packet=packet,
        now=NOW,
    )
    assert contract_failure["blocking_reason"] == "authorization_request_contract_not_admissible"
    assert contract_failure["execution_authorized"] is False


def test_authorization_request_id_is_bound_to_its_freshness_window() -> None:
    packet = _review_packet()
    request = _request()
    request["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    request["expires_at"] = (NOW + timedelta(seconds=901)).isoformat()
    request.pop("integrity")
    request["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": authorization._sha256(
            authorization._canonical(request)
        ),
    }

    verification = authorization.verify_authorization_request(
        request,
        review_packet=packet,
        now=NOW + timedelta(seconds=1),
    )

    assert verification["status"] == "blocked"
    assert verification["blocking_reason"] == (
        "authorization_request_contract_not_admissible"
    )
    assert verification["authorization_recorded"] is False
    assert verification["execution_authorized"] is False
    assert verification["deployment_or_restart_authorized"] is False


def test_authorization_request_expires_and_never_becomes_implicit_consent() -> None:
    packet = _review_packet()
    request = _request()

    expired = authorization.verify_authorization_request(
        request,
        review_packet=packet,
        now=NOW + timedelta(seconds=901),
    )

    assert expired["status"] == "blocked"
    assert expired["blocking_reason"] == "authorization_request_not_fresh"
    assert expired["authorization_recorded"] is False
    assert expired["execution_authorized"] is False
    assert expired["deployment_or_restart_authorized"] is False


def test_current_request_verifier_binds_private_file_to_current_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _review_packet()
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request_path = tmp_path / "request.json"

    authorization.materialize_current_authorization_request(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )
    verified = authorization.verify_current_authorization_request(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["authorization_recorded"] is False
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600

    action_source = str(packet["action"]["source_generated_at"])
    changed_packet = _review_packet(
        now=NOW + timedelta(seconds=1),
        source_generated_at=action_source,
    )
    changed_digest = authorization._sha256(authorization._canonical(changed_packet))
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (changed_packet, changed_digest, current),
    )
    refreshed_evidence = authorization.verify_current_authorization_request(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=1),
    )
    assert refreshed_evidence["status"] == "verified"
    assert refreshed_evidence["binding_mode"] == "stable_scope_current_evidence"
    assert refreshed_evidence["review_packet_binding_current"] is False
    assert refreshed_evidence["progress"]["current_evidence_verified"] is True

    changed_scope_packet = _review_packet(
        now=NOW + timedelta(seconds=2),
        source_generated_at=action_source,
        probe_origin="http://127.0.0.1:8091",
    )
    changed_scope_digest = authorization._sha256(
        authorization._canonical(changed_scope_packet)
    )
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (changed_scope_packet, changed_scope_digest, current),
    )
    drifted = authorization.verify_current_authorization_request(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=2),
    )
    assert drifted["status"] == "blocked"
    assert drifted["execution_authorized"] is False


def test_authorization_handoff_reuses_stable_scope_across_packet_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _review_packet()
    packet_digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, packet_digest, current),
    )
    request_path = tmp_path / "request.json"
    authorization.materialize_current_authorization_request(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )
    original = request_path.read_bytes()
    original_mtime_ns = request_path.stat().st_mtime_ns

    refreshed_packet = _review_packet(
        now=NOW + timedelta(seconds=1),
        source_generated_at=str(packet["action"]["source_generated_at"]),
    )
    refreshed_digest = authorization._sha256(
        authorization._canonical(refreshed_packet)
    )
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (refreshed_packet, refreshed_digest, current),
    )
    handoff = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=1),
    )

    assert handoff["status"] == "ready"
    assert handoff["request_state"] == "reused"
    assert handoff["request_state_updated"] is False
    assert handoff["binding_mode"] == "stable_scope_current_evidence"
    assert handoff["current_evidence_verified"] is True
    assert handoff["authorization_recorded"] is False
    assert handoff["execution_authorized"] is False
    assert handoff["deployment_or_restart_authorized"] is False
    assert handoff["provider_quota_consumption_allowed"] is False
    assert handoff["delivery_authorized"] is False
    assert request_path.read_bytes() == original
    assert request_path.stat().st_mtime_ns == original_mtime_ns


def test_authorization_handoff_does_not_extend_a_valid_near_expiry_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _review_packet()
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request_path = tmp_path / "request.json"
    initial = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )
    original = request_path.read_bytes()

    near_expiry = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=601),
    )

    assert initial["remaining_seconds"] == authorization.DEFAULT_TTL_SECONDS
    assert near_expiry["request_state"] == "reused"
    assert near_expiry["request_state_updated"] is False
    assert near_expiry["request_id"] == initial["request_id"]
    assert near_expiry["expires_at"] == initial["expires_at"]
    assert near_expiry["remaining_seconds"] == 299
    assert request_path.read_bytes() == original
    assert near_expiry["authorization_recorded"] is False
    assert near_expiry["execution_authorized"] is False
    assert near_expiry["deployment_or_restart_authorized"] is False


def test_authorization_handoff_stages_missing_request_without_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _review_packet()
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request_path = tmp_path / "request.json"

    handoff = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )

    assert handoff["status"] == "ready"
    assert handoff["request_state"] == "refreshed"
    assert handoff["request_refresh_reason"] == "missing"
    assert handoff["previous_request_sha256"] == ""
    assert handoff["request_state_updated"] is True
    assert handoff["authorization_recorded"] is False
    assert handoff["execution_authorized"] is False
    assert handoff["deployment_or_restart_authorized"] is False
    assert handoff["provider_quota_consumption_allowed"] is False
    assert handoff["delivery_authorized"] is False
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600


def test_tunnel_review_stages_local_merge_authorization_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_environment = _local_merge_deployment_environment(tmp_path)
    monkeypatch.setattr(
        review,
        "inspect_deployment_environment",
        lambda **_kwargs: deployment_environment,
    )
    packet = _review_packet(
        reason="live_runtime_tunnel_unavailable",
        deployment_environment_posture=deployment_environment,
    )
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request_path = tmp_path / "request.json"

    handoff = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )

    assert handoff["status"] == "ready"
    assert handoff["request_state"] == "refreshed"
    assert handoff["request_refresh_reason"] == "missing"
    assert handoff["scope"] == {
        "operation": "runtime_configuration_change",
        "change_id": "local_environment_candidate_merge",
        "recovery_preview_sha256": packet["consent_gate"][
            "authorization_scope"
        ]["recovery_preview_sha256"],
        "candidate_path": (
            "_completion/propertyquarry_ooda_notification_cycle/"
            "deployment-environment-local-runtime-bindings.env"
        ),
        "candidate_policy_schema": (
            "propertyquarry."
            "deployment_environment_local_runtime_candidate.v2"
        ),
        "candidate_review_keys": sorted(review._LOCAL_CANDIDATE_KEYS),
        "candidate_review_key_count": 8,
        "configuration_merge_policy": "add_missing_keys_only",
        "existing_environment_values_overwrite_allowed": False,
        "rollback_required": True,
        "compose_project": "property",
    }
    assert handoff["authorization_recorded"] is False
    assert handoff["execution_authorized"] is False
    assert handoff["deployment_or_restart_authorized"] is False
    assert handoff["provider_quota_consumption_allowed"] is False
    assert handoff["delivery_authorized"] is False
    assert request_path.exists()
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    persisted = json.loads(request_path.read_text(encoding="utf-8"))
    assert persisted["schema"] == authorization.SCHEMA
    assert persisted["secret_values_recorded"] is False
    assert "must-not-be-recorded" not in json.dumps(persisted)


def test_local_merge_handoff_reuses_receipt_churn_and_refreshes_changed_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_environment = _local_merge_deployment_environment(tmp_path)
    monkeypatch.setattr(
        review,
        "inspect_deployment_environment",
        lambda **_kwargs: deployment_environment,
    )
    packet = _review_packet(
        reason="live_runtime_tunnel_unavailable",
        deployment_environment_posture=deployment_environment,
    )
    packet_digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, packet_digest, current),
    )
    request_path = tmp_path / "request.json"
    staged = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )
    original = request_path.read_bytes()

    churned_packet = _review_packet(
        reason="live_runtime_tunnel_unavailable",
        now=NOW + timedelta(seconds=1),
        source_generated_at=str(packet["action"]["source_generated_at"]),
        deployment_environment_posture=deployment_environment,
    )
    churned_digest = authorization._sha256(
        authorization._canonical(churned_packet)
    )
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (churned_packet, churned_digest, current),
    )
    reused = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=1),
    )

    assert reused["status"] == "ready"
    assert reused["request_state"] == "reused"
    assert reused["request_id"] == staged["request_id"]
    assert request_path.read_bytes() == original

    changed_packet = _review_packet(
        reason="live_runtime_tunnel_unavailable",
        now=NOW + timedelta(seconds=2),
        source_generated_at=str(packet["action"]["source_generated_at"]),
        release_worktree_fingerprint_sha256="9" * 64,
        deployment_environment_posture=deployment_environment,
    )
    changed_digest = authorization._sha256(
        authorization._canonical(changed_packet)
    )
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (changed_packet, changed_digest, current),
    )
    refreshed = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=2),
    )

    assert refreshed["status"] == "ready"
    assert refreshed["request_state"] == "refreshed"
    assert refreshed["request_refresh_reason"] == "superseded"
    assert refreshed["request_id"] != staged["request_id"]
    assert refreshed["authorization_recorded"] is False
    assert refreshed["execution_authorized"] is False
    assert refreshed["deployment_or_restart_authorized"] is False


def test_authorization_handoff_refreshes_changed_scope_without_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _review_packet()
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request_path = tmp_path / "request.json"
    authorization.materialize_current_authorization_request(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )
    prior_sha256 = authorization._sha256(request_path.read_bytes())

    changed_packet = _review_packet(
        now=NOW + timedelta(seconds=1),
        source_generated_at=str(packet["action"]["source_generated_at"]),
        probe_origin="http://127.0.0.1:8091",
    )
    changed_digest = authorization._sha256(
        authorization._canonical(changed_packet)
    )
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (changed_packet, changed_digest, current),
    )

    handoff = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=1),
    )

    assert handoff["status"] == "ready"
    assert handoff["request_state"] == "refreshed"
    assert handoff["request_refresh_reason"] == "superseded"
    assert handoff["previous_request_sha256"] == prior_sha256
    assert handoff["request_sha256"] != prior_sha256
    assert handoff["scope"]["current_value"] == "http://127.0.0.1:8091"
    assert handoff["authorization_recorded"] is False
    assert handoff["execution_authorized"] is False
    assert handoff["deployment_or_restart_authorized"] is False
    assert handoff["provider_quota_consumption_allowed"] is False
    assert handoff["delivery_authorized"] is False


def test_authorization_handoff_refreshes_expired_request_without_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _review_packet()
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request_path = tmp_path / "request.json"
    authorization.materialize_current_authorization_request(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW,
    )
    prior_request = json.loads(request_path.read_text(encoding="utf-8"))
    prior_sha256 = authorization._sha256(request_path.read_bytes())

    handoff = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=901),
    )

    assert handoff["status"] == "ready"
    assert handoff["request_state"] == "refreshed"
    assert handoff["request_refresh_reason"] == "expired"
    assert handoff["request_id"] != prior_request["request_id"]
    assert handoff["previous_request_sha256"] == prior_sha256
    assert handoff["request_state_updated"] is True
    assert handoff["remaining_seconds"] == authorization.DEFAULT_TTL_SECONDS
    assert handoff["authorization_recorded"] is False
    assert handoff["execution_authorized"] is False
    assert handoff["deployment_or_restart_authorized"] is False
    assert handoff["protected_operation_executed"] is False
    assert handoff["provider_quota_consumption_allowed"] is False
    assert handoff["delivery_authorized"] is False
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    refreshed = json.loads(request_path.read_text(encoding="utf-8"))
    assert refreshed["schema"] == authorization.SCHEMA
    assert refreshed["request_id"] == handoff["request_id"]


def test_authorization_handoff_supersedes_expired_v2_with_new_instance_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_environment = _local_merge_deployment_environment(tmp_path)
    monkeypatch.setattr(
        review,
        "inspect_deployment_environment",
        lambda **_kwargs: deployment_environment,
    )
    packet = _review_packet(
        reason="live_runtime_tunnel_unavailable",
        deployment_environment_posture=deployment_environment,
    )
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request_path = tmp_path / "request.json"
    previous = _as_previous_request(
        authorization.build_authorization_request(
            review_packet=packet,
            review_packet_sha256=digest,
            now=NOW,
        )
    )
    request_path.write_bytes(authorization._canonical(previous))
    request_path.chmod(0o600)

    handoff = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=901),
    )

    persisted = json.loads(request_path.read_text(encoding="utf-8"))
    assert handoff["status"] == "ready"
    assert handoff["request_state"] == "refreshed"
    assert handoff["request_refresh_reason"] == "expired"
    assert handoff["request_id"] != previous["request_id"]
    assert persisted["schema"] == authorization.SCHEMA
    assert persisted["request_id"] == handoff["request_id"]
    assert handoff["authorization_recorded"] is False
    assert handoff["execution_authorized"] is False
    assert handoff["deployment_or_restart_authorized"] is False
    assert handoff["protected_operation_executed"] is False


def test_authorization_handoff_refuses_to_overwrite_unsafe_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _review_packet()
    digest = authorization._sha256(authorization._canonical(packet))
    current = {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }
    monkeypatch.setattr(
        authorization,
        "_review_packet_current",
        lambda **_kwargs: (packet, digest, current),
    )
    request = _request()
    request["authorization"]["recorded"] = True
    normalized = dict(request)
    normalized.pop("integrity")
    request["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": authorization._sha256(
            authorization._canonical(normalized)
        ),
    }
    request_path = tmp_path / "request.json"
    request_path.write_bytes(authorization._canonical(request))
    request_path.chmod(0o600)
    original = request_path.read_bytes()

    handoff = authorization.stage_current_authorization_handoff(
        request_path=request_path,
        packet_path=tmp_path / "review.json",
        now=NOW + timedelta(seconds=1),
    )

    assert handoff["status"] == "blocked"
    assert (
        handoff["blocking_reason"]
        == "authorization_handoff_existing_request_not_admissible"
    )
    assert handoff["request_state_updated"] is False
    assert handoff["authorization_recorded"] is False
    assert handoff["execution_authorized"] is False
    assert request_path.read_bytes() == original


def test_cli_failure_receipt_is_sanitized_and_overwrites_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    request_path = tmp_path / "request.json"
    verification_path = tmp_path / "verification.json"
    monkeypatch.setattr(
        authorization,
        "materialize_current_authorization_request",
        lambda **_kwargs: {"generated_at": NOW.isoformat()},
    )
    monkeypatch.setattr(
        authorization,
        "verify_current_authorization_request",
        lambda **_kwargs: {
            **authorization._blocked("explicit_operator_decision_required", now=NOW),
            "status": "verified",
        },
    )

    success = authorization.main(
        [
            "--request",
            str(request_path),
            "--verification-write",
            str(verification_path),
        ]
    )
    assert success == 0
    assert json.loads(verification_path.read_text())["status"] == "verified"
    capsys.readouterr()

    monkeypatch.setattr(
        authorization,
        "materialize_current_authorization_request",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("must-not-be-recorded")),
    )
    failure = authorization.main(
        [
            "--request",
            str(request_path),
            "--verification-write",
            str(verification_path),
        ]
    )
    receipt = json.loads(verification_path.read_text())

    assert failure == 1
    assert receipt["blocking_reason"] == "authorization_request_materialization_failed"
    assert receipt["authorization_recorded"] is False
    assert receipt["execution_authorized"] is False
    assert receipt["verification_receipt_persisted"] is True
    assert "must-not-be-recorded" not in json.dumps(receipt)
