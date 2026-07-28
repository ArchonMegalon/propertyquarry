from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

import scripts.check_property_release_hygiene as release_hygiene
import scripts.verify_generated_release_artifacts_clean as generated_release_artifacts
from scripts.materialize_ea_browser_workflow_proof import build_receipt as build_browser_receipt
from scripts.materialize_ea_flagship_release_gate import build_receipt as build_flagship_receipt
from scripts.propertyquarry_release_receipt_binding import (
    CANONICAL_BROWSER_RECEIPT,
    CANONICAL_FLAGSHIP_RECEIPT,
    CANONICAL_RELEASE_MANIFEST,
    CANONICAL_SEED,
    CANONICAL_WEEKLY_PULSE,
    file_digest_binding,
    sha256_bytes,
)
from scripts.verify_propertyquarry_deploy_receipts import verify_deploy_receipts


ROOT = Path(__file__).resolve().parents[1]
TRUTH_PLANE = Path(".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md")
RELEASE_DOCS = (
    "README.md",
    "RUNBOOK.md",
    "RELEASE_CHECKLIST.md",
    "PRODUCT_RELEASE_CHECKLIST.md",
)
SOURCE_FILE = "tests/test_propertyquarry_workspace_redesign.py"
SOURCE_CASES = [
    "test_propertyquarry_workspace_routes_render_greenfield_surfaces",
    "test_propertyquarry_failed_run_stays_on_activity_surface",
    "test_property_workspace_sign_out_clears_workspace_session_cookie",
    "test_property_saved_shortlist_candidates_persist_across_runs",
    "test_propertyquarry_account_exposes_working_lifecycle_controls",
    "test_propertyquarry_pricing_checkout_failure_copy_is_safe_and_accessible",
    "test_propertyquarry_public_home_survives_unreadable_optional_tour_media",
]
BROWSER_FILE = "tests/e2e/test_propertyquarry_greenfield_browser.py"
BROWSER_CASES = [
    "test_propertyquarry_greenfield_workspace_in_real_browser",
    "test_propertyquarry_greenfield_workspace_is_mobile_usable",
    "test_propertyquarry_expired_session_next_action_moves_keyboard_focus_to_sign_in_options",
    "test_propertyquarry_workbench_candidate_history_stays_in_place",
    "test_propertyquarry_flagship_operating_loop_in_browser",
    "test_propertyquarry_decision_to_clippy_to_packet_followup_flow_in_browser",
    "test_propertyquarry_packet_tracks_followup_state_in_browser",
    "test_propertyquarry_account_notifications_save_multi_channel_preferences_in_real_browser",
    "test_propertyquarry_browser_alert_button_toggles_enabled_state",
]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str = "proof fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _seed() -> dict[str, Any]:
    return {
        "product": "propertyquarry",
        "surface": "propertyquarry_flagship_release_control",
        "version": 1,
        "truth_plane": {
            "source": TRUTH_PLANE.as_posix(),
            "legacy_history": "MILESTONE.json",
        },
        "release_claim": {
            "summary": "PropertyQuarry is releasable only when current proof receipts match this seed.",
            "required_conditions": ["current browser proof passes"],
        },
        "ea_product_canon": {
            "source_root": ".codex-design/ea",
            "scope_label": "EA product surface canon",
            "required_docs": [".codex-design/ea/START_HERE.md"],
        },
        "browser_workflow_proof": {
            "proof_target": "propertyquarry",
            "evidence_sources": [
                {"file": SOURCE_FILE, "cases": SOURCE_CASES},
                {"file": BROWSER_FILE, "cases": BROWSER_CASES},
            ],
            "expected_browser_signals": ["/app/properties", "/app/research", "mobile"],
        },
        "journey_evidence_matrix": {
            "version": 1,
            "readiness_scope": "candidate_source_and_browser_proof",
            "required_journey_ids": [
                "public_entry",
                "onboarding_auth",
                "search_ranking",
                "shortlist_research_revisit",
                "account_pricing_privacy_recovery",
                "packets_tours",
                "feedback",
                "notifications",
            ],
            "rows": [
                {
                    "journey_id": "public_entry",
                    "label": "Public entry and optional media recovery",
                    "evidence_sources": [
                        {
                            "file": SOURCE_FILE,
                            "cases": [
                                "test_propertyquarry_public_home_survives_unreadable_optional_tour_media"
                            ],
                        }
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-public-release-gate.json",
                        "required_profile": "launch",
                    },
                },
                {
                    "journey_id": "onboarding_auth",
                    "label": "Onboarding, authentication, session return, and sign-out",
                    "evidence_sources": [
                        {
                            "file": SOURCE_FILE,
                            "cases": [
                                "test_property_workspace_sign_out_clears_workspace_session_cookie"
                            ],
                        },
                        {
                            "file": BROWSER_FILE,
                            "cases": [
                                "test_propertyquarry_expired_session_next_action_moves_keyboard_focus_to_sign_in_options"
                            ],
                        },
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-activation-to-value-*.json",
                        "required_profile": "launch",
                    },
                },
                {
                    "journey_id": "search_ranking",
                    "label": "Search setup, ranked results, and failed-run recovery",
                    "evidence_sources": [
                        {
                            "file": SOURCE_FILE,
                            "cases": [
                                "test_propertyquarry_workspace_routes_render_greenfield_surfaces",
                                "test_propertyquarry_failed_run_stays_on_activity_surface",
                            ],
                        },
                        {
                            "file": BROWSER_FILE,
                            "cases": [
                                "test_propertyquarry_greenfield_workspace_in_real_browser"
                            ],
                        },
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-authenticated-release-gate.json",
                        "required_profile": "launch",
                    },
                },
                {
                    "journey_id": "shortlist_research_revisit",
                    "label": "Shortlist, research detail, persistence, and revisit",
                    "evidence_sources": [
                        {
                            "file": SOURCE_FILE,
                            "cases": [
                                "test_property_saved_shortlist_candidates_persist_across_runs"
                            ],
                        },
                        {
                            "file": BROWSER_FILE,
                            "cases": [
                                "test_propertyquarry_greenfield_workspace_is_mobile_usable",
                                "test_propertyquarry_workbench_candidate_history_stays_in_place",
                            ],
                        },
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-mobile-release-gate.json",
                        "required_profile": "launch",
                    },
                },
                {
                    "journey_id": "account_pricing_privacy_recovery",
                    "label": "Account, pricing, privacy lifecycle, and recovery",
                    "evidence_sources": [
                        {
                            "file": SOURCE_FILE,
                            "cases": [
                                "test_propertyquarry_account_exposes_working_lifecycle_controls",
                                "test_propertyquarry_pricing_checkout_failure_copy_is_safe_and_accessible",
                            ],
                        }
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-authenticated-release-gate.json",
                        "required_profile": "launch",
                    },
                },
                {
                    "journey_id": "packets_tours",
                    "label": "Decision packets, sharing, tours, and recovery",
                    "evidence_sources": [
                        {
                            "file": BROWSER_FILE,
                            "cases": [
                                "test_propertyquarry_flagship_operating_loop_in_browser"
                            ],
                        }
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-public-release-gate.json",
                        "required_profile": "launch",
                    },
                },
                {
                    "journey_id": "feedback",
                    "label": "Decision feedback and packet follow-up",
                    "evidence_sources": [
                        {
                            "file": BROWSER_FILE,
                            "cases": [
                                "test_propertyquarry_decision_to_clippy_to_packet_followup_flow_in_browser",
                                "test_propertyquarry_packet_tracks_followup_state_in_browser",
                            ],
                        }
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-authenticated-release-gate.json",
                        "required_profile": "launch",
                    },
                },
                {
                    "journey_id": "notifications",
                    "label": "Alert controls, channel preferences, and external delivery",
                    "evidence_sources": [
                        {
                            "file": BROWSER_FILE,
                            "cases": [
                                "test_propertyquarry_account_notifications_save_multi_channel_preferences_in_real_browser",
                                "test_propertyquarry_browser_alert_button_toggles_enabled_state",
                            ],
                        }
                    ],
                    "live_requirement": {
                        "status": "not_evaluated",
                        "authority": "_completion/smoke/property-live-notification-delivery.json",
                        "required_profile": "launch",
                    },
                },
            ],
        },
        "verification_binding": {
            "primary_verifier": "scripts/verify_release_assets.sh",
            "supporting_test": "tests/test_flagship_truth_plane.py",
        },
    }


def _passing_runner(
    root: Path,
    *,
    python_bin: str,
    test_file: str,
    cases: list[str],
    real_browser: bool,
) -> dict[str, Any]:
    del python_bin, real_browser
    return {
        "status": "pass",
        "command": "pytest",
        "cwd": root.as_posix(),
        "python_bin": "python3",
        "test_file": test_file,
        "cases": cases,
        "required_case_count": len(cases),
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
        "duration_seconds": 1.0,
        "output_excerpt": [f"{len(cases)} passed"],
        "limitations": [],
    }


def _prepare_code_commit(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "PropertyQuarry Test")
    _git(root, "config", "user.email", "propertyquarry-test@example.invalid")
    _write_json(root / CANONICAL_SEED, _seed())
    _write_text(root / TRUTH_PLANE)
    _write_text(root / ".codex-design/ea/START_HERE.md")
    for path in RELEASE_DOCS:
        _write_text(root / path)
    _write_text(root / SOURCE_FILE)
    _write_text(root / BROWSER_FILE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "immutable code proof parent")
    return _git(root, "rev-parse", "HEAD")


def _canonical_receipt_repo(
    tmp_path: Path,
    *,
    mutate: Callable[[Path, dict[str, Any], dict[str, Any]], None] | None = None,
) -> tuple[Path, str, str, str]:
    root = tmp_path / "propertyquarry"
    code_parent = _prepare_code_commit(root)
    browser = build_browser_receipt(
        root,
        seed_path=CANONICAL_SEED,
        runner=_passing_runner,
        require_source_binding=True,
    )
    assert browser["status"] == "pass", browser
    _write_json(root / CANONICAL_BROWSER_RECEIPT, browser)
    flagship = build_flagship_receipt(
        root,
        seed_path=CANONICAL_SEED,
        truth_plane_path=TRUTH_PLANE,
        browser_proof_receipt_path=CANONICAL_BROWSER_RECEIPT,
        require_source_binding=True,
    )
    assert flagship["status"] == "pass", flagship
    if mutate is not None:
        mutate(root, browser, flagship)
    _write_json(root / CANONICAL_BROWSER_RECEIPT, browser)
    if mutate is not None:
        flagship["browser_receipt_binding"] = file_digest_binding(root, CANONICAL_BROWSER_RECEIPT)
    _write_json(root / CANONICAL_FLAGSHIP_RECEIPT, flagship)
    _git(
        root,
        "add",
        "-f",
        CANONICAL_BROWSER_RECEIPT.as_posix(),
        CANONICAL_FLAGSHIP_RECEIPT.as_posix(),
    )
    _git(root, "commit", "-q", "-m", "canonical release receipt metadata")
    receipt_commit = _git(root, "rev-parse", "HEAD")
    assert _git(root, "rev-parse", "HEAD^") == code_parent
    manifest_values = dict(generated_release_artifacts.RELEASE_MANIFEST_STATIC_VALUES)
    manifest_values.update({
        "release_commit_sha": receipt_commit,
        "release_artifact_set": (
            "propertyquarry-generated-release-artifacts-v1@sha256:" + "a" * 64
        ),
        "release_label": "synthetic-canonical-release-envelope",
        "release_generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "release_deployment_id": "pending-synthetic-verified-deploy",
    })
    manifest = f"""# PropertyQuarry Release Manifest

| Field | Value |
| --- | --- |
| Product | PropertyQuarry |
| Runtime commit SHA | `{receipt_commit}` |

{generated_release_artifacts.RELEASE_MANIFEST_JSON_START}
```json
{json.dumps(manifest_values, indent=2, sort_keys=True)}
```
{generated_release_artifacts.RELEASE_MANIFEST_JSON_END}
"""
    _write_text(root / CANONICAL_RELEASE_MANIFEST, manifest)
    flagship_bytes = (root / CANONICAL_FLAGSHIP_RECEIPT).read_bytes()
    pulse = {
        "contract_name": "ea.weekly_product_pulse",
        "contract_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "release_truth_source": CANONICAL_FLAGSHIP_RECEIPT.as_posix(),
        "release_truth_provenance": {
            "source_path": (root / CANONICAL_FLAGSHIP_RECEIPT).as_posix(),
            "resolved_path": (root / CANONICAL_FLAGSHIP_RECEIPT).resolve().as_posix(),
            "sha256": sha256_bytes(flagship_bytes),
            "git_repo_root": root.as_posix(),
            "git_head": receipt_commit,
            "repo_relative_path": CANONICAL_FLAGSHIP_RECEIPT.as_posix(),
        },
        "supporting_signals": {
            "flagship_release_receipt_source": CANONICAL_FLAGSHIP_RECEIPT.as_posix(),
            "flagship_release_receipt_git_head": receipt_commit,
        },
    }
    _write_json(root / CANONICAL_WEEKLY_PULSE, pulse)
    _git(
        root,
        "add",
        "-f",
        CANONICAL_RELEASE_MANIFEST.as_posix(),
        CANONICAL_WEEKLY_PULSE.as_posix(),
    )
    _git(root, "commit", "-q", "-m", "governed manifest and weekly pulse metadata")
    deploy_head = _git(root, "rev-parse", "HEAD")
    assert _git(root, "rev-parse", "HEAD^") == receipt_commit
    assert not _git(root, "status", "--porcelain")
    return root, deploy_head, receipt_commit, code_parent


def _unbound_browser_receipt(seed: dict[str, Any]) -> dict[str, Any]:
    sources = seed["browser_workflow_proof"]["evidence_sources"]
    seed_matrix = seed["journey_evidence_matrix"]

    def lane(source: dict[str, Any]) -> dict[str, Any]:
        cases = list(source["cases"])
        return {
            "status": "pass",
            "test_file": source["file"],
            "cases": cases,
            "required_case_count": len(cases),
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
            "limitations": [],
        }

    journey_rows = [
        {
            "journey_id": row["journey_id"],
            "label": row["label"],
            "proof_status": "pass",
            "evidence_sources": [
                {
                    "file": source["file"],
                    "cases": list(source["cases"]),
                    "lane_status": "pass",
                }
                for source in row["evidence_sources"]
            ],
            "live_requirement": dict(row["live_requirement"]),
            "blocking_reasons": [],
        }
        for row in seed_matrix["rows"]
    ]

    return {
        "contract_name": "ea.browser_workflow_proof",
        "product": seed["product"],
        "surface": "browser_workflow_proof",
        "proof_target": seed["browser_workflow_proof"]["proof_target"],
        "version": 1,
        "kind": "proof_receipt",
        "generated_by": "scripts/materialize_ea_browser_workflow_proof.py",
        "status": "pass",
        "release_claim_summary": seed["release_claim"]["summary"],
        "expected_browser_signals": seed["browser_workflow_proof"]["expected_browser_signals"],
        "source_backed_journey_proof": lane(sources[0]),
        "real_browser_e2e_proof": lane(sources[1]),
        "journey_evidence_matrix": {
            "version": seed_matrix["version"],
            "status": "pass",
            "readiness_scope": seed_matrix["readiness_scope"],
            "runtime_commit_sha": "",
            "required_journey_ids": list(seed_matrix["required_journey_ids"]),
            "rows": journey_rows,
        },
        "blocking_reasons": [],
        "current_limitations": [],
    }


def test_browser_pass_never_overwrites_truth_or_source_blockers(tmp_path: Path) -> None:
    seed = _seed()
    _write_json(tmp_path / CANONICAL_SEED, seed)
    _write_json(tmp_path / CANONICAL_BROWSER_RECEIPT, _unbound_browser_receipt(seed))
    for path in RELEASE_DOCS:
        _write_text(tmp_path / path)
    _write_text(tmp_path / ".codex-design/ea/START_HERE.md")
    _write_text(tmp_path / SOURCE_FILE)

    receipt = build_flagship_receipt(
        tmp_path,
        seed_path=CANONICAL_SEED,
        truth_plane_path=TRUTH_PLANE,
        browser_proof_receipt_path=CANONICAL_BROWSER_RECEIPT,
    )

    assert receipt["status"] == "blocked"
    assert f"missing truth plane: {TRUTH_PLANE.as_posix()}" in receipt["blocking_reasons"]
    assert any(reason.startswith("missing browser proof sources:") for reason in receipt["blocking_reasons"])


def test_flagship_materializer_blocks_browser_blockers_and_limitations(tmp_path: Path) -> None:
    seed = _seed()
    browser = _unbound_browser_receipt(seed)
    browser["blocking_reasons"] = ["provider lane failed"]
    browser["current_limitations"] = ["mobile proof incomplete"]
    browser["kind"] = "legacy_proof"
    browser["surface"] = "executive_assistant"
    browser["version"] = 9
    browser["generated_by"] = "legacy_materializer.py"
    browser["release_claim_summary"] = "old claim"
    browser["expected_browser_signals"] = ["/legacy"]
    _write_json(tmp_path / CANONICAL_SEED, seed)
    _write_json(tmp_path / CANONICAL_BROWSER_RECEIPT, browser)
    _write_text(tmp_path / TRUTH_PLANE)
    _write_text(tmp_path / ".codex-design/ea/START_HERE.md")
    for path in RELEASE_DOCS:
        _write_text(tmp_path / path)
    _write_text(tmp_path / SOURCE_FILE)
    _write_text(tmp_path / BROWSER_FILE)

    receipt = build_flagship_receipt(
        tmp_path,
        seed_path=CANONICAL_SEED,
        truth_plane_path=TRUTH_PLANE,
        browser_proof_receipt_path=CANONICAL_BROWSER_RECEIPT,
    )

    assert receipt["status"] == "blocked"
    blockers = "\n".join(receipt["blocking_reasons"])
    assert "provider lane failed" in blockers
    assert "mobile proof incomplete" in blockers
    assert "wrong browser proof receipt kind" in blockers
    assert "wrong browser proof surface" in blockers
    assert "wrong browser proof version" in blockers
    assert "governed browser proof materializer" in blockers
    assert "release claim does not match" in blockers
    assert "browser signals do not match" in blockers


def test_canonical_three_commit_release_envelope_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, deploy_head, receipt_commit, code_parent = _canonical_receipt_repo(tmp_path)

    assert verify_deploy_receipts(
        root=root,
        expected_head=deploy_head,
        expected_receipt_commit=receipt_commit,
        expected_code_parent=code_parent,
    ) == []
    monkeypatch.setattr(release_hygiene, "ROOT", root)
    hygiene = release_hygiene.build_release_hygiene_receipt()
    assert hygiene["status"] == "pass", hygiene
    assert hygiene["head_commit"] == deploy_head
    assert hygiene["parent_commit"] == receipt_commit
    assert hygiene["manifest_runtime_commit"] == receipt_commit


def test_canonical_verifier_rejects_stale_legacy_ea_pass(tmp_path: Path) -> None:
    def mutate(_root: Path, browser: dict[str, Any], _flagship: dict[str, Any]) -> None:
        browser["product"] = "executive-assistant"
        browser["proof_target"] = "executive-assistant"

    root, deploy_head, receipt_commit, code_parent = _canonical_receipt_repo(tmp_path, mutate=mutate)
    issues = verify_deploy_receipts(
        root=root,
        expected_head=deploy_head,
        expected_receipt_commit=receipt_commit,
        expected_code_parent=code_parent,
    )

    assert any("targets product executive-assistant, expected propertyquarry" in issue for issue in issues)
    assert any("targets executive-assistant, expected propertyquarry" in issue for issue in issues)


def test_canonical_verifier_rejects_receipt_limitations(tmp_path: Path) -> None:
    def mutate(_root: Path, browser: dict[str, Any], _flagship: dict[str, Any]) -> None:
        browser["current_limitations"] = ["browser evidence is incomplete"]

    root, deploy_head, receipt_commit, code_parent = _canonical_receipt_repo(tmp_path, mutate=mutate)
    issues = verify_deploy_receipts(
        root=root,
        expected_head=deploy_head,
        expected_receipt_commit=receipt_commit,
        expected_code_parent=code_parent,
    )

    assert any("browser evidence is incomplete" in issue for issue in issues)


def test_canonical_verifier_rejects_symlinked_receipt_path(tmp_path: Path) -> None:
    root, deploy_head, receipt_commit, code_parent = _canonical_receipt_repo(tmp_path)
    browser_path = root / CANONICAL_BROWSER_RECEIPT
    external = tmp_path / "substituted-browser.json"
    external.write_bytes(browser_path.read_bytes())
    browser_path.unlink()
    browser_path.symlink_to(external)

    issues = verify_deploy_receipts(
        root=root,
        expected_head=deploy_head,
        expected_receipt_commit=receipt_commit,
        expected_code_parent=code_parent,
    )

    assert any("symlink" in issue for issue in issues)


def test_canonical_verifier_rejects_older_same_contract_under_new_head(tmp_path: Path) -> None:
    root, _deploy_head, _receipt_commit, _code_parent = _canonical_receipt_repo(tmp_path)
    _write_text(root / "unrelated.txt", "not receipt metadata\n")
    _git(root, "add", "unrelated.txt")
    _git(root, "commit", "-q", "-m", "unrelated newer commit")
    newer_head = _git(root, "rev-parse", "HEAD")
    immediate_parent = _git(root, "rev-parse", "HEAD^")
    immediate_grandparent = _git(root, "rev-parse", "HEAD^^")

    issues = verify_deploy_receipts(
        root=root,
        expected_head=newer_head,
        expected_receipt_commit=immediate_parent,
        expected_code_parent=immediate_grandparent,
    )

    assert "receipt metadata commit must change exactly the canonical browser and flagship receipts" in issues
    assert "deploy metadata commit must change exactly the canonical release manifest and weekly pulse" in issues
    assert any("exact code parent" in issue for issue in issues)


def test_canonical_verifier_rejects_stale_receipt_and_metadata_window(tmp_path: Path) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")

    def mutate(_root: Path, browser: dict[str, Any], flagship: dict[str, Any]) -> None:
        browser["generated_at"] = stale
        flagship["generated_at"] = stale

    root, deploy_head, receipt_commit, code_parent = _canonical_receipt_repo(tmp_path, mutate=mutate)
    issues = verify_deploy_receipts(
        root=root,
        expected_head=deploy_head,
        expected_receipt_commit=receipt_commit,
        expected_code_parent=code_parent,
        max_age_seconds=86_400,
    )

    assert "browser workflow proof is stale" in issues
    assert "flagship release receipt is stale" in issues


def test_make_deploy_uses_canonical_receipts_and_rejects_staging_live_identity() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "deploy_propertyquarry.sh").read_text(encoding="utf-8")
    make_deploy_recipe = makefile.split("\ndeploy:\n", 1)[1].split("\n\ndeploy-legacy-ea-stack:", 1)[0]

    assert "./scripts/deploy_propertyquarry.sh" in make_deploy_recipe
    assert "PROPERTYQUARRY_COMPOSE_FILE" not in make_deploy_recipe
    assert "materialize" not in make_deploy_recipe
    assert "docker compose" not in make_deploy_recipe
    assert 'requested_mode="${EA_RUNTIME_MODE:-prod}"' in deploy
    assert 'operation="deploy-run"' in deploy
    assert 'operation="candidate-run"' in deploy
    assert "--signed-request-fd" in deploy
    assert "--candidate-root-fd" in deploy
    assert "--controller-owns-all-privileged-actions" in deploy
    assert "--contain-before-candidate-validation" in deploy
    assert "--forbid-caller-compose" in deploy
    assert "--forbid-candidate-output-authority" in deploy
    assert "PROPERTYQUARRY_FLAGSHIP_GATE_SEED" not in deploy
    assert "PROPERTYQUARRY_BROWSER_PROOF_RECEIPT" not in deploy
    assert "PROPERTYQUARRY_FLAGSHIP_RELEASE_RECEIPT" not in deploy
    assert "scripts/verify_propertyquarry_deploy_receipts.py" not in deploy
    assert "propertyquarry_deploy_controller_guard.py" not in deploy
    assert "docker compose" not in deploy
    assert "materialize_ea_browser_workflow_proof.py" not in deploy
    assert "materialize_ea_flagship_release_gate.py" not in deploy
    assert "/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller" in deploy
    assert "/etc/propertyquarry/release-control/external-deploy-controller.v1.json" in deploy
