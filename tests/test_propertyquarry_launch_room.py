from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.propertyquarry_launch_room import build_launch_room, render_markdown


ROOT = Path(__file__).resolve().parents[1]


def test_launch_room_reports_one_canonical_authority_and_no_false_launch() -> None:
    report = build_launch_room(ROOT)

    assert report["canonical_repository"] == "ArchonMegalon/propertyquarry"
    assert report["canonical_release_authority"] is True
    assert report["legacy_repository"] == "ArchonMegalon/property"
    assert report["legacy_release_authority"] is False
    assert report["repositories_are_exact_mirrors"] is False
    assert report["production_launch_ready"] is False
    assert report["github_actions"]["status"] == (
        "missing_current_protected_actions_evidence"
    )
    assert report["github_actions"]["network_freshness_proven"] is False
    assert report["live_deployment"]["production_launch"] is False


def test_launch_room_keeps_source_journey_and_browser_counts_distinct() -> None:
    report = build_launch_room(ROOT)
    proof = report["candidate_proof"]

    assert proof["candidate_bound"] is True
    assert proof["source"] == {"passed": True, "selected": 7, "required": 7}
    assert proof["journeys"] == {"passed": True, "selected": 8, "required": 8}
    assert proof["browser"] == {"passed": True, "selected": 16, "required": 16}


def test_launch_room_marks_core_candidate_only_and_advanced_unavailable() -> None:
    report = build_launch_room(ROOT)

    assert report["core_gold"] == {
        "status": "candidate_eligible_production_blocked",
        "production_claim": False,
    }
    assert report["advanced_visual_gold"] == {
        "status": "unavailable_unbound_producer_receipts",
        "production_claim": False,
    }


def test_launch_room_markdown_has_current_truth_and_next_action() -> None:
    rendered = render_markdown(build_launch_room(ROOT))

    assert "# PropertyQuarry launch room" in rendered
    assert "| Canonical repo | `ArchonMegalon/propertyquarry` |" in rendered
    assert "7/7 source; 8/8 journeys; 16/16 browser" in rendered
    assert "| Production launch | `BLOCKED` |" in rendered
    assert "## Next action" in rendered


def test_launch_room_cli_writes_json(tmp_path: Path) -> None:
    output = tmp_path / "launch-room.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "propertyquarry_launch_room.py"),
            "--format",
            "json",
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
