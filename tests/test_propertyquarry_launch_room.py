from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import propertyquarry_launch_room as launch_room
from scripts.propertyquarry_launch_room import (
    _deployment_status,
    _public_launch_authority_handoff,
    _public_launch_status,
    build_launch_room,
    render_markdown,
)


ROOT = Path(__file__).resolve().parents[1]


def test_launch_room_reports_one_canonical_authority_and_no_false_launch(
    tmp_path: Path,
) -> None:
    report = build_launch_room(
        ROOT,
        deployment_receipt_path=tmp_path / "missing.json",
    )

    assert report["canonical_repository"] == "ArchonMegalon/propertyquarry"
    assert report["canonical_release_authority"] is True
    assert report["legacy_repository"] == "ArchonMegalon/property"
    assert report["legacy_release_authority"] is False
    assert report["repositories_are_exact_mirrors"] is False
    assert report["production_launch_ready"] is False
    assert report["public_launch"]["status"] == (
        "blocked_external_authority_receipt_missing"
    )
    assert report["public_launch"]["blockers"] == [
        "external_public_launch_authority_receipt_missing",
        "google_play_public_launch_authority_unverified",
        "paid_billing_safe_handoff_authority_unverified",
        "encrypted_off_host_disaster_recovery_authority_unverified",
    ]
    assert report["release_proof_plane"] == {
        "status": "local_docker_operator_receipts",
        "github_actions_used": False,
    }
    assert report["live_deployment"]["status"] == (
        "missing_local_docker_deployment_receipt"
    )
    assert report["live_deployment"]["local_runtime_ready"] is False
    handoff = report["public_launch"]["authority_handoff"]
    assert handoff["status"] == "blocked_local_runtime_precondition"
    assert handoff["ready_for_external_issuance"] is False
    assert handoff["exact_candidate"]["bindings_complete"] is False
    assert set(handoff["required_evidence"]) == {
        "google_play_public_launch",
        "paid_billing_safe_handoff",
        "encrypted_off_host_disaster_recovery",
    }


def test_launch_room_keeps_source_journey_and_browser_counts_distinct(
    tmp_path: Path,
) -> None:
    report = build_launch_room(
        ROOT,
        deployment_receipt_path=tmp_path / "missing.json",
    )
    proof = report["candidate_proof"]

    assert proof["candidate_bound"] is True
    assert proof["source"] == {"passed": True, "selected": 7, "required": 7}
    assert proof["journeys"] == {"passed": True, "selected": 8, "required": 8}
    assert proof["browser"] == {"passed": True, "selected": 16, "required": 16}


def test_launch_room_marks_core_candidate_only_and_advanced_unavailable(
    tmp_path: Path,
) -> None:
    report = build_launch_room(
        ROOT,
        deployment_receipt_path=tmp_path / "missing.json",
    )

    assert report["core_gold"] == {
        "status": "candidate_eligible_local_deployment_missing",
        "local_runtime_claim": False,
        "production_claim": False,
    }
    assert report["advanced_visual_gold"] == {
        "status": "unavailable_unbound_producer_receipts",
        "local_runtime_claim": False,
        "production_claim": False,
    }


def test_healthy_local_runtime_never_substitutes_for_public_launch_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    envelope_sha = "b" * 40
    image_digest = "sha256:" + ("c" * 64)

    def fake_git(_root: Path, *args: str) -> str:
        if args and args[0] == "status":
            return ""
        if args and args[-1] == "HEAD^{commit}":
            return envelope_sha
        return ""

    monkeypatch.setattr(launch_room, "_git", fake_git)
    monkeypatch.setattr(
        launch_room,
        "_deployment_status",
        lambda *_args, **_kwargs: {
            "status": "healthy_exact_candidate_local_docker",
            "local_runtime_ready": True,
            "release_image_digest": image_digest,
        },
    )

    report = build_launch_room(
        ROOT,
        deployment_receipt_path=tmp_path / "deployment.json",
        public_launch_authority_receipt_path=tmp_path / "missing-public.json",
    )

    assert report["local_runtime_ready"] is True
    assert report["production_launch_ready"] is False
    assert report["core_gold"]["local_runtime_claim"] is True
    assert report["core_gold"]["production_claim"] is False
    assert report["public_launch"]["status"] == (
        "blocked_external_authority_receipt_missing"
    )
    handoff = report["public_launch"]["authority_handoff"]
    assert handoff["status"] == "ready_for_external_authority"
    assert handoff["ready_for_external_issuance"] is True
    assert handoff["exact_candidate"] == {
        "bindings_complete": True,
        "binding_values_well_formed": True,
        "deployment_proven": True,
        "envelope_head_sha": envelope_sha,
        "runtime_commit_sha": report["runtime_candidate"],
        "release_image_digest": image_digest,
    }


def test_public_launch_authority_handoff_is_exact_and_non_substitutable() -> None:
    handoff = _public_launch_authority_handoff(
        envelope_head_sha="b" * 40,
        runtime_sha="a" * 40,
        image_digest="sha256:" + ("c" * 64),
        local_runtime_ready=True,
        authority_passed=False,
    )

    assert handoff["status"] == "ready_for_external_authority"
    assert handoff["ready_for_external_issuance"] is True
    assert handoff["authority_scope"] == "external_global_governance"
    assert handoff["local_substitution_allowed"] is False
    assert handoff["receipt_contract"] == (
        "propertyquarry.public_launch_authority.v2"
    )
    assert handoff["receipt_path"] == (
        "/run/propertyquarry/release-control/"
        "propertyquarry-public-launch-authority.v2.json"
    )
    assert handoff["trust_store"] == {
        "environment_variable": "PROPERTYQUARRY_GLOBAL_GOVERNANCE_TRUST_STORE_FILE",
        "path": (
            "/etc/propertyquarry/release-control/"
            "global-governance-trust-store.v1.json"
        ),
        "caller_selected_path_allowed": False,
    }
    assert handoff["receipt_constraints"] == {
        "canonical_repository": "ArchonMegalon/propertyquarry",
        "passed_required": True,
        "secret_values_recorded_required": False,
        "canonical_json_required": True,
        "external_signature_required": True,
        "fixed_receipt_path_required": True,
        "fixed_trust_store_required": True,
        "candidate_binding_required": True,
    }
    assert all(
        row["authority_state"] == "unverified"
        for row in handoff["required_evidence"].values()
    )

    stale = _public_launch_authority_handoff(
        envelope_head_sha="b" * 40,
        runtime_sha="a" * 40,
        image_digest="sha256:" + ("c" * 64),
        local_runtime_ready=False,
        authority_passed=False,
    )
    assert stale["status"] == "blocked_local_runtime_precondition"
    assert stale["ready_for_external_issuance"] is False
    assert stale["exact_candidate"]["binding_values_well_formed"] is True
    assert stale["exact_candidate"]["deployment_proven"] is False
    assert stale["exact_candidate"]["bindings_complete"] is False


def test_public_launch_handoff_observes_material_without_minting_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "public-launch-authority.v2.json"
    trust_store = tmp_path / "global-governance-trust-store.v1.json"
    receipt.write_text("{}", encoding="utf-8")
    trust_store.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(launch_room, "PUBLIC_LAUNCH_RECEIPT_PATH", receipt)
    monkeypatch.setattr(
        launch_room,
        "PUBLIC_LAUNCH_TRUST_STORE_PATH",
        trust_store,
    )
    monkeypatch.setenv(launch_room.TRUST_STORE_ENV, str(trust_store))

    handoff = _public_launch_authority_handoff(
        envelope_head_sha="b" * 40,
        runtime_sha="a" * 40,
        image_digest="sha256:" + ("c" * 64),
        local_runtime_ready=True,
        authority_passed=False,
    )

    assert handoff["verification_material"] == {
        "receipt_installed": True,
        "trust_store_installed": True,
        "trust_store_environment_configured": True,
        "present": True,
    }
    assert handoff["status"] == "ready_for_external_authority"
    assert handoff["local_substitution_allowed"] is False


def test_launch_room_markdown_has_current_truth_and_next_action(
    tmp_path: Path,
) -> None:
    rendered = render_markdown(
        build_launch_room(
            ROOT,
            deployment_receipt_path=tmp_path / "missing.json",
        )
    )

    assert "# PropertyQuarry launch room" in rendered
    assert "| Canonical repo | `ArchonMegalon/propertyquarry` |" in rendered
    assert "7/7 source; 8/8 journeys; 16/16 browser" in rendered
    assert "| Release proof plane | `local_docker_operator_receipts` |" in rendered
    assert "| Local runtime | `BLOCKED` |" in rendered
    assert "| Public launch | `BLOCKED` |" in rendered
    assert "## Next action" in rendered


def test_exact_local_docker_receipt_is_authoritative(tmp_path: Path) -> None:
    runtime_sha = "a" * 40
    envelope_sha = "b" * 40
    receipt = tmp_path / "deployment.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "propertyquarry.local_docker_deployment.v1",
                "passed": True,
                "secret_values_recorded": False,
                "runtime_commit_sha": runtime_sha,
                "envelope_head_sha": envelope_sha,
                "observed_at": "2026-07-28T12:00:00Z",
                "authority": {
                    "scope": "local_docker",
                    "proof_plane": "local_docker_operator_receipts",
                    "github_actions_used": False,
                    "canonical_repository": "ArchonMegalon/propertyquarry",
                },
                "compose": {"project": "property"},
                "services": {
                    name: {}
                    for name in (
                        "propertyquarry-api",
                        "propertyquarry-migrate",
                        "propertyquarry-worker",
                        "propertyquarry-scheduler",
                        "propertyquarry-render-tools",
                        "propertyquarry-db",
                        "propertyquarry-cloudflared",
                    )
                },
                "local_probe": {"status": "pass"},
            }
        ),
        encoding="utf-8",
    )

    status = _deployment_status(
        receipt,
        runtime_sha,
        envelope_head_sha=envelope_sha,
    )

    assert status["status"] == "healthy_exact_candidate_local_docker"
    assert status["local_runtime_ready"] is True
    assert status["envelope_head_bound"] is True
    assert status["service_count"] == 7

    stale = _deployment_status(
        receipt,
        runtime_sha,
        envelope_head_sha="c" * 40,
    )

    assert stale["local_runtime_ready"] is False
    assert stale["envelope_head_bound"] is False


def test_unsigned_public_launch_receipt_cannot_mint_external_authority(
    tmp_path: Path,
) -> None:
    envelope_sha = "b" * 40
    runtime_sha = "a" * 40
    image_digest = "sha256:" + ("c" * 64)
    receipt = tmp_path / "public-launch.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "propertyquarry.public_launch_authority.v1",
                "passed": True,
                "secret_values_recorded": False,
                "canonical_repository": "ArchonMegalon/propertyquarry",
                "envelope_head_sha": envelope_sha,
                "runtime_commit_sha": runtime_sha,
                "release_image_digest": image_digest,
                "authority": {
                    "scope": "public_launch",
                    "proof_plane": "external_authority_receipts",
                },
                "requirements": {
                    "google_play_public_launch": {
                        "status": "pass",
                        "evidence_ref": "play-console:production-access",
                    },
                    "paid_billing_safe_handoff": {
                        "status": "pass",
                        "evidence_ref": "billing:no-second-login-canary",
                    },
                    "encrypted_off_host_disaster_recovery": {
                        "status": "pass",
                        "evidence_ref": "dr:off-host-restore-drill",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    status = _public_launch_status(
        receipt,
        envelope_head_sha=envelope_sha,
        runtime_sha=runtime_sha,
        image_digest=image_digest,
    )

    assert status["status"] == "blocked_external_authority_receipt_invalid"
    assert status["authority_passed"] is False
    assert status["verifier_error"] == (
        "public_launch_authority_receipt_path_untrusted"
    )
    assert status["blockers"] == [
        "external_public_launch_authority:"
        "public_launch_authority_receipt_path_untrusted",
        "google_play_public_launch_authority_unverified",
        "paid_billing_safe_handoff_authority_unverified",
        "encrypted_off_host_disaster_recovery_authority_unverified",
    ]


def test_verified_public_launch_receipt_is_projected_without_local_substitution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    receipt = tmp_path / "public-launch.json"
    receipt.write_text("{}", encoding="utf-8")
    calls: list[dict[str, object]] = []

    class Verified:
        def as_dict(self) -> dict[str, object]:
            return {
                "receipt_contract": (
                    "propertyquarry.public_launch_authority.v2"
                ),
                "receipt_id": "1" * 64,
            }

    def verify(path: Path, **bindings: object) -> Verified:
        calls.append({"path": path, **bindings})
        return Verified()

    monkeypatch.setattr(launch_room, "verify_public_launch_authority", verify)

    status = _public_launch_status(
        receipt,
        envelope_head_sha="b" * 40,
        runtime_sha="a" * 40,
        image_digest="sha256:" + ("c" * 64),
    )

    assert status["status"] == "verified_external_public_launch_authority"
    assert status["authority_passed"] is True
    assert status["blockers"] == []
    assert status["verification"]["receipt_id"] == "1" * 64
    assert calls == [
        {
            "path": receipt,
            "expected_envelope_head_sha": "b" * 40,
            "expected_runtime_commit_sha": "a" * 40,
            "expected_release_image_digest": "sha256:" + ("c" * 64),
        }
    ]


def test_launch_room_cli_writes_json(tmp_path: Path) -> None:
    output = tmp_path / "launch-room.json"
    missing = tmp_path / "missing-deployment.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "propertyquarry_launch_room.py"),
            "--format",
            "json",
            "--deployment-receipt",
            str(missing),
            "--write",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "propertyquarry.launch_room.v3"
    assert payload["production_launch_ready"] is False
    assert payload["release_proof_plane"]["github_actions_used"] is False


def test_launch_room_cli_can_require_production_readiness(tmp_path: Path) -> None:
    output = tmp_path / "launch-room.json"
    missing = tmp_path / "missing-deployment.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "propertyquarry_launch_room.py"),
            "--format",
            "json",
            "--deployment-receipt",
            str(missing),
            "--write",
            str(output),
            "--require-production-ready",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "launch-room production readiness is blocked\n"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["production_launch_ready"] is False


def test_launch_room_cli_can_require_local_runtime_readiness(tmp_path: Path) -> None:
    output = tmp_path / "launch-room.json"
    missing = tmp_path / "missing-deployment.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "propertyquarry_launch_room.py"),
            "--format",
            "json",
            "--deployment-receipt",
            str(missing),
            "--write",
            str(output),
            "--require-local-runtime-ready",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "launch-room local runtime readiness is blocked\n"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["local_runtime_ready"] is False
