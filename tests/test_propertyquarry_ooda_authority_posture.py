from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

from scripts import propertyquarry_ooda_authority_posture as authority


NOW = datetime(2026, 8, 26, 17, 0, tzinfo=timezone.utc)


def _component(
    schema: str,
    *,
    status: str,
    current: bool,
    exact_scope_authorized: bool = False,
) -> dict[str, object]:
    return {
        "schema": schema,
        "status": status,
        "updated_at": NOW.isoformat(),
        "blocking_reason": "" if status == "verified" else "not_current",
        "progress": {"current_evidence_verified": current},
        "authorization_recorded": exact_scope_authorized,
        "exact_scope_authorized": exact_scope_authorized,
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "posture_path": tmp_path / "posture.json",
        "review_verification_path": tmp_path / "review.json",
        "request_verification_path": tmp_path / "request.json",
        "decision_verification_path": tmp_path / "decision.json",
        "plan_verification_path": tmp_path / "plan.json",
    }


def test_authority_posture_refreshes_inactive_chain_as_current_blocked_receipts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        authority.review,
        "verify_current_review_packet",
        lambda **_kwargs: _component(
            authority.review.VERIFY_SCHEMA,
            status="blocked",
            current=False,
        ),
    )
    monkeypatch.setattr(
        authority.request,
        "verify_current_authorization_request",
        lambda **_kwargs: _component(
            authority.request.VERIFY_SCHEMA,
            status="blocked",
            current=False,
        ),
    )
    monkeypatch.setattr(
        authority.decision,
        "verify_current_authorization_decision",
        lambda **_kwargs: _component(
            authority.decision.VERIFY_SCHEMA,
            status="blocked",
            current=False,
        ),
    )
    monkeypatch.setattr(
        authority.configuration,
        "verify_current_configuration_plan",
        lambda **_kwargs: _component(
            authority.configuration.VERIFY_SCHEMA,
            status="blocked",
            current=False,
        ),
    )

    result = authority.refresh_current_authority_posture(
        **_paths(tmp_path),
        now=NOW,
    )

    assert result["status"] == "inactive"
    assert result["blocking_reason"] == "current_authority_chain_unavailable"
    assert result["exact_scope_authorized"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert result["progress"] == {
        "verification_receipt_count": 4,
        "verification_receipt_persisted_count": 4,
        "request_current": False,
        "decision_current": False,
        "plan_current": False,
        "current_evidence_verified": False,
    }
    for path in _paths(tmp_path).values():
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text(encoding="utf-8"))


def test_authority_posture_never_converts_exact_scope_into_execution_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        authority.review,
        "verify_current_review_packet",
        lambda **_kwargs: _component(
            authority.review.VERIFY_SCHEMA,
            status="verified",
            current=True,
        ),
    )
    monkeypatch.setattr(
        authority.request,
        "verify_current_authorization_request",
        lambda **_kwargs: _component(
            authority.request.VERIFY_SCHEMA,
            status="verified",
            current=True,
        ),
    )
    monkeypatch.setattr(
        authority.decision,
        "verify_current_authorization_decision",
        lambda **_kwargs: _component(
            authority.decision.VERIFY_SCHEMA,
            status="verified",
            current=True,
            exact_scope_authorized=True,
        ),
    )
    monkeypatch.setattr(
        authority.configuration,
        "verify_current_configuration_plan",
        lambda **_kwargs: _component(
            authority.configuration.VERIFY_SCHEMA,
            status="verified",
            current=True,
            exact_scope_authorized=True,
        ),
    )

    result = authority.refresh_current_authority_posture(
        **_paths(tmp_path),
        now=NOW,
    )

    assert result["status"] == "authorized_exact_scope"
    assert result["authorization_recorded"] is True
    assert result["exact_scope_authorized"] is True
    assert result["automatic_execution_allowed"] is False
    assert result["execution_authorized"] is False
    assert result["execution_performed"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_authority_posture_fails_closed_when_current_receipts_cannot_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        authority.review,
        "verify_current_review_packet",
        lambda **_kwargs: _component(
            authority.review.VERIFY_SCHEMA,
            status="verified",
            current=True,
        ),
    )
    monkeypatch.setattr(
        authority.request,
        "verify_current_authorization_request",
        lambda **_kwargs: _component(
            authority.request.VERIFY_SCHEMA,
            status="verified",
            current=True,
        ),
    )
    monkeypatch.setattr(
        authority.decision,
        "verify_current_authorization_decision",
        lambda **_kwargs: _component(
            authority.decision.VERIFY_SCHEMA,
            status="verified",
            current=True,
            exact_scope_authorized=True,
        ),
    )
    monkeypatch.setattr(
        authority.configuration,
        "verify_current_configuration_plan",
        lambda **_kwargs: _component(
            authority.configuration.VERIFY_SCHEMA,
            status="verified",
            current=True,
            exact_scope_authorized=True,
        ),
    )
    monkeypatch.setattr(
        authority,
        "atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("read-only authority receipt directory")
        ),
    )

    result = authority.refresh_current_authority_posture(
        **_paths(tmp_path),
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == "authority_posture_receipt_persistence_failed"
    assert result["posture_receipt_persisted"] is False
    assert result["progress"]["verification_receipt_persisted_count"] == 0
    assert result["authorization_recorded"] is False
    assert result["exact_scope_authorized"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False


def test_authority_posture_does_not_report_recorded_authority_after_partial_receipt_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        authority.review,
        "verify_current_review_packet",
        lambda **_kwargs: _component(
            authority.review.VERIFY_SCHEMA,
            status="verified",
            current=True,
        ),
    )
    monkeypatch.setattr(
        authority.request,
        "verify_current_authorization_request",
        lambda **_kwargs: _component(
            authority.request.VERIFY_SCHEMA,
            status="verified",
            current=True,
        ),
    )
    monkeypatch.setattr(
        authority.decision,
        "verify_current_authorization_decision",
        lambda **_kwargs: _component(
            authority.decision.VERIFY_SCHEMA,
            status="verified",
            current=True,
            exact_scope_authorized=True,
        ),
    )
    monkeypatch.setattr(
        authority.configuration,
        "verify_current_configuration_plan",
        lambda **_kwargs: _component(
            authority.configuration.VERIFY_SCHEMA,
            status="verified",
            current=True,
            exact_scope_authorized=True,
        ),
    )
    real_atomic_write_bytes = authority.atomic_write_bytes

    def fail_review_receipt(path: Path, payload: bytes, **kwargs) -> None:
        if Path(path).name == "review.json":
            raise OSError("review receipt storage unavailable")
        real_atomic_write_bytes(path, payload, **kwargs)

    monkeypatch.setattr(authority, "atomic_write_bytes", fail_review_receipt)

    result = authority.refresh_current_authority_posture(
        **_paths(tmp_path),
        now=NOW,
    )

    assert result["status"] == "blocked"
    assert result["blocking_reason"] == (
        "authority_verification_receipt_persistence_incomplete"
    )
    assert result["posture_receipt_persisted"] is True
    assert result["progress"]["verification_receipt_persisted_count"] == 3
    assert result["authorization_recorded"] is False
    assert result["exact_scope_authorized"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["protected_operation_executed"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
