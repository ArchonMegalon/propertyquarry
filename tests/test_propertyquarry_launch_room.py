from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.propertyquarry_launch_room import (
    _deployment_status,
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
    assert report["release_proof_plane"] == {
        "status": "local_docker_operator_receipts",
        "github_actions_used": False,
    }
    assert report["live_deployment"]["status"] == (
        "missing_local_docker_deployment_receipt"
    )
    assert report["live_deployment"]["production_launch"] is False


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
        "production_claim": False,
    }
    assert report["advanced_visual_gold"] == {
        "status": "unavailable_unbound_producer_receipts",
        "production_claim": False,
    }


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
    assert "| Production launch | `BLOCKED` |" in rendered
    assert "## Next action" in rendered


def test_exact_local_docker_receipt_is_authoritative(tmp_path: Path) -> None:
    runtime_sha = "a" * 40
    receipt = tmp_path / "deployment.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "propertyquarry.local_docker_deployment.v1",
                "passed": True,
                "secret_values_recorded": False,
                "runtime_commit_sha": runtime_sha,
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

    status = _deployment_status(receipt, runtime_sha)

    assert status["status"] == "healthy_exact_candidate_local_docker"
    assert status["production_launch"] is True
    assert status["service_count"] == 7


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
    assert payload["schema"] == "propertyquarry.launch_room.v1"
    assert payload["production_launch_ready"] is False
    assert payload["release_proof_plane"]["github_actions_used"] is False
