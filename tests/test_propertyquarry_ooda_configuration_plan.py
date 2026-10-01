from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_configuration_plan as plan


NOW = datetime(2026, 8, 26, 9, 20, tzinfo=timezone.utc)


def _source_root(tmp_path: Path, *, aligned: bool = False) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    expression = (
        plan.PROPOSED_PORT_EXPRESSION if aligned else plan.CURRENT_PORT_EXPRESSION
    )
    (tmp_path / plan.COMPOSE_PATH).write_text(
        f'services:\n  propertyquarry-api:\n    ports:\n      - "{expression}"\n',
        encoding="utf-8",
    )
    (tmp_path / plan.ISOLATION_PATH).write_text(
        'API_HOST_PORT_KEY = "EA_HOST_PORT"\n'
        'if root_values.get(API_HOST_PORT_KEY) != "8097":\n'
        "    raise ValueError\n",
        encoding="utf-8",
    )
    (tmp_path / plan.DEPLOYMENT_AUDIT_PATH).write_text(
        'DEFAULT_LOCAL_ORIGIN: Final = "http://127.0.0.1:8097"\n',
        encoding="utf-8",
    )
    (tmp_path / plan.LIVE_SMOKE_PATH).write_text(
        'default=_env("PROPERTYQUARRY_LIVE_BASE_URL", "http://localhost:8097")\n',
        encoding="utf-8",
    )
    return tmp_path


def _request() -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_runtime_authorization_request.v1",
        "request_id": "pqar_0123456789abcdef01234567",
        "generated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "binding": {
            "review_packet_sha256": "a" * 64,
            "proposal_sha256": "b" * 64,
        },
        "scope": {
            "operation": "runtime_configuration_change",
            "change_id": "live_probe_origin",
            "current_value": "http://127.0.0.1:8090",
            "proposed_value": "http://127.0.0.1:8097",
            "public_host": "propertyquarry.com",
            "public_origin": "https://propertyquarry.com",
            "compose_project": "property",
        },
    }


def _request_verification() -> dict[str, object]:
    return {
        "status": "verified",
        "progress": {"current_evidence_verified": True},
        "authorization_recorded": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _local_request() -> dict[str, object]:
    review_keys = sorted(plan.runtime_review._LOCAL_CANDIDATE_KEYS)
    return {
        "schema": plan.authorization_request.SCHEMA,
        "request_id": "pqar_89abcdef0123456789abcdef",
        "generated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "binding": {
            "review_packet_sha256": "c" * 64,
            "proposal_sha256": "d" * 64,
        },
        "scope": {
            "operation": "runtime_configuration_change",
            "change_id": "local_environment_candidate_merge",
            "recovery_preview_sha256": "e" * 64,
            "candidate_path": (
                "_completion/propertyquarry_ooda_notification_cycle/"
                "deployment-environment-local-runtime-bindings.env"
            ),
            "candidate_policy_schema": (
                "propertyquarry."
                "deployment_environment_local_runtime_candidate.v2"
            ),
            "candidate_review_keys": review_keys,
            "candidate_review_key_count": len(review_keys),
            "configuration_merge_policy": "add_missing_keys_only",
            "existing_environment_values_overwrite_allowed": False,
            "rollback_required": True,
            "compose_project": "property",
        },
    }


def _local_sources(request: dict[str, object]) -> dict[str, object]:
    scope = request["scope"]
    assert isinstance(scope, dict)
    review_keys = list(scope["candidate_review_keys"])
    posture: dict[str, object] = {
        "status": "ready",
        "root": "private_environment_layers",
        "target": {
            "path": ".env",
            "file_mode": 0o600,
            "missing_keys": review_keys,
            "missing_key_count": len(review_keys),
            "configuration_merge_policy": "add_missing_keys_only",
            "existing_environment_values_overwrite_allowed": False,
            "change_required": True,
        },
        "candidate": {
            "path": scope["candidate_path"],
            "schema": scope["candidate_policy_schema"],
            "file_mode": 0o600,
            "status": "ready_for_merge_review",
            "review_keys": review_keys,
            "review_key_count": len(review_keys),
            "candidate_matches_current_derivation": True,
            "candidate_secret_policy_verified": True,
            "candidate_values_recorded": False,
            "candidate_values_hashed": False,
        },
        "recovery_preview_sha256": scope["recovery_preview_sha256"],
        "environment_values_recorded": False,
        "environment_values_hashed": False,
        "secret_values_recorded": False,
    }
    posture["fingerprint_sha256"] = plan._sha256(plan._canonical(posture))
    return posture


def _decision_verification(
    request: dict[str, object],
    *,
    value: str = "pending",
) -> dict[str, object]:
    request_sha256 = plan._sha256(plan._canonical(request))
    verified = value != "pending"
    exact = value == "approve_exact_scope"
    return {
        "status": "verified" if verified else "pending",
        "decision": value if verified else "",
        "progress": {"current_evidence_verified": True},
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "exact_scope_authorized": exact,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _build(
    source_posture: dict[str, object],
    *,
    decision_value: str = "pending",
    now: datetime = NOW,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    request = _request()
    decision = _decision_verification(request, value=decision_value)
    result = plan.build_configuration_plan(
        request=request,
        request_sha256=plan._sha256(plan._canonical(request)),
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=source_posture,
        now=now,
    )
    return request, decision, result


def test_source_inspection_finds_one_exact_compose_drift_and_aligned_authorities(
    tmp_path: Path,
) -> None:
    sources = plan.inspect_configuration_sources(root=_source_root(tmp_path))

    assert sources["status"] == "ready"
    assert sources["target"]["change_required"] is True
    assert sources["target"]["current_expression_count"] == 1
    assert sources["target"]["proposed_expression_count"] == 0
    assert [row["status"] for row in sources["supporting_contracts"]] == [
        "aligned",
        "aligned",
        "aligned",
    ]
    assert sources["secret_values_recorded"] is False


def test_pending_plan_is_exact_non_applying_and_hash_bound(tmp_path: Path) -> None:
    sources = plan.inspect_configuration_sources(root=_source_root(tmp_path))
    request, decision, result = _build(sources)

    verification = plan.verify_configuration_plan(
        result,
        request=request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )

    assert verification["status"] == "verified"
    assert result["status"] == "review_ready"
    assert result["change"] == {
        "operation": "replace_exact_text",
        "path": "docker-compose.property.yml",
        "selector": "services.propertyquarry-api.ports[0]",
        "expected_before_sha256": sources["target"]["sha256"],
        "current_expression": plan.CURRENT_PORT_EXPRESSION,
        "proposed_expression": plan.PROPOSED_PORT_EXPRESSION,
        "change_required": True,
        "replacement_count": 1,
        "rollback_expression": plan.CURRENT_PORT_EXPRESSION,
    }
    assert result["authorization"] == {
        "required": True,
        "recorded": False,
        "decision": "pending",
        "exact_scope_authorized": False,
    }
    assert result["manual_apply_authorized"] is False
    assert result["automatic_apply_allowed"] is False
    assert result["apply_performed"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


@pytest.mark.parametrize(
    ("decision_value", "manual_apply_authorized"),
    [("pending", False), ("approve_exact_scope", True)],
)
def test_local_environment_merge_plan_is_add_only_value_free_and_non_executing(
    decision_value: str,
    manual_apply_authorized: bool,
) -> None:
    request = _local_request()
    sources = _local_sources(request)
    decision = _decision_verification(request, value=decision_value)
    result = plan.build_configuration_plan(
        request=request,
        request_sha256=plan._sha256(plan._canonical(request)),
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    verification = plan.verify_configuration_plan(
        result,
        request=request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )

    assert verification["status"] == "verified"
    assert result["change"] == {
        "operation": "merge_dotenv_add_missing_keys",
        "target_path": ".env",
        "candidate_path": request["scope"]["candidate_path"],
        "candidate_policy_schema": request["scope"][
            "candidate_policy_schema"
        ],
        "review_keys": request["scope"]["candidate_review_keys"],
        "review_key_count": 8,
        "configuration_merge_policy": "add_missing_keys_only",
        "existing_environment_values_overwrite_allowed": False,
        "change_required": True,
        "rollback_policy": "restore_private_pre_apply_snapshot",
        "environment_values_recorded": False,
        "environment_values_hashed": False,
    }
    assert result["validation"] == plan.LOCAL_ENVIRONMENT_VALIDATION_STEPS
    assert result["manual_apply_authorized"] is manual_apply_authorized
    assert result["automatic_apply_allowed"] is False
    assert result["apply_performed"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["secret_values_recorded"] is False
    assert "must-not-be-recorded" not in json.dumps(result)


def test_local_environment_merge_plan_rejects_overwrite_policy_tamper() -> None:
    request = _local_request()
    sources = _local_sources(request)
    decision = _decision_verification(request)
    result = plan.build_configuration_plan(
        request=request,
        request_sha256=plan._sha256(plan._canonical(request)),
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    result["change"]["existing_environment_values_overwrite_allowed"] = True
    result.pop("integrity")
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": plan._sha256(plan._canonical(result)),
    }

    verification = plan.verify_configuration_plan(
        result,
        request=request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )

    assert verification["status"] == "blocked"
    assert verification["blocking_reason"] == (
        "configuration_plan_contract_not_admissible"
    )
    assert verification["manual_apply_authorized"] is False
    assert verification["execution_authorized"] is False


def test_approved_plan_allows_only_manual_exact_edit_not_execution(
    tmp_path: Path,
) -> None:
    sources = plan.inspect_configuration_sources(root=_source_root(tmp_path))
    request, decision, result = _build(
        sources,
        decision_value="approve_exact_scope",
    )

    verification = plan.verify_configuration_plan(
        result,
        request=request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )

    assert verification["status"] == "verified"
    assert verification["exact_scope_authorized"] is True
    assert verification["manual_apply_authorized"] is True
    assert verification["automatic_apply_allowed"] is False
    assert verification["apply_performed"] is False
    assert verification["execution_authorized"] is False
    assert verification["deployment_or_restart_authorized"] is False


@pytest.mark.parametrize("decision_value", ["reject", "defer"])
def test_reject_and_defer_close_manual_apply(
    tmp_path: Path,
    decision_value: str,
) -> None:
    sources = plan.inspect_configuration_sources(root=_source_root(tmp_path))
    _request_value, _decision, result = _build(
        sources,
        decision_value=decision_value,
    )

    assert result["status"] == ("rejected" if decision_value == "reject" else "deferred")
    assert result["manual_apply_authorized"] is False
    assert result["apply_performed"] is False


def test_already_aligned_source_needs_no_edit_even_if_scope_was_approved(
    tmp_path: Path,
) -> None:
    sources = plan.inspect_configuration_sources(
        root=_source_root(tmp_path, aligned=True)
    )
    _request_value, _decision, result = _build(
        sources,
        decision_value="approve_exact_scope",
    )

    assert sources["target"]["change_required"] is False
    assert result["change"]["replacement_count"] == 0
    assert result["manual_apply_authorized"] is False
    assert result["apply_performed"] is False


def test_source_inspection_rejects_ambiguous_or_drifted_target(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    (root / plan.COMPOSE_PATH).write_text(
        "services:\n  propertyquarry-api:\n    ports:\n      - 127.0.0.1:9999:8090\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="compose_host_port_contract_not_admissible"):
        plan.inspect_configuration_sources(root=root)


def test_plan_tamper_fails_even_with_recomputed_integrity(tmp_path: Path) -> None:
    sources = plan.inspect_configuration_sources(root=_source_root(tmp_path))
    request, decision, result = _build(sources)
    result["apply_performed"] = True

    integrity_failure = plan.verify_configuration_plan(
        result,
        request=request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    assert integrity_failure["blocking_reason"] == "configuration_plan_integrity_invalid"

    normalized = dict(result)
    normalized.pop("integrity")
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": plan._sha256(plan._canonical(normalized)),
    }
    contract_failure = plan.verify_configuration_plan(
        result,
        request=request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    assert contract_failure["blocking_reason"] == "configuration_plan_contract_not_admissible"
    assert contract_failure["manual_apply_authorized"] is False
    assert contract_failure["apply_performed"] is False


def test_current_plan_is_private_and_detects_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = _source_root(tmp_path / "source")
    sources = plan.inspect_configuration_sources(root=source_root)
    request = _request()
    request_sha256 = plan._sha256(plan._canonical(request))
    request_verification = _request_verification()
    decision_verification = _decision_verification(request)
    monkeypatch.setattr(
        plan,
        "_current_inputs",
        lambda **_kwargs: (
            request,
            request_sha256,
            request_verification,
            decision_verification,
            sources,
        ),
    )
    plan_path = tmp_path / "private" / "plan.json"

    plan.materialize_current_configuration_plan(
        plan_path=plan_path,
        root=source_root,
        now=NOW,
    )
    verified = plan.verify_current_configuration_plan(
        plan_path=plan_path,
        root=source_root,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["manual_apply_authorized"] is False
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600

    drifted_sources = dict(sources)
    drifted_sources["fingerprint_sha256"] = "f" * 64
    monkeypatch.setattr(
        plan,
        "_current_inputs",
        lambda **_kwargs: (
            request,
            request_sha256,
            request_verification,
            decision_verification,
            drifted_sources,
        ),
    )
    drifted = plan.verify_current_configuration_plan(
        plan_path=plan_path,
        root=source_root,
        now=NOW,
    )
    assert drifted["status"] == "blocked"
    assert drifted["apply_performed"] is False


def test_configuration_handoff_stages_and_reuses_pending_non_applying_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = _source_root(tmp_path / "source")
    sources = plan.inspect_configuration_sources(root=source_root)
    request = _request()
    request_sha256 = plan._sha256(plan._canonical(request))
    decision = _decision_verification(request)
    monkeypatch.setattr(
        plan,
        "_current_inputs",
        lambda **_kwargs: (
            request,
            request_sha256,
            _request_verification(),
            decision,
            sources,
        ),
    )
    plan_path = tmp_path / "private" / "plan.json"

    staged = plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW,
    )
    original = plan_path.read_bytes()
    original_mtime_ns = plan_path.stat().st_mtime_ns
    reused = plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW + timedelta(seconds=1),
    )

    assert staged["status"] == "ready"
    assert staged["plan_state"] == "refreshed"
    assert staged["plan_refresh_reason"] == "missing"
    assert staged["plan_status"] == "review_ready"
    assert staged["request_id"] == request["request_id"]
    assert staged["request_sha256"] == request_sha256
    assert staged["authorization_decision"] == "pending"
    assert staged["authorization_recorded"] is False
    assert staged["manual_apply_authorized"] is False
    assert staged["automatic_apply_allowed"] is False
    assert staged["apply_performed"] is False
    assert staged["execution_authorized"] is False
    assert staged["deployment_or_restart_authorized"] is False
    assert staged["provider_quota_consumption_allowed"] is False
    assert staged["delivery_authorized"] is False
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert reused["status"] == "ready"
    assert reused["plan_state"] == "reused"
    assert reused["plan_state_updated"] is False
    assert plan_path.read_bytes() == original
    assert plan_path.stat().st_mtime_ns == original_mtime_ns


@pytest.mark.parametrize(
    ("decision_value", "expected_status", "manual_apply_authorized"),
    [
        ("approve_exact_scope", "exact_scope_authorized", True),
        ("reject", "rejected", False),
        ("defer", "deferred", False),
    ],
)
def test_configuration_handoff_refreshes_for_explicit_decision_without_applying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision_value: str,
    expected_status: str,
    manual_apply_authorized: bool,
) -> None:
    source_root = _source_root(tmp_path / "source")
    sources = plan.inspect_configuration_sources(root=source_root)
    request = _request()
    request_sha256 = plan._sha256(plan._canonical(request))
    current = {"decision": _decision_verification(request)}
    monkeypatch.setattr(
        plan,
        "_current_inputs",
        lambda **_kwargs: (
            request,
            request_sha256,
            _request_verification(),
            current["decision"],
            sources,
        ),
    )
    plan_path = tmp_path / "private" / "plan.json"
    initial = plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW,
    )
    current["decision"] = _decision_verification(
        request,
        value=decision_value,
    )

    refreshed = plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW + timedelta(seconds=1),
    )

    assert refreshed["status"] == "ready"
    assert refreshed["plan_state"] == "refreshed"
    assert refreshed["plan_refresh_reason"] == "superseded"
    assert refreshed["previous_plan_sha256"] == initial["plan_sha256"]
    assert refreshed["plan_status"] == expected_status
    assert refreshed["authorization_decision"] == decision_value
    assert refreshed["authorization_recorded"] is True
    assert refreshed["manual_apply_authorized"] is manual_apply_authorized
    assert refreshed["automatic_apply_allowed"] is False
    assert refreshed["apply_performed"] is False
    assert refreshed["execution_authorized"] is False
    assert refreshed["deployment_or_restart_authorized"] is False
    assert refreshed["provider_quota_consumption_allowed"] is False
    assert refreshed["delivery_authorized"] is False


def test_configuration_handoff_refreshes_after_request_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = _source_root(tmp_path / "source")
    sources = plan.inspect_configuration_sources(root=source_root)
    current = {"request": _request()}

    def current_inputs(**_kwargs: object) -> tuple[object, ...]:
        request = current["request"]
        return (
            request,
            plan._sha256(plan._canonical(request)),
            _request_verification(),
            _decision_verification(request),
            sources,
        )

    monkeypatch.setattr(plan, "_current_inputs", current_inputs)
    plan_path = tmp_path / "private" / "plan.json"
    initial = plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW,
    )
    rotated = dict(current["request"])
    rotated["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    rotated["expires_at"] = (NOW + timedelta(minutes=15, seconds=1)).isoformat()
    current["request"] = rotated

    refreshed = plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW + timedelta(seconds=1),
    )

    assert refreshed["status"] == "ready"
    assert refreshed["plan_state"] == "refreshed"
    assert refreshed["plan_refresh_reason"] == "superseded"
    assert refreshed["previous_plan_sha256"] == initial["plan_sha256"]
    assert refreshed["plan_sha256"] != initial["plan_sha256"]
    assert refreshed["authorization_recorded"] is False
    assert refreshed["manual_apply_authorized"] is False
    assert refreshed["apply_performed"] is False


def test_configuration_handoff_refuses_to_overwrite_unsafe_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = _source_root(tmp_path / "source")
    sources = plan.inspect_configuration_sources(root=source_root)
    request = _request()
    request_sha256 = plan._sha256(plan._canonical(request))
    decision = _decision_verification(request)
    monkeypatch.setattr(
        plan,
        "_current_inputs",
        lambda **_kwargs: (
            request,
            request_sha256,
            _request_verification(),
            decision,
            sources,
        ),
    )
    plan_path = tmp_path / "private" / "plan.json"
    plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW,
    )
    unsafe = json.loads(plan_path.read_text())
    unsafe["apply_performed"] = True
    normalized = dict(unsafe)
    normalized.pop("integrity")
    unsafe["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": plan._sha256(plan._canonical(normalized)),
    }
    plan_path.write_bytes(plan._canonical(unsafe))
    original = plan_path.read_bytes()

    handoff = plan.stage_current_configuration_handoff(
        plan_path=plan_path,
        root=source_root,
        now=NOW + timedelta(seconds=1),
    )

    assert handoff["status"] == "blocked"
    assert (
        handoff["blocking_reason"]
        == "configuration_handoff_existing_plan_not_admissible"
    )
    assert handoff["plan_state_updated"] is False
    assert handoff["manual_apply_authorized"] is False
    assert handoff["apply_performed"] is False
    assert handoff["execution_authorized"] is False
    assert plan_path.read_bytes() == original


def test_cli_failure_receipt_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    verification_path = tmp_path / "verification.json"
    monkeypatch.setattr(
        plan,
        "materialize_current_configuration_plan",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("must-not-be-recorded")),
    )

    status = plan.main(
        [
            "--plan",
            str(tmp_path / "plan.json"),
            "--verification-write",
            str(verification_path),
        ]
    )
    receipt = json.loads(verification_path.read_text())

    assert status == 1
    assert receipt["blocking_reason"] == "configuration_plan_materialization_failed"
    assert receipt["manual_apply_authorized"] is False
    assert receipt["apply_performed"] is False
    assert receipt["execution_authorized"] is False
    assert receipt["verification_receipt_persisted"] is True
    assert "must-not-be-recorded" not in json.dumps(receipt)
    capsys.readouterr()
