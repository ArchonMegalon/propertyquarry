from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts import verify_propertyquarry_global_governance_assets as verifier


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def test_checked_in_global_governance_release_assets_verify() -> None:
    assert verifier.verify_global_governance_assets(ROOT, now=NOW) == []


def test_release_asset_shell_requires_and_runs_governance_verifier() -> None:
    release_verifier = (ROOT / "scripts/verify_release_assets.sh").read_text(
        encoding="utf-8"
    )
    for relative in (
        "config/monitoring/propertyquarry_global_experience.v1.json",
        "docs/PROPERTYQUARRY_GLOBAL_EXPERIENCE_EVIDENCE.md",
        "config/monitoring/propertyquarry_flagship_operations.v1.json",
        "docs/PROPERTYQUARRY_GLOBAL_LAUNCH_TERMINAL_INSTALL.md",
        "packaging/propertyquarry-global-launch-terminal/global-launch-terminal-bundle.v1.schema.json",
        "scripts/propertyquarry_global_experience_gate.py",
        "config/compliance/propertyquarry_jurisdiction_privacy_rights.v1.json",
        "docs/PROPERTYQUARRY_JURISDICTION_PRIVACY_AND_PROVIDER_RIGHTS.md",
        "scripts/propertyquarry_jurisdiction_privacy_rights_gate.py",
        "scripts/build_propertyquarry_global_launch_terminal_bundle.py",
        "scripts/propertyquarry_global_launch_terminal.py",
        "scripts/propertyquarry_gold_status.py",
        "scripts/verify_propertyquarry_global_governance_assets.py",
    ):
        assert f'"{relative}"' in release_verifier
    assert (
        '"${RELEASE_PYTHON}" scripts/verify_propertyquarry_global_governance_assets.py'
        in release_verifier
    )


def test_source_only_governance_checks_stay_blocked_without_inventing_proof() -> None:
    assert verifier._source_only_gate_issues(ROOT, NOW) == []


def test_gold_release_contract_keeps_launch_required_and_lower_profiles_optional() -> None:
    assert verifier._gold_contract_issues(ROOT) == []


def test_global_terminal_contract_is_single_manifest_driven_fixed_argv() -> None:
    assert verifier._terminal_contract_issues(ROOT) == []


def test_missing_mandatory_governance_asset_fails_closed(
    monkeypatch,
) -> None:
    missing = "docs/required-global-governance-asset-that-does-not-exist.md"
    monkeypatch.setattr(
        verifier,
        "REQUIRED_ASSETS",
        (*verifier.REQUIRED_ASSETS, missing),
    )
    assert verifier.verify_global_governance_assets(ROOT, now=NOW) == [
        f"missing required global-governance asset: {missing}"
    ]


def test_governance_schema_constant_drift_fails_release_verification(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        verifier.experience_gate,
        "CONTRACT_SCHEMA",
        "propertyquarry.global_experience.changed",
    )
    issues = verifier.verify_global_governance_assets(ROOT, now=NOW)
    assert (
        "governance schema constant changed: global_experience_contract" in issues
    )
    assert any(
        issue.startswith("global-experience contract:") for issue in issues
    )


def test_global_governance_verifier_cli_reports_machine_readable_pass(capsys) -> None:
    assert verifier.main(["--root", str(ROOT)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema": verifier.VERIFICATION_SCHEMA,
        "status": "pass",
        "issue_count": 0,
        "issues": [],
    }
