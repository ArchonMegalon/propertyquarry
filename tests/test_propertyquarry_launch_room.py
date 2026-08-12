from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import propertyquarry_launch_room as launch_room
from scripts.propertyquarry_launch_room import (
    _deployment_status,
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
        "blocked_external_authority_verifier_unconfigured"
    )
    assert report["public_launch"]["blockers"] == [
        "external_public_launch_authority_verifier_unconfigured",
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
        "blocked_external_authority_verifier_unconfigured"
    )


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

    assert status["status"] == (
        "blocked_external_authority_verifier_unconfigured"
    )
    assert status["authority_passed"] is False
    assert status["receipt_present_unverified"] is True
    assert status["blockers"] == [
        "external_public_launch_authority_verifier_unconfigured",
        "google_play_public_launch_authority_unverified",
        "paid_billing_safe_handoff_authority_unverified",
        "encrypted_off_host_disaster_recovery_authority_unverified",
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
