from __future__ import annotations

import copy
import errno
import json
import os
import subprocess
from pathlib import Path

import pytest

import scripts.materialize_ea_flagship_release_gate as flagship_materializer
from scripts.materialize_ea_flagship_release_gate import (
    _browser_lane_pass_is_supported,
    _present,
    browser_receipt_pass_blockers,
)
from scripts.propertyquarry_release_receipt_binding import build_source_binding


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "materialize_ea_flagship_release_gate.py"
OUTPUT = Path(".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json")
SEED = Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json")
TRUTH_PLANE = Path(".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md")
BROWSER_PROOF = Path(".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json")
PRODUCT_CANON_DOCS = [
    Path(".codex-design/ea/README.md"),
    Path(".codex-design/ea/START_HERE.md"),
    Path(".codex-design/ea/VISION.md"),
    Path(".codex-design/ea/PUBLIC_NAVIGATION.yaml"),
    Path(".codex-design/ea/APP_NAVIGATION.yaml"),
    Path(".codex-design/ea/SURFACE_DESIGN_SYSTEM.md"),
    Path(".codex-design/ea/FIRST_VALUE_JOURNEY.md"),
    Path(".codex-design/ea/COPY_PRINCIPLES.md"),
    Path(".codex-design/ea/METRICS_AND_SLOS.yaml"),
    Path(".codex-design/ea/LTD_INTEGRATION_MAP.md"),
]
JOURNEY_IDS = [
    "public_entry",
    "onboarding_auth",
    "search_ranking",
    "shortlist_research_revisit",
    "account_pricing_privacy_recovery",
    "packets_tours",
    "feedback",
    "notifications",
]


def test_materializer_presence_check_rejects_non_regular_and_external_paths(
    tmp_path: Path,
) -> None:
    regular = Path("docs/regular.md")
    target = tmp_path / regular
    target.parent.mkdir(parents=True)
    target.write_text("release evidence\n", encoding="utf-8")
    directory = Path("docs/directory")
    (tmp_path / directory).mkdir()
    symlink = Path("docs/symlink.md")
    (tmp_path / symlink).symlink_to(target.name)
    external = tmp_path.parent / "external-release-evidence.md"
    external.write_text("external\n", encoding="utf-8")

    assert _present(tmp_path, regular)
    assert not _present(tmp_path, directory)
    assert not _present(tmp_path, symlink)
    assert not _present(tmp_path, external)
    assert not _present(tmp_path, Path("../external-release-evidence.md"))


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_flagship_sources(root: Path) -> dict[str, object]:
    tracked = sorted(
        {
            SEED.as_posix(),
            TRUTH_PLANE.as_posix(),
            *(path.as_posix() for path in PRODUCT_CANON_DOCS),
            "README.md",
            "RUNBOOK.md",
            "RELEASE_CHECKLIST.md",
            "PRODUCT_RELEASE_CHECKLIST.md",
            "tests/test_propertyquarry_workspace_redesign.py",
            "tests/e2e/test_propertyquarry_greenfield_browser.py",
        }
    )
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "PropertyQuarry Fixture")
    _git(root, "config", "user.email", "propertyquarry-fixture@example.invalid")
    _git(root, "add", "--", *tracked)
    _git(root, "commit", "--quiet", "-m", "fixture: immutable flagship sources")
    seed = json.loads((root / SEED).read_text(encoding="utf-8"))
    return build_source_binding(
        root,
        seed_path=SEED,
        evidence_sources=seed["browser_workflow_proof"]["evidence_sources"],
    )


def _passing_browser_lane(root: Path, *, test_file: str, cases: list[str]) -> dict[str, object]:
    return {
        "status": "pass",
        "command": "python3 -m pytest -q "
        + " ".join(f"{test_file}::{case}" for case in cases),
        "cwd": root.as_posix(),
        "python_bin": "python3",
        "test_file": test_file,
        "cases": cases,
        "required_case_count": len(cases),
        "selected_count": len(cases),
        "executed_count": len(cases),
        "outcome_counts": {
            "passed": len(cases),
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
        },
        "exit_code": 0,
        "duration_seconds": 0.01,
        "output_excerpt": [f"{len(cases)} passed"],
        "limitations": [],
    }


def _load_passing_browser_contract(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    _write_minimal_flagship_tree(root, browser_proof_status="pass")
    seed = json.loads((root / SEED).read_text(encoding="utf-8"))
    receipt = json.loads((root / BROWSER_PROOF).read_text(encoding="utf-8"))
    assert browser_receipt_pass_blockers(receipt, seed) == []
    return receipt, seed


def _journey_matrix(
    *,
    source_file: str,
    source_case: str,
    browser_file: str,
    browser_case: str,
    receipt: bool = False,
    runtime_commit_sha: str = "",
) -> dict[str, object]:
    row_sources = {
        "public_entry": [(source_file, [source_case])],
        "onboarding_auth": [(source_file, [source_case])],
        "search_ranking": [(source_file, [source_case]), (browser_file, [browser_case])],
        "shortlist_research_revisit": [(browser_file, [browser_case])],
        "account_pricing_privacy_recovery": [(source_file, [source_case])],
        "packets_tours": [(browser_file, [browser_case])],
        "feedback": [(browser_file, [browser_case])],
        "notifications": [(browser_file, [browser_case])],
    }
    rows: list[dict[str, object]] = []
    for journey_id in JOURNEY_IDS:
        sources = [
            {
                "file": test_file,
                "cases": cases,
                **({"lane_status": "pass"} if receipt else {}),
            }
            for test_file, cases in row_sources[journey_id]
        ]
        row: dict[str, object] = {
            "journey_id": journey_id,
            "label": journey_id.replace("_", " ").title(),
            "evidence_sources": sources,
            "live_requirement": {
                "status": "not_evaluated",
                "authority": f"_completion/smoke/property-live-{journey_id}.json",
                "required_profile": "launch",
            },
        }
        if receipt:
            row["proof_status"] = "pass"
            row["blocking_reasons"] = []
        rows.append(row)
    matrix: dict[str, object] = {
        "version": 1,
        "readiness_scope": "candidate_source_and_browser_proof",
        "required_journey_ids": JOURNEY_IDS,
        "rows": rows,
    }
    if receipt:
        matrix["status"] = "pass"
        matrix["runtime_commit_sha"] = runtime_commit_sha
    return matrix


def _write_minimal_flagship_tree(
    root: Path,
    *,
    browser_proof_status: str | None = None,
) -> None:
    (root / SEED).parent.mkdir(parents=True, exist_ok=True)
    (root / TRUTH_PLANE).parent.mkdir(parents=True, exist_ok=True)
    (root / OUTPUT).parent.mkdir(parents=True, exist_ok=True)
    (root / BROWSER_PROOF).parent.mkdir(parents=True, exist_ok=True)
    for rel in PRODUCT_CANON_DOCS:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("# canon\n", encoding="utf-8")

    seed = {
        "product": "propertyquarry",
        "surface": "propertyquarry_flagship_release_control",
        "version": 1,
        "truth_plane": {
            "source": ".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md",
            "legacy_history": "MILESTONE.json",
        },
        "release_claim": {
            "summary": "The standalone PropertyQuarry surface can only claim flagship-grade release truth when browser proof and release verification agree.",
            "required_conditions": [
                "EA product surface canon exists and names the public navigation, app navigation, first-value journey, surface system, copy rules, and LTD delivery map",
                "source-backed browser proof renders the standalone PropertyQuarry workspace",
                "real browser E2E opens ranked PropertyQuarry results on desktop",
                "real browser E2E proves the PropertyQuarry workbench remains usable on mobile",
                "release asset verification knows the EA flagship truth plane, the EA product surface canon, and the gate seed",
                "release checklists cite the EA truth plane and the EA product surface canon instead of using MILESTONE green as the oracle",
            ],
        },
        "ea_product_canon": {
            "source_root": ".codex-design/ea",
            "scope_label": "EA product surface canon",
            "required_docs": [path.as_posix() for path in PRODUCT_CANON_DOCS],
        },
        "browser_workflow_proof": {
            "proof_target": "propertyquarry",
            "expected_browser_signals": ["/app/properties", "/app/research"],
            "evidence_sources": [
                {
                    "file": "tests/test_propertyquarry_workspace_redesign.py",
                    "cases": ["test_propertyquarry_workspace_routes_render_greenfield_surfaces"],
                },
                {
                    "file": "tests/e2e/test_propertyquarry_greenfield_browser.py",
                    "cases": ["test_propertyquarry_greenfield_workspace_in_real_browser"],
                },
            ]
        },
        "verification_binding": {
            "primary_verifier": "scripts/verify_release_assets.sh",
            "supporting_test": "tests/test_flagship_truth_plane.py",
        },
    }
    sources = seed["browser_workflow_proof"]["evidence_sources"]
    seed["journey_evidence_matrix"] = _journey_matrix(
        source_file=sources[0]["file"],
        source_case=sources[0]["cases"][0],
        browser_file=sources[1]["file"],
        browser_case=sources[1]["cases"][0],
    )
    (root / SEED).write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
    (root / TRUTH_PLANE).write_text("# EA flagship truth plane\n", encoding="utf-8")
    for rel in ("README.md", "RUNBOOK.md", "RELEASE_CHECKLIST.md", "PRODUCT_RELEASE_CHECKLIST.md"):
        (root / rel).write_text(
            "\n".join(
                [
                    "EA_FLAGSHIP_TRUTH_PLANE.md",
                    "EA_FLAGSHIP_RELEASE_GATE.json",
                    "EA_FLAGSHIP_RELEASE_GATE.generated.json",
                    "scripts/materialize_ea_flagship_release_gate.py",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    for rel in ("tests/test_propertyquarry_workspace_redesign.py", "tests/e2e/test_propertyquarry_greenfield_browser.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("# browser proof source\n", encoding="utf-8")
    source_binding = _commit_flagship_sources(root)
    if browser_proof_status is not None:
        browser_proof: dict[str, object] = {
            "contract_name": "ea.browser_workflow_proof",
            "kind": "proof_receipt",
            "surface": "browser_workflow_proof",
            "version": 1,
            "generated_at": "2026-07-13T12:00:00Z",
            "generated_by": "scripts/materialize_ea_browser_workflow_proof.py",
            "product": "propertyquarry",
            "status": browser_proof_status,
            "proof_target": "propertyquarry",
            "release_claim_summary": seed["release_claim"]["summary"],
            "expected_browser_signals": seed["browser_workflow_proof"][
                "expected_browser_signals"
            ],
            "source_binding": source_binding,
            "journey_evidence_matrix": _journey_matrix(
                source_file=sources[0]["file"],
                source_case=sources[0]["cases"][0],
                browser_file=sources[1]["file"],
                browser_case=sources[1]["cases"][0],
                receipt=True,
                runtime_commit_sha=str(source_binding["code_commit"]),
            ),
            "blocking_reasons": [],
            "current_limitations": [],
        }
        if browser_proof_status == "pass":
            browser_proof.update(
                {
                    "source_backed_journey_proof": _passing_browser_lane(
                        root,
                        test_file=sources[0]["file"],
                        cases=sources[0]["cases"],
                    ),
                    "real_browser_e2e_proof": _passing_browser_lane(
                        root,
                        test_file=sources[1]["file"],
                        cases=sources[1]["cases"],
                    ),
                }
            )
        (root / BROWSER_PROOF).write_text(
            json.dumps(browser_proof, indent=2) + "\n",
            encoding="utf-8",
        )


def test_materializer_writes_preview_only_receipt_without_browser_execution_receipt(tmp_path: Path) -> None:
    _write_minimal_flagship_tree(tmp_path)

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--seed",
            SEED.as_posix(),
            "--truth-plane",
            TRUTH_PLANE.as_posix(),
            "--output",
            OUTPUT.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads((tmp_path / OUTPUT).read_text(encoding="utf-8"))

    assert receipt["product"] == "propertyquarry"
    assert receipt["surface"] == "propertyquarry_flagship_release_control"
    assert receipt["version"] == 1
    assert receipt["status"] == "preview_only"
    assert len(receipt["source_binding"]["code_commit"]) == 40
    assert receipt["truth_plane"]["source"] == ".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md"
    assert receipt["ea_product_canon"]["source_root"] == ".codex-design/ea"
    assert receipt["ea_product_canon"]["scope_label"] == "EA product surface canon"
    assert receipt["ea_product_canon"]["all_required_docs_present"] is True
    assert receipt["browser_workflow_proof"]["proof_target"] == "propertyquarry"
    assert receipt["browser_workflow_proof"]["published_receipt_present"] is False
    assert receipt["browser_workflow_proof"]["source_files_present"][0]["present"] is True
    assert receipt["browser_workflow_proof"]["source_files_present"][1]["present"] is True
    assert receipt["journey_evidence_matrix"]["status"] == "not_evaluated"
    assert receipt["journey_evidence_matrix"]["runtime_commit_sha"] == receipt["source_binding"]["code_commit"]
    assert receipt["journey_evidence_matrix"]["required_journey_ids"] == JOURNEY_IDS
    assert all(row["proof_status"] == "not_evaluated" for row in receipt["journey_evidence_matrix"]["rows"])
    assert "no published browser execution receipt is attached yet" in receipt["current_limitations"]
    assert receipt["blocking_reasons"] == []
    assert "preview_only" in receipt["operator_summary"]


def test_materializer_can_publish_pass_when_browser_execution_receipt_exists(tmp_path: Path) -> None:
    _write_minimal_flagship_tree(tmp_path, browser_proof_status="pass")

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--seed",
            SEED.as_posix(),
            "--truth-plane",
            TRUTH_PLANE.as_posix(),
            "--output",
            OUTPUT.as_posix(),
            "--browser-proof-receipt",
            BROWSER_PROOF.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads((tmp_path / OUTPUT).read_text(encoding="utf-8"))

    assert receipt["status"] == "pass"
    browser_proof = json.loads((tmp_path / BROWSER_PROOF).read_text(encoding="utf-8"))
    assert receipt["source_binding"] == browser_proof["source_binding"]
    assert receipt["browser_receipt_binding"]["path"] == BROWSER_PROOF.as_posix()
    assert receipt["browser_workflow_proof"]["published_receipt_present"] is True
    assert receipt["browser_workflow_proof"]["published_receipt"] == BROWSER_PROOF.as_posix()
    assert receipt["current_limitations"] == []
    assert receipt["blocking_reasons"] == []
    assert receipt["ea_product_canon"]["all_required_docs_present"] is True
    assert receipt["journey_evidence_matrix"]["status"] == "pass"
    assert receipt["journey_evidence_matrix"]["runtime_commit_sha"] == receipt["source_binding"]["code_commit"]
    assert all(row["proof_status"] == "pass" for row in receipt["journey_evidence_matrix"]["rows"])
    assert "green" in receipt["operator_summary"]


def test_browser_lane_pass_requires_exact_integer_types_and_consistent_counts() -> None:
    test_file = "tests/test_propertyquarry_workspace_redesign.py"
    cases = ["test_propertyquarry_workspace_routes_render_greenfield_surfaces"]
    valid = _passing_browser_lane(Path("/candidate"), test_file=test_file, cases=cases)
    assert _browser_lane_pass_is_supported(
        valid,
        expected_test_file=test_file,
        expected_cases=cases,
    )

    mutations = {
        "boolean required count": ("required_case_count", True),
        "floating required count": ("required_case_count", 1.0),
        "string executed count": ("executed_count", "1"),
        "boolean exit code": ("exit_code", False),
        "negative selected count": ("selected_count", -1),
        "inconsistent selected count": ("selected_count", 2),
        "inconsistent executed count": ("executed_count", 2),
    }
    for label, (field, value) in mutations.items():
        lane = copy.deepcopy(valid)
        lane[field] = value
        assert not _browser_lane_pass_is_supported(
            lane,
            expected_test_file=test_file,
            expected_cases=cases,
        ), label


def test_browser_lane_pass_requires_exact_structured_outcome_counts() -> None:
    test_file = "tests/test_propertyquarry_workspace_redesign.py"
    cases = ["test_propertyquarry_workspace_routes_render_greenfield_surfaces"]
    valid = _passing_browser_lane(Path("/candidate"), test_file=test_file, cases=cases)

    invalid_passed_counts = [True, 1.0, "1", -1, 2, float("inf")]
    for value in invalid_passed_counts:
        lane = copy.deepcopy(valid)
        lane["outcome_counts"]["passed"] = value
        assert not _browser_lane_pass_is_supported(
            lane,
            expected_test_file=test_file,
            expected_cases=cases,
        ), repr(value)

    excerpt_only = copy.deepcopy(valid)
    excerpt_only.pop("outcome_counts")
    assert not _browser_lane_pass_is_supported(
        excerpt_only,
        expected_test_file=test_file,
        expected_cases=cases,
    )

    extra_outcome = copy.deepcopy(valid)
    extra_outcome["outcome_counts"]["warnings"] = 0
    assert not _browser_lane_pass_is_supported(
        extra_outcome,
        expected_test_file=test_file,
        expected_cases=cases,
    )


def test_browser_lane_pass_rejects_non_finite_duration() -> None:
    test_file = "tests/test_propertyquarry_workspace_redesign.py"
    cases = ["test_propertyquarry_workspace_routes_render_greenfield_surfaces"]
    lane = _passing_browser_lane(Path("/candidate"), test_file=test_file, cases=cases)
    lane["duration_seconds"] = float("nan")

    assert not _browser_lane_pass_is_supported(
        lane,
        expected_test_file=test_file,
        expected_cases=cases,
    )


def test_browser_receipt_pass_requires_exact_contract_and_source_binding_versions(tmp_path: Path) -> None:
    valid, seed = _load_passing_browser_contract(tmp_path)

    for field, value in (("version", True), ("version", 1.0), ("version", "1")):
        receipt = copy.deepcopy(valid)
        receipt[field] = value
        assert "published pass has the wrong browser proof version" in browser_receipt_pass_blockers(
            receipt,
            seed,
        )

    for value in (True, 1.0, "1", 0, -1):
        receipt = copy.deepcopy(valid)
        receipt["source_binding"]["version"] = value
        assert "published pass has the wrong immutable source binding version" in browser_receipt_pass_blockers(
            receipt,
            seed,
        )


def test_browser_receipt_pass_requires_current_exact_journey_matrix_version(tmp_path: Path) -> None:
    valid, seed = _load_passing_browser_contract(tmp_path)

    for value in (None, True, 1.0, "1", 0, -1, 2):
        receipt = copy.deepcopy(valid)
        receipt["journey_evidence_matrix"]["version"] = value
        assert "published pass journey matrix has the wrong version" in browser_receipt_pass_blockers(
            receipt,
            seed,
        )


def test_browser_receipt_pass_rejects_mixed_type_journey_lists(
    tmp_path: Path,
) -> None:
    valid, seed = _load_passing_browser_contract(tmp_path)
    receipt = copy.deepcopy(valid)
    malformed_seed = copy.deepcopy(seed)
    receipt["journey_evidence_matrix"]["required_journey_ids"].append(7)
    malformed_seed["journey_evidence_matrix"]["required_journey_ids"].append(7)

    assert (
        "published pass journey IDs must be governed lists"
        in browser_receipt_pass_blockers(receipt, malformed_seed)
    )

    receipt = copy.deepcopy(valid)
    malformed_seed = copy.deepcopy(seed)
    receipt["journey_evidence_matrix"]["rows"][0]["evidence_sources"][0][
        "cases"
    ].append(False)
    malformed_seed["journey_evidence_matrix"]["rows"][0]["evidence_sources"][0][
        "cases"
    ].append(False)

    assert (
        "published pass journey public_entry has stale evidence nodes"
        in browser_receipt_pass_blockers(receipt, malformed_seed)
    )


def test_browser_receipt_pass_rejects_noncanonical_runtime_commit(tmp_path: Path) -> None:
    valid, seed = _load_passing_browser_contract(tmp_path)
    receipt = copy.deepcopy(valid)
    receipt["source_binding"]["code_commit"] = True
    receipt["journey_evidence_matrix"]["runtime_commit_sha"] = True

    assert "published pass has an invalid immutable source binding commit" in browser_receipt_pass_blockers(
        receipt,
        seed,
    )

    canonical_commit = "a" * 40
    for runtime_commit in ("A" * 40, f" {canonical_commit} "):
        receipt = copy.deepcopy(valid)
        receipt["source_binding"]["code_commit"] = canonical_commit
        receipt["journey_evidence_matrix"]["runtime_commit_sha"] = runtime_commit
        assert (
            "published pass journey matrix is not bound to the browser receipt runtime commit"
            in browser_receipt_pass_blockers(receipt, seed)
        )


def test_materializer_surfaces_browser_proof_blockers_when_published_receipt_is_blocked(tmp_path: Path) -> None:
    _write_minimal_flagship_tree(tmp_path)
    (tmp_path / BROWSER_PROOF).write_text(
        json.dumps(
            {
                "status": "blocked",
                "blocking_reasons": [
                    "source-backed browser journey proof is not passing",
                    "real browser E2E proof is not passing",
                ],
                "current_limitations": [],
                "journey_evidence_matrix": {
                    "status": "blocked",
                    "runtime_commit_sha": "0" * 40,
                    "rows": [],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--seed",
            SEED.as_posix(),
            "--truth-plane",
            TRUTH_PLANE.as_posix(),
            "--output",
            OUTPUT.as_posix(),
            "--browser-proof-receipt",
            BROWSER_PROOF.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads((tmp_path / OUTPUT).read_text(encoding="utf-8"))

    assert receipt["status"] == "blocked"
    assert "browser workflow proof: source-backed browser journey proof is not passing" in receipt["blocking_reasons"]
    assert "browser workflow proof: real browser E2E proof is not passing" in receipt["blocking_reasons"]
    assert receipt["journey_evidence_matrix"]["status"] == "not_evaluated"
    assert receipt["journey_evidence_matrix"]["runtime_commit_sha"] == receipt["source_binding"]["code_commit"]


def test_materializer_blocks_internally_inconsistent_browser_pass_with_all_real_browser_cases_skipped(
    tmp_path: Path,
) -> None:
    _write_minimal_flagship_tree(tmp_path)
    (tmp_path / BROWSER_PROOF).write_text(
        json.dumps(
            {
                "contract_name": "ea.browser_workflow_proof",
                "product": "propertyquarry",
                "status": "pass",
                "proof_target": "propertyquarry",
                "blocking_reasons": [],
                "current_limitations": [],
                "source_backed_journey_proof": {
                    "status": "pass",
                    "test_file": "tests/test_propertyquarry_workspace_redesign.py",
                    "cases": ["test_propertyquarry_workspace_routes_render_greenfield_surfaces"],
                    "exit_code": 0,
                    "output_excerpt": ["1 passed"],
                    "limitations": [],
                },
                "real_browser_e2e_proof": {
                    "status": "pass",
                    "test_file": "tests/e2e/test_propertyquarry_greenfield_browser.py",
                    "cases": ["test_propertyquarry_greenfield_workspace_in_real_browser"],
                    "exit_code": 0,
                    "output_excerpt": ["1 skipped, 20 deselected in 0.79s"],
                    "limitations": ["real browser E2E did not run to completion"],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--seed",
            SEED.as_posix(),
            "--truth-plane",
            TRUTH_PLANE.as_posix(),
            "--output",
            OUTPUT.as_posix(),
            "--browser-proof-receipt",
            BROWSER_PROOF.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads((tmp_path / OUTPUT).read_text(encoding="utf-8"))

    assert receipt["status"] == "blocked"
    assert (
        "browser workflow proof: published pass lacks completed real browser E2E proof"
        in receipt["blocking_reasons"]
    )
    assert "green" not in receipt["operator_summary"]


def test_materializer_blocks_stale_pass_that_does_not_match_current_seed_nodes(tmp_path: Path) -> None:
    _write_minimal_flagship_tree(tmp_path, browser_proof_status="pass")
    stale = json.loads((tmp_path / BROWSER_PROOF).read_text(encoding="utf-8"))
    stale["real_browser_e2e_proof"]["cases"] = ["test_previous_release_browser_case"]
    stale["real_browser_e2e_proof"]["required_case_count"] = 1
    stale["real_browser_e2e_proof"]["executed_count"] = 1
    stale["real_browser_e2e_proof"]["outcome_counts"] = {"passed": 1}
    stale["journey_evidence_matrix"]["rows"].append("unexpected")
    (tmp_path / BROWSER_PROOF).write_text(json.dumps(stale), encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--seed",
            SEED.as_posix(),
            "--truth-plane",
            TRUTH_PLANE.as_posix(),
            "--output",
            OUTPUT.as_posix(),
            "--browser-proof-receipt",
            BROWSER_PROOF.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads((tmp_path / OUTPUT).read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked"
    assert (
        "browser workflow proof: published pass lacks completed real browser E2E proof"
        in receipt["blocking_reasons"]
    )
    assert (
        "browser workflow proof: published pass journey rows do not exactly cover the current matrix"
        in receipt["blocking_reasons"]
    )


def test_materializer_blocks_pass_with_a_tampered_journey_runtime_binding(tmp_path: Path) -> None:
    _write_minimal_flagship_tree(tmp_path, browser_proof_status="pass")
    tampered = json.loads((tmp_path / BROWSER_PROOF).read_text(encoding="utf-8"))
    tampered["journey_evidence_matrix"]["runtime_commit_sha"] = "0" * 40
    (tmp_path / BROWSER_PROOF).write_text(json.dumps(tampered), encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--seed",
            SEED.as_posix(),
            "--truth-plane",
            TRUTH_PLANE.as_posix(),
            "--output",
            OUTPUT.as_posix(),
            "--browser-proof-receipt",
            BROWSER_PROOF.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads((tmp_path / OUTPUT).read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked"
    assert (
        "browser workflow proof: published pass journey matrix is not bound to the browser receipt runtime commit"
        in receipt["blocking_reasons"]
    )


def test_materializer_blocks_ambiguous_duplicate_key_browser_receipt(
    tmp_path: Path,
) -> None:
    _write_minimal_flagship_tree(tmp_path, browser_proof_status="pass")
    browser_path = tmp_path / BROWSER_PROOF
    payload = browser_path.read_text(encoding="utf-8")
    ambiguous = payload.replace(
        '"status": "pass",',
        '"status": "blocked",\n  "status": "pass",',
        1,
    )
    assert ambiguous != payload
    browser_path.write_text(ambiguous, encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--seed",
            SEED.as_posix(),
            "--truth-plane",
            TRUTH_PLANE.as_posix(),
            "--output",
            OUTPUT.as_posix(),
            "--browser-proof-receipt",
            BROWSER_PROOF.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    receipt = json.loads((tmp_path / OUTPUT).read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked"
    assert any(
        reason.startswith("browser workflow proof:")
        for reason in receipt["blocking_reasons"]
    )


@pytest.mark.parametrize(
    "relative_path",
    (Path("../outside.json"), Path("/tmp/propertyquarry-outside.json")),
)
def test_flagship_stable_writer_rejects_output_escape(
    tmp_path: Path,
    relative_path: Path,
) -> None:
    with pytest.raises(ValueError, match="safe repository-relative path"):
        flagship_materializer._write_json_stable(
            relative_path,
            {"status": "blocked"},
            root=tmp_path,
        )


def test_flagship_stable_writer_rejects_symlinks_and_repairs_ambiguous_output(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text('{"preserve":true}\n', encoding="utf-8")
    destination = tmp_path / "receipt.json"
    destination.symlink_to(victim.name)

    with pytest.raises(ValueError, match="symlinked or unreadable"):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "blocked"},
            root=tmp_path,
        )
    assert victim.read_text(encoding="utf-8") == '{"preserve":true}\n'

    destination.unlink()
    destination.write_text(
        '{"status":"blocked","status":"pass"}\n',
        encoding="utf-8",
    )
    canonical = {"status": "pass"}
    flagship_materializer._write_json_stable(
        Path("receipt.json"),
        canonical,
        root=tmp_path,
    )
    assert json.loads(destination.read_text(encoding="utf-8")) == canonical
    assert destination.read_text(encoding="utf-8").count('"status"') == 1

    stale = {
        "status": "pass",
        "source_binding": {"seed": {"sha256": "a" * 64}},
    }
    payload = {
        "status": "pass",
        "source_binding": {"seed": {"sha256": "b" * 64}},
    }
    destination.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    flagship_materializer._write_json_stable(
        Path("receipt.json"),
        payload,
        root=tmp_path,
    )
    assert json.loads(destination.read_text(encoding="utf-8")) == payload

    external_parent = tmp_path / "external-parent"
    external_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external_parent.name, target_is_directory=True)
    with pytest.raises(ValueError, match="parent is symlinked"):
        flagship_materializer._write_json_stable(
            Path("linked-parent/receipt.json"),
            payload,
            root=tmp_path,
        )
    assert not (external_parent / "receipt.json").exists()

    (tmp_path / "directory-output").mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        flagship_materializer._write_json_stable(
            Path("directory-output"),
            payload,
            root=tmp_path,
        )
    (tmp_path / "file-parent").write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(ValueError, match="parent is symlinked or not a directory"):
        flagship_materializer._write_json_stable(
            Path("file-parent/receipt.json"),
            payload,
            root=tmp_path,
        )

    flagship_materializer._write_json_stable(
        Path("new/canonical/receipt.json"),
        payload,
        root=tmp_path,
    )
    created = tmp_path / "new/canonical/receipt.json"
    assert json.loads(created.read_text(encoding="utf-8")) == payload
    assert created.stat().st_mode & 0o777 == 0o644


def test_flagship_stable_writer_repairs_equal_payload_mode_with_cas(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "receipt.json"
    payload = {"status": "pass"}
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o777)

    flagship_materializer._write_json_stable(
        Path("receipt.json"),
        payload,
        root=tmp_path,
    )

    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert destination.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("mutation", ("in_place", "replacement"))
def test_flagship_stable_writer_preserves_final_window_destination_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    destination = tmp_path / "receipt.json"
    destination.write_bytes(b'{"status":"approved"}\n')
    concurrent_bytes = b'{"status":"concurrent-operator-edit"}\n'
    original_exchange = flagship_materializer._rename_exchange
    exchange_calls = 0

    def exchange_with_destination_edit(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            if mutation == "in_place":
                destination.write_bytes(concurrent_bytes)
            else:
                replacement = tmp_path / "operator-replacement.json"
                replacement.write_bytes(concurrent_bytes)
                replacement.replace(destination)
        original_exchange(parent_fd, source, destination_name)

    monkeypatch.setattr(
        flagship_materializer,
        "_rename_exchange",
        exchange_with_destination_edit,
    )

    with pytest.raises(RuntimeError, match="changed before publication"):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert destination.read_bytes() == concurrent_bytes


@pytest.mark.parametrize("destination_exists", (True, False))
def test_flagship_stable_writer_detects_staged_path_substitution_at_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_exists: bool,
) -> None:
    destination = tmp_path / "receipt.json"
    approved_bytes = b'{"status":"approved"}\n'
    substituted_bytes = b'{"status":"substituted-staging-path"}\n'
    if destination_exists:
        destination.write_bytes(approved_bytes)
        helper_name = "_rename_exchange"
    else:
        helper_name = "_rename_noreplace"
    original_rename = getattr(flagship_materializer, helper_name)
    rename_calls = 0

    def rename_with_staged_substitution(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            replacement = tmp_path / "staging-substitute.json"
            replacement.write_bytes(substituted_bytes)
            replacement.replace(tmp_path / source)
        original_rename(parent_fd, source, destination_name)

    monkeypatch.setattr(
        flagship_materializer,
        helper_name,
        rename_with_staged_substitution,
    )

    with pytest.raises(
        flagship_materializer._PreserveStagedOutputError,
        match="staging path changed",
    ):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    if destination_exists:
        assert destination.read_bytes() == approved_bytes
        recovery = [
            path
            for path in tmp_path.iterdir()
            if path.name.startswith(".receipt.json.release-write-")
        ]
        assert [path.read_bytes() for path in recovery] == [substituted_bytes]
    else:
        assert not destination.exists()
        recovery = [
            path
            for path in tmp_path.iterdir()
            if path.name.startswith(".receipt.json.release-write-")
        ]
        assert [path.read_bytes() for path in recovery] == [substituted_bytes]


def test_flagship_stable_writer_preserves_displaced_data_if_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "receipt.json"
    destination.write_bytes(b'{"status":"approved"}\n')
    concurrent_bytes = b'{"status":"concurrent-before-failed-rollback"}\n'
    original_exchange = flagship_materializer._rename_exchange
    exchange_calls = 0

    def exchange_with_failed_rollback(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            destination.write_bytes(concurrent_bytes)
            original_exchange(parent_fd, source, destination_name)
            return
        raise OSError(errno.EIO, "injected exchange rollback failure")

    monkeypatch.setattr(
        flagship_materializer,
        "_rename_exchange",
        exchange_with_failed_rollback,
    )

    with pytest.raises(
        flagship_materializer._PreserveStagedOutputError,
        match="rollback failed",
    ):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "fresh"}
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".receipt.json.release-write-")
    ]
    assert [path.read_bytes() for path in recovery] == [concurrent_bytes]


@pytest.mark.parametrize("destination_exists", (True, False))
def test_flagship_stable_writer_rolls_back_staged_symlink_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_exists: bool,
) -> None:
    destination = tmp_path / "receipt.json"
    approved_bytes = b'{"status":"approved"}\n'
    victim = tmp_path / "symlink-victim.json"
    victim_bytes = b'{"preserve":"symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    if destination_exists:
        destination.write_bytes(approved_bytes)
        helper_name = "_rename_exchange"
    else:
        helper_name = "_rename_noreplace"
    original_rename = getattr(flagship_materializer, helper_name)
    rename_calls = 0

    def rename_with_staged_symlink(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            staged_path = tmp_path / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
        original_rename(parent_fd, source, destination_name)

    monkeypatch.setattr(
        flagship_materializer,
        helper_name,
        rename_with_staged_symlink,
    )

    with pytest.raises(flagship_materializer._PreserveStagedOutputError):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    if destination_exists:
        assert not destination.is_symlink()
        assert destination.read_bytes() == approved_bytes
    else:
        assert not destination.exists()
        assert not destination.is_symlink()
    assert victim.read_bytes() == victim_bytes
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".receipt.json.release-write-")
    ]
    assert len(recovery) == 1
    assert recovery[0].is_symlink()
    assert recovery[0].readlink() == Path(victim.name)


def test_flagship_stable_writer_preserves_displaced_bytes_when_symlink_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "receipt.json"
    approved_bytes = b'{"status":"approved"}\n'
    destination.write_bytes(approved_bytes)
    victim = tmp_path / "symlink-victim.json"
    victim_bytes = b'{"preserve":"symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    original_exchange = flagship_materializer._rename_exchange
    exchange_calls = 0

    def exchange_with_failed_symlink_rollback(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            staged_path = tmp_path / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
            original_exchange(parent_fd, source, destination_name)
            return
        raise OSError(errno.EIO, "injected symlink rollback failure")

    monkeypatch.setattr(
        flagship_materializer,
        "_rename_exchange",
        exchange_with_failed_symlink_rollback,
    )

    with pytest.raises(
        flagship_materializer._PreserveStagedOutputError,
        match="rollback failed",
    ):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert destination.is_symlink()
    assert destination.readlink() == Path(victim.name)
    assert victim.read_bytes() == victim_bytes
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".receipt.json.release-write-")
    ]
    assert [path.read_bytes() for path in recovery] == [approved_bytes]


def test_flagship_stable_writer_preserves_symlink_when_noreplace_quarantine_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "receipt.json"
    victim = tmp_path / "symlink-victim.json"
    victim_bytes = b'{"preserve":"symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    original_noreplace = flagship_materializer._rename_noreplace
    rename_calls = 0

    def noreplace_with_failed_quarantine(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            staged_path = tmp_path / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
            original_noreplace(parent_fd, source, destination_name)
            return
        raise OSError(errno.EIO, "injected noreplace quarantine failure")

    monkeypatch.setattr(
        flagship_materializer,
        "_rename_noreplace",
        noreplace_with_failed_quarantine,
    )

    with pytest.raises(
        flagship_materializer._PreserveStagedOutputError,
        match="could not be quarantined",
    ):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert destination.is_symlink()
    assert destination.readlink() == Path(victim.name)
    assert victim.read_bytes() == victim_bytes


def test_flagship_stable_writer_preserves_quarantine_after_identity_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "receipt.json"
    concurrent_bytes = b'{"status":"concurrent-staged-entry"}\n'
    original_noreplace = flagship_materializer._rename_noreplace
    rename_calls = 0
    opened_parent_fds: list[int] = []
    original_open_parent = flagship_materializer._open_output_parent

    def capture_parent_fd(root: Path, relative_path: Path) -> int:
        parent_fd = original_open_parent(root, relative_path)
        opened_parent_fds.append(parent_fd)
        return parent_fd

    def noreplace_with_staged_replacement(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            replacement = tmp_path / "concurrent-staged-entry.json"
            replacement.write_bytes(concurrent_bytes)
            replacement.replace(tmp_path / source)
        original_noreplace(parent_fd, source, destination_name)

    monkeypatch.setattr(
        flagship_materializer,
        "_open_output_parent",
        capture_parent_fd,
    )
    monkeypatch.setattr(
        flagship_materializer,
        "_rename_noreplace",
        noreplace_with_staged_replacement,
    )
    monkeypatch.setattr(
        flagship_materializer,
        "_entry_identity_no_follow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EIO, "injected post-quarantine identity failure")
        ),
    )

    with pytest.raises(flagship_materializer._PreserveStagedOutputError):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert not destination.exists()
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".receipt.json.release-write-")
    ]
    assert [path.read_bytes() for path in recovery] == [concurrent_bytes]
    assert opened_parent_fds
    for parent_fd in opened_parent_fds:
        with pytest.raises(OSError):
            os.fstat(parent_fd)


def test_flagship_stable_writer_preserves_rollback_entry_after_identity_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "receipt.json"
    approved_bytes = b'{"status":"approved"}\n'
    destination.write_bytes(approved_bytes)
    victim = tmp_path / "symlink-victim.json"
    victim_bytes = b'{"preserve":"symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    original_exchange = flagship_materializer._rename_exchange
    exchange_calls = 0

    def exchange_with_staged_symlink(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            staged_path = tmp_path / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
        original_exchange(parent_fd, source, destination_name)

    monkeypatch.setattr(
        flagship_materializer,
        "_rename_exchange",
        exchange_with_staged_symlink,
    )
    monkeypatch.setattr(
        flagship_materializer,
        "_entry_identity_no_follow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EIO, "injected post-rollback identity failure")
        ),
    )

    with pytest.raises(flagship_materializer._PreserveStagedOutputError):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert not destination.is_symlink()
    assert destination.read_bytes() == approved_bytes
    assert victim.read_bytes() == victim_bytes
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".receipt.json.release-write-")
    ]
    assert len(recovery) == 1
    assert recovery[0].is_symlink()
    assert recovery[0].readlink() == Path(victim.name)


def test_flagship_stable_writer_preserves_new_destination_racing_noreplace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "receipt.json"
    concurrent_bytes = b'{"status":"concurrent-new-destination"}\n'
    original_noreplace = flagship_materializer._rename_noreplace

    def noreplace_after_destination_appears(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        destination.write_bytes(concurrent_bytes)
        original_noreplace(parent_fd, source, destination_name)

    monkeypatch.setattr(
        flagship_materializer,
        "_rename_noreplace",
        noreplace_after_destination_appears,
    )

    with pytest.raises(RuntimeError, match="appeared during publication"):
        flagship_materializer._write_json_stable(
            Path("receipt.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert destination.read_bytes() == concurrent_bytes


def test_browser_receipt_blockers_fail_closed_on_malformed_seed_sources() -> None:
    blockers = browser_receipt_pass_blockers(
        {
            "status": "pass",
            "product": "propertyquarry",
            "proof_target": "propertyquarry",
            "blocking_reasons": [],
            "current_limitations": [],
        },
        {
            "product": "propertyquarry",
            "browser_workflow_proof": {
                "proof_target": "propertyquarry",
                "evidence_sources": 1,
            },
        },
    )

    assert "current gate seed browser evidence sources must be a governed list" in blockers
    rendered, missing = flagship_materializer._build_browser_sources(
        Path("/docker/property"),
        {"browser_workflow_proof": 1},
    )
    assert rendered == []
    assert missing == ["invalid browser evidence source list"]


def test_flagship_materializer_turns_binding_type_error_into_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_flagship_tree(tmp_path)

    def malformed_binding(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TypeError("malformed nested evidence cases")

    monkeypatch.setattr(
        flagship_materializer,
        "build_source_binding",
        malformed_binding,
    )
    receipt = flagship_materializer.build_receipt(
        tmp_path,
        require_source_binding=True,
    )

    assert receipt["status"] == "blocked"
    assert (
        "immutable source binding failed: malformed nested evidence cases"
        in receipt["blocking_reasons"]
    )
