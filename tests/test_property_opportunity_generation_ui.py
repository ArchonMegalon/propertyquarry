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


def test_property_result_card_exposes_honest_receipt_backed_concept_cover() -> None:
    results_template = (REPO_ROOT / "ea/app/templates/app/_property_results_list.html").read_text()
    workbench_script = (REPO_ROOT / "ea/app/templates/app/_property_workbench_script.html").read_text()

    assert "data-pqx-opportunity-cover-generate" in results_template
    assert "data-pqx-opportunity-cover" in results_template
    assert ">Create cover</button>" in results_template
    assert "[data-pqx-opportunity-cover-generate]" in workbench_script
    assert "/app/api/property/opportunities/${encodeURIComponent(candidateRef)}/generate-cover" in workbench_script
    assert "/app/api/property/opportunities/generations/${encodeURIComponent(generationId)}" in workbench_script
    assert "receipt?.principal_bound !== true" in workbench_script
    assert "receipt?.proof_scope !== 'provider_call'" in workbench_script
    assert "Synthetic illustration · not listing photography" in workbench_script
    assert "Private concept cover · ${provider} · verified" in workbench_script
    assert "button.hidden = true" in workbench_script
    assert "grid-template-columns:44px minmax(0,1fr)" in results_template
    assert "font-size:.62rem;line-height:1.22" in results_template
    helper_index = workbench_script.index("const boundedCoverFetch = async")
    brief_handler_index = workbench_script.index("[data-pqx-opportunity-generate]")
    cover_handler_index = workbench_script.index("[data-pqx-opportunity-cover-generate]")
    assert helper_index < brief_handler_index < cover_handler_index
