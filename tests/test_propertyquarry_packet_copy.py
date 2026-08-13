from __future__ import annotations

from pathlib import Path


WORKBENCH_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ea/app/templates/app/_property_workbench_script.html"
)
WORKBENCH_FEEDBACK_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ea/app/templates/app/_property_workbench_feedback_script.html"
)


def test_local_packet_actions_do_not_claim_external_sharing() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")
    feedback_source = WORKBENCH_FEEDBACK_SCRIPT.read_text(encoding="utf-8")

    assert "Save review packet" in source
    assert "Save shortlist packet" in source
    assert "Local review shortlist from PropertyQuarry" in source
    assert 'data-rybbit-event="pq.packet.saved"' in source
    assert "Share this home" not in source
    assert ">Share results</button>" not in source
    assert "pq.packet.shared" not in source
    assert "Shareable shortlist from PropertyQuarry" not in source
    assert "Share this home" not in feedback_source
    assert "Share results" not in feedback_source
    assert "pq.packet.shared" not in feedback_source
    assert "Save review packet" in feedback_source
    assert "Save shortlist packet" in feedback_source
    assert "pq.packet.saved" in feedback_source


def test_research_decision_tray_includes_persisted_viewing_and_agent_actions() -> None:
    source = WORKBENCH_FEEDBACK_SCRIPT.read_text(encoding="utf-8")

    assert "Mark viewing requested" in source
    assert "recordDecisionState(candidate, 'viewing_requested'" in source
    assert "Ask the agent" in source
    assert "clippyQuestionNode?.focus()" in source
