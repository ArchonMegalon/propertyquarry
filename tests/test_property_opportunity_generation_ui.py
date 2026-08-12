from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_property_result_card_exposes_guarded_opportunity_brief_action() -> None:
    results_template = (REPO_ROOT / "ea/app/templates/app/_property_results_list.html").read_text()
    workbench_script = (REPO_ROOT / "ea/app/templates/app/_property_workbench_script.html").read_text()

    assert "data-pqx-opportunity-generate" in results_template
    assert "not is_shortlist_surface" in results_template
    assert "opportunity.get('status') in ['ready', 'preview']" in results_template
    assert "run.get('run_id')" in results_template
    assert "data-pqx-opportunity-artifact" in results_template

    assert "[data-pqx-opportunity-generate]" in workbench_script
    assert "!shortlistSurface && opportunityReady" in workbench_script
    assert "/app/api/property/opportunities/${encodeURIComponent(candidateRef)}/generate" in workbench_script
    assert "artifact?.body_markdown" in workbench_script
    assert "Private brief · ${provider}" in workbench_script
    assert "payload?.generation?.provider || 'PropertyQuarry'" in workbench_script
    assert "external_publication" not in workbench_script


def test_property_result_cards_expose_receipt_backed_onemin_assessment() -> None:
    results_template = (REPO_ROOT / "ea/app/templates/app/_property_results_list.html").read_text()
    workbench_script = (REPO_ROOT / "ea/app/templates/app/_property_workbench_script.html").read_text()

    assert "data-pqx-ai-assessment" in results_template
    assert "ai_assessment_receipt.get('manager_routed')" in results_template
    assert "evidence review" in results_template
    assert "candidate?.ai_assessment" in workbench_script
    assert "aiAssessmentReceipt?.manager_routed === true" in workbench_script
    assert "data-pqx-ai-assessment" in workbench_script
