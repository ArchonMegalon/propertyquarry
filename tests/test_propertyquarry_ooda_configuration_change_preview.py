from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_configuration_change_preview as preview
from scripts import propertyquarry_ooda_configuration_plan as plan


NOW = datetime(2026, 8, 26, 14, 30, tzinfo=timezone.utc)


def _source_root(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    (tmp_path / plan.COMPOSE_PATH).write_text(
        "services:\n"
        "  propertyquarry-api:\n"
        "    ports:\n"
        f'      - "{plan.CURRENT_PORT_EXPRESSION}"\n',
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


def _request(*, generated_at: datetime = NOW) -> dict[str, object]:
    return {
        "schema": "propertyquarry.ooda_runtime_authorization_request.v1",
        "request_id": "pqar_0123456789abcdef01234567",
        "generated_at": generated_at.isoformat(),
        "expires_at": (generated_at + timedelta(minutes=15)).isoformat(),
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


def _decision(
    request: dict[str, object],
    *,
    value: str,
    recorded_at: datetime = NOW,
) -> dict[str, object]:
    request_sha256 = plan._sha256(plan._canonical(request))
    verified = value != "pending"
    exact = value == "approve_exact_scope"
    return {
        "status": "verified" if verified else "pending",
        "decision": value if verified else "",
        "decision_id": "pqad_0123456789abcdef01234567" if verified else "",
        "decision_sha256": "c" * 64 if verified else "",
        "recorded_at": recorded_at.isoformat() if verified else "",
        "expires_at": request["expires_at"],
        "scope": dict(request["scope"]),
        "progress": {
            "current_evidence_verified": True,
            "authorization_decision_recorded": verified,
        },
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "authorization_required": True,
        "authorization_recorded": verified,
        "exact_scope_authorized": exact,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "execution_performed": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _inputs(
    root: Path,
    *,
    decision_value: str,
    request: dict[str, object] | None = None,
) -> tuple[object, ...]:
    current_request = request or _request()
    request_sha256 = plan._sha256(plan._canonical(current_request))
    decision = _decision(current_request, value=decision_value)
    sources = plan.inspect_configuration_sources(root=root)
    artifact = plan.build_configuration_plan(
        request=current_request,
        request_sha256=request_sha256,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    verification = plan.verify_configuration_plan(
        artifact,
        request=current_request,
        request_verification=_request_verification(),
        decision_verification=decision,
        source_posture=sources,
        now=NOW,
    )
    assert verification["status"] == "verified"
    verification["progress"]["current_evidence_verified"] = True
    source_text, source_raw, source_sha256 = plan._source_snapshot(
        plan.COMPOSE_PATH,
        root=root,
    )
    return (
        current_request,
        request_sha256,
        artifact,
        verification,
        decision,
        sources,
        source_text,
        source_raw,
        source_sha256,
    )


@pytest.mark.parametrize("decision_value", ["pending", "reject", "defer"])
def test_non_approval_never_stages_preview_or_manual_authority(
    decision_value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    current = _inputs(root, decision_value=decision_value)
    monkeypatch.setattr(preview, "_current_inputs", lambda **_kwargs: current)
    preview_dir = tmp_path / "previews"

    result = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW,
    )

    assert result["status"] == "not_authorized"
    assert result["authorization_decision"] == decision_value
    assert result["exact_scope_authorized"] is False
    assert result["manual_apply_authorized"] is False
    assert result["automatic_apply_allowed"] is False
    assert result["source_edit_performed"] is False
    assert result["execution_authorized"] is False
    assert result["deployment_or_restart_authorized"] is False
    assert result["provider_quota_consumption_allowed"] is False
    assert result["delivery_authorized"] is False
    assert not preview_dir.exists()


def test_approved_preview_is_exact_private_non_applying_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    current = _inputs(root, decision_value="approve_exact_scope")
    monkeypatch.setattr(preview, "_current_inputs", lambda **_kwargs: current)
    preview_dir = tmp_path / "previews"
    source_before = (root / plan.COMPOSE_PATH).read_bytes()

    staged = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW,
    )
    preview_path = Path(staged["preview_path"])
    original = preview_path.read_bytes()
    original_mtime_ns = preview_path.stat().st_mtime_ns
    reused = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW + timedelta(seconds=1),
    )

    assert staged["status"] == "ready"
    assert staged["preview_state"] == "refreshed"
    assert staged["preview_state_updated"] is True
    assert staged["current_evidence_verified"] is True
    assert staged["preview_verified"] is True
    assert staged["manual_apply_authorized"] is True
    assert staged["automatic_apply_allowed"] is False
    assert staged["source_edit_performed"] is False
    assert staged["execution_authorized"] is False
    assert staged["deployment_or_restart_authorized"] is False
    assert staged["provider_quota_consumption_allowed"] is False
    assert staged["delivery_authorized"] is False
    assert stat.S_IMODE(preview_path.stat().st_mode) == 0o600
    assert preview_path.name == f"{staged['preview_id']}.json"
    assert staged["preview"]["before_sha256"] == current[-1]
    assert staged["preview"]["after_sha256"] != current[-1]
    assert plan.CURRENT_PORT_EXPRESSION in staged["preview"]["forward_unified_diff"]
    assert plan.PROPOSED_PORT_EXPRESSION in staged["preview"]["forward_unified_diff"]
    assert plan.PROPOSED_PORT_EXPRESSION in staged["preview"]["rollback_unified_diff"]
    assert plan.CURRENT_PORT_EXPRESSION in staged["preview"]["rollback_unified_diff"]
    assert reused["status"] == "ready"
    assert reused["preview_state"] == "reused"
    assert reused["preview_state_updated"] is False
    assert preview_path.read_bytes() == original
    assert preview_path.stat().st_mtime_ns == original_mtime_ns
    assert (root / plan.COMPOSE_PATH).read_bytes() == source_before


def test_tampered_existing_preview_is_blocked_and_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    current = _inputs(root, decision_value="approve_exact_scope")
    monkeypatch.setattr(preview, "_current_inputs", lambda **_kwargs: current)
    preview_dir = tmp_path / "previews"
    staged = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW,
    )
    preview_path = Path(staged["preview_path"])
    payload = json.loads(preview_path.read_text())
    payload["preview"]["after_sha256"] = "f" * 64
    payload.pop("integrity")
    payload["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": preview._sha256(preview._canonical(payload)),
    }
    preview_path.write_bytes(preview._canonical(payload))
    preview_path.chmod(0o600)
    tampered = preview_path.read_bytes()

    blocked = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW + timedelta(seconds=1),
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "configuration_change_preview_persisted_not_verified"
    assert blocked["manual_apply_authorized"] is False
    assert blocked["source_edit_performed"] is False
    assert preview_path.read_bytes() == tampered


def test_source_drift_fails_closed_without_touching_preview_or_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    current = list(_inputs(root, decision_value="approve_exact_scope"))
    monkeypatch.setattr(preview, "_current_inputs", lambda **_kwargs: tuple(current))
    preview_dir = tmp_path / "previews"
    staged = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW,
    )
    preview_path = Path(staged["preview_path"])
    original_preview = preview_path.read_bytes()
    original_source = (root / plan.COMPOSE_PATH).read_bytes()
    drifted_text = str(current[6]) + "# unrelated drift\n"
    drifted_raw = drifted_text.encode("utf-8")
    current[6] = drifted_text
    current[7] = drifted_raw
    current[8] = preview._sha256(drifted_raw)

    blocked = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW + timedelta(seconds=1),
    )

    assert blocked["status"] == "blocked"
    assert blocked["blocking_reason"] == "configuration_change_preview_build_failed"
    assert blocked["manual_apply_authorized"] is False
    assert preview_path.read_bytes() == original_preview
    assert (root / plan.COMPOSE_PATH).read_bytes() == original_source


def test_refreshed_exact_approval_uses_a_distinct_immutable_preview_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_root(tmp_path / "source")
    current = {"inputs": _inputs(root, decision_value="approve_exact_scope")}
    monkeypatch.setattr(
        preview,
        "_current_inputs",
        lambda **_kwargs: current["inputs"],
    )
    preview_dir = tmp_path / "previews"
    first = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW,
    )
    first_path = Path(first["preview_path"])
    first_bytes = first_path.read_bytes()
    refreshed_request = _request(generated_at=NOW + timedelta(minutes=1))
    current["inputs"] = _inputs(
        root,
        decision_value="approve_exact_scope",
        request=refreshed_request,
    )

    second = preview.stage_current_configuration_change_preview(
        preview_dir=preview_dir,
        root=root,
        now=NOW + timedelta(minutes=1),
    )

    assert second["status"] == "ready"
    assert second["preview_state"] == "refreshed"
    assert second["preview_path"] != first["preview_path"]
    assert Path(second["preview_path"]).exists()
    assert first_path.read_bytes() == first_bytes


def test_preview_verifier_rejects_expiry_and_grants_no_stale_authority(
    tmp_path: Path,
) -> None:
    root = _source_root(tmp_path / "source")
    current = _inputs(root, decision_value="approve_exact_scope")
    artifact = preview.build_configuration_change_preview(
        request=current[0],
        request_sha256=current[1],
        plan=current[2],
        plan_verification=current[3],
        decision_verification=current[4],
        source_posture=current[5],
        source_text=current[6],
        source_raw=current[7],
        source_sha256=current[8],
        now=NOW,
    )

    expired = preview.verify_configuration_change_preview(
        artifact,
        request=current[0],
        request_sha256=current[1],
        plan=current[2],
        plan_verification=current[3],
        decision_verification=current[4],
        source_posture=current[5],
        source_text=current[6],
        source_raw=current[7],
        source_sha256=current[8],
        now=NOW + timedelta(minutes=16),
    )

    assert expired["status"] == "blocked"
    assert expired["blocking_reason"] == "configuration_change_preview_not_fresh"
    assert expired["manual_apply_authorized"] is False
    assert expired["source_edit_performed"] is False
    assert expired["deployment_or_restart_authorized"] is False
