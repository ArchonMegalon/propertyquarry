from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_TEMPLATES = (
    ROOT / "ea/app/templates/app/property_decision_workbench.html",
    ROOT / "ea/app/templates/app/property_packets.html",
    ROOT / "ea/app/templates/app/property_research_detail.html",
    ROOT / "ea/app/templates/app/_property_selected_review_panel.html",
    ROOT / "ea/app/templates/pricing_page.html",
    ROOT / "ea/app/templates/property_billing_commercial_lane.html",
    ROOT / "ea/app/templates/propertyquarry_home.html",
    ROOT / "ea/app/templates/get_started.html",
    ROOT / "ea/app/templates/sign_in.html",
    ROOT / "ea/app/templates/onboarding/_hero.html",
    ROOT / "ea/app/templates/onboarding/_step_rules.html",
    ROOT / "ea/app/services/premium_dossier/templates/propertyquarry_dossier.html.j2",
)

CUSTOMER_COPY_SOURCES = CUSTOMER_TEMPLATES + (
    ROOT / "ea/app/api/routes/landing.py",
    ROOT / "ea/app/api/routes/landing_content.py",
    ROOT / "ea/app/api/routes/landing_view_models.py",
    ROOT / "ea/app/api/routes/landing_property_workspace_helpers.py",
    ROOT / "ea/app/api/routes/landing_property_research.py",
    ROOT / "ea/app/api/routes/landing_property_shortlist_panel.py",
    ROOT / "ea/app/api/routes/landing_property_workspace_payload.py",
    ROOT / "ea/app/templates/app/_property_running_panel.html",
    ROOT / "ea/app/templates/app/_property_workbench_feedback_script.html",
    ROOT / "ea/app/templates/app/_property_workbench_script.html",
    ROOT / "ea/app/templates/app/property_ranked_run_fast.html",
)

PREMIUM_PUBLIC_COPY_SOURCES = CUSTOMER_COPY_SOURCES + (
    ROOT / "ea/app/api/routes/public_tours.py",
    ROOT / "ea/app/product/property_score_methodology.py",
    ROOT / "ea/app/product/property_evidence_overlays.py",
    ROOT / "ea/app/product/property_worker_queues.py",
    ROOT / "ea/app/services/dossier_writer/evidence.py",
    ROOT / "ea/app/templates/app/_property_account_panel.html",
    ROOT / "ea/app/templates/app/_property_results_list.html",
)


def _template_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in CUSTOMER_TEMPLATES)


def test_propertyquarry_customer_templates_avoid_internal_operator_language() -> None:
    body = _template_text()

    forbidden = (
        ">EA<",
        " EA ranks",
        "EA post-filtered",
        "OODA",
        "Artifact receipts",
        "Generated asset receipts",
        "Telegram links",
        "premium_dossier_render_failed",
        "tour_control_3dvista_export_missing",
        "tour_control_matterport_export_missing",
        "NeuronWriter",
        "private_packet_guard",
        "search worker",
        "search workers",
        "provider scans running",
        "Collect video proof",
        "Risk register and next proof",
        "flythrough lane",
        "account lane",
        "optimization lane active",
        "Provider repair lane",
        "follow-up artifacts",
    )
    for marker in forbidden:
        assert marker not in body


def test_propertyquarry_customer_copy_avoids_operations_lane_language() -> None:
    body = "\n".join(path.read_text(encoding="utf-8") for path in CUSTOMER_COPY_SOURCES)
    lowered = body.lower()

    forbidden_phrases = (
        "visible lanes",
        "run slots",
        "crawl lane",
        "candidate lane",
        "risk lane",
        "risk lanes",
        "source lanes",
        "lanes in progress",
        "billing lane",
        "account lane",
        "provider lane",
        "working lane",
        "foreclosure lane",
        "lane <b",
    )
    for phrase in forbidden_phrases:
        assert phrase not in lowered


def test_propertyquarry_public_copy_avoids_proof_heavy_language() -> None:
    body = "\n".join(path.read_text(encoding="utf-8") for path in PREMIUM_PUBLIC_COPY_SOURCES)
    lowered = body.lower()

    forbidden_phrases = (
        "facts confirmed",
        "confirmed automatically from provider evidence",
        "confirmed facts",
        "evidence added",
        "what we confirmed",
        "inspect the evidence before you open the raw listing",
        "video evidence",
        "visual evidence",
        "magicfit walkthrough",
        "rendered walkthrough",
        "risk and evidence",
        "decision support",
        "run ranking",
        "no confirmed supermarket distance yet",
        "source tour. this is the evidence-grade visual baseline.",
        "google return path verified",
        "listing evidence",
        "official evidence",
        "provider listing evidence confirmed",
        "current listing evidence",
        "visual control verifier",
        "cached teable rollup",
        "freshness pending",
        "key details came from the listing",
        "from listing",
        ">score:",
        "score: {{",
        "score: ${",
        "ranked homes",
        "provider scope",
        "hard rules",
        "ready layers",
        "visible ranking",
        "how ranking works",
        "how each home is scored",
        "how fit works",
        "score at a glance",
        "best signals",
        "no strong signal attached yet",
        "no caution attached yet",
        "analytics:",
        "engagement:",
        "next best action:",
        "share state:",
        "reviewed feedback",
        "optimization recommendations",
        "saved durably",
        "no risk summary captured yet",
        "current answer",
        "repair notes",
        "repair state",
        "coverage, pages, repair",
        "health, coverage, repair",
        "how the provider is selling",
        "original 360 media",
        "usable original 360",
        "rebuild the tour",
        "missing-fact research queued",
        "raw pdfs",
        "raw portal",
        "long raw urls",
        "raw scopes",
        "optional fallback",
        "select providers",
        "all providers",
        "provider allowance",
        "providers selected",
        "checking providers",
        "details caught up",
        "search warming up",
        "preparing sources",
        "waiting for the first source",
        "sources selected",
        "selected sources",
        "checking sources",
        "select sources",
        "all sources",
        "choose trusted sources",
        "one source changed",
        "this source stopped",
        "this source changed",
        "source coverage",
        "widen sources",
        "waiting for sources",
        "open latest run",
        "provider sign-in",
        "provider sweep",
        "force fresh crawl",
        "current crawl",
        "source-by-source progress",
        "operational crawl details",
        "repair blocked provider lanes",
        "retry sources",
        "source boundaries",
        "run problems",
        "first run",
        "latest run",
        "the search is queued",
        "run updates",
        "run events",
        "open live run",
        "delete run",
        "property candidate",
        "propertyquarry candidate",
        "no finished run yet",
        "waiting for first run",
        "run state",
        "search · run",
        "back to run",
        "propertyquarry packets",
        "no property packet is ready",
        "share packet",
        "packets already sent",
        "moved to the latest run",
        "original run link expired",
        "open latest shortlist",
        "that run is no longer available",
        "expired run link",
        "old run",
        "old run snapshot",
        "open an earlier run",
        "recent runs",
        "recent run outcomes",
        "after the first run",
        "rest of the run",
        "packet is refreshed",
        "research packet",
        "packet ready",
        "packet explains",
        "current packet",
        "open property ready",
        "property research packet",
        "packet event",
        "packet feedback",
        "packet collaboration",
        "no new deltas",
        "no cached rollup",
        "crawl or index",
        "did not crawl",
        "uncertainty:",
        "fresh cached read",
        "terms-safe",
        "no matching homes in this run",
        "the run is taking longer",
        "during the run",
        "useful run",
        "upgrade required for this run",
        "this run could not finish",
        "after the run was interrupted",
        "final matching homes in this run",
        "run health",
        "no active run",
        "keep the run visible",
        "review the finished run",
        "while the run is active",
        "the run is complete",
        "this run finished",
        "next run changes",
        "choose one small change, then rerun",
        "estimated homes that may appear after rerun",
        "homes may appear after rerun",
        "rerun to see how many homes recover",
        "review saved searches, rerun them",
        "recurring searches ready to rerun",
        "saved runs",
        "preferences, runs",
        "remove saved runs",
        "edit, run, save",
        "retry running",
        "run it again",
    )
    for phrase in forbidden_phrases:
        assert phrase not in lowered


def test_propertyquarry_mobile_navigation_stays_branded_and_compact() -> None:
    workbench = (ROOT / "ea/app/templates/app/property_decision_workbench.html").read_text(encoding="utf-8")
    public_base = (ROOT / "ea/app/templates/base_public.html").read_text(encoding="utf-8")

    assert 'grid-template-areas: "brand nav actions";' in workbench
    assert '.pqx-top-actions > :not([data-property-start-top]):not(.pqx-account-menu):not([data-pqx-delete-run])' in workbench
    assert ".pqx-brand {\n        display: none !important;" in workbench
    assert '.pqx-shell[data-pqx-surface="agents"] .pqx-topbar,\n      .pqx-shell[data-pqx-surface="alerts"] .pqx-topbar {\n        grid-template-columns: minmax(0, 1fr);\n        grid-template-areas: "nav";' in workbench
    assert '.pqx-shell[data-pqx-surface="agents"] .pqx-top-actions,\n      .pqx-shell[data-pqx-surface="alerts"] .pqx-top-actions {\n        display: none;' in workbench
    assert '<details class="pqx-mobile-nav-menu" data-pqx-mobile-nav-menu' in workbench
    assert '.pqx-mobile-nav-menu > summary {\n        min-height: 40px;' in workbench
    assert '.pqx-mobile-nav-menu[open] .pqx-primary-nav {' in workbench
    assert '<details class="mobile-nav-sheet">' in public_base
    assert '<summary>Menu</summary>' in public_base
    assert '.mobile-nav { display: none; }' in public_base


def test_propertyquarry_layout_guide_is_the_design_contract() -> None:
    gate = (ROOT / "docs/PROPERTYQUARRY_DESIGN_SYSTEM_GATE.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/PROPERTYQUARRY_APP_LAYOUT_GUIDE.md").read_text(encoding="utf-8")
    registry = (ROOT / "docs/PROPERTYQUARRY_SURFACE_REGISTRY.md").read_text(encoding="utf-8")

    assert "docs/PROPERTYQUARRY_APP_LAYOUT_GUIDE.md" in gate
    assert "No decision wizard inside result cards." in guide
    assert "Progress card dimensions" in guide
    assert "panel width desktop: 360-420px" in guide
    assert "Table columns" in guide
    assert "This file defines \"all surfaces\"" in registry
    assert "Public Acquisition" in registry
    assert "Authenticated App" in registry
    assert "Generated Artifacts" in registry


def test_propertyquarry_surface_registry_defines_all_product_surfaces() -> None:
    from app.product.property_surface_registry import (
        ACCEPTANCE_GATES,
        AUDIT_AXES,
        PROOF_TYPES,
        clickrank_property_surface_keys,
        neuronwriter_property_surface_keys,
        property_surface_acceptance_matrix,
        property_surface_processor_policy_violations,
        property_surface_keys,
        property_surfaces_by_group,
    )

    keys = set(property_surface_keys())
    grouped = property_surfaces_by_group()

    assert len(keys) >= 25
    assert len(keys) == len(property_surface_keys())
    assert set(grouped) == {
        "public_acquisition",
        "auth_handoff",
        "authenticated_app",
        "results_research",
        "shared_public_artifacts",
        "generated_artifacts",
        "delivery",
        "management",
        "system_states",
    }
    for group, rows in grouped.items():
        assert rows, group

    required_keys = {
        "public_home",
        "public_pricing",
        "public_docs_guides",
        "registration",
        "sign_in",
        "app_shell",
        "search_wizard",
        "what_matters",
        "run_home",
        "shortlist",
        "property_research_detail",
        "agents",
        "account",
        "billing",
        "public_packet",
        "public_tour",
        "premium_dossier",
        "video_walkthrough",
        "email_delivery",
        "telegram_delivery",
        "whatsapp_delivery",
        "provider_management",
        "fleet_repair",
        "ltd_runtime",
        "loading_empty_error_states",
    }
    assert required_keys.issubset(keys)
    assert "legacy_object_detail" not in keys

    assert {"navigation", "clickability", "accessibility", "performance", "privacy"}.issubset(set(AUDIT_AXES))
    assert set(clickrank_property_surface_keys()) == {
        "public_home",
        "public_pricing",
        "public_trust",
        "public_docs_guides",
    }
    assert "app_shell" not in clickrank_property_surface_keys()
    assert "public_tour" not in clickrank_property_surface_keys()
    assert set(neuronwriter_property_surface_keys()) == {
        "public_home",
        "public_pricing",
        "public_trust",
        "public_docs_guides",
    }
    assert property_surface_processor_policy_violations() == ()
    assert "property_research_detail" not in neuronwriter_property_surface_keys()
    assert "app_shell" not in neuronwriter_property_surface_keys()

    expected_gates = {
        "route_ownership",
        "clickable_controls_do_real_work",
        "premium_visual_density_and_responsive_layout",
        "loading_empty_degraded_failed_and_repairing_states",
        "privacy_and_tenancy_boundary",
        "performance_budget",
        "seo_and_optimizer_boundary",
        "analytics_without_private_payloads",
        "accessibility_and_keyboard_flow",
        "regression_proof",
    }
    assert expected_gates == set(ACCEPTANCE_GATES)
    assert {"unit_contract", "screenshot", "privacy_fixture", "performance_probe"}.issubset(set(PROOF_TYPES))

    matrix = property_surface_acceptance_matrix()
    assert set(matrix) == keys
    for key, row in matrix.items():
        assert expected_gates.issubset(set(row["required_gates"])), key
        assert row["proof_types"], key
    assert matrix["public_home"]["clickrank_allowed"] is True
    assert matrix["public_home"]["neuronwriter_allowed"] is True
    assert "/guides/wohnung-kaufen-wien-checkliste" in matrix["public_docs_guides"]["routes"]
    assert "/markets/vienna" in matrix["public_docs_guides"]["routes"]
    assert "/blog" not in matrix["public_docs_guides"]["routes"]
    assert "/compare" not in matrix["public_docs_guides"]["routes"]
    assert matrix["app_shell"]["clickrank_allowed"] is False
    assert matrix["app_shell"]["neuronwriter_allowed"] is False
    assert matrix["public_results"]["routes"] == (
        "/results/:slug",
        "/results/:slug.json",
        "/results/files/:slug/:asset",
    )
    assert matrix["public_packet"]["routes"] == (
        "/v1/integrations/fliplink/documents/property-packets/:token",
        "/app/properties/packets",
    )
    assert matrix["public_tour"]["clickrank_allowed"] is False
    assert matrix["public_results"]["neuronwriter_allowed"] is False
    assert matrix["public_packet"]["neuronwriter_allowed"] is False
    assert matrix["public_tour"]["routes"] == ("/tours/:slug", "/tours/:slug.json", "/tours/files/:slug/:asset")
    assert matrix["agents"]["routes"] == ("/app/agents", "/app/automation", "/app/automations")
    assert "/app/settings/access" in matrix["account"]["routes"]
    assert matrix["premium_dossier"]["routes"] == ("/app/api/properties/packets/:publication_id/pdf",)
    assert matrix["premium_dossier"]["neuronwriter_allowed"] is False
    assert "/app/api/signals/willhaben/property-tour" in matrix["floorplan_and_tour_control"]["routes"]
    assert "/app/api/property-video/requests/dadan" in matrix["video_walkthrough"]["routes"]
    assert "/v1/integrations/dadan/webhooks/recording-submitted" in matrix["video_walkthrough"]["routes"]
    assert "/v1/channels/telegram/ingest" in matrix["telegram_delivery"]["routes"]
    assert "/v1/integrations/heyy/whatsapp/webhook" in matrix["whatsapp_delivery"]["routes"]
    assert matrix["email_delivery"]["neuronwriter_allowed"] is False
    assert matrix["telegram_delivery"]["neuronwriter_allowed"] is False
    assert matrix["whatsapp_delivery"]["neuronwriter_allowed"] is False


def test_propertyquarry_surface_processor_policy_flags_any_non_public_optimizer(monkeypatch) -> None:
    import app.product.property_surface_registry as registry

    monkeypatch.setattr(
        registry,
        "PROPERTY_SURFACES",
        (
            registry.PropertySurface(
                key="public_ok",
                group="public_acquisition",
                label="Public editorial page",
                routes=("/",),
                clickrank_allowed=True,
                neuronwriter_allowed=True,
            ),
            registry.PropertySurface(
                key="private_research_bad",
                group="results_research",
                label="Private research detail",
                routes=("/app/research/:candidate_ref",),
                clickrank_allowed=True,
                neuronwriter_allowed=True,
            ),
            registry.PropertySurface(
                key="delivery_bad",
                group="delivery",
                label="Delivery payload",
                routes=("/app/account?billing=1#delivery",),
                clickrank_allowed=True,
            ),
        ),
    )

    assert registry.property_surface_processor_policy_violations() == (
        "clickrank:private_research_bad",
        "neuronwriter:private_research_bad",
        "clickrank:delivery_bad",
    )


def test_propertyquarry_clickable_looking_recent_reviews_are_real_links_or_plain_rows() -> None:
    body = (ROOT / "ea/app/templates/app/property_decision_workbench.html").read_text(encoding="utf-8")

    assert "href=\"{{ packet.get('url') or '#' }}\"" not in body
    assert "pqx-recent-review-static" in body
    assert "<span class=\"pqx-pill\">{{ packet.get('title') }}</span>" not in body
    assert ".pqx-recent-review" in body
    assert "white-space: normal;" in body
    assert "overflow-wrap: normal;" in body


def test_propertyquarry_search_results_keep_hidden_homes_copy_minimal() -> None:
    body = (ROOT / "ea/app/templates/app/property_decision_workbench.html").read_text(encoding="utf-8")

    assert "Search guard" not in body
    assert "Review outside-brief homes" in body
    assert "Review hidden homes" not in body
    assert "Widen one requirement" in body
    assert "Choose one small change, then search again." in body
    assert "provider active" not in body
    assert "providers active" not in body
    assert "Preparing providers" not in body
    assert "data-pqx-counterfactual" in body
    assert "Best matches" in body


def test_propertyquarry_brand_marks_route_to_public_or_dashboard_home() -> None:
    public_shell = (ROOT / "ea/app/templates/base_public.html").read_text(encoding="utf-8")
    console_shell = (ROOT / "ea/app/templates/base_console.html").read_text(encoding="utf-8")
    workbench = (ROOT / "ea/app/templates/app/property_decision_workbench.html").read_text(encoding="utf-8")

    assert "{% set brand_home_href = '/?home=1' if (brand.key == 'propertyquarry' and public_signed_in) else ((brand.app_home or '/app/properties') if public_signed_in else (brand.public_base_url or '/')) %}" in public_shell
    assert '<a class="brand" href="{{ brand_home_href }}" aria-label="{{ brand.name }} home">' in public_shell
    assert "{% set brand_home_href = '/?home=1' if brand.key == 'propertyquarry' else (brand.app_home or '/app/properties') %}" in console_shell
    assert '<a class="brand" href="{{ brand_home_href }}" aria-label="{{ brand.name }} home">' in console_shell
    assert "run.get('run_id')" not in workbench.split('<a class="pqx-brand"', 1)[1].split(">", 1)[0]
    assert '<a class="pqx-brand" href="/?home=1" aria-label="PropertyQuarry public home">' in workbench


def test_propertyquarry_app_surfaces_expose_account_navigation() -> None:
    console_shell = (ROOT / "ea/app/templates/base_console.html").read_text(encoding="utf-8")
    workbench = (ROOT / "ea/app/templates/app/property_decision_workbench.html").read_text(encoding="utf-8")

    for body in (console_shell, workbench):
        assert "Account navigation" in body
        assert ">Saved defaults<" in body
        assert "Billing account" in body
        assert ">Access<" in body
        assert ">Log out<" in body


def test_propertyquarry_customer_surfaces_keep_accessibility_exit_gates() -> None:
    public_shell = (ROOT / "ea/app/templates/base_public.html").read_text(encoding="utf-8")
    workbench = (ROOT / "ea/app/templates/app/property_decision_workbench.html").read_text(encoding="utf-8")

    assert "a:focus-visible" in public_shell
    assert "button:focus-visible" in public_shell
    assert "@media (prefers-reduced-motion: reduce)" in public_shell
    assert "@media (prefers-reduced-motion: reduce)" in workbench
    assert 'aria-label="PropertyQuarry sections"' in workbench
    assert 'aria-label="PropertyQuarry view mode"' in workbench
    assert 'aria-label="Search setup"' in workbench
    assert 'aria-label="Account navigation"' in workbench
    assert 'aria-label="Close outside-brief homes"' in workbench
    assert 'aria-label="Close hidden homes"' not in workbench


def test_propertyquarry_console_shell_uses_new_property_surface_links() -> None:
    body = (ROOT / "ea/app/templates/base_console.html").read_text(encoding="utf-8")

    assert 'href="/app/account{{ query_suffix }}"' in body
    assert 'href="/app/agents{{ query_suffix }}"' in body
    assert "Search, market watch, decision packets, and review in one persistent shell." in body
    assert 'href="/app/settings{{ query_suffix }}"' not in body
    assert 'href="/app/research{{ query_suffix }}"' not in body
