from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_SCRIPT = ROOT / "ea/app/templates/app/_property_workbench_script.html"
WORKBENCH_FEEDBACK_SCRIPT = ROOT / "ea/app/templates/app/_property_workbench_feedback_script.html"
SELECTED_REVIEW_PANEL = ROOT / "ea/app/templates/app/_property_selected_review_panel.html"
DECISION_WORKBENCH = ROOT / "ea/app/templates/app/property_decision_workbench.html"
ACCOUNT_PANEL = ROOT / "ea/app/templates/app/_property_account_panel.html"
PACKETS_DASHBOARD = ROOT / "ea/app/templates/app/property_packets.html"
LOCALIZATION = ROOT / "ea/app/api/propertyquarry_localization.py"


def _assert_no_external_share_claim(source: str) -> None:
    assert "Share this home" not in source
    assert ">Share results</button>" not in source
    assert "pq.packet.shared" not in source
    assert "Shareable shortlist from PropertyQuarry" not in source
    assert "Share property pages and keep the replies together." not in source


def test_local_packet_actions_do_not_claim_external_sharing() -> None:
    source = WORKBENCH_SCRIPT.read_text(encoding="utf-8")
    feedback_source = WORKBENCH_FEEDBACK_SCRIPT.read_text(encoding="utf-8")
    review_source = SELECTED_REVIEW_PANEL.read_text(encoding="utf-8")
    workbench_source = DECISION_WORKBENCH.read_text(encoding="utf-8")
    account_source = ACCOUNT_PANEL.read_text(encoding="utf-8")
    packets_source = PACKETS_DASHBOARD.read_text(encoding="utf-8")
    localization_source = LOCALIZATION.read_text(encoding="utf-8")

    assert "Save review packet" in source
    assert "Save shortlist packet" in source
    assert "Local review shortlist from PropertyQuarry" in source
    assert 'data-rybbit-event="pq.packet.saved"' in source
    _assert_no_external_share_claim(source)
    _assert_no_external_share_claim(feedback_source)
    assert "Save review packet" in feedback_source
    assert "Save shortlist packet" in feedback_source
    assert "pq.packet.saved" in feedback_source
    assert "Save review packet" in review_source
    assert "Save shortlist packet" in review_source
    _assert_no_external_share_claim(review_source)
    assert "Save review packet" in workbench_source
    assert "Save shortlist packet" in workbench_source
    _assert_no_external_share_claim(workbench_source)
    assert ">Saved packets</a>" in account_source
    assert "Shared pages" not in account_source
    _assert_no_external_share_claim(account_source)
    assert "PropertyQuarry Saved Packets" in packets_source
    assert "Saved packets" in packets_source
    assert "Save local review packets and keep the replies together." in packets_source
    assert "Keep only packets that are ready to review." in packets_source
    assert "No saved packet is ready yet." in packets_source
    assert "or 'save packet'" in packets_source
    assert "have saved recipient notes." in packets_source
    assert "across saved packets." in packets_source
    assert "Paste external link" in packets_source
    assert "Save external link" in packets_source
    assert "Save recipient" in packets_source
    assert "No recipients yet." in packets_source
    assert "were shared." not in packets_source
    assert "across shared homes." not in packets_source
    assert "Paste shared link" not in packets_source
    assert "Save shared link" not in packets_source
    assert "Not shared yet." not in packets_source
    assert "PropertyQuarry Shared Pages" not in packets_source
    assert "Shared pages" not in packets_source
    assert "Send only pages" not in packets_source
    assert "or 'share page'" not in packets_source
    _assert_no_external_share_claim(packets_source)
    assert '"Share this home"' not in localization_source
    assert '"Share results"' not in localization_source
    assert '"Save review packet"' in localization_source
    assert '"Save shortlist packet"' in localization_source
    assert '"Saved packets"' in localization_source
    assert '"Still unknown"' in localization_source


def test_research_decision_tray_includes_persisted_viewing_and_agent_actions() -> None:
    source = WORKBENCH_FEEDBACK_SCRIPT.read_text(encoding="utf-8")
    review_source = SELECTED_REVIEW_PANEL.read_text(encoding="utf-8")
    workbench_script = WORKBENCH_SCRIPT.read_text(encoding="utf-8")

    assert "Mark viewing requested" in source
    assert "recordDecisionState(candidate, 'viewing_requested'" in source
    assert "Ask the agent" in source
    assert "clippyQuestionNode?.focus()" in source
    assert "Before you visit" in review_source
    assert "data-pw-visit-sheet" in review_source
    assert "data-pw-visit-recommendation" in review_source
    assert "Still unknown" in review_source
    assert "data-pw-ask-agent" in review_source
    assert "Ask the agent" in review_source
    assert "data-pw-visit-sheet" in source
    assert "Still unknown" in source
    assert "data-pw-ask-agent" in workbench_script
    assert "Can you confirm the ${firstUnknown}?" in workbench_script
    assert "candidate.evidence_overlays" in workbench_script
    assert "nearest_subway_m ? `Subway" not in workbench_script
    assert "school_atlas_progression_summary ? 'verified'" not in workbench_script
