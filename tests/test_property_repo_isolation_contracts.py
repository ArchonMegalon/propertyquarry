from __future__ import annotations

from pathlib import Path

from scripts import check_property_repo_isolation as repo_isolation


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_repo_isolation_requires_the_governed_deploy_handoff() -> None:
    assert repo_isolation._makefile_deploy_uses_governed_handoff(
        "deploy:\n\t./scripts/deploy_propertyquarry.sh\n"
    )
    assert not repo_isolation._makefile_deploy_uses_governed_handoff(
        "deploy:\n\tdocker compose -f docker-compose.property.yml up -d\n"
    )
    assert not repo_isolation._makefile_deploy_uses_governed_handoff(
        "deploy:\n\t./scripts/deploy_propertyquarry.sh\n\tdocker compose up -d\n"
    )


def test_property_release_scripts_bootstrap_repo_local_app_imports() -> None:
    script_paths = [
        *sorted((ROOT / "scripts").glob("propertyquarry_*.py")),
        *sorted((ROOT / "scripts").glob("check_property_*.py")),
        *sorted((ROOT / "scripts").glob("verify_*property*.py")),
    ]
    offenders: list[str] = []
    for path in script_paths:
        body = path.read_text(encoding="utf-8")
        if "from app." not in body and "import app." not in body:
            continue
        bootstraps_ea = (
            "sys.path.insert" in body
            and (
                'ROOT / "ea"' in body
                or "ROOT / 'ea'" in body
                or 'EA_ROOT = ROOT / "ea"' in body
                or "EA_ROOT = ROOT / 'ea'" in body
            )
        )
        if not bootstraps_ea:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_repo_isolation_quarantines_inherited_docs() -> None:
    script = _read("scripts/check_property_repo_isolation.py")
    dockerignore = _read(".dockerignore")
    doc = _read("docs/REPO_ISOLATION.md")

    for entry in (
        "docs/black_ledger_newsroom/",
        "docs/chummer5a_parity_lab/",
        "docs/chummer_explain_narration_packs/",
        "docs/chummer_governor_packets/",
        "docs/chummer_launch_followthrough/",
        "docs/chummer_operator_safe_packets/",
        "docs/chummer_organizer_packets/",
    ):
        assert entry in script
        assert entry in dockerignore
        assert entry in doc


def test_property_release_gates_include_phase_and_master_regressions() -> None:
    script = _read("scripts/property_release_gates.sh")

    for required in (
        "tests/test_propertyquarry_phase1_exit_gate.py",
        "tests/test_propertyquarry_phase2_exit_gate.py",
        "tests/test_propertyquarry_phase3_exit_gate.py",
        "tests/test_propertyquarry_phase4_exit_gate.py",
        "tests/test_propertyquarry_phase5_exit_gate.py",
        "tests/test_propertyquarry_phase6_exit_gate.py",
        "tests/test_propertyquarry_phase7_exit_gate.py",
        "tests/test_propertyquarry_master_regression_gate.py",
        "tests/test_propertyquarry_tester_gold_gate.py",
        "tests/e2e/test_propertyquarry_phase_regression_browser.py",
        "tests/e2e/test_propertyquarry_packet_engagement_browser.py",
        "tests/e2e/test_propertyquarry_feedback_browser.py",
        "tests/e2e/test_propertyquarry_summary_artifacts_browser.py",
        "tests/e2e/test_propertyquarry_packet_publishing_browser.py",
        "tests/e2e/test_propertyquarry_timeline_browser.py",
        "tests/e2e/test_propertyquarry_commercial_optimization_browser.py",
        "tests/e2e/test_propertyquarry_flagship_flow.py",
        "tests/e2e/test_propertyquarry_public_tour_browser.py",
        "scripts/check_property_release_hygiene.py",
        "scripts/check_property_public_tour_manifest_contract.py",
        "scripts/check_property_furniture_style_contract.py",
        "scripts/check_property_bts_methodology_contract.py",
        "scripts/check_property_tour_delivery_contract.py",
        "scripts/verify_property_tour_controls.py",
        "scripts/check_property_surface_accessibility.py",
        "scripts/propertyquarry_authenticated_performance_smoke.py",
        "scripts/propertyquarry_repair_fleet_canary.py",
        "scripts/property_live_provider_smoke.py",
        "scripts/propertyquarry_gold_status.py",
        "_completion/repair/propertyquarry-repair-canary-release-gate.json",
        "_completion/provider_smoke/release-gate-provider-matrix.json",
        "_completion/furniture_styles/property-furniture-style-contract-release-gate.json",
        "_completion/bts_methodology/property-bts-methodology-contract-release-gate.json",
        "_completion/tour_delivery/property-tour-delivery-contract-release-gate.json",
        "_completion/property_gold_status/release-gate.json",
    ):
        assert required in script


def test_property_release_workflow_runs_the_gold_gate_bundle() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    release_gate = _read("scripts/property_release_gates.sh")

    for required in (
        "push:",
        "pull_request:",
        "workflow_dispatch:",
        "property-security-posture:",
        "security-static:",
        "propertyquarry-browser-contracts:",
        "product-browser-e2e:",
        "browser-engine: [chromium, firefox, webkit]",
        "test_propertyquarry_flagship_operating_loop_in_browser",
        "propertyquarry-live-release-gates:",
        "PROPERTYQUARRY_LIVE_MOBILE_REQUIRED_BROWSER_ENGINES: chromium,firefox,webkit",
        "run: ./scripts/propertyquarry_live_release_gates.sh",
        "scripts/propertyquarry_release_make_dispatch.py property-release-gates",
        "smoke-runtime-api:",
        "smoke-runtime-postgres:",
        "postgres-runtime-contracts:",
    ):
        assert required in workflow
    assert workflow.count(
        "shell: /bin/bash --noprofile --norc -p -e -u -o pipefail {0}"
    ) == 2
    for required in (
        "tests/test_dossier_writer.py",
        "tests/test_dadan_video_request_workflow.py",
        "tests/test_property_media_factory.py",
        "tests/test_premium_dossier_contracts.py",
        "tests/test_public_rybbit.py",
        "tests/test_telegram_delivery_service.py",
        "tests/test_property_live_public_smoke.py",
        "tests/test_property_live_provider_smoke.py",
        "PROPERTYQUARRY_VISUAL_WATCH_URL",
        "scripts/propertyquarry_visual_watch.py",
        "PROPERTYQUARRY_VISUAL_WATCH_MOBILE_VIEWPORT",
    ):
        assert required in release_gate


def test_property_security_posture_blocks_public_tour_render_time_research() -> None:
    script = _read("scripts/check_property_security_posture.py")

    for forbidden_fetcher in (
        "_fetch_listing_research",
        "_reverse_geocode",
        "_fetch_nearby_poi_research",
        "nominatim.openstreetmap.org",
        "overpass-api.de",
    ):
        assert forbidden_fetcher in script
    assert "stored research snapshots" in script
    assert "PROPERTYQUARRY_PUBLIC_MEDIA_ALLOWED_HOSTS" in script
