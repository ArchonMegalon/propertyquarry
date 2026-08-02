from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import propertyquarry_release_proof_baseline as release_proof_baseline
from scripts.verify_flagship_release_readiness import verify as verify_flagship_candidate


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / ".codex-design" / "repo" / "EA_FLAGSHIP_RELEASE_GATE.json"
TRUTH_PLANE_PATH = ROOT / ".codex-design" / "repo" / "EA_FLAGSHIP_TRUTH_PLANE.md"
IMPLEMENTATION_SCOPE_PATH = ROOT / ".codex-design" / "repo" / "IMPLEMENTATION_SCOPE.md"
GENERATED_GATE_PATH = ROOT / ".codex-design" / "product" / "EA_FLAGSHIP_RELEASE_GATE.generated.json"
PULSE_PATH = ROOT / ".codex-design" / "product" / "WEEKLY_PRODUCT_PULSE.generated.json"
BROWSER_PROOF_PATH = ROOT / ".codex-studio" / "published" / "EA_BROWSER_WORKFLOW_PROOF.generated.json"
RELEASE_CHECKLIST_PATH = ROOT / "RELEASE_CHECKLIST.md"
PRODUCT_RELEASE_CHECKLIST_PATH = ROOT / "PRODUCT_RELEASE_CHECKLIST.md"
README_PATH = ROOT / "README.md"
RUNBOOK_PATH = ROOT / "RUNBOOK.md"
CLOSEOUT_PLAN_PATH = ROOT / "FLAGSHIP_CLOSEOUT_PLAN.md"
GLOBAL_FLAGSHIP_GOAL_PATH = ROOT / "docs" / "PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md"
VERIFY_RELEASE_ASSETS_PATH = ROOT / "scripts" / "verify_release_assets.sh"
NO_GITHUB_ACTIONS_PATH = ROOT / ".github" / "NO_GITHUB_ACTIONS.md"
REAL_BROWSER_TEST_FILE = "tests/e2e/test_propertyquarry_greenfield_browser.py"
EVIDENCE_SOURCE_TEST_FILE = "tests/test_property_evidence_overlays.py"
EVIDENCE_SOURCE_CASE = "test_property_research_rows_preserve_evidence_states_and_original_article_link"
REAL_BROWSER_EVIDENCE_CASE = "test_propertyquarry_research_evidence_states_and_links_render_in_real_browser"
REQUIRED_PACKETS_TOURS_REAL_BROWSER_CASES = (
    "test_propertyquarry_flagship_operating_loop_in_browser",
    "test_propertyquarry_best_match_opens_hosted_3d_tour_and_flythrough_in_real_browser",
    "test_propertyquarry_blocked_3d_tour_can_be_retried_from_research_packet_in_real_browser",
    "test_propertyquarry_research_detail_never_shows_fake_open_tour_for_generated_reconstruction_status",
    "test_propertyquarry_generated_reconstruction_public_launch_renders_honest_shell_in_real_browser",
    "test_propertyquarry_generated_reconstruction_public_launch_is_mobile_safe",
    "test_propertyquarry_expired_flat_preview_explains_3d_unavailable_in_real_browser",
)
REQUIRED_JOURNEY_IDS = [
    "public_entry",
    "onboarding_auth",
    "search_ranking",
    "shortlist_research_revisit",
    "account_pricing_privacy_recovery",
    "packets_tours",
    "feedback",
    "notifications",
]


def _verify_pulse(tmp_path: Path, pulse: dict[str, object]) -> list[str]:
    pulse_path = tmp_path / "pulse.json"
    pulse_path.write_text(json.dumps(pulse, indent=2) + "\n", encoding="utf-8")
    return verify_flagship_candidate(
        pulse_path=pulse_path,
        flagship_receipt_path=GENERATED_GATE_PATH,
        browser_proof_path=BROWSER_PROOF_PATH,
        journey_gates_path=Path(str(pulse["journey_gate_source"])),
        flagship_seed_path=GATE_PATH,
        implementation_scope_path=IMPLEMENTATION_SCOPE_PATH,
        required_contract_paths=(),
    )


def test_flagship_truth_plane_seed_points_at_browser_workflow_proof() -> None:
    gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))

    assert gate["product"] == "propertyquarry"
    assert gate["surface"] == "propertyquarry_flagship_release_control"
    assert gate["version"] == 2
    assert gate["truth_plane"]["source"] == ".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md"
    assert gate["truth_plane"]["legacy_history"] == "MILESTONE.json"
    assert release_proof_baseline.approved_seed_baseline_blockers(gate) == []

    global_contract = gate["global_launch_contract"]
    assert release_proof_baseline.approved_global_launch_contract_blockers(global_contract) == []
    assert global_contract["universal_market_claim"] is False
    assert global_contract["required_browser_engines"] == ["chromium", "firefox", "webkit"]
    assert global_contract["market_envelope"]["only_full_e2e_markets_are_launch_supported"] is True
    assert global_contract["core_advanced_visual_separation"] == {
        "core_must_not_require_paid_advanced_visuals": True,
        "advanced_visual_claim_requires_additive_live_binding": True,
    }

    browser_proof = gate["browser_workflow_proof"]["evidence_sources"]
    evidence_index = {entry["file"]: set(entry["cases"]) for entry in browser_proof}
    ordered_evidence_index = {entry["file"]: list(entry["cases"]) for entry in browser_proof}

    assert gate["browser_workflow_proof"]["proof_target"] == "propertyquarry"
    assert "tests/test_propertyquarry_workspace_redesign.py" in evidence_index
    assert "tests/e2e/test_propertyquarry_greenfield_browser.py" in evidence_index
    assert EVIDENCE_SOURCE_TEST_FILE in evidence_index
    assert (
        "test_propertyquarry_workspace_routes_render_greenfield_surfaces"
        in evidence_index["tests/test_propertyquarry_workspace_redesign.py"]
    )
    assert (
        "test_propertyquarry_failed_run_stays_on_activity_surface"
        in evidence_index["tests/test_propertyquarry_workspace_redesign.py"]
    )
    assert (
        "test_propertyquarry_greenfield_workspace_in_real_browser"
        in evidence_index["tests/e2e/test_propertyquarry_greenfield_browser.py"]
    )
    assert (
        "test_propertyquarry_greenfield_workspace_is_mobile_usable"
        in evidence_index["tests/e2e/test_propertyquarry_greenfield_browser.py"]
    )
    assert REAL_BROWSER_EVIDENCE_CASE in evidence_index[REAL_BROWSER_TEST_FILE]
    assert evidence_index[EVIDENCE_SOURCE_TEST_FILE] == {EVIDENCE_SOURCE_CASE}
    assert sum(len(cases) for test_file, cases in evidence_index.items() if "/e2e/" not in test_file) == 8
    assert len(evidence_index[REAL_BROWSER_TEST_FILE]) == 16
    assert ordered_evidence_index[REAL_BROWSER_TEST_FILE][4:11] == list(
        REQUIRED_PACKETS_TOURS_REAL_BROWSER_CASES
    )

    journey_matrix = gate["journey_evidence_matrix"]
    assert journey_matrix["version"] == 1
    assert journey_matrix["readiness_scope"] == "candidate_source_and_browser_proof"
    assert journey_matrix["required_journey_ids"] == REQUIRED_JOURNEY_IDS
    assert [row["journey_id"] for row in journey_matrix["rows"]] == REQUIRED_JOURNEY_IDS
    packets_tours = next(row for row in journey_matrix["rows"] if row["journey_id"] == "packets_tours")
    assert packets_tours["evidence_sources"] == [
        {
            "file": REAL_BROWSER_TEST_FILE,
            "cases": list(REQUIRED_PACKETS_TOURS_REAL_BROWSER_CASES),
        }
    ]
    mapped_cases: dict[str, set[str]] = {test_file: set() for test_file in evidence_index}
    for row in journey_matrix["rows"]:
        assert row["label"]
        assert row["evidence_sources"]
        assert row["live_requirement"]["status"] == "not_evaluated"
        assert row["live_requirement"]["authority"]
        assert row["live_requirement"]["required_profile"] == "launch"
        for source in row["evidence_sources"]:
            mapped_cases[source["file"]].update(source["cases"])
    assert mapped_cases == evidence_index

    conditions = gate["release_claim"]["required_conditions"]
    assert any("EA product surface canon exists" in condition for condition in conditions)
    assert any("PropertyQuarry" in condition for condition in conditions)
    assert any("release asset verification" in condition for condition in conditions)
    assert any("MILESTONE green" in condition or "MILESTONE" in condition for condition in conditions)
    assert not any("parity-oracle" in condition for condition in conditions)
    assert not any("noise-auditor" in condition for condition in conditions)

    expected_signals = gate["browser_workflow_proof"]["expected_browser_signals"]
    assert any("/app/properties" in signal for signal in expected_signals)
    assert any("ranked" in signal.lower() for signal in expected_signals)
    assert any("/app/research/" in signal for signal in expected_signals)
    assert any("mobile" in signal.lower() for signal in expected_signals)
    assert any("expired flat-preview" in signal.lower() for signal in expected_signals)

    canon = gate["ea_product_canon"]
    assert canon["source_root"] == ".codex-design/ea"
    assert canon["scope_label"] == "EA product surface canon"
    assert ".codex-design/ea/START_HERE.md" in canon["required_docs"]
    assert ".codex-design/ea/SURFACE_DESIGN_SYSTEM.md" in canon["required_docs"]
    assert ".codex-design/ea/LTD_INTEGRATION_MAP.md" in canon["required_docs"]


def test_flagship_release_docs_cite_the_truth_plane_instead_of_milestone_as_oracle() -> None:
    truth_plane = TRUTH_PLANE_PATH.read_text(encoding="utf-8")
    implementation_scope = IMPLEMENTATION_SCOPE_PATH.read_text(encoding="utf-8")
    release_checklist = RELEASE_CHECKLIST_PATH.read_text(encoding="utf-8")
    product_release_checklist = PRODUCT_RELEASE_CHECKLIST_PATH.read_text(encoding="utf-8")
    readme = README_PATH.read_text(encoding="utf-8")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    global_goal = GLOBAL_FLAGSHIP_GOAL_PATH.read_text(encoding="utf-8")

    assert "EA_FLAGSHIP_TRUTH_PLANE.md" in truth_plane
    assert "tests/test_propertyquarry_workspace_redesign.py" in truth_plane
    assert "tests/e2e/test_propertyquarry_greenfield_browser.py" in truth_plane
    assert "legacy assistant browser files are intentionally skipped" in truth_plane
    assert ".codex-design/ea/START_HERE.md" in truth_plane
    assert "MILESTONE.json" in truth_plane
    assert "docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md" in truth_plane
    assert "source-and-browser checkpoint" in truth_plane.lower()
    assert "not global launch authority" in truth_plane.lower()
    assert "globally credible" in global_goal
    assert "WCAG 2.2 Level AA" in global_goal
    assert release_proof_baseline.GLOBAL_LAUNCH_TERMINAL_COMMAND in global_goal.replace("\n  ", " ")
    assert ".codex-design/ea/*" in implementation_scope
    assert "EA product surface canon under `.codex-design/ea/*`" in implementation_scope
    assert "EA_FLAGSHIP_RELEASE_GATE.json" in release_checklist
    assert "EA_FLAGSHIP_RELEASE_GATE.generated.json" in release_checklist
    assert ".codex-design/ea/START_HERE.md" in release_checklist
    assert "EA_FLAGSHIP_TRUTH_PLANE.md" in release_checklist
    assert "EA_FLAGSHIP_RELEASE_GATE.json" in product_release_checklist
    assert "EA_FLAGSHIP_RELEASE_GATE.generated.json" in product_release_checklist
    assert ".codex-design/ea/START_HERE.md" in product_release_checklist
    assert "EA_FLAGSHIP_TRUTH_PLANE.md" in product_release_checklist
    assert ".codex-design/ea/START_HERE.md" in readme
    assert ".codex-design/ea/SURFACE_DESIGN_SYSTEM.md" in readme
    assert "EA_FLAGSHIP_TRUTH_PLANE.md" in readme
    assert "EA_FLAGSHIP_RELEASE_GATE.json" in readme
    assert "EA_FLAGSHIP_RELEASE_GATE.generated.json" in readme
    assert "scripts/materialize_ea_flagship_release_gate.py" in readme
    assert ".codex-design/ea/START_HERE.md" in runbook
    assert "EA_FLAGSHIP_TRUTH_PLANE.md" in runbook
    assert "EA_FLAGSHIP_RELEASE_GATE.generated.json" in runbook
    assert "scripts/materialize_ea_flagship_release_gate.py" in runbook


def test_flagship_release_receipt_is_materialized_or_expected_to_materialize() -> None:
    assert GENERATED_GATE_PATH.exists()
    receipt = json.loads(GENERATED_GATE_PATH.read_text(encoding="utf-8"))

    assert receipt["readiness_scope"] == "source_and_browser_proof"
    assert receipt["approved_baseline"] == release_proof_baseline.approved_baseline_binding()
    assert receipt["live_readiness"] == {
        "status": "not_evaluated",
        "authority": "_completion/property_gold_status/release-gate.json",
        "required_profile": "launch",
    }
    assert receipt["global_launch_readiness"] == {
        "status": "not_evaluated",
        "market_envelope_authority": release_proof_baseline.GLOBAL_LAUNCH_MARKET_ENVELOPE_AUTHORITY,
        "terminal_command": release_proof_baseline.GLOBAL_LAUNCH_TERMINAL_COMMAND,
        "source_browser_checkpoint_is_sufficient": False,
    }
    assert receipt["global_launch_contract"] == json.loads(GATE_PATH.read_text(encoding="utf-8"))[
        "global_launch_contract"
    ]
    assert "final live readiness is not evaluated" in receipt["operator_summary"].lower()
    assert "does not establish global launch authority" in receipt["operator_summary"].lower()
    matrix = receipt["journey_evidence_matrix"]
    assert matrix["required_journey_ids"] == REQUIRED_JOURNEY_IDS
    rows = matrix["rows"]
    assert [row["journey_id"] for row in rows] == REQUIRED_JOURNEY_IDS
    assert all(row["evidence_sources"] for row in rows)
    assert all(row["live_requirement"]["status"] == "not_evaluated" for row in rows)

    browser_receipt = json.loads(BROWSER_PROOF_PATH.read_text(encoding="utf-8"))
    assert browser_receipt["approved_baseline"] == release_proof_baseline.approved_baseline_binding()
    source_binding = receipt.get("source_binding")
    if isinstance(source_binding, dict):
        assert matrix["status"] == "pass"
        assert matrix["runtime_commit_sha"] == source_binding["code_commit"]
        assert all(row["proof_status"] == "pass" for row in rows)

        packets_tours = next(row for row in rows if row["journey_id"] == "packets_tours")
        assert packets_tours["evidence_sources"] == [
            {
                "file": REAL_BROWSER_TEST_FILE,
                "cases": list(REQUIRED_PACKETS_TOURS_REAL_BROWSER_CASES),
                "lane_status": "pass",
            }
        ]

        gate_sources = json.loads(GATE_PATH.read_text(encoding="utf-8"))[
            "browser_workflow_proof"
        ]["evidence_sources"]
        expected_browser_cases = next(
            entry["cases"]
            for entry in gate_sources
            if entry["file"] == REAL_BROWSER_TEST_FILE
        )
        expected_source_backed = [
            entry for entry in gate_sources if "/e2e/" not in entry["file"]
        ]
        source_backed_lanes = browser_receipt["source_backed_journey_proofs"]
        assert browser_receipt["source_backed_journey_proof"] == source_backed_lanes[0]
        assert [lane["test_file"] for lane in source_backed_lanes] == [
            entry["file"] for entry in expected_source_backed
        ]
        assert [lane["cases"] for lane in source_backed_lanes] == [
            entry["cases"] for entry in expected_source_backed
        ]
        assert sum(lane["required_case_count"] for lane in source_backed_lanes) == 8
        browser_lane = browser_receipt["real_browser_e2e_proof"]
        assert browser_lane["cases"] == expected_browser_cases
        assert browser_lane["required_case_count"] == len(expected_browser_cases)
        assert browser_lane["selected_count"] == len(expected_browser_cases)
        assert browser_lane["executed_count"] == len(expected_browser_cases)
        assert browser_lane["outcome_counts"] == {
            "passed": len(expected_browser_cases),
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
        }
    else:
        assert source_binding is None
        assert matrix["status"] == "not_evaluated"
        assert matrix["runtime_commit_sha"] == ""
        assert all(row["proof_status"] != "pass" for row in rows)
        blockers = receipt["blocking_reasons"]
        limitations = receipt["current_limitations"]
        assert isinstance(blockers, list)
        assert isinstance(limitations, list)
        assert any(str(item).strip() for item in [*blockers, *limitations])


def test_remote_workflow_is_retired_for_local_docker_release_authority() -> None:
    policy = NO_GITHUB_ACTIONS_PATH.read_text(encoding="utf-8")
    normalized_policy = " ".join(policy.split())

    assert "not as an execution or release-authority plane" in normalized_policy
    assert "governed local Docker host" in normalized_policy
    assert "scripts/propertyquarry_local_deployment_receipt.py" in normalized_policy
    assert "Adding a `.yml` or `.yaml` file under `.github/workflows/`" in normalized_policy
    workflow_root = ROOT / ".github" / "workflows"
    workflow_files = (
        [
            path
            for path in workflow_root.iterdir()
            if path.is_file() and path.suffix.casefold() in {".yml", ".yaml"}
        ]
        if workflow_root.exists()
        else []
    )
    assert workflow_files == []


def test_flagship_closeout_claim_is_scoped_to_the_proven_propertyquarry_surface() -> None:
    closeout = CLOSEOUT_PLAN_PATH.read_text(encoding="utf-8")

    assert "# Standalone PropertyQuarry Flagship Closeout Plan" in closeout
    assert "cannot establish Executive Assistant core eligibility" in closeout
    assert "tests/test_propertyquarry_workspace_redesign.py" in closeout
    assert "tests/e2e/test_propertyquarry_greenfield_browser.py" in closeout
    assert "Executive Assistant core is flagship-release eligible" not in closeout


def test_release_asset_verifier_binds_generated_receipts_to_current_propertyquarry_seed() -> None:
    verifier = VERIFY_RELEASE_ASSETS_PATH.read_text(encoding="utf-8")

    assert 'assert gate["product"] == "propertyquarry"' in verifier
    assert 'assert gate["surface"] == "propertyquarry_flagship_release_control"' in verifier
    assert 'browser_receipt_pass_blockers(browser_receipt, gate)' in verifier
    assert 'approved_seed_baseline_blockers(gate)' in verifier
    assert 'approved_global_launch_contract_blockers(gate["global_launch_contract"])' in verifier
    assert 'assert browser_receipt["product"] == gate["product"]' in verifier
    assert 'assert flagship_receipt["product"] == gate["product"]' in verifier
    assert 'assert flagship_receipt["readiness_scope"] == "source_and_browser_proof"' in verifier
    assert 'assert flagship_receipt["global_launch_contract"] == gate["global_launch_contract"]' in verifier
    assert '"source_browser_checkpoint_is_sufficient": False' in verifier
    assert '"authority": "_completion/property_gold_status/release-gate.json"' in verifier
    assert '"required_profile": "launch"' in verifier
    assert '"docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",' in verifier


def test_flagship_candidate_verifier_accepts_v2_fail_closed_truth(tmp_path: Path) -> None:
    pulse = json.loads(PULSE_PATH.read_text(encoding="utf-8"))
    receipt = json.loads(GENERATED_GATE_PATH.read_text(encoding="utf-8"))
    issues = _verify_pulse(tmp_path, pulse)

    if isinstance(receipt.get("source_binding"), dict):
        assert issues == []
    else:
        assert receipt["source_binding"] is None
        assert "flagship release receipt is blocked, expected pass" in issues
        assert all("live readiness" not in issue.lower() for issue in issues)

    pulse["journey_gate_health"]["state"] = "blocked"
    pulse["journey_gate_health"]["blocked_count"] = 99
    assert _verify_pulse(tmp_path, pulse) == issues


def test_flagship_candidate_verifier_rejects_legacy_production_overclaims(tmp_path: Path) -> None:
    baseline = json.loads(PULSE_PATH.read_text(encoding="utf-8"))
    mutations = [
        ("legacy contract", lambda pulse: pulse.__setitem__("contract_version", 1)),
        ("clear generic release state", lambda pulse: pulse["release_health"].__setitem__("state", "clear")),
        ("clear generic flagship state", lambda pulse: pulse["flagship_readiness"].__setitem__("state", "clear")),
        (
            "generic fleet-derived progress",
            lambda pulse: pulse["supporting_signals"].__setitem__("overall_progress_percent", 100),
        ),
        ("unmeasured drift zero", lambda pulse: pulse.__setitem__("design_drift_count", 0)),
        (
            "unvalidated live pass",
            lambda pulse: pulse["release_health"].__setitem__(
                "reported_live_readiness_state", "pass_unverified"
            ),
        ),
    ]

    for label, mutate in mutations:
        pulse = json.loads(json.dumps(baseline))
        mutate(pulse)
        assert _verify_pulse(tmp_path, pulse), label
